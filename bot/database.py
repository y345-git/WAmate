import sqlite3
import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

NON_ATTACHMENT_MEDIA_TYPES = ("text", "contact", "poll", "location", "link")
DOWNLOADABLE_MEDIA_TYPES = ("image", "video", "document", "audio", "voice_note", "sticker")
_SEARCH_STOP_WORDS = frozenset({
    "the", "a", "an", "and", "or", "is", "are", "was", "were", "to", "for",
    "of", "in", "on", "at", "from", "by", "with", "about", "what", "who",
    "did", "do", "does", "my", "me", "please",
})


def parse_date_to_timestamp(
    val: str | int | None, end_of_day: bool = False, tz_name: str = ""
) -> int:
    """Parse YYYY-MM-DD, ISO timestamp, 'today'/'yesterday', or unix epoch into Unix seconds (0 if empty/invalid)."""
    if val is None:
        return 0
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        ts = int(val)
        return ts // 1000 if ts > 10_000_000_000 else max(0, ts)

    raw = str(val).strip()
    if not raw:
        return 0
    if raw.isdigit():
        ts = int(raw)
        return ts // 1000 if ts > 10_000_000_000 else max(0, ts)

    tz = None
    if tz_name:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = None
    if tz is None:
        tz = datetime.now().astimezone().tzinfo

    low = raw.lower()
    if low in ("today", "yesterday"):
        now_dt = datetime.now(tz)
        if low == "yesterday":
            now_dt = now_dt - timedelta(days=1)
        base = now_dt.replace(hour=23 if end_of_day else 0, minute=59 if end_of_day else 0, second=59 if end_of_day else 0, microsecond=0)
        return max(0, int(base.timestamp()))

    # Support full ISO datetimes with time component if explicitly provided
    norm = raw.replace("Z", "+00:00")
    if len(norm) > 10 and ("T" in norm or " " in norm):
        try:
            dt = datetime.fromisoformat(norm)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
            return max(0, int(dt.timestamp()))
        except ValueError:
            pass

    try:
        dt = datetime.strptime(raw[:10], "%Y-%m-%d").replace(tzinfo=tz)
        if end_of_day:
            dt = dt + timedelta(hours=23, minutes=59, seconds=59)
        return max(0, int(dt.timestamp()))
    except ValueError:
        return 0


def _format_timestamp(ts: int | None) -> str:
    """Safely format integer timestamp to human readable date string."""
    if not ts:
        return "Unknown"
    if ts > 10_000_000_000:
        ts = ts // 1000
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OverflowError, OSError):
        return str(ts)


class DatabaseManager:
    """Manages SQLite storage for WhatsApp messages, rich media, contacts, and full-text search."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._local = threading.local()
        self._conns_lock = threading.Lock()
        self._all_conns: list[sqlite3.Connection] = []
        self._pool_generation = 0
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        gen = getattr(self._local, "pool_gen", -1)
        if conn is None or gen != self._pool_generation:
            conn = sqlite3.connect(self.db_path, timeout=60.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            # Enable WAL mode and memory cache once per pooled thread-local connection
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA busy_timeout=60000;")
            conn.execute("PRAGMA temp_store=MEMORY;")
            conn.execute("PRAGMA cache_size=-16384;")
            self._local.conn = conn
            self._local.pool_gen = self._pool_generation
            with self._conns_lock:
                self._all_conns.append(conn)
        return conn

    def close_all(self) -> None:
        """Close all pooled thread-local SQLite connections."""
        with self._conns_lock:
            self._pool_generation += 1
            for c in self._all_conns:
                try:
                    c.close()
                except Exception:
                    pass
            self._all_conns.clear()
        if hasattr(self._local, "conn"):
            self._local.conn = None

    def _init_db(self) -> None:
        """Initialize database schema with contacts, messages, and FTS5 tables."""
        with self._get_connection() as conn:
            # 1. Contacts & Group Chats Table
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS contacts (
                    jid TEXT PRIMARY KEY,
                    name TEXT,
                    push_name TEXT,
                    phone_number TEXT,
                    is_group INTEGER DEFAULT 0,
                    updated_at INTEGER
                );
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_contacts_name ON contacts(name);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_contacts_push_name ON contacts(push_name);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_contacts_phone ON contacts(phone_number);")

            # 2. Messages Table
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    chat_jid TEXT NOT NULL,
                    chat_name TEXT,
                    sender_jid TEXT NOT NULL,
                    sender_name TEXT,
                    text TEXT,
                    timestamp INTEGER NOT NULL,
                    is_from_me INTEGER DEFAULT 0,
                    is_group INTEGER DEFAULT 0,
                    media_type TEXT DEFAULT 'text',
                    media_filename TEXT,
                    media_mimetype TEXT,
                    media_size INTEGER DEFAULT 0,
                    media_metadata TEXT DEFAULT '{}'
                );
                """
            )

            # Schema migration check for existing databases
            existing_cols = [c["name"] for c in conn.execute("PRAGMA table_info(messages)").fetchall()]
            for col_name, col_type in [
                ("media_filename", "TEXT"),
                ("media_mimetype", "TEXT"),
                ("media_size", "INTEGER DEFAULT 0"),
                ("media_metadata", "TEXT DEFAULT '{}'"),
                ("raw_message", "BLOB"),
                ("local_path", "TEXT"),
                ("media_hash", "TEXT"),
                ("local_ocr_done", "INTEGER DEFAULT 0"),
                ("local_doc_done", "INTEGER DEFAULT 0"),
                ("is_deleted", "INTEGER DEFAULT 0"),
                ("deleted_at", "INTEGER DEFAULT 0"),
                ("deleted_by", "TEXT DEFAULT ''"),
                ("is_edited", "INTEGER DEFAULT 0"),
                ("edited_at", "INTEGER DEFAULT 0"),
                ("original_text", "TEXT DEFAULT ''"),
            ]:
                if col_name not in existing_cols:
                    conn.execute(f"ALTER TABLE messages ADD COLUMN {col_name} {col_type};")

            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages(chat_jid, timestamp DESC);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp DESC);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_jid);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_media_type ON messages(media_type);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_media_filename ON messages(media_filename);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_local_path ON messages(local_path);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_media_hash ON messages(media_hash);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_local_ocr ON messages(local_ocr_done);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_local_doc ON messages(local_doc_done);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_is_deleted ON messages(is_deleted);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_is_edited ON messages(is_edited);")

            # 3. Full-Text Search Virtual Table (FTS5)
            conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                    text,
                    chat_name,
                    sender_name,
                    content='messages',
                    content_rowid='rowid'
                );
                """
            )

            # 4. Triggers to keep FTS index synchronized with messages table
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
                    INSERT INTO messages_fts(rowid, text, chat_name, sender_name)
                    VALUES (new.rowid, new.text, new.chat_name, new.sender_name);
                END;
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
                    INSERT INTO messages_fts(messages_fts, rowid, text, chat_name, sender_name)
                    VALUES ('delete', old.rowid, old.text, old.chat_name, old.sender_name);
                END;
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE OF text, chat_name, sender_name ON messages BEGIN
                    INSERT INTO messages_fts(messages_fts, rowid, text, chat_name, sender_name)
                    VALUES ('delete', old.rowid, old.text, old.chat_name, old.sender_name);
                    INSERT INTO messages_fts(rowid, text, chat_name, sender_name)
                    VALUES (new.rowid, new.text, new.chat_name, new.sender_name);
                END;
                """
            )

            # 5. Media OCR Cache Table (persistent OCR results for images & scanned documents)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS media_ocr_cache (
                    msg_id TEXT PRIMARY KEY,
                    ocr_text TEXT NOT NULL,
                    mimetype TEXT,
                    created_at INTEGER
                );
                """
            )

            # 6. Deleted & Edited Message Revisions Audit Table ("Anti-Delete Vault")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS message_revisions (
                    revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    msg_id TEXT NOT NULL,
                    chat_jid TEXT,
                    event_type TEXT NOT NULL,
                    actor TEXT,
                    previous_text TEXT,
                    new_text TEXT,
                    event_ts INTEGER NOT NULL
                );
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_revisions_msg_id ON message_revisions(msg_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_revisions_event_ts ON message_revisions(event_ts DESC);")

            # 7. Persistent Web UI AI Conversation History
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS web_ai_history (
                    turn_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    chat_focus TEXT DEFAULT '',
                    context_msg_id TEXT DEFAULT '',
                    context_label TEXT DEFAULT '',
                    latency_ms INTEGER DEFAULT 0,
                    is_error INTEGER DEFAULT 0,
                    created_at INTEGER NOT NULL
                );
                """
            )

            # Schema migration check for contacts table (e.g. notes)
            contact_cols = [c["name"] for c in conn.execute("PRAGMA table_info(contacts)").fetchall()]
            if "notes" not in contact_cols:
                conn.execute("ALTER TABLE contacts ADD COLUMN notes TEXT DEFAULT '';")

            # 8. Broadcast Roles Table
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS roles (
                    name TEXT PRIMARY KEY,
                    color TEXT DEFAULT '',
                    created_at INTEGER
                );
                """
            )

            # 9. Entity Roles Mapping (Contacts & Groups)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS entity_roles (
                    jid TEXT NOT NULL,
                    role TEXT NOT NULL,
                    created_at INTEGER,
                    PRIMARY KEY (jid, role)
                );
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_entity_roles_jid ON entity_roles(jid);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_entity_roles_role ON entity_roles(role);")

            # 10. Scheduled Campaigns Planner
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduled_campaigns (
                    id TEXT PRIMARY KEY,
                    message TEXT,
                    attachment_path TEXT DEFAULT '',
                    attachment_filename TEXT DEFAULT '',
                    attachment_mimetype TEXT DEFAULT '',
                    target_audience TEXT DEFAULT 'both',
                    roles TEXT DEFAULT '[]',
                    specific_recipients TEXT DEFAULT '[]',
                    scheduled_time TEXT NOT NULL,
                    scheduled_ts INTEGER NOT NULL,
                    status TEXT DEFAULT 'pending',
                    created_at INTEGER,
                    sent_at INTEGER DEFAULT 0,
                    total_targets INTEGER DEFAULT 0,
                    sent_count INTEGER DEFAULT 0,
                    failed_count INTEGER DEFAULT 0,
                    log_details TEXT DEFAULT '[]'
                );
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sched_status_ts ON scheduled_campaigns(status, scheduled_ts);")

            # 11. Broadcast Campaign Delivery History Logs
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS campaign_history (
                    id TEXT PRIMARY KEY,
                    campaign_type TEXT DEFAULT 'instant',
                    message TEXT,
                    attachment_path TEXT DEFAULT '',
                    attachment_filename TEXT DEFAULT '',
                    roles TEXT DEFAULT '[]',
                    target_audience TEXT DEFAULT 'both',
                    recipients_count INTEGER DEFAULT 0,
                    sent_count INTEGER DEFAULT 0,
                    failed_count INTEGER DEFAULT 0,
                    duration_sec REAL DEFAULT 0,
                    started_at INTEGER,
                    finished_at INTEGER,
                    logs TEXT DEFAULT '[]'
                );
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_camp_hist_started ON campaign_history(started_at DESC);")

            # Seed initial default roles if none exist, or enrich missing role colors
            try:
                DEFAULT_ROLE_PALETTE = {
                    "VIP": "#f59e0b",
                    "Clients": "#38bdf8",
                    "Business": "#6366f1",
                    "Friends": "#ec4899",
                    "Family": "#10b981",
                    "Nature": "#84cc16",
                }
                existing_roles_cnt = conn.execute("SELECT COUNT(*) FROM roles").fetchone()[0]
                init_ts = int(time.time())
                if existing_roles_cnt == 0:
                    for def_role, def_color in DEFAULT_ROLE_PALETTE.items():
                        conn.execute(
                            "INSERT OR IGNORE INTO roles (name, color, created_at) VALUES (?, ?, ?)",
                            (def_role, def_color, init_ts),
                        )
                else:
                    for def_role, def_color in DEFAULT_ROLE_PALETTE.items():
                        conn.execute(
                            "UPDATE roles SET color = ? WHERE name = ? AND (color IS NULL OR color = '')",
                            (def_color, def_role),
                        )
            except Exception as re_err:
                logger.debug("Default roles seed skipped: %s", re_err)

            # Clean up duplicate companion device IDs in entity_roles and legacy @lid contacts
            try:
                conn.execute("""
                    UPDATE entity_roles 
                    SET jid = SUBSTR(jid, 1, INSTR(jid, ':') - 1) || '@' || SUBSTR(jid, INSTR(jid, '@') + 1) 
                    WHERE jid LIKE '%:%@%'
                """)
                conn.execute("""
                    DELETE FROM entity_roles 
                    WHERE rowid NOT IN (SELECT MIN(rowid) FROM entity_roles GROUP BY jid, role)
                """)
                conn.execute("DELETE FROM contacts WHERE jid LIKE '%@lid'")
                conn.execute("DELETE FROM contacts WHERE jid LIKE '%:%'")
            except Exception as cl_err:
                logger.debug("Entity roles / contacts cleanup notice: %s", cl_err)

            conn.commit()
            logger.info("Database initialized successfully at %s", self.db_path)

        # Backfill any groups or contacts present in messages table into contacts table
        self.backfill_contacts_and_groups_from_messages()

        downloads_dir = Path(self.db_path).parent / "downloads"
        if downloads_dir.exists():
            self.reconcile_stored_downloads(downloads_dir)

    def backfill_contacts_and_groups_from_messages(self) -> None:
        """Ensure all group and contact JIDs in messages table exist in contacts table."""
        try:
            with self._get_connection() as conn:
                # Backfill WhatsApp groups
                conn.execute(
                    """
                    INSERT OR IGNORE INTO contacts (jid, name, is_group, updated_at)
                    SELECT chat_jid,
                           COALESCE(NULLIF(chat_name, ''), chat_jid),
                           1,
                           strftime('%s', 'now')
                    FROM messages
                    WHERE (is_group = 1 OR chat_jid LIKE '%@g.us')
                      AND chat_jid != 'status@broadcast'
                    GROUP BY chat_jid
                    """
                )
                # Backfill 1-on-1 contact chats
                conn.execute(
                    """
                    INSERT OR IGNORE INTO contacts (jid, name, phone_number, is_group, updated_at)
                    SELECT chat_jid,
                           COALESCE(NULLIF(chat_name, ''), chat_jid),
                           REPLACE(REPLACE(chat_jid, '@s.whatsapp.net', ''), '@c.us', ''),
                           0,
                           strftime('%s', 'now')
                    FROM messages
                    WHERE is_group = 0
                      AND chat_jid LIKE '%@s.whatsapp.net'
                      AND chat_jid != 'status@broadcast'
                    GROUP BY chat_jid
                    """
                )
                conn.commit()
        except Exception as exc:
            logger.debug("backfill_contacts_and_groups_from_messages error: %s", exc)

    def reconcile_stored_downloads(self, downloads_dir: Path) -> int:
        """Scan downloads_dir and record local_path for any previously downloaded files,
        and clean up non-attachment and expired status records so they are never queried repeatedly.
        """
        updated = 0
        with self._get_connection() as conn:
            # 1. Reconcile existing files on disk
            if downloads_dir.exists():
                for p in downloads_dir.glob("*"):
                    if p.is_file() and "_" in p.name:
                        msg_id = p.name.split("_", 1)[0]
                        if len(msg_id) >= 16:
                            cur = conn.execute(
                                "UPDATE messages SET local_path = ? WHERE id = ? AND (local_path IS NULL OR local_path = '')",
                                (str(p), msg_id),
                            )
                            updated += cur.rowcount

            # 2. Mark non-attachment messages (text, contact cards, polls, locations) as non-downloadable
            conn.execute("""
                UPDATE messages 
                SET local_path = '[NO_ATTACHMENT]' 
                WHERE media_type IN ('text', 'contact', 'poll', 'location', 'link') 
                  AND (local_path IS NULL OR local_path = '')
            """)

            # 3. Mark historical WhatsApp status updates older than 24 hours as expired
            status_cutoff = int(time.time()) - 86400
            conn.execute("""
                UPDATE messages 
                SET local_path = '[STATUS_EXPIRED_24H]' 
                WHERE chat_jid = 'status@broadcast' 
                  AND (local_path IS NULL OR local_path = '') 
                  AND timestamp < ?
            """, (status_cutoff,))

            # 4. Drop redundant protobuf BLOBs on non-attachments or already-processed rows
            conn.execute("""
                UPDATE messages
                SET raw_message = NULL
                WHERE raw_message IS NOT NULL
                  AND (
                      media_type IN ('text', 'contact', 'poll', 'location', 'link')
                      OR (local_path IS NOT NULL AND local_path != '')
                  )
            """)

            conn.commit()
        return updated

    def upsert_contact(
        self,
        jid: str,
        name: str | None = None,
        push_name: str | None = None,
        phone_number: str | None = None,
        is_group: bool = False,
    ) -> None:
        """Insert or update contact metadata."""
        if not jid:
            return

        now = int(datetime.now().timestamp())
        query = """
        INSERT INTO contacts (jid, name, push_name, phone_number, is_group, updated_at)
        VALUES (:jid, :name, :push_name, :phone_number, :is_group, :updated_at)
        ON CONFLICT(jid) DO UPDATE SET
            name = COALESCE(:name, contacts.name),
            push_name = COALESCE(:push_name, contacts.push_name),
            phone_number = COALESCE(:phone_number, contacts.phone_number),
            is_group = :is_group,
            updated_at = :updated_at
        """
        with self._get_connection() as conn:
            conn.execute(
                query,
                {
                    "jid": jid,
                    "name": name if name else None,
                    "push_name": push_name if push_name else None,
                    "phone_number": phone_number if phone_number else None,
                    "is_group": 1 if is_group else 0,
                    "updated_at": now,
                },
            )
            conn.commit()

    def upsert_contacts_batch(self, contacts: list[dict[str, Any]]) -> None:
        """Batch insert or update contacts in a single transaction."""
        if not contacts:
            return
        now = int(datetime.now().timestamp())
        query = """
        INSERT INTO contacts (jid, name, push_name, phone_number, is_group, updated_at)
        VALUES (:jid, :name, :push_name, :phone_number, :is_group, :updated_at)
        ON CONFLICT(jid) DO UPDATE SET
            name = COALESCE(:name, contacts.name),
            push_name = COALESCE(:push_name, contacts.push_name),
            phone_number = COALESCE(:phone_number, contacts.phone_number),
            is_group = :is_group,
            updated_at = :updated_at
        """
        rows = []
        for c in contacts:
            jid = c.get("jid")
            if not jid:
                continue
            rows.append({
                "jid": jid,
                "name": c.get("name") if c.get("name") else None,
                "push_name": c.get("push_name") if c.get("push_name") else None,
                "phone_number": c.get("phone_number") if c.get("phone_number") else None,
                "is_group": 1 if c.get("is_group") else 0,
                "updated_at": now,
            })
        if rows:
            with self._get_connection() as conn:
                conn.executemany(query, rows)
                conn.commit()

    def get_contact(self, jid: str) -> dict[str, Any] | None:
        """Fetch contact metadata for a specific JID."""
        if not jid:
            return None
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT jid, name, push_name, phone_number, is_group, updated_at FROM contacts WHERE jid = ?",
                (jid,),
            ).fetchone()
            if not row:
                return None
            return {
                "jid": row["jid"],
                "name": row["name"],
                "push_name": row["push_name"],
                "phone_number": row["phone_number"],
                "is_group": bool(row["is_group"]),
                "updated_at": row["updated_at"],
            }

    def get_ocr_cache(self, msg_id: str) -> str | None:
        """Retrieve cached OCR analysis for a message if previously processed."""
        if not msg_id:
            return None
        with self._get_connection() as conn:
            row = conn.execute("SELECT ocr_text FROM media_ocr_cache WHERE msg_id = ?", (msg_id,)).fetchone()
            if not row:
                return None
            val = row["ocr_text"]
            if val.startswith(("[MEDIA_", "[OCR_SKIPPED")):
                return None
            return val

    def save_ocr_cache(self, msg_id: str, ocr_text: str, mimetype: str = "") -> None:
        """Save OCR analysis for a media message into cache and update messages text for FTS indexing."""
        if not msg_id or not ocr_text:
            return
        now = int(datetime.now().timestamp())
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO media_ocr_cache (msg_id, ocr_text, mimetype, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(msg_id) DO UPDATE SET
                    ocr_text = excluded.ocr_text,
                    mimetype = excluded.mimetype,
                    created_at = excluded.created_at
                """,
                (msg_id, ocr_text, mimetype, now),
            )
            # Only append OCR text snippet to messages.text if it is actual OCR text (not an error/expired marker)
            if not ocr_text.startswith(("[MEDIA_", "[OCR_SKIPPED")):
                row = conn.execute("SELECT text FROM messages WHERE id = ?", (msg_id,)).fetchone()
                if row:
                    current_text = row["text"] or ""
                    if "[OCR:" not in current_text:
                        ocr_snippet = ocr_text.strip()
                        updated_text = f"{current_text}\n[OCR: {ocr_snippet}]" if current_text else f"[OCR: {ocr_snippet}]"
                        conn.execute("UPDATE messages SET text = ? WHERE id = ?", (updated_text, msg_id))
            conn.commit()

    def _get_local_index_candidates(
        self,
        done_column: str,
        media_filter_sql: str,
        limit: int = 50,
        self_jid: str = "",
    ) -> list[dict[str, Any]]:
        """Shared candidate query for offline background indexers (Self-Chat -> DMs -> Groups -> Statuses)."""
        if done_column not in {"local_ocr_done", "local_doc_done"}:
            raise ValueError(f"Invalid local index column: {done_column}")
        sql = f"""
        SELECT m.id, m.chat_jid, m.sender_jid, m.timestamp, m.local_path,
               m.media_type, m.media_mimetype, m.media_filename, m.chat_name, m.sender_name, m.is_group
        FROM messages m
        WHERE COALESCE(m.{done_column}, 0) = 0
          AND m.local_path IS NOT NULL
          AND m.local_path NOT LIKE '[%'
          AND {media_filter_sql}
        ORDER BY
          CASE
            WHEN m.chat_jid = ? OR m.sender_jid = ? THEN 1             -- Self-chat
            WHEN m.is_group = 0 AND m.chat_jid != 'status@broadcast' THEN 2 -- Direct 1-on-1 chats (DMs)
            WHEN m.is_group = 1 THEN 3                                 -- Group chats & announcements
            WHEN m.chat_jid = 'status@broadcast' THEN 4                -- Status stories
            ELSE 5
          END ASC,
          m.timestamp DESC
        LIMIT ?
        """
        with self._get_connection() as conn:
            rows = conn.execute(sql, (self_jid, self_jid, limit)).fetchall()
            return [dict(r) for r in rows]

    def get_local_ocr_candidates(
        self,
        limit: int = 50,
        self_jid: str = "",
    ) -> list[dict[str, Any]]:
        """Fetch stored image messages not yet scanned by local CPU OCR."""
        return self._get_local_index_candidates(
            done_column="local_ocr_done",
            media_filter_sql="(m.media_type LIKE '%image%' OR m.media_mimetype LIKE 'image/%')",
            limit=limit,
            self_jid=self_jid,
        )

    def get_local_doc_candidates(
        self,
        limit: int = 50,
        self_jid: str = "",
    ) -> list[dict[str, Any]]:
        """Fetch stored document messages not yet text-indexed locally."""
        return self._get_local_index_candidates(
            done_column="local_doc_done",
            media_filter_sql="m.media_type = 'document'",
            limit=limit,
            self_jid=self_jid,
        )

    def get_unstored_media_candidates(
        self,
        lookback_days: int = 30,
        limit: int = 50,
        media_types: tuple[str, ...] = ("all",),
        max_size_bytes: int = 0,
        include_statuses: bool = True,
        self_jid: str = "",
    ) -> list[dict[str, Any]]:
        """Fetch media messages within lookback_days that are not yet downloaded locally, prioritizing:
        1. Self-chat messages / notes to self
        2. Direct 1-on-1 Messages (DMs with contacts or unsaved numbers)
        3. Group chats & Announcements
        4. WhatsApp Status Stories (only if <= 24h old, processed last)
        """
        now_ts = int(time.time())
        cutoff_ts = now_ts - (lookback_days * 86400)

        # Status updates expire from WhatsApp CDN in 24 hours. Never attempt statuses older than 24h.
        status_24h_cutoff = now_ts - 86400
        status_filter = (
            f"AND (m.chat_jid != 'status@broadcast' OR m.timestamp >= {status_24h_cutoff})"
            if include_statuses
            else "AND m.chat_jid != 'status@broadcast'"
        )

        # Downloadable attachment types ONLY (excludes text, contact, poll, location, link)
        VALID_ATTACHMENT_TYPES = ("image", "video", "document", "audio", "voice_note", "sticker")
        is_all_types = not media_types or "all" in media_types or "any" in media_types
        selected_types = list(VALID_ATTACHMENT_TYPES) if is_all_types else [t for t in media_types if t in VALID_ATTACHMENT_TYPES]
        if not selected_types:
            selected_types = list(VALID_ATTACHMENT_TYPES)

        type_filter = f"AND m.media_type IN ({','.join('?' for _ in selected_types)})"

        # If max_size_bytes <= 0, no size restriction
        size_filter = "" if max_size_bytes <= 0 else "AND (m.media_size <= ? OR m.media_size IS NULL OR m.media_size = 0)"

        sql = f"""
        SELECT m.id, m.chat_jid, m.sender_jid, m.timestamp, m.text, 
               m.media_type, m.media_filename, m.media_mimetype, m.media_size,
               m.chat_name, m.sender_name, m.is_group
        FROM messages m
        WHERE (m.local_path IS NULL OR m.local_path = '')
          {type_filter}
          AND m.timestamp >= ?
          AND m.raw_message IS NOT NULL
          {size_filter}
          {status_filter}
        ORDER BY 
          CASE 
            WHEN m.chat_jid = ? OR m.sender_jid = ? THEN 1             -- Self-chat
            WHEN m.is_group = 0 AND m.chat_jid != 'status@broadcast' THEN 2 -- Direct 1-on-1 DMs
            WHEN m.is_group = 1 THEN 3                                 -- Group chats
            WHEN m.chat_jid = 'status@broadcast' THEN 4                -- Status stories
            ELSE 5
          END ASC,
          m.timestamp DESC
        LIMIT ?
        """
        params = list(selected_types)
        params.append(cutoff_ts)
        if max_size_bytes > 0:
            params.append(max_size_bytes)
        params.extend([self_jid, self_jid, limit])
        with self._get_connection() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def get_ocr_stats(self) -> dict[str, int]:
        """Return counts of processed and skipped OCR items."""
        with self._get_connection() as conn:
            total_cached = conn.execute(
                "SELECT COUNT(*) FROM media_ocr_cache WHERE ocr_text NOT LIKE '[MEDIA_%' AND ocr_text NOT LIKE '[OCR_SKIPPED%'"
            ).fetchone()[0]
            total_expired = conn.execute(
                "SELECT COUNT(*) FROM media_ocr_cache WHERE ocr_text LIKE '[MEDIA_%' OR ocr_text LIKE '[OCR_SKIPPED%'"
            ).fetchone()[0]
            return {"valid_ocr": total_cached, "expired_or_skipped": total_expired}

    def backfill_contact_names(self) -> None:
        """Backfill chat_name and sender_name across messages from contacts table and phone JIDs."""
        with self._get_connection() as conn:
            # 1. Update chat_name from contacts table (name, push_name, or phone_number)
            conn.execute("""
            UPDATE messages
            SET chat_name = (
                SELECT COALESCE(NULLIF(contacts.name, ''), NULLIF(contacts.push_name, ''), NULLIF(contacts.phone_number, ''))
                FROM contacts WHERE contacts.jid = messages.chat_jid
            )
            WHERE chat_jid IN (
                SELECT jid FROM contacts WHERE (name IS NOT NULL AND name != '') 
                    OR (push_name IS NOT NULL AND push_name != '')
                    OR (phone_number IS NOT NULL AND phone_number != '')
            );
            """)

            # 2. Update sender_name from contacts table
            conn.execute("""
            UPDATE messages
            SET sender_name = (
                SELECT COALESCE(NULLIF(contacts.name, ''), NULLIF(contacts.push_name, ''), NULLIF(contacts.phone_number, ''))
                FROM contacts WHERE contacts.jid = messages.sender_jid
            )
            WHERE is_from_me = 0 AND sender_jid IN (
                SELECT jid FROM contacts WHERE (name IS NOT NULL AND name != '') 
                    OR (push_name IS NOT NULL AND push_name != '') 
                    OR (phone_number IS NOT NULL AND phone_number != '')
            );
            """)

            # 3. For any 1-on-1 chats where chat_name is still empty, fallback to phone number from chat_jid
            conn.execute("""
            UPDATE messages
            SET chat_name = SUBSTR(chat_jid, 1, INSTR(chat_jid, '@') - 1)
            WHERE is_group = 0 AND (chat_name IS NULL OR chat_name = '') AND chat_jid LIKE '%@s.whatsapp.net';
            """)

            # 4. For any 1-on-1 chats where sender_name is still empty, fallback to phone number from sender_jid
            conn.execute("""
            UPDATE messages
            SET sender_name = SUBSTR(sender_jid, 1, INSTR(sender_jid, '@') - 1)
            WHERE is_group = 0 AND is_from_me = 0 AND (sender_name IS NULL OR sender_name = '') AND sender_jid LIKE '%@s.whatsapp.net';
            """)

            # 5. If chat_name is still empty for any message, use the non-empty sender_name of the other party in that chat
            conn.execute("""
            UPDATE messages
            SET chat_name = (
                SELECT m2.sender_name FROM messages m2
                WHERE m2.chat_jid = messages.chat_jid AND m2.is_from_me = 0 
                  AND m2.sender_name IS NOT NULL AND m2.sender_name != ''
                ORDER BY m2.timestamp DESC
                LIMIT 1
            )
            WHERE is_group = 0 AND (chat_name IS NULL OR chat_name = '')
              AND EXISTS (
                SELECT 1 FROM messages m2 
                WHERE m2.chat_jid = messages.chat_jid AND m2.is_from_me = 0 
                  AND m2.sender_name IS NOT NULL AND m2.sender_name != ''
              );
            """)

            # 6. Fallback any remaining empty chat_name to chat_jid prefix
            conn.execute("""
            UPDATE messages
            SET chat_name = SUBSTR(chat_jid, 1, INSTR(chat_jid, '@') - 1)
            WHERE (chat_name IS NULL OR chat_name = '') AND INSTR(chat_jid, '@') > 1;
            """)

            conn.commit()

    def insert_message(
        self,
        msg_id: str,
        chat_jid: str,
        chat_name: str | None,
        sender_jid: str,
        sender_name: str | None,
        text: str,
        timestamp: int,
        is_from_me: bool = False,
        is_group: bool = False,
        media_type: str = "text",
        media_filename: str = "",
        media_mimetype: str = "",
        media_size: int = 0,
        media_metadata: dict[str, Any] | str = "",
        raw_message: bytes | None = None,
    ) -> bool:
        """Insert a message with rich media attributes if it does not already exist."""
        if not msg_id or not chat_jid:
            return False

        # Ensure timestamp is normalized to seconds (handle milliseconds)
        if timestamp > 10_000_000_000:
            timestamp = timestamp // 1000

        # If chat_name or sender_name is missing, try looking them up in contacts or phone JID
        if not chat_name or not sender_name:
            with self._get_connection() as conn:
                if not chat_name:
                    row = conn.execute("SELECT name, push_name, phone_number FROM contacts WHERE jid = ?", (chat_jid,)).fetchone()
                    if row:
                        chat_name = row["name"] or row["push_name"] or row["phone_number"]
                    if not chat_name and not is_group and "@s.whatsapp.net" in chat_jid:
                        chat_name = chat_jid.split("@")[0]
                if not sender_name:
                    row = conn.execute("SELECT name, push_name, phone_number FROM contacts WHERE jid = ?", (sender_jid,)).fetchone()
                    if row:
                        sender_name = row["name"] or row["push_name"] or row["phone_number"]
                    if not sender_name and not is_group and "@s.whatsapp.net" in sender_jid:
                        sender_name = sender_jid.split("@")[0]

        if isinstance(media_metadata, dict):
            metadata_str = json.dumps(media_metadata)
        else:
            metadata_str = str(media_metadata or "{}")

        query = """
        INSERT INTO messages
        (id, chat_jid, chat_name, sender_jid, sender_name, text, timestamp, is_from_me, is_group,
         media_type, media_filename, media_mimetype, media_size, media_metadata, raw_message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            chat_name = COALESCE(excluded.chat_name, messages.chat_name),
            sender_name = COALESCE(excluded.sender_name, messages.sender_name),
            text = CASE
                WHEN COALESCE(messages.is_deleted, 0) = 0
                 AND COALESCE(messages.is_edited, 0) = 0
                 AND COALESCE(messages.text, '') = ''
                 AND excluded.text != ''
                THEN excluded.text
                ELSE messages.text
            END,
            media_type = CASE WHEN excluded.media_type != 'text' THEN excluded.media_type ELSE messages.media_type END,
            media_filename = CASE WHEN excluded.media_filename != '' THEN excluded.media_filename ELSE messages.media_filename END,
            media_mimetype = CASE WHEN excluded.media_mimetype != '' THEN excluded.media_mimetype ELSE messages.media_mimetype END,
            media_size = CASE WHEN excluded.media_size > 0 THEN excluded.media_size ELSE messages.media_size END,
            media_metadata = CASE WHEN excluded.media_metadata != '{}' THEN excluded.media_metadata ELSE messages.media_metadata END,
            raw_message = COALESCE(excluded.raw_message, messages.raw_message)
        """
        with self._get_connection() as conn:
            cursor = conn.execute(
                query,
                (
                    msg_id,
                    chat_jid,
                    chat_name,
                    sender_jid,
                    sender_name,
                    text,
                    timestamp,
                    1 if is_from_me else 0,
                    1 if is_group else 0,
                    media_type,
                    media_filename or "",
                    media_mimetype or "",
                    media_size or 0,
                    metadata_str,
                    raw_message,
                ),
            )
            conn.commit()
            return cursor.rowcount > 0

    def insert_messages_batch(self, messages_data: list[dict[str, Any]]) -> int:
        """Batch insert messages in a single transaction for high-performance syncing."""
        if not messages_data:
            return 0

        query = """
        INSERT INTO messages
        (id, chat_jid, chat_name, sender_jid, sender_name, text, timestamp, is_from_me, is_group,
         media_type, media_filename, media_mimetype, media_size, media_metadata, raw_message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            chat_name = COALESCE(excluded.chat_name, messages.chat_name),
            sender_name = COALESCE(excluded.sender_name, messages.sender_name),
            text = CASE
                WHEN COALESCE(messages.is_deleted, 0) = 0
                 AND COALESCE(messages.is_edited, 0) = 0
                 AND COALESCE(messages.text, '') = ''
                 AND excluded.text != ''
                THEN excluded.text
                ELSE messages.text
            END,
            media_type = CASE WHEN excluded.media_type != 'text' THEN excluded.media_type ELSE messages.media_type END,
            media_filename = CASE WHEN excluded.media_filename != '' THEN excluded.media_filename ELSE messages.media_filename END,
            media_mimetype = CASE WHEN excluded.media_mimetype != '' THEN excluded.media_mimetype ELSE messages.media_mimetype END,
            media_size = CASE WHEN excluded.media_size > 0 THEN excluded.media_size ELSE messages.media_size END,
            media_metadata = CASE WHEN excluded.media_metadata != '{}' THEN excluded.media_metadata ELSE messages.media_metadata END,
            raw_message = COALESCE(excluded.raw_message, messages.raw_message)
        """

        rows = []
        for m in messages_data:
            msg_id = m.get("msg_id")
            chat_jid = m.get("chat_jid")
            if not msg_id or not chat_jid:
                continue

            ts = m.get("timestamp", 0)
            if ts > 10_000_000_000:
                ts = ts // 1000

            meta = m.get("media_metadata")
            metadata_str = json.dumps(meta) if isinstance(meta, dict) else str(meta or "{}")

            rows.append(
                (
                    msg_id,
                    chat_jid,
                    m.get("chat_name"),
                    m.get("sender_jid", chat_jid),
                    m.get("sender_name"),
                    m.get("text", ""),
                    ts,
                    1 if m.get("is_from_me") else 0,
                    1 if m.get("is_group") else 0,
                    m.get("media_type", "text"),
                    m.get("media_filename") or "",
                    m.get("media_mimetype") or "",
                    m.get("media_size") or 0,
                    metadata_str,
                    m.get("raw_message"),
                )
            )

        if not rows:
            return 0

        with self._get_connection() as conn:
            cursor = conn.executemany(query, rows)
            conn.commit()
            return cursor.rowcount

    def get_message_raw(self, msg_id: str) -> dict[str, Any] | None:
        """Fetch raw message blob and metadata for downloading media content."""
        sql = """
        SELECT id, chat_jid, chat_name, sender_jid, sender_name, text, timestamp,
               media_type, media_filename, media_mimetype, media_size, media_metadata, raw_message, local_path
        FROM messages
        WHERE id = ?
        """
        with self._get_connection() as conn:
            row = conn.execute(sql, (msg_id,)).fetchone()
            if not row:
                return None
            return {
                "id": row["id"],
                "chat_jid": row["chat_jid"],
                "chat_name": row["chat_name"],
                "sender_jid": row["sender_jid"],
                "sender_name": row["sender_name"],
                "text": row["text"],
                "timestamp": row["timestamp"],
                "media_type": row["media_type"],
                "filename": row["media_filename"],
                "mimetype": row["media_mimetype"],
                "size": row["media_size"],
                "metadata": row["media_metadata"],
                "raw_message": row["raw_message"],
                "local_path": row["local_path"] if "local_path" in row.keys() else None,
            }

    def update_message_local_path(
        self, msg_id: str, local_path: str, media_hash: str | None = None
    ) -> None:
        """Record stored local file path (or terminal status marker) and release raw protobuf BLOB."""
        with self._get_connection() as conn:
            if media_hash is not None:
                conn.execute(
                    "UPDATE messages SET local_path = ?, media_hash = ?, raw_message = NULL WHERE id = ?",
                    (local_path, media_hash, msg_id),
                )
            else:
                conn.execute(
                    "UPDATE messages SET local_path = ?, raw_message = NULL WHERE id = ?",
                    (local_path, msg_id),
                )
            conn.commit()

    def find_existing_local_path_by_hash(self, media_hash: str) -> str | None:
        """Check if an identical file (by SHA-256 content hash) already exists cleanly on disk."""
        if not media_hash:
            return None
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT local_path FROM messages
                WHERE media_hash = ?
                  AND local_path IS NOT NULL
                  AND local_path NOT LIKE '[%'
                LIMIT 5
                """,
                (media_hash,),
            ).fetchall()
        for r in rows:
            lp = r["local_path"]
            if lp:
                p = Path(str(lp))
                if p.exists() and p.is_file() and p.stat().st_size > 0:
                    return str(p)
        return None

    def _mark_local_index_done(
        self,
        msg_id: str,
        done_column: str,
        extracted_text: str = "",
        marker: str = "[Local Text:",
        min_len: int = 8,
        max_chars: int = 4000,
    ) -> None:
        """Shared helper to mark local OCR/Doc indexing done and append searchable text to messages.text."""
        if done_column not in {"local_ocr_done", "local_doc_done"}:
            raise ValueError(f"Invalid local index column: {done_column}")
        if not msg_id:
            return
        clean_snippet = (extracted_text or "").strip()
        with self._get_connection() as conn:
            row = conn.execute("SELECT text FROM messages WHERE id = ?", (msg_id,)).fetchone()
            if not row:
                return
            current_text = row["text"] or ""
            if len(clean_snippet) >= min_len and marker not in current_text and "[OCR:" not in current_text:
                capped = clean_snippet[:max_chars]
                updated = f"{current_text}\n{marker} {capped}]" if current_text else f"{marker} {capped}]"
                conn.execute(
                    f"UPDATE messages SET text = ?, {done_column} = 1 WHERE id = ?",
                    (updated, msg_id),
                )
            else:
                conn.execute(f"UPDATE messages SET {done_column} = 1 WHERE id = ?", (msg_id,))
            conn.commit()

    def mark_local_ocr_done(
        self, msg_id: str, extracted_text: str = "", marker: str = "[Local Text:"
    ) -> None:
        """Mark image as locally OCR-scanned and append extracted text to messages.text if meaningful."""
        self._mark_local_index_done(
            msg_id=msg_id,
            done_column="local_ocr_done",
            extracted_text=extracted_text,
            marker=marker,
            min_len=8,
            max_chars=4000,
        )

    def mark_local_doc_done(
        self, msg_id: str, extracted_text: str = "", marker: str = "[Doc Text:"
    ) -> None:
        """Mark document as locally text-indexed and append searchable snippet to messages.text if meaningful."""
        self._mark_local_index_done(
            msg_id=msg_id,
            done_column="local_doc_done",
            extracted_text=extracted_text,
            marker=marker,
            min_len=20,
            max_chars=6000,
        )

    @staticmethod
    def _normalize_event_ts(event_ts: int | None) -> int:
        ts = int(event_ts or 0)
        if ts > 10_000_000_000:
            ts //= 1000
        return ts if ts > 0 else int(time.time())

    def mark_message_deleted(
        self,
        target_msg_id: str,
        deleted_by: str = "",
        deleted_ts: int | None = None,
        chat_jid: str = "",
    ) -> dict[str, Any] | None:
        """Preserve original content and media on Delete-for-Everyone while tagging the message as deleted."""
        if not target_msg_id:
            return None
        ts = self._normalize_event_ts(deleted_ts)
        with self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT id, chat_jid, chat_name, sender_jid, sender_name, text, original_text,
                       media_type, media_filename, local_path, is_deleted, deleted_at, deleted_by
                FROM messages WHERE id = ?
                """,
                (target_msg_id,),
            ).fetchone()

            if not row:
                conn.execute(
                    """
                    INSERT INTO message_revisions (msg_id, chat_jid, event_type, actor, previous_text, new_text, event_ts)
                    VALUES (?, ?, 'deleted', ?, '', '', ?)
                    """,
                    (target_msg_id, chat_jid, deleted_by or "Sender", ts),
                )
                conn.commit()
                return None

            actor_label = (deleted_by or row["sender_name"] or row["sender_jid"] or "Sender").strip()
            if row["is_deleted"]:
                return {
                    "id": row["id"],
                    "chat": row["chat_name"] or row["chat_jid"],
                    "sender": row["sender_name"] or row["sender_jid"],
                    "original_text": row["original_text"] or row["text"] or "",
                    "media_type": row["media_type"] or "text",
                    "media_filename": row["media_filename"] or "",
                    "local_path": row["local_path"] or "",
                    "deleted_by": row["deleted_by"] or actor_label,
                    "deleted_at": _format_timestamp(row["deleted_at"] or ts),
                    "already_recorded": True,
                }

            current_text = row["text"] or ""
            preserved_original = row["original_text"] or current_text
            marker = f"[Deleted for everyone by {actor_label} at {_format_timestamp(ts)}]"
            if "[Deleted for everyone" not in current_text:
                updated_text = f"{current_text}\n{marker}" if current_text else marker
            else:
                updated_text = current_text

            conn.execute(
                """
                UPDATE messages
                SET is_deleted = 1,
                    deleted_at = ?,
                    deleted_by = ?,
                    original_text = ?,
                    text = ?
                WHERE id = ?
                """,
                (ts, actor_label, preserved_original, updated_text, target_msg_id),
            )
            conn.execute(
                """
                INSERT INTO message_revisions (msg_id, chat_jid, event_type, actor, previous_text, new_text, event_ts)
                VALUES (?, ?, 'deleted', ?, ?, '', ?)
                """,
                (target_msg_id, row["chat_jid"], actor_label, current_text, ts),
            )
            conn.commit()
            return {
                "id": row["id"],
                "chat": row["chat_name"] or row["chat_jid"],
                "sender": row["sender_name"] or row["sender_jid"],
                "original_text": preserved_original,
                "media_type": row["media_type"] or "text",
                "media_filename": row["media_filename"] or "",
                "local_path": row["local_path"] or "",
                "deleted_by": actor_label,
                "deleted_at": _format_timestamp(ts),
                "already_recorded": False,
            }

    @staticmethod
    def _strip_vault_markers(text: str) -> str:
        cleaned = re.sub(r"\n?\[Edited at [^\]]*\]", "", text or "")
        cleaned = re.sub(r"\n?\[Deleted for everyone[^\]]*\]", "", cleaned)
        return cleaned.strip()

    def record_message_edit(
        self,
        target_msg_id: str,
        new_text: str,
        edited_by: str = "",
        edited_ts: int | None = None,
        chat_jid: str = "",
    ) -> dict[str, Any] | None:
        """Preserve original pre-edit wording and append revision audit metadata when a message is edited."""
        if not target_msg_id:
            return None
        ts = self._normalize_event_ts(edited_ts)
        clean_new = (new_text or "").strip()

        with self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT id, chat_jid, chat_name, sender_jid, sender_name, text, original_text, is_deleted
                FROM messages WHERE id = ?
                """,
                (target_msg_id,),
            ).fetchone()

            if not row:
                conn.execute(
                    """
                    INSERT INTO message_revisions (msg_id, chat_jid, event_type, actor, previous_text, new_text, event_ts)
                    VALUES (?, ?, 'edited', ?, '', ?, ?)
                    """,
                    (target_msg_id, chat_jid, edited_by or "Sender", clean_new, ts),
                )
                conn.commit()
                return None

            actor_label = (edited_by or row["sender_name"] or row["sender_jid"] or "Sender").strip()
            current_text = row["text"] or ""
            prev_base = self._strip_vault_markers(current_text)
            prev_clean_body = re.sub(r"\n?\[(?:OCR|Local Text|Doc Text):.*", "", prev_base, flags=re.DOTALL).strip()
            first_orig = row["original_text"] or prev_clean_body
            if not clean_new:
                clean_new = prev_clean_body

            # Preserve any existing Deleted or local OCR/Doc markers appended after the message body
            preserved_tags = []
            del_match = re.search(r"(\[Deleted for everyone[^\]]*\])", current_text)
            if del_match:
                preserved_tags.append(del_match.group(1))
            local_match = re.search(r"(\[(?:OCR|Local Text|Doc Text):.*)", current_text, flags=re.DOTALL)
            if local_match:
                preserved_tags.append(local_match.group(1).strip())

            orig_inline = first_orig[:500].replace("\n", " ").replace("[", "(").replace("]", ")")
            edit_marker = f'[Edited at {_format_timestamp(ts)} | Original: "{orig_inline}"]'
            parts = [clean_new, edit_marker, *preserved_tags]
            updated_text = "\n".join(p for p in parts if p)

            conn.execute(
                """
                UPDATE messages
                SET is_edited = 1,
                    edited_at = ?,
                    original_text = ?,
                    text = ?
                WHERE id = ?
                """,
                (ts, first_orig, updated_text, target_msg_id),
            )
            conn.execute(
                """
                INSERT INTO message_revisions (msg_id, chat_jid, event_type, actor, previous_text, new_text, event_ts)
                VALUES (?, ?, 'edited', ?, ?, ?, ?)
                """,
                (target_msg_id, row["chat_jid"], actor_label, prev_clean_body, clean_new, ts),
            )
            conn.commit()
            return {
                "id": row["id"],
                "chat": row["chat_name"] or row["chat_jid"],
                "sender": row["sender_name"] or row["sender_jid"],
                "original_text": first_orig,
                "previous_text": prev_clean_body,
                "new_text": clean_new,
                "edited_at": _format_timestamp(ts),
            }

    def find_deleted_or_edited_messages(
        self,
        mode: str = "all",
        chat_name_or_jid: str = "",
        chat_scope: str = "all",
        limit: int = 20,
        date_from: int = 0,
        date_to: int = 0,
        include_statuses: bool = True,
    ) -> list[dict[str, Any]]:
        """Retrieve deleted and/or edited messages preserved by the Anti-Delete & Edit Vault."""
        norm_mode = (mode or "all").strip().lower()
        if norm_mode == "deleted":
            conditions = ["m.is_deleted = 1"]
        elif norm_mode == "edited":
            conditions = ["m.is_edited = 1"]
        else:
            conditions = ["(m.is_deleted = 1 OR m.is_edited = 1)"]

        if not include_statuses:
            conditions.append("m.chat_jid != 'status@broadcast'")

        with self._get_connection() as conn:
            chat_conds, params = self._build_chat_filter_conditions(
                conn, chat_filter=chat_name_or_jid, chat_scope=chat_scope, alias="m", include_sender=True
            )
            conditions.extend(chat_conds)
            ts_expr = "MAX(COALESCE(m.deleted_at, 0), COALESCE(m.edited_at, 0), COALESCE(m.timestamp, 0))"
            dt_conds, dt_params = self._build_date_range_conditions(date_from, date_to, ts_expr=ts_expr)
            conditions.extend(dt_conds)
            params.extend(dt_params)

            where_sql = " AND ".join(conditions)
            sql = f"""
            SELECT m.id, m.chat_jid, m.chat_name, m.sender_jid, m.sender_name, m.is_from_me,
                   m.text, m.original_text, m.timestamp, m.media_type, m.media_filename, m.local_path,
                   m.is_deleted, m.deleted_at, m.deleted_by, m.is_edited, m.edited_at
            FROM messages m
            WHERE {where_sql}
            ORDER BY MAX(COALESCE(m.deleted_at, 0), COALESCE(m.edited_at, 0), COALESCE(m.timestamp, 0)) DESC
            LIMIT ?
            """
            params.append(limit)
            rows = conn.execute(sql, tuple(params)).fetchall()
            msg_ids = [r["id"] for r in rows]
            revisions_by_msg: dict[str, list[dict[str, Any]]] = {}
            if msg_ids:
                placeholders = ",".join("?" for _ in msg_ids)
                rev_rows = conn.execute(
                    f"""
                    SELECT msg_id, event_type, actor, previous_text, new_text, event_ts
                    FROM message_revisions
                    WHERE msg_id IN ({placeholders})
                    ORDER BY event_ts ASC, revision_id ASC
                    """,
                    tuple(msg_ids),
                ).fetchall()
                for rv in rev_rows:
                    revisions_by_msg.setdefault(rv["msg_id"], []).append({
                        "event": rv["event_type"],
                        "actor": rv["actor"],
                        "previous_text": rv["previous_text"] or "",
                        "new_text": rv["new_text"] or "",
                        "timestamp": _format_timestamp(rv["event_ts"]),
                    })

            results = []
            for r in rows:
                sender = "You" if r["is_from_me"] else (r["sender_name"] or r["sender_jid"])
                current_body = self._strip_vault_markers(r["text"] or "")
                has_local_media = bool(r["local_path"] and not str(r["local_path"]).startswith("["))
                results.append({
                    "id": r["id"],
                    "chat": r["chat_name"] or r["chat_jid"],
                    "chat_jid": r["chat_jid"],
                    "sender": sender,
                    "sent_at": _format_timestamp(r["timestamp"]),
                    "is_deleted": bool(r["is_deleted"]),
                    "deleted_at": _format_timestamp(r["deleted_at"]) if r["deleted_at"] else "",
                    "deleted_by": r["deleted_by"] or "",
                    "is_edited": bool(r["is_edited"]),
                    "edited_at": _format_timestamp(r["edited_at"]) if r["edited_at"] else "",
                    "original_text": r["original_text"] or "",
                    "current_text": current_body,
                    "media_type": r["media_type"] or "text",
                    "media_filename": r["media_filename"] or "",
                    "media_saved_on_disk": has_local_media,
                    "revisions": revisions_by_msg.get(r["id"], []),
                })
            return results

    def prune_redundant_raw_messages(self, vacuum: bool = False) -> dict[str, Any]:
        """Clear redundant protobuf BLOBs on non-attachments and already-processed media rows."""
        db_p = Path(self.db_path)
        before_bytes = db_p.stat().st_size if db_p.exists() else 0

        with self._get_connection() as conn:
            cur = conn.execute("""
                UPDATE messages
                SET raw_message = NULL
                WHERE raw_message IS NOT NULL
                  AND (
                      media_type IN ('text', 'contact', 'poll', 'location', 'link')
                      OR (local_path IS NOT NULL AND local_path != '')
                  )
            """)
            rows_pruned = cur.rowcount or 0
            conn.commit()

        if vacuum:
            # Close pooled connections cleanly so exclusive VACUUM succeeds without lock contention
            self.close_all()
            v_conn = sqlite3.connect(self.db_path, timeout=60.0)
            try:
                v_conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                v_conn.execute("VACUUM;")
                v_conn.commit()
            finally:
                v_conn.close()

        after_bytes = db_p.stat().st_size if db_p.exists() else 0
        before_mb = round(before_bytes / (1024 * 1024), 2)
        after_mb = round(after_bytes / (1024 * 1024), 2)
        return {
            "rows_pruned": rows_pruned,
            "before_mb": before_mb,
            "after_mb": after_mb,
            "saved_mb": round(max(0.0, before_mb - after_mb), 2),
        }

    def prune_expired_status_media(self, retention_days: int) -> int:
        """Delete old stored status story files past retention_days if no other message references the file."""
        if retention_days <= 0:
            return 0
        cutoff_ts = int(time.time()) - (retention_days * 86400)
        removed = 0
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, local_path FROM messages
                WHERE chat_jid = 'status@broadcast'
                  AND timestamp < ?
                  AND local_path IS NOT NULL
                  AND local_path NOT LIKE '[%'
                """,
                (cutoff_ts,),
            ).fetchall()

            for r in rows:
                msg_id = r["id"]
                lp = r["local_path"]
                ref_count = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE local_path = ? AND id != ?",
                    (lp, msg_id),
                ).fetchone()[0]
                if ref_count == 0 and lp:
                    try:
                        p = Path(str(lp))
                        if p.exists() and p.is_file():
                            p.unlink()
                    except Exception as e:
                        logger.debug("Could not remove pruned status file %s: %s", lp, e)
                conn.execute(
                    "UPDATE messages SET local_path = '[STATUS_PRUNED]', raw_message = NULL WHERE id = ?",
                    (msg_id,),
                )
                removed += 1
            if removed:
                conn.commit()
        return removed

    # -------------------------------------------------------------
    # Gemini Tool Helper Methods
    # -------------------------------------------------------------

    def find_contacts(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search contacts and group chats by name, push name, phone number, or JID.
        Supports both full substring and multi-word out-of-order matching (e.g. 'Patil Ayush' matches 'Ayush Patil').
        """
        cleaned_query = f"%{query.strip().lower()}%"
        digits = self._normalize_phone_digits(query)
        like_digits = f"%{digits}%" if digits else cleaned_query
        words = [w for w in query.strip().lower().split() if len(w) > 1]

        with self._get_connection() as conn:
            # 1. Exact / Full substring + normalized phone-digit match first
            sql_exact = """
            SELECT jid, name, push_name, phone_number, is_group, updated_at
            FROM contacts
            WHERE (LOWER(name) LIKE ?
               OR LOWER(push_name) LIKE ?
               OR phone_number LIKE ?
               OR jid LIKE ?
               OR phone_number LIKE ?
               OR jid LIKE ?)
              AND jid != 'status@broadcast'
            ORDER BY updated_at DESC
            LIMIT ?
            """
            rows = list(
                conn.execute(
                    sql_exact,
                    (cleaned_query, cleaned_query, cleaned_query, cleaned_query, like_digits, like_digits, limit),
                ).fetchall()
            )

            # 2. If multi-word query and few results, match if ALL words appear across name or push_name
            if len(rows) < limit and len(words) > 1:
                seen_jids = {r["jid"] for r in rows}
                name_conds = " AND ".join(["(LOWER(name) LIKE ? OR LOWER(push_name) LIKE ?)" for _ in words])
                params = []
                for w in words:
                    params.extend([f"%{w}%", f"%{w}%"])
                params.append(limit - len(rows))

                sql_words = f"""
                SELECT jid, name, push_name, phone_number, is_group, updated_at
                FROM contacts
                WHERE ({name_conds})
                  AND jid != 'status@broadcast'
                ORDER BY updated_at DESC
                LIMIT ?
                """
                extra_rows = conn.execute(sql_words, tuple(params)).fetchall()
                for er in extra_rows:
                    if er["jid"] not in seen_jids:
                        rows.append(er)
                        seen_jids.add(er["jid"])

            results = []
            for r in rows:
                results.append(
                    {
                        "jid": r["jid"],
                        "name": r["name"] or r["push_name"] or "Unknown",
                        "push_name": r["push_name"],
                        "phone_number": r["phone_number"],
                        "is_group": bool(r["is_group"]),
                        "last_updated": _format_timestamp(r["updated_at"]) if r["updated_at"] else None,
                    }
                )
            return results

    def resolve_chat_jid(self, contact_or_chat: str) -> str | None:
        """Resolve a contact name, chat name, phone number, or JID to the most relevant active chat_jid in the database."""
        if not contact_or_chat:
            return None
        cleaned = contact_or_chat.strip()
        if not cleaned:
            return None

        with self._get_connection() as conn:
            # 1. If explicit JID is given:
            if "@" in cleaned:
                # If messages exist directly for this JID, return it
                exists = conn.execute("SELECT chat_jid FROM messages WHERE chat_jid = ? LIMIT 1", (cleaned,)).fetchone()
                if exists:
                    return cleaned
                # If it's an LID, try mapping to phone JID via contacts
                if cleaned.endswith("@lid"):
                    c_row = conn.execute("SELECT phone_number FROM contacts WHERE jid = ? AND phone_number IS NOT NULL", (cleaned,)).fetchone()
                    if c_row and c_row["phone_number"]:
                        phone_jid = f"{c_row['phone_number']}@s.whatsapp.net"
                        if conn.execute("SELECT 1 FROM messages WHERE chat_jid = ? LIMIT 1", (phone_jid,)).fetchone():
                            return phone_jid
                return cleaned

            like_name = f"%{cleaned.lower()}%"

            # 2. Check directly in messages table for an active chat with matching chat_name
            # Prioritize chats with the most recent messages!
            row_chat = conn.execute(
                """
                SELECT chat_jid
                FROM messages
                WHERE LOWER(chat_name) LIKE ? AND chat_jid != 'status@broadcast'
                GROUP BY chat_jid
                ORDER BY MAX(timestamp) DESC
                LIMIT 1
                """,
                (like_name,)
            ).fetchone()
            if row_chat:
                return row_chat["chat_jid"]

            # 3. Check messages table by sender_name
            row_sender = conn.execute(
                """
                SELECT chat_jid
                FROM messages
                WHERE LOWER(sender_name) LIKE ? AND chat_jid != 'status@broadcast'
                GROUP BY chat_jid
                ORDER BY MAX(timestamp) DESC
                LIMIT 1
                """,
                (like_name,)
            ).fetchone()
            if row_sender:
                return row_sender["chat_jid"]

            # 4. Search contacts table (prioritize contacts that actually have messages in the database)
            contacts = self.find_contacts(cleaned, limit=10)
            if contacts:
                for c in contacts:
                    c_jid = c["jid"]
                    has_msgs = conn.execute("SELECT 1 FROM messages WHERE chat_jid = ? LIMIT 1", (c_jid,)).fetchone()
                    if has_msgs:
                        return c_jid
                # If none have messages, return the first contact's phone JID if available
                phone_contacts = [c for c in contacts if not c["jid"].endswith("@lid")]
                if phone_contacts:
                    return phone_contacts[0]["jid"]
                return contacts[0]["jid"]

            return None

    @staticmethod
    def _tokenize_search_terms(query: str) -> list[str]:
        """Extract clean word/number tokens for FTS5 and multi-keyword matching."""
        return [t for t in re.findall(r"[^\W_]+", (query or "").strip(), flags=re.UNICODE) if t]

    @classmethod
    def _build_fts_match_expressions(cls, query: str) -> list[str]:
        """Build ordered FTS5 MATCH expressions (strict tokenized AND -> stopword-relaxed AND)."""
        tokens = cls._tokenize_search_terms(query)
        if not tokens:
            return []

        def _fmt_tok(tok: str) -> str:
            escaped = tok.replace('"', '""')
            return f'"{escaped}"*' if len(tok) >= 2 else f'"{escaped}"'

        exprs: list[str] = [" AND ".join(_fmt_tok(t) for t in tokens)]
        filtered = [t for t in tokens if t.lower() not in _SEARCH_STOP_WORDS]
        if filtered and len(filtered) < len(tokens):
            relaxed = " AND ".join(_fmt_tok(t) for t in filtered)
            if relaxed not in exprs:
                exprs.append(relaxed)
        return exprs

    @staticmethod
    def _format_search_hit(r: sqlite3.Row) -> dict[str, Any]:
        """Format a message (+ optional OCR cache excerpt) for Gemini agent tools."""
        keys = set(r.keys())
        sender = "You" if r["is_from_me"] else (r["sender_name"] or r["sender_jid"])
        hit: dict[str, Any] = {
            "id": r["id"],
            "chat": r["chat_name"] or r["chat_jid"],
            "chat_jid": r["chat_jid"],
            "sender": sender,
            "timestamp": _format_timestamp(r["timestamp"]),
            "text": r["text"],
            "media_type": r["media_type"],
            "filename": r["media_filename"],
        }
        if "ocr_cached" in keys and r["ocr_cached"]:
            ocr_txt = str(r["ocr_cached"])
            if not ocr_txt.startswith(("[MEDIA_", "[OCR_SKIPPED")):
                hit["ocr_excerpt"] = ocr_txt[:500].rstrip() + ("..." if len(ocr_txt) > 500 else "")
        return hit

    def get_chat_history(
        self,
        chat_jid_or_name: str,
        limit: int = 50,
        date_from: int = 0,
        date_to: int = 0,
    ) -> list[dict[str, Any]]:
        """Get recent message history with a specific contact or group in chronological order."""
        target_jid = self.resolve_chat_jid(chat_jid_or_name) or chat_jid_or_name.strip()
        like_name = f"%{chat_jid_or_name.strip().lower()}%"
        conditions = ["(chat_jid = ? OR (is_group = 0 AND (LOWER(chat_name) LIKE ? OR LOWER(sender_name) LIKE ?)))"]
        if target_jid != "status@broadcast" and "status" not in chat_jid_or_name.strip().lower():
            conditions.append("chat_jid != 'status@broadcast'")
        params: list[Any] = [target_jid, like_name, like_name]
        dt_conds, dt_params = self._build_date_range_conditions(date_from, date_to, ts_expr="timestamp")
        conditions.extend(dt_conds)
        params.extend(dt_params)
        params.append(limit)

        sql = f"""
        SELECT id, chat_jid, chat_name, sender_jid, sender_name, text, timestamp, is_from_me, is_group,
               media_type, media_filename, media_mimetype, media_size, media_metadata
        FROM messages
        WHERE {' AND '.join(conditions)}
        ORDER BY timestamp DESC
        LIMIT ?
        """
        with self._get_connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
            results = []
            # Reverse to return in chronological order (oldest to newest)
            for r in reversed(rows):
                time_str = _format_timestamp(r["timestamp"])
                sender = "You" if r["is_from_me"] else (r["sender_name"] or r["sender_jid"])
                results.append(
                    {
                        "id": r["id"],
                        "chat_name": r["chat_name"] or r["chat_jid"],
                        "chat_jid": r["chat_jid"],
                        "sender": sender,
                        "is_from_me": bool(r["is_from_me"]),
                        "timestamp": time_str,
                        "text": r["text"],
                        "media_type": r["media_type"],
                        "filename": r["media_filename"],
                        "mimetype": r["media_mimetype"],
                    }
                )
            return results

    def search_messages(
        self,
        query: str = "",
        chat_jid: str | None = None,
        limit: int = 25,
        date_from: int = 0,
        date_to: int = 0,
        include_statuses: bool = False,
    ) -> list[dict[str, Any]]:
        """Search across messages, filenames, preserved originals, and cached OCR/transcripts."""
        q_clean = (query or "").strip()
        cand_limit = max(limit * 2, 40)

        with self._get_connection() as conn:
            base_conds: list[str] = []
            if not include_statuses:
                base_conds.append("m.chat_jid != 'status@broadcast'")
            chat_conds, base_params = self._build_chat_filter_conditions(
                conn, chat_filter=chat_jid or "", chat_scope="all", alias="m", include_sender=True
            )
            base_conds.extend(chat_conds)
            dt_conds, dt_params = self._build_date_range_conditions(date_from, date_to, ts_expr="m.timestamp")
            base_conds.extend(dt_conds)
            base_params.extend(dt_params)

            if not q_clean:
                where_sql = f"WHERE {' AND '.join(base_conds)}" if base_conds else ""
                sql = f"""
                SELECT m.id, m.chat_jid, m.chat_name, m.sender_jid, m.sender_name, m.text, m.timestamp,
                       m.is_from_me, m.media_type, m.media_filename, m.media_mimetype,
                       c.ocr_text AS ocr_cached
                FROM messages m
                LEFT JOIN media_ocr_cache c ON m.id = c.msg_id
                {where_sql}
                ORDER BY m.timestamp DESC
                LIMIT ?
                """
                rows = conn.execute(sql, tuple([*base_params, limit])).fetchall()
                return [self._format_search_hit(r) for r in rows]

            hits_by_id: dict[str, tuple[tuple[int, float, int], sqlite3.Row]] = {}
            q_low = q_clean.lower()
            phrase_like = f"%{q_low}%"

            # 1. Tokenized FTS5 search with BM25 relevance + exact phrase boost
            for match_expr in self._build_fts_match_expressions(q_clean):
                try:
                    fts_conds = ["messages_fts MATCH ?", *base_conds]
                    sql = f"""
                    SELECT m.id, m.chat_jid, m.chat_name, m.sender_jid, m.sender_name, m.text, m.timestamp,
                           m.is_from_me, m.media_type, m.media_filename, m.media_mimetype,
                           c.ocr_text AS ocr_cached, f.rank AS fts_rank
                    FROM messages_fts f
                    JOIN messages m ON f.rowid = m.rowid
                    LEFT JOIN media_ocr_cache c ON m.id = c.msg_id
                    WHERE {' AND '.join(fts_conds)}
                    ORDER BY (CASE WHEN LOWER(COALESCE(m.text, '')) LIKE ? OR LOWER(COALESCE(m.media_filename, '')) LIKE ? THEN 0 ELSE 1 END),
                             f.rank ASC,
                             m.timestamp DESC
                    LIMIT ?
                    """
                    fts_rows = conn.execute(
                        sql, tuple([match_expr, *base_params, phrase_like, phrase_like, cand_limit])
                    ).fetchall()
                    if fts_rows:
                        for r in fts_rows:
                            hay = f"{r['text'] or ''} {r['media_filename'] or ''}".lower()
                            phrase_miss = 0 if q_low in hay else 1
                            score = (phrase_miss, float(r["fts_rank"] or 0.0), -int(r["timestamp"] or 0))
                            hits_by_id[r["id"]] = (score, r)
                        break
                except Exception as e:
                    logger.warning("FTS search encountered error '%s', falling back to standard LIKE search", e)

            # 2. Cached OCR / document text / voice transcript search (media_ocr_cache)
            raw_tokens = [t.lower() for t in self._tokenize_search_terms(q_clean)]
            ocr_tokens = [t for t in raw_tokens if t not in _SEARCH_STOP_WORDS] or raw_tokens
            ocr_conds = [
                "c.ocr_text NOT LIKE '[MEDIA_%'",
                "c.ocr_text NOT LIKE '[OCR_SKIPPED%'",
                *base_conds,
            ]
            ocr_params: list[Any] = []
            if ocr_tokens:
                for tok in ocr_tokens:
                    ocr_conds.append(
                        "(LOWER(c.ocr_text) LIKE ? OR LOWER(COALESCE(m.text, '')) LIKE ? OR LOWER(COALESCE(m.media_filename, '')) LIKE ?)"
                    )
                    tp = f"%{tok}%"
                    ocr_params.extend([tp, tp, tp])
            else:
                ocr_conds.append("LOWER(c.ocr_text) LIKE ?")
                ocr_params.append(phrase_like)

            ocr_sql = f"""
            SELECT m.id, m.chat_jid, m.chat_name, m.sender_jid, m.sender_name, m.text, m.timestamp,
                   m.is_from_me, m.media_type, m.media_filename, m.media_mimetype,
                   c.ocr_text AS ocr_cached
            FROM media_ocr_cache c
            JOIN messages m ON c.msg_id = m.id
            WHERE {' AND '.join(ocr_conds)}
            ORDER BY m.timestamp DESC
            LIMIT ?
            """
            ocr_rows = conn.execute(ocr_sql, tuple([*ocr_params, *base_params, cand_limit])).fetchall()
            for r in ocr_rows:
                ocr_txt = (r["ocr_cached"] or "").lower()
                phrase_miss = 0 if (q_low in ocr_txt or q_low in (r["text"] or "").lower()) else 1
                score = (phrase_miss, -0.5 if phrase_miss == 0 else 0.0, -int(r["timestamp"] or 0))
                if r["id"] not in hits_by_id or score < hits_by_id[r["id"]][0]:
                    hits_by_id[r["id"]] = (score, r)

            if hits_by_id:
                ranked = sorted(hits_by_id.values(), key=lambda item: item[0])[:limit]
                return [self._format_search_hit(row) for _, row in ranked]

            # 3. Tokenized LIKE fallback across text, filename, original_text, and ocr_cache (for punctuation/URLs/codes)
            like_conds = list(base_conds)
            like_params: list[Any] = []
            if ocr_tokens:
                for tok in ocr_tokens:
                    tp = f"%{tok}%"
                    like_conds.append(
                        "(LOWER(COALESCE(m.text, '')) LIKE ? OR LOWER(COALESCE(m.media_filename, '')) LIKE ? OR LOWER(COALESCE(m.original_text, '')) LIKE ? OR LOWER(COALESCE(c.ocr_text, '')) LIKE ?)"
                    )
                    like_params.extend([tp, tp, tp, tp])
            else:
                like_conds.append(
                    "(LOWER(COALESCE(m.text, '')) LIKE ? OR LOWER(COALESCE(m.media_filename, '')) LIKE ? OR LOWER(COALESCE(m.original_text, '')) LIKE ? OR LOWER(COALESCE(c.ocr_text, '')) LIKE ?)"
                )
                like_params.extend([phrase_like, phrase_like, phrase_like, phrase_like])

            fallback_sql = f"""
            SELECT m.id, m.chat_jid, m.chat_name, m.sender_jid, m.sender_name, m.text, m.timestamp,
                   m.is_from_me, m.media_type, m.media_filename, m.media_mimetype,
                   c.ocr_text AS ocr_cached
            FROM messages m
            LEFT JOIN media_ocr_cache c ON m.id = c.msg_id
            WHERE {' AND '.join(like_conds)}
            ORDER BY m.timestamp DESC
            LIMIT ?
            """
            rows = conn.execute(fallback_sql, tuple([*base_params, *like_params, limit])).fetchall()
            return [self._format_search_hit(r) for r in rows]

    def find_documents(
        self,
        query: str = "",
        chat_name_or_jid: str = "",
        limit: int = 15,
        date_from: int = 0,
        date_to: int = 0,
    ) -> list[dict[str, Any]]:
        """Search specifically for documents (PDFs, spreadsheets, docs, presentations) by filename, caption, or indexed text.
        Supports stemming, flexible multi-keyword matching, and optional Unix date bounds.
        """
        target_jid = self.resolve_chat_jid(chat_name_or_jid) if chat_name_or_jid else None

        conditions = ["media_type = 'document'", "chat_jid != 'status@broadcast'"]
        params: list[Any] = []

        if chat_name_or_jid:
            like_name = f"%{chat_name_or_jid.strip().lower()}%"
            if target_jid:
                conditions.append("(chat_jid = ? OR sender_jid = ? OR LOWER(sender_name) LIKE ? OR LOWER(chat_name) LIKE ?)")
                params.extend([target_jid, target_jid, like_name, like_name])
            else:
                conditions.append("(LOWER(sender_name) LIKE ? OR LOWER(chat_name) LIKE ?)")
                params.extend([like_name, like_name])

        dt_conds, dt_params = self._build_date_range_conditions(date_from, date_to, ts_expr="timestamp")
        conditions.extend(dt_conds)
        params.extend(dt_params)

        if query:
            def _stem(w: str) -> str:
                w = w.lower().strip()
                if w.endswith("ies") and len(w) > 4:
                    return w[:-3]
                if w.endswith("es") and len(w) > 3 and not w.endswith("ees"):
                    return w[:-2]
                if w.endswith("s") and len(w) > 2 and not w.endswith("ss"):
                    return w[:-1]
                return w

            doc_stop = _SEARCH_STOP_WORDS | {"get", "send", "give", "show", "find", "all", "recent", "latest", "file", "files"}
            tokens = [w for w in re.findall(r"\w+", query.lower()) if w not in doc_stop]
            stemmed = [_stem(t) for t in tokens if len(_stem(t)) >= 2]

            if stemmed:
                for st in stemmed:
                    tp = f"%{st}%"
                    conditions.append(
                        "(LOWER(media_filename) LIKE ? OR LOWER(text) LIKE ? OR id IN (SELECT msg_id FROM media_ocr_cache WHERE LOWER(ocr_text) LIKE ?))"
                    )
                    params.extend([tp, tp, tp])
            else:
                cleaned = f"%{query.strip().lower()}%"
                conditions.append(
                    "(LOWER(media_filename) LIKE ? OR LOWER(text) LIKE ? OR id IN (SELECT msg_id FROM media_ocr_cache WHERE LOWER(ocr_text) LIKE ?))"
                )
                params.extend([cleaned, cleaned, cleaned])

        where_clause = " AND ".join(conditions)
        sql = f"""
        SELECT id, chat_jid, chat_name, sender_jid, sender_name, text, timestamp,
               is_from_me, media_filename, media_mimetype, media_size, media_metadata
        FROM messages
        WHERE {where_clause}
        ORDER BY timestamp DESC
        LIMIT ?
        """
        params.append(limit)

        with self._get_connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
            results = []
            for r in rows:
                sender = "You" if r["is_from_me"] else (r["sender_name"] or r["sender_jid"])
                try:
                    meta = json.loads(r["media_metadata"]) if r["media_metadata"] else {}
                except Exception:
                    meta = {}

                filename = r["media_filename"] or meta.get("filename", "")
                if not filename:
                    if r["media_mimetype"]:
                        mime_ext = r["media_mimetype"].split("/")[-1].upper()
                        filename = f"Document ({mime_ext})"
                    elif r["text"] and not r["text"].startswith("📄 [Document"):
                        filename = f"Document: {r['text'][:40]}"
                    else:
                        filename = "Shared Document (Synced before rich media update)"

                raw_caption = r["text"] or ""
                if len(raw_caption) > 600:
                    raw_caption = raw_caption[:600].rstrip() + "..."
                results.append(
                    {
                        "id": r["id"],
                        "filename": filename,
                        "pages": meta.get("page_count", 0),
                        "mimetype": r["media_mimetype"],
                        "chat": r["chat_name"] or r["chat_jid"],
                        "sender": sender,
                        "timestamp": _format_timestamp(r["timestamp"]),
                        "caption_or_text": raw_caption,
                    }
                )
            return results

    def find_shared_contacts(self, query: str = "", chat_name_or_jid: str = "", limit: int = 10) -> list[dict[str, Any]]:
        """Find contact cards (vCards) shared across WhatsApp chats by contact name, phone, or profession."""
        target_jid = self.resolve_chat_jid(chat_name_or_jid) if chat_name_or_jid else None

        conditions = ["media_type = 'contact'", "chat_jid != 'status@broadcast'"]
        params: list[Any] = []

        if chat_name_or_jid:
            like_name = f"%{chat_name_or_jid.strip().lower()}%"
            if target_jid:
                conditions.append("(chat_jid = ? OR sender_jid = ? OR LOWER(sender_name) LIKE ? OR LOWER(chat_name) LIKE ?)")
                params.extend([target_jid, target_jid, like_name, like_name])
            else:
                conditions.append("(LOWER(sender_name) LIKE ? OR LOWER(chat_name) LIKE ?)")
                params.extend([like_name, like_name])

        if query:
            cleaned = f"%{query.strip().lower()}%"
            conditions.append("LOWER(text) LIKE ?")
            params.append(cleaned)

        where_clause = " AND ".join(conditions)
        sql = f"""
        SELECT id, chat_jid, chat_name, sender_jid, sender_name, text, timestamp,
               is_from_me, media_metadata
        FROM messages
        WHERE {where_clause}
        ORDER BY timestamp DESC
        LIMIT ?
        """
        params.append(limit)

        with self._get_connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
            results = []
            for r in rows:
                sender = "You" if r["is_from_me"] else (r["sender_name"] or r["sender_jid"])
                try:
                    meta = json.loads(r["media_metadata"]) if r["media_metadata"] else {}
                except Exception:
                    meta = {}

                results.append(
                    {
                        "id": r["id"],
                        "contact_name": meta.get("display_name", ""),
                        "phone": meta.get("phone", ""),
                        "organization": meta.get("organization", ""),
                        "email": meta.get("email", ""),
                        "chat": r["chat_name"] or r["chat_jid"],
                        "shared_by": sender,
                        "timestamp": _format_timestamp(r["timestamp"]),
                        "details": r["text"],
                    }
                )
            return results

    def get_polls(self, query: str = "", chat_name_or_jid: str = "", limit: int = 10) -> list[dict[str, Any]]:
        """Find polls created in chats, including questions and option choices."""
        target_jid = self.resolve_chat_jid(chat_name_or_jid) if chat_name_or_jid else None

        conditions = ["media_type = 'poll'", "chat_jid != 'status@broadcast'"]
        params: list[Any] = []

        if chat_name_or_jid:
            like_name = f"%{chat_name_or_jid.strip().lower()}%"
            if target_jid:
                conditions.append("(chat_jid = ? OR sender_jid = ? OR LOWER(sender_name) LIKE ? OR LOWER(chat_name) LIKE ?)")
                params.extend([target_jid, target_jid, like_name, like_name])
            else:
                conditions.append("(LOWER(sender_name) LIKE ? OR LOWER(chat_name) LIKE ?)")
                params.extend([like_name, like_name])

        if query:
            cleaned = f"%{query.strip().lower()}%"
            conditions.append("LOWER(text) LIKE ?")
            params.append(cleaned)

        where_clause = " AND ".join(conditions)
        sql = f"""
        SELECT id, chat_jid, chat_name, sender_jid, sender_name, text, timestamp,
               is_from_me, media_metadata
        FROM messages
        WHERE {where_clause}
        ORDER BY timestamp DESC
        LIMIT ?
        """
        params.append(limit)

        with self._get_connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
            results = []
            for r in rows:
                sender = "You" if r["is_from_me"] else (r["sender_name"] or r["sender_jid"])
                try:
                    meta = json.loads(r["media_metadata"]) if r["media_metadata"] else {}
                except Exception:
                    meta = {}

                results.append(
                    {
                        "id": r["id"],
                        "poll_question": meta.get("question", ""),
                        "options": meta.get("options", []),
                        "choice_type": meta.get("choice_type", "Single Choice"),
                        "chat": r["chat_name"] or r["chat_jid"],
                        "created_by": sender,
                        "timestamp": _format_timestamp(r["timestamp"]),
                        "summary": r["text"],
                    }
                )
            return results

    def find_voice_notes_and_media(
        self,
        media_type: str = "voice_note",
        query: str = "",
        chat_name_or_jid: str = "",
        limit: int = 15,
        date_from: int = 0,
        date_to: int = 0,
        include_statuses: bool = False,
    ) -> list[dict[str, Any]]:
        """Find voice notes, audio files, images, or videos sent in conversations.
        Searches in direct chats, matching sender names, captions, filenames, or cached OCR/transcripts.
        """
        target_jid = self.resolve_chat_jid(chat_name_or_jid) if chat_name_or_jid else None

        conditions: list[str] = []
        params: list[Any] = []
        if not include_statuses:
            conditions.append("chat_jid != 'status@broadcast'")

        if media_type:
            conditions.append("media_type = ?")
            params.append(media_type)

        if chat_name_or_jid:
            like_name = f"%{chat_name_or_jid.strip().lower()}%"
            if target_jid:
                conditions.append("(chat_jid = ? OR sender_jid = ? OR LOWER(sender_name) LIKE ? OR LOWER(chat_name) LIKE ?)")
                params.extend([target_jid, target_jid, like_name, like_name])
            else:
                conditions.append("(LOWER(sender_name) LIKE ? OR LOWER(chat_name) LIKE ?)")
                params.extend([like_name, like_name])

        dt_conds, dt_params = self._build_date_range_conditions(date_from, date_to, ts_expr="timestamp")
        conditions.extend(dt_conds)
        params.extend(dt_params)

        if query:
            raw_tokens = [t.lower() for t in self._tokenize_search_terms(query)]
            tokens = [t for t in raw_tokens if t not in _SEARCH_STOP_WORDS] or raw_tokens
            if tokens:
                for tok in tokens:
                    tp = f"%{tok}%"
                    conditions.append(
                        "(LOWER(COALESCE(text, '')) LIKE ? OR LOWER(COALESCE(media_filename, '')) LIKE ? OR id IN (SELECT msg_id FROM media_ocr_cache WHERE LOWER(ocr_text) LIKE ?))"
                    )
                    params.extend([tp, tp, tp])
            else:
                cleaned = f"%{query.strip().lower()}%"
                conditions.append(
                    "(LOWER(COALESCE(text, '')) LIKE ? OR LOWER(COALESCE(media_filename, '')) LIKE ? OR id IN (SELECT msg_id FROM media_ocr_cache WHERE LOWER(ocr_text) LIKE ?))"
                )
                params.extend([cleaned, cleaned, cleaned])

        where_clause = " AND ".join(conditions) if conditions else "1=1"
        sql = f"""
        SELECT id, chat_jid, chat_name, sender_jid, sender_name, text, timestamp,
               is_from_me, media_type, media_metadata
        FROM messages
        WHERE {where_clause}
        ORDER BY timestamp DESC
        LIMIT ?
        """
        params.append(limit)

        with self._get_connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
            results = []
            for r in rows:
                sender = "You" if r["is_from_me"] else (r["sender_name"] or r["sender_jid"])
                try:
                    meta = json.loads(r["media_metadata"]) if r["media_metadata"] else {}
                except Exception:
                    meta = {}

                results.append(
                    {
                        "id": r["id"],
                        "media_type": r["media_type"],
                        "duration": meta.get("duration_formatted", ""),
                        "caption": meta.get("caption", ""),
                        "chat": r["chat_name"] or r["chat_jid"],
                        "sender": sender,
                        "timestamp": _format_timestamp(r["timestamp"]),
                        "description": r["text"],
                    }
                )
            return results

    def get_recent_activity(self, hours: int = 48, limit: int = 50) -> list[dict[str, Any]]:
        """Retrieve recent messages across all WhatsApp chats within the last N hours."""
        cutoff = int(datetime.now().timestamp()) - (hours * 3600)
        sql = """
        SELECT id, chat_jid, chat_name, sender_jid, sender_name, text, timestamp, is_from_me, is_group,
               media_type, media_filename
        FROM messages
        WHERE timestamp >= ? AND chat_jid != 'status@broadcast'
        ORDER BY timestamp DESC
        LIMIT ?
        """
        with self._get_connection() as conn:
            rows = conn.execute(sql, (cutoff, limit)).fetchall()
            results = []
            for r in rows:
                time_str = _format_timestamp(r["timestamp"])
                sender = "You" if r["is_from_me"] else (r["sender_name"] or r["sender_jid"])
                results.append(
                    {
                        "id": r["id"],
                        "chat": r["chat_name"] or r["chat_jid"],
                        "chat_jid": r["chat_jid"],
                        "sender": sender,
                        "timestamp": time_str,
                        "text": r["text"],
                        "media_type": r["media_type"],
                        "is_group": bool(r["is_group"]),
                    }
                )
            return results

    def get_stats(self) -> dict[str, Any]:
        """Return comprehensive counts and date ranges of the indexed message database."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            total_msgs = cursor.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            total_contacts = cursor.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
            total_groups = cursor.execute("SELECT COUNT(*) FROM contacts WHERE is_group = 1").fetchone()[0]
            total_ocr = 0
            try:
                total_ocr = cursor.execute(
                    "SELECT COUNT(*) FROM media_ocr_cache WHERE ocr_text NOT LIKE '[MEDIA_%' AND ocr_text NOT LIKE '[OCR_SKIPPED%'"
                ).fetchone()[0]
            except Exception:
                pass

            total_stored_media = 0
            dedup_saved_files = 0
            try:
                stored_row = cursor.execute(
                    """
                    SELECT COUNT(*), COUNT(DISTINCT local_path)
                    FROM messages
                    WHERE local_path IS NOT NULL AND local_path NOT LIKE '[%'
                    """
                ).fetchone()
                total_stored_media = stored_row[0] or 0
                distinct_stored = stored_row[1] or 0
                dedup_saved_files = max(0, total_stored_media - distinct_stored)
            except Exception:
                pass

            raw_blob_mb = 0.0
            try:
                raw_bytes = cursor.execute(
                    "SELECT SUM(LENGTH(raw_message)) FROM messages WHERE raw_message IS NOT NULL"
                ).fetchone()[0]
                if raw_bytes:
                    raw_blob_mb = round(raw_bytes / (1024 * 1024), 2)
            except Exception:
                pass

            total_local_ocr = 0
            total_local_docs = 0
            total_deleted = 0
            total_edited = 0
            try:
                counts_row = cursor.execute(
                    """
                    SELECT
                        COALESCE(SUM(CASE WHEN local_ocr_done = 1 THEN 1 ELSE 0 END), 0),
                        COALESCE(SUM(CASE WHEN local_doc_done = 1 THEN 1 ELSE 0 END), 0),
                        COALESCE(SUM(CASE WHEN is_deleted = 1 THEN 1 ELSE 0 END), 0),
                        COALESCE(SUM(CASE WHEN is_edited = 1 THEN 1 ELSE 0 END), 0)
                    FROM messages
                    """
                ).fetchone()
                if counts_row:
                    total_local_ocr = counts_row[0] or 0
                    total_local_docs = counts_row[1] or 0
                    total_deleted = counts_row[2] or 0
                    total_edited = counts_row[3] or 0
            except Exception:
                pass

            oldest_ts = cursor.execute("SELECT MIN(timestamp) FROM messages WHERE timestamp > 0").fetchone()[0]
            newest_ts = cursor.execute("SELECT MAX(timestamp) FROM messages WHERE timestamp > 0").fetchone()[0]

            db_p = Path(self.db_path)
            db_size_bytes = db_p.stat().st_size if db_p.exists() else 0
            db_size_mb = round(db_size_bytes / (1024 * 1024), 2)

            return {
                "total_messages": total_msgs,
                "total_contacts": total_contacts,
                "total_groups": total_groups,
                "total_ocr_cached": total_ocr,
                "total_local_ocr": total_local_ocr,
                "total_local_docs": total_local_docs,
                "total_deleted": total_deleted,
                "total_edited": total_edited,
                "total_stored_media": total_stored_media,
                "dedup_saved_files": dedup_saved_files,
                "raw_blob_mb": raw_blob_mb,
                "oldest_timestamp": oldest_ts,
                "newest_timestamp": newest_ts,
                "oldest_date": _format_timestamp(oldest_ts) if oldest_ts else "N/A",
                "newest_date": _format_timestamp(newest_ts) if newest_ts else "N/A",
                "db_size_mb": db_size_mb,
            }

    @staticmethod
    def _extract_phone_from_jid(jid: str | None) -> str:
        """Extract raw digits phone number from a standard @s.whatsapp.net JID if present."""
        if not jid or not jid.endswith("@s.whatsapp.net"):
            return ""
        user_part = jid.split("@", 1)[0]
        return user_part if user_part.isdigit() else ""

    @staticmethod
    def _normalize_phone_digits(query: str) -> str:
        """Extract normalized phone digits when the filter looks like a phone fragment (>= 3 digits)."""
        if not query:
            return ""
        digits = re.sub(r"\D+", "", query)
        return digits if len(digits) >= 3 else ""

    @staticmethod
    def _build_chat_scope_clause(chat_scope: str, alias: str = "m") -> tuple[str, list[Any]]:
        """Build SQL clause for 'all', 'dm', or 'group' filtering."""
        sc = (chat_scope or "").strip().lower()
        p = f"{alias}." if alias else ""
        if sc == "dm":
            return f"{p}is_group = 0 AND {p}chat_jid != 'status@broadcast'", []
        if sc == "group":
            return f"{p}is_group = 1", []
        return "", []

    def _resolve_matching_jids_and_digits(
        self, conn: sqlite3.Connection, chat_filter: str, limit: int = 30
    ) -> tuple[list[str], str]:
        """Resolve all matching contact JIDs (including @lid and phone JIDs) plus normalized digits."""
        raw = (chat_filter or "").strip()
        if not raw:
            return [], ""
        digits = self._normalize_phone_digits(raw)
        like_q = f"%{raw.lower()}%"

        conds = [
            "LOWER(COALESCE(name, '')) LIKE ?",
            "LOWER(COALESCE(push_name, '')) LIKE ?",
            "LOWER(jid) LIKE ?",
        ]
        params: list[Any] = [like_q, like_q, like_q]
        if digits:
            like_d = f"%{digits}%"
            conds.extend(["COALESCE(phone_number, '') LIKE ?", "jid LIKE ?"])
            params.extend([like_d, like_d])
            if len(digits) > 10:
                conds.append("COALESCE(phone_number, '') LIKE ?")
                params.append(f"%{digits[-10:]}%")

        rows = conn.execute(
            f"""
            SELECT jid, phone_number
            FROM contacts
            WHERE jid != 'status@broadcast' AND ({' OR '.join(conds)})
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            tuple([*params, limit]),
        ).fetchall()

        jids: list[str] = []
        seen: set[str] = set()
        for r in rows:
            for candidate in (
                r["jid"],
                f"{r['phone_number']}@s.whatsapp.net" if r["phone_number"] else "",
            ):
                if candidate and candidate not in seen:
                    seen.add(candidate)
                    jids.append(candidate)

        if "@" in raw and raw not in seen:
            jids.append(raw)
        return jids, digits

    def _build_chat_filter_conditions(
        self,
        conn: sqlite3.Connection,
        chat_filter: str = "",
        chat_scope: str = "all",
        alias: str = "m",
        include_sender: bool = True,
    ) -> tuple[list[str], list[Any]]:
        """Build unified SQL WHERE conditions for chat name, phone number (+country/digits), JID, and scope."""
        conditions: list[str] = []
        params: list[Any] = []
        p = f"{alias}." if alias else ""

        scope_sql, scope_params = self._build_chat_scope_clause(chat_scope, alias=alias)
        if scope_sql:
            conditions.append(scope_sql)
            params.extend(scope_params)

        raw = (chat_filter or "").strip()
        if not raw:
            return conditions, params

        like_q = f"%{raw.lower()}%"
        or_clauses = [
            f"LOWER(COALESCE({p}chat_name, '')) LIKE ?",
            f"LOWER({p}chat_jid) LIKE ?",
        ]
        or_params: list[Any] = [like_q, like_q]

        if include_sender:
            or_clauses.extend([
                f"LOWER(COALESCE({p}sender_name, '')) LIKE ?",
                f"LOWER(COALESCE({p}sender_jid, '')) LIKE ?",
            ])
            or_params.extend([like_q, like_q])

        matched_jids, digits = self._resolve_matching_jids_and_digits(conn, raw)
        if digits:
            digit_patterns = [f"%{digits}%"]
            if len(digits) > 10:
                digit_patterns.append(f"%{digits[-10:]}%")
            for dp in digit_patterns:
                or_clauses.append(f"{p}chat_jid LIKE ?")
                or_params.append(dp)
                if include_sender:
                    or_clauses.append(f"{p}sender_jid LIKE ?")
                    or_params.append(dp)

        if matched_jids:
            ph = ",".join("?" for _ in matched_jids)
            or_clauses.append(f"{p}chat_jid IN ({ph})")
            or_params.extend(matched_jids)
            if include_sender:
                or_clauses.append(f"{p}sender_jid IN ({ph})")
                or_params.extend(matched_jids)

        conditions.append(f"({' OR '.join(or_clauses)})")
        params.extend(or_params)
        return conditions, params

    @staticmethod
    def _format_web_message_row(r: sqlite3.Row) -> dict[str, Any]:
        """Convert a database message row (+ optional media_ocr_cache join) into a Web UI payload dict."""
        keys = set(r.keys())
        sender = "You" if r["is_from_me"] else (r["sender_name"] or r["sender_jid"])
        lp = r["local_path"] if "local_path" in keys else None
        has_media = bool(lp and not str(lp).startswith("["))
        ocr_raw = r["ocr_cached"] if "ocr_cached" in keys else ""
        if ocr_raw and str(ocr_raw).startswith(("[MEDIA_", "[OCR_SKIPPED")):
            ocr_raw = ""
        return {
            "id": r["id"],
            "chat_jid": r["chat_jid"],
            "chat_name": r["chat_name"] or r["chat_jid"],
            "chat_phone": DatabaseManager._extract_phone_from_jid(r["chat_jid"]),
            "sender": sender,
            "is_from_me": bool(r["is_from_me"]),
            "is_group": bool(r["is_group"]),
            "timestamp": int(r["timestamp"] or 0),
            "timestamp_str": _format_timestamp(r["timestamp"]),
            "text": r["text"] or "",
            "original_text": r["original_text"] if "original_text" in keys else "",
            "media_type": r["media_type"] or "text",
            "media_filename": r["media_filename"] or "",
            "media_mimetype": r["media_mimetype"] or "",
            "media_size": int(r["media_size"] or 0) if "media_size" in keys else 0,
            "has_local_media": has_media,
            "is_deleted": bool(r["is_deleted"]) if "is_deleted" in keys else False,
            "deleted_at": _format_timestamp(r["deleted_at"]) if ("deleted_at" in keys and r["deleted_at"]) else "",
            "deleted_by": (r["deleted_by"] or "") if "deleted_by" in keys else "",
            "is_edited": bool(r["is_edited"]) if "is_edited" in keys else False,
            "edited_at": _format_timestamp(r["edited_at"]) if ("edited_at" in keys and r["edited_at"]) else "",
            "ocr_cached": ocr_raw or "",
        }

    @staticmethod
    def _build_date_range_conditions(
        date_from: int = 0, date_to: int = 0, ts_expr: str = "m.timestamp"
    ) -> tuple[list[str], list[Any]]:
        """Build SQL timestamp range clauses for inclusive unix second bounds."""
        conds: list[str] = []
        params: list[Any] = []
        if date_from and int(date_from) > 0:
            conds.append(f"{ts_expr} >= ?")
            params.append(int(date_from))
        if date_to and int(date_to) > 0:
            conds.append(f"{ts_expr} <= ?")
            params.append(int(date_to))
        return conds, params

    @staticmethod
    def _build_media_type_clause(media_type: str, alias: str = "m") -> tuple[str, list[Any]]:
        """Build SQL filter clause and params for Web UI media type filter tabs."""
        mt = (media_type or "").strip().lower()
        if not mt or mt == "all":
            return "", []
        if mt == "audio":
            return f"{alias}.media_type IN ('voice_note', 'audio')", []
        return f"{alias}.media_type = ?", [mt]

    def list_chats(
        self,
        search: str = "",
        chat_scope: str = "all",
        include_statuses: bool = False,
        limit: int = 80,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return synced chats ordered by latest activity with message counts, phone numbers, and last preview."""
        with self._get_connection() as conn:
            conditions: list[str] = []
            if not include_statuses:
                conditions.append("chat_jid != 'status@broadcast'")
            chat_conds, params = self._build_chat_filter_conditions(
                conn, chat_filter=search, chat_scope=chat_scope, alias="", include_sender=True
            )
            conditions.extend(chat_conds)
            where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""

            sql = f"""
            SELECT chat_jid,
                   MAX(chat_name) AS chat_name,
                   MAX(is_group) AS is_group,
                   COUNT(*) AS msg_count,
                   MAX(timestamp) AS last_ts,
                   MAX(rowid) AS last_rowid
            FROM messages
            {where_sql}
            GROUP BY chat_jid
            ORDER BY last_ts DESC
            LIMIT ? OFFSET ?
            """
            params.extend([limit, offset])

            groups = conn.execute(sql, tuple(params)).fetchall()
            if not groups:
                return []

            jids = [g["chat_jid"] for g in groups]
            rowids = [g["last_rowid"] for g in groups if g["last_rowid"]]
            contact_names: dict[str, str] = {}
            contact_phones: dict[str, str] = {}
            last_msgs: dict[str, sqlite3.Row] = {}

            jid_ph = ",".join("?" for _ in jids)
            for cr in conn.execute(
                f"SELECT jid, name, push_name, phone_number FROM contacts WHERE jid IN ({jid_ph})",
                tuple(jids),
            ).fetchall():
                contact_names[cr["jid"]] = cr["name"] or cr["push_name"] or cr["phone_number"] or ""
                if cr["phone_number"]:
                    contact_phones[cr["jid"]] = str(cr["phone_number"])

            if rowids:
                rid_ph = ",".join("?" for _ in rowids)
                for lr in conn.execute(
                    f"SELECT chat_jid, text, media_type, sender_name, is_from_me FROM messages WHERE rowid IN ({rid_ph})",
                    tuple(rowids),
                ).fetchall():
                    last_msgs[lr["chat_jid"]] = lr

            results = []
            for g in groups:
                cj = g["chat_jid"]
                is_group = bool(g["is_group"])
                cname = g["chat_name"] or contact_names.get(cj) or cj
                if cj == "status@broadcast":
                    cname = "Status Updates"
                phone = "" if (is_group or cj == "status@broadcast") else (contact_phones.get(cj) or self._extract_phone_from_jid(cj))
                lm = last_msgs.get(cj)
                preview = ""
                last_media = "text"
                if lm:
                    preview = re.sub(r"\s+", " ", (lm["text"] or "")).strip()[:120]
                    last_media = lm["media_type"] or "text"
                results.append({
                    "chat_jid": cj,
                    "chat_name": cname,
                    "phone_number": phone,
                    "is_group": is_group,
                    "msg_count": int(g["msg_count"] or 0),
                    "last_ts": int(g["last_ts"] or 0),
                    "last_time": _format_timestamp(g["last_ts"]),
                    "preview": preview,
                    "last_media_type": last_media,
                })
            return results

    def get_chat_messages_paged(
        self, chat_jid: str, limit: int = 80, before_ts: int = 0
    ) -> list[dict[str, Any]]:
        """Fetch chronological messages for a specific chat with optional pagination cursor."""
        if not chat_jid:
            return []
        conditions = ["m.chat_jid = ?"]
        params: list[Any] = [chat_jid]
        if before_ts > 0:
            conditions.append("m.timestamp < ?")
            params.append(before_ts)
        where_sql = " AND ".join(conditions)
        sql = f"""
        SELECT m.id, m.chat_jid, m.chat_name, m.sender_jid, m.sender_name, m.text, m.original_text,
               m.timestamp, m.is_from_me, m.is_group, m.media_type, m.media_filename, m.media_mimetype,
               m.media_size, m.local_path, m.is_deleted, m.deleted_at, m.deleted_by, m.is_edited, m.edited_at,
               c.ocr_text AS ocr_cached
        FROM messages m
        LEFT JOIN media_ocr_cache c ON m.id = c.msg_id
        WHERE {where_sql}
        ORDER BY m.timestamp DESC, m.rowid DESC
        LIMIT ?
        """
        params.append(limit)
        with self._get_connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [self._format_web_message_row(r) for r in reversed(rows)]

    def search_messages_web(
        self,
        query: str = "",
        chat_jid: str = "",
        chat_scope: str = "all",
        media_type: str = "",
        include_statuses: bool = False,
        limit: int = 60,
        offset: int = 0,
        date_from: int = 0,
        date_to: int = 0,
    ) -> list[dict[str, Any]]:
        """Rich search endpoint for the Web UI combining FTS5/LIKE text search with chat/phone, scope, date & media filters."""
        cols_sql = """
        SELECT m.id, m.chat_jid, m.chat_name, m.sender_jid, m.sender_name, m.text, m.original_text,
               m.timestamp, m.is_from_me, m.is_group, m.media_type, m.media_filename, m.media_mimetype,
               m.media_size, m.local_path, m.is_deleted, m.deleted_at, m.deleted_by, m.is_edited, m.edited_at,
               c.ocr_text AS ocr_cached
        """
        q_clean = (query or "").strip()

        with self._get_connection() as conn:
            base_conditions: list[str] = []
            if not include_statuses:
                base_conditions.append("m.chat_jid != 'status@broadcast'")
            chat_conds, base_params = self._build_chat_filter_conditions(
                conn, chat_filter=chat_jid, chat_scope=chat_scope, alias="m", include_sender=True
            )
            base_conditions.extend(chat_conds)

            dt_conds, dt_params = self._build_date_range_conditions(date_from, date_to, ts_expr="m.timestamp")
            base_conditions.extend(dt_conds)
            base_params.extend(dt_params)

            mt_sql, mt_params = self._build_media_type_clause(media_type, alias="m")
            if mt_sql:
                base_conditions.append(mt_sql)
                base_params.extend(mt_params)

            if q_clean:
                raw_tokens = [t.lower() for t in self._tokenize_search_terms(q_clean)]
                ocr_tokens = [t for t in raw_tokens if t not in _SEARCH_STOP_WORDS] or raw_tokens
                ocr_sub_conds = ["c2.ocr_text NOT LIKE '[MEDIA_%'", "c2.ocr_text NOT LIKE '[OCR_SKIPPED%'"]
                ocr_sub_params: list[Any] = []
                if ocr_tokens:
                    for tok in ocr_tokens:
                        ocr_sub_conds.append("LOWER(c2.ocr_text) LIKE ?")
                        ocr_sub_params.append(f"%{tok}%")
                else:
                    ocr_sub_conds.append("LOWER(c2.ocr_text) LIKE ?")
                    ocr_sub_params.append(f"%{q_clean.lower()}%")

                for match_expr in self._build_fts_match_expressions(q_clean):
                    try:
                        union_conds = [
                            f"""m.rowid IN (
                                SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?
                                UNION
                                SELECT m2.rowid FROM media_ocr_cache c2
                                JOIN messages m2 ON c2.msg_id = m2.id
                                WHERE {' AND '.join(ocr_sub_conds)}
                            )""",
                            *base_conditions,
                        ]
                        fts_params: list[Any] = [match_expr, *ocr_sub_params, *base_params, limit, offset]
                        sql = f"""
                        {cols_sql}
                        FROM messages m
                        LEFT JOIN media_ocr_cache c ON m.id = c.msg_id
                        WHERE {' AND '.join(union_conds)}
                        ORDER BY m.timestamp DESC
                        LIMIT ? OFFSET ?
                        """
                        rows = conn.execute(sql, tuple(fts_params)).fetchall()
                        if rows:
                            return [self._format_web_message_row(r) for r in rows]
                    except Exception as e:
                        logger.debug("Web FTS search fell back to LIKE (%s)", e)

                like_conds = list(base_conditions)
                like_params: list[Any] = []
                if ocr_tokens:
                    for tok in ocr_tokens:
                        tp = f"%{tok}%"
                        like_conds.append(
                            "(LOWER(COALESCE(m.text, '')) LIKE ? OR LOWER(COALESCE(m.media_filename, '')) LIKE ? OR LOWER(COALESCE(m.original_text, '')) LIKE ? OR LOWER(COALESCE(c.ocr_text, '')) LIKE ?)"
                        )
                        like_params.extend([tp, tp, tp, tp])
                else:
                    like_q = f"%{q_clean.lower()}%"
                    like_conds.append(
                        "(LOWER(COALESCE(m.text, '')) LIKE ? OR LOWER(COALESCE(m.media_filename, '')) LIKE ? OR LOWER(COALESCE(m.original_text, '')) LIKE ? OR LOWER(COALESCE(c.ocr_text, '')) LIKE ?)"
                    )
                    like_params.extend([like_q, like_q, like_q, like_q])

                sql = f"""
                {cols_sql}
                FROM messages m
                LEFT JOIN media_ocr_cache c ON m.id = c.msg_id
                WHERE {' AND '.join(like_conds)}
                ORDER BY m.timestamp DESC
                LIMIT ? OFFSET ?
                """
                rows = conn.execute(sql, tuple([*base_params, *like_params, limit, offset])).fetchall()
                return [self._format_web_message_row(r) for r in rows]

            where_sql = f"WHERE {' AND '.join(base_conditions)}" if base_conditions else ""
            sql = f"""
            {cols_sql}
            FROM messages m
            LEFT JOIN media_ocr_cache c ON m.id = c.msg_id
            {where_sql}
            ORDER BY m.timestamp DESC
            LIMIT ? OFFSET ?
            """
            rows = conn.execute(sql, tuple([*base_params, limit, offset])).fetchall()
            return [self._format_web_message_row(r) for r in rows]

    def list_stored_media(
        self,
        media_filter: str = "all",
        query: str = "",
        chat: str = "",
        chat_scope: str = "all",
        include_statuses: bool = True,
        limit: int = 60,
        offset: int = 0,
        date_from: int = 0,
        date_to: int = 0,
    ) -> list[dict[str, Any]]:
        """List media files stored locally on disk with chat/phone, scope, type, date, and keyword filters."""
        conditions = ["m.local_path IS NOT NULL", "m.local_path NOT LIKE '[%'"]
        params: list[Any] = []
        mf = (media_filter or "all").strip().lower()
        if mf == "status":
            conditions.append("m.chat_jid = 'status@broadcast'")
        else:
            if not include_statuses:
                conditions.append("m.chat_jid != 'status@broadcast'")
            mt_sql, mt_params = self._build_media_type_clause(mf, alias="m")
            if mt_sql:
                conditions.append(mt_sql)
                params.extend(mt_params)

        dt_conds, dt_params = self._build_date_range_conditions(date_from, date_to, ts_expr="m.timestamp")
        conditions.extend(dt_conds)
        params.extend(dt_params)

        if query and query.strip():
            like_q = f"%{query.strip().lower()}%"
            conditions.append(
                "(LOWER(COALESCE(m.media_filename, '')) LIKE ? OR LOWER(COALESCE(m.text, '')) LIKE ? OR LOWER(COALESCE(m.chat_name, '')) LIKE ? OR LOWER(COALESCE(c.ocr_text, '')) LIKE ?)"
            )
            params.extend([like_q, like_q, like_q, like_q])

        with self._get_connection() as conn:
            chat_conds, chat_params = self._build_chat_filter_conditions(
                conn, chat_filter=chat, chat_scope=chat_scope, alias="m", include_sender=True
            )
            conditions.extend(chat_conds)
            params.extend(chat_params)

            sql = f"""
            SELECT m.id, m.chat_jid, m.chat_name, m.sender_jid, m.sender_name, m.text, m.original_text,
                   m.timestamp, m.is_from_me, m.is_group, m.media_type, m.media_filename, m.media_mimetype,
                   m.media_size, m.local_path, m.is_deleted, m.deleted_at, m.deleted_by, m.is_edited, m.edited_at,
                   c.ocr_text AS ocr_cached
            FROM messages m
            LEFT JOIN media_ocr_cache c ON m.id = c.msg_id
            WHERE {' AND '.join(conditions)}
            ORDER BY m.timestamp DESC
            LIMIT ? OFFSET ?
            """
            params.extend([limit, offset])
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [self._format_web_message_row(r) for r in rows]

    def list_chat_suggestions(self, query: str = "", limit: int = 120) -> list[dict[str, Any]]:
        """Return deduplicated chat names, group names, and phone numbers for Web UI filter autocomplete."""
        chats = self.list_chats(search=query, include_statuses=False, limit=limit)
        seen: set[str] = set()
        suggestions: list[dict[str, Any]] = []
        for c in chats:
            key = c["chat_jid"]
            if key in seen:
                continue
            seen.add(key)
            suggestions.append({
                "chat_jid": c["chat_jid"],
                "chat_name": c["chat_name"],
                "phone_number": c.get("phone_number", ""),
                "is_group": c["is_group"],
            })

        if len(suggestions) < limit and query.strip():
            for ct in self.find_contacts(query.strip(), limit=limit - len(suggestions)):
                key = ct["jid"]
                if key in seen:
                    continue
                seen.add(key)
                suggestions.append({
                    "chat_jid": ct["jid"],
                    "chat_name": ct["name"],
                    "phone_number": ct.get("phone_number") or self._extract_phone_from_jid(ct["jid"]),
                    "is_group": ct["is_group"],
                })
        return suggestions

    def get_message_media_info(self, msg_id: str) -> dict[str, Any] | None:
        """Retrieve local file path and metadata for serving or forwarding a stored attachment by msg_id."""
        if not msg_id:
            return None
        with self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT m.id, m.chat_jid, m.chat_name, m.sender_name, m.is_from_me, m.timestamp,
                       m.local_path, m.media_filename, m.media_mimetype, m.media_type,
                       m.text, c.ocr_text AS ocr_cached
                FROM messages m
                LEFT JOIN media_ocr_cache c ON m.id = c.msg_id
                WHERE m.id = ?
                """,
                (msg_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_message_context_by_id(self, msg_id: str) -> dict[str, Any] | None:
        """Retrieve formatted message metadata + OCR cache snippet by msg_id for Web UI Ask AI anchoring."""
        if not msg_id:
            return None
        with self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT m.id, m.chat_jid, m.chat_name, m.sender_jid, m.sender_name, m.text, m.original_text,
                       m.timestamp, m.is_from_me, m.is_group, m.media_type, m.media_filename, m.media_mimetype,
                       m.media_size, m.local_path, m.is_deleted, m.deleted_at, m.deleted_by, m.is_edited, m.edited_at,
                       c.ocr_text AS ocr_cached
                FROM messages m
                LEFT JOIN media_ocr_cache c ON m.id = c.msg_id
                WHERE m.id = ?
                """,
                (msg_id,),
            ).fetchone()
            return self._format_web_message_row(row) if row else None

    def append_web_ai_turn(
        self,
        role: str,
        content: str,
        chat_focus: str = "",
        context_msg_id: str = "",
        context_label: str = "",
        latency_ms: int = 0,
        is_error: bool = False,
        created_at: int | None = None,
    ) -> int:
        """Persist one user or assistant message in web_ai_history and return its turn_id."""
        ts = int(created_at or time.time())
        with self._get_connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO web_ai_history
                (role, content, chat_focus, context_msg_id, context_label, latency_ms, is_error, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (role or "user")[:16],
                    content or "",
                    (chat_focus or "")[:200],
                    (context_msg_id or "")[:120],
                    (context_label or "")[:240],
                    int(latency_ms or 0),
                    1 if is_error else 0,
                    ts,
                ),
            )
            conn.commit()
            return int(cur.lastrowid or 0)

    def get_web_ai_history(self, limit: int = 80) -> list[dict[str, Any]]:
        """Load persisted Web UI AI conversation turns in chronological order."""
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT turn_id, role, content, chat_focus, context_msg_id, context_label,
                       latency_ms, is_error, created_at
                FROM web_ai_history
                ORDER BY turn_id DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
            out: list[dict[str, Any]] = []
            for r in reversed(rows):
                out.append({
                    "id": int(r["turn_id"]),
                    "role": r["role"],
                    "text": r["content"] or "",
                    "chat": r["chat_focus"] or "",
                    "context_msg_id": r["context_msg_id"] or "",
                    "context_label": r["context_label"] or "",
                    "latency_ms": int(r["latency_ms"] or 0),
                    "is_error": bool(r["is_error"]),
                    "ts": datetime.fromtimestamp(int(r["created_at"] or 0)).strftime("%I:%M %p"),
                })
            return out

    def clear_web_ai_history(self) -> int:
        """Delete all persisted Web UI AI conversation turns and return count removed."""
        with self._get_connection() as conn:
            cur = conn.execute("DELETE FROM web_ai_history")
            conn.commit()
            return int(cur.rowcount or 0)

    # =========================================================================
    # Role-Based Messaging, Scheduling, and Contact/Group Roles
    # =========================================================================

    def get_roles(self) -> list[dict[str, Any]]:
        """Return all defined broadcast roles with their assigned recipient counts."""
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT r.name, r.color, r.created_at, COUNT(DISTINCT er.jid) as count
                FROM roles r
                LEFT JOIN entity_roles er ON r.name = er.role
                GROUP BY r.name
                ORDER BY r.name COLLATE NOCASE ASC
                """
            ).fetchall()
            return [
                {
                    "name": r["name"],
                    "color": r["color"] or "",
                    "created_at": int(r["created_at"] or 0),
                    "count": int(r["count"] or 0),
                    "assigned_count": int(r["count"] or 0),
                }
                for r in rows
            ]

    def add_role(self, name: str, color: str = "") -> bool:
        """Create a new role tag."""
        role_name = (name or "").strip()
        if not role_name:
            return False
        with self._get_connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO roles (name, color, created_at) VALUES (?, ?, ?)",
                (role_name, (color or "").strip(), int(time.time())),
            )
            conn.commit()
            return True

    def delete_role(self, name: str) -> bool:
        """Delete a role and unassign it from all contacts and groups."""
        role_name = (name or "").strip()
        if not role_name:
            return False
        with self._get_connection() as conn:
            conn.execute("DELETE FROM roles WHERE LOWER(name) = LOWER(?)", (role_name,))
            conn.execute("DELETE FROM entity_roles WHERE LOWER(role) = LOWER(?)", (role_name,))
            conn.commit()
            return True

    def get_entity_roles(self, jid: str) -> list[str]:
        """Get list of roles assigned to a contact or group."""
        if not jid:
            return []
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT role FROM entity_roles WHERE jid = ? ORDER BY role COLLATE NOCASE ASC",
                (jid.strip(),),
            ).fetchall()
            return [r["role"] for r in rows]

    def set_entity_roles(self, jid: str, roles: list[str]) -> bool:
        """Replace all assigned roles for a contact or group."""
        clean_jid = (jid or "").strip()
        if not clean_jid:
            return False
        now_ts = int(time.time())
        unique_roles = {r.strip() for r in roles if r and r.strip()}
        with self._get_connection() as conn:
            conn.execute("DELETE FROM entity_roles WHERE jid = ?", (clean_jid,))
            for r in unique_roles:
                # Ensure role exists in roles table
                conn.execute(
                    "INSERT OR IGNORE INTO roles (name, color, created_at) VALUES (?, '', ?)",
                    (r, now_ts),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO entity_roles (jid, role, created_at) VALUES (?, ?, ?)",
                    (clean_jid, r, now_ts),
                )
            conn.commit()
            return True

    def save_contact_with_roles(
        self,
        jid: str,
        name: str = "",
        phone_number: str = "",
        roles: list[str] | None = None,
        notes: str = "",
        is_group: bool = False,
    ) -> bool:
        """Create or update a contact or group record with assigned roles and notes."""
        clean_jid = (jid or "").strip()
        if not clean_jid:
            return False
        now_ts = int(time.time())
        with self._get_connection() as conn:
            existing = conn.execute("SELECT jid, name, phone_number, is_group, notes FROM contacts WHERE jid = ?", (clean_jid,)).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE contacts
                    SET name = CASE WHEN ? != '' THEN ? ELSE name END,
                        phone_number = CASE WHEN ? != '' THEN ? ELSE phone_number END,
                        notes = ?,
                        updated_at = ?
                    WHERE jid = ?
                    """,
                    (name.strip(), name.strip(), phone_number.strip(), phone_number.strip(), (notes or "").strip(), now_ts, clean_jid),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO contacts (jid, name, push_name, phone_number, is_group, notes, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (clean_jid, name.strip() or None, None, phone_number.strip() or None, 1 if is_group else 0, (notes or "").strip(), now_ts),
                )
            conn.commit()

        if roles is not None:
            self.set_entity_roles(clean_jid, roles)
        return True

    def delete_custom_contact(self, jid: str) -> bool:
        """Remove a custom contact and its role mappings."""
        clean_jid = (jid or "").strip()
        if not clean_jid:
            return False
        with self._get_connection() as conn:
            conn.execute("DELETE FROM contacts WHERE jid = ?", (clean_jid,))
            conn.execute("DELETE FROM entity_roles WHERE jid = ?", (clean_jid,))
            conn.commit()
            return True

    def list_entities_with_roles(
        self,
        is_group: bool | None = None,
        search: str = "",
        role: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List contacts or groups with assigned roles, notes, and search/filter support."""
        where = ["c.jid != 'status@broadcast'"]
        params: list[Any] = []

        if is_group is not None:
            where.append("c.is_group = ?")
            params.append(1 if is_group else 0)
            if not is_group:
                # Only show saved contacts with a real name (exclude @lid, companion IDs, raw phone numbers)
                where.append("c.jid LIKE '%@s.whatsapp.net'")
                where.append("c.jid NOT LIKE '%:%'")
                where.append("""(
                    c.jid IN (SELECT jid FROM entity_roles)
                    OR (
                        c.name IS NOT NULL
                        AND TRIM(c.name) != ''
                        AND c.name NOT GLOB '+[0-9]*'
                        AND (c.phone_number IS NULL OR TRIM(c.name) != TRIM(c.phone_number))
                        AND TRIM(c.name) != TRIM(c.jid)
                    )
                )""")

        clean_search = (search or "").strip().lower()
        if clean_search:
            pattern = f"%{clean_search}%"
            where.append(
                "(LOWER(COALESCE(c.name, '')) LIKE ? OR LOWER(COALESCE(c.push_name, '')) LIKE ? "
                "OR LOWER(COALESCE(c.phone_number, '')) LIKE ? OR LOWER(c.jid) LIKE ? "
                "OR LOWER(COALESCE(c.notes, '')) LIKE ?)"
            )
            params.extend([pattern, pattern, pattern, pattern, pattern])

        clean_role = (role or "").strip()
        if clean_role:
            where.append("c.jid IN (SELECT jid FROM entity_roles WHERE LOWER(role) = LOWER(?))")
            params.append(clean_role)

        where_sql = " WHERE " + " AND ".join(where)

        with self._get_connection() as conn:
            count_row = conn.execute(f"SELECT COUNT(*) FROM contacts c{where_sql}", tuple(params)).fetchone()
            total = count_row[0] if count_row else 0

            query_sql = f"""
                SELECT c.jid, c.name, c.push_name, c.phone_number, c.is_group, c.notes,
                       (SELECT GROUP_CONCAT(er.role, ',') FROM (SELECT role FROM entity_roles WHERE jid = c.jid ORDER BY role COLLATE NOCASE) er) as roles
                FROM contacts c
                {where_sql}
                ORDER BY COALESCE(NULLIF(c.name, ''), NULLIF(c.push_name, ''), c.phone_number, c.jid) COLLATE NOCASE ASC
                LIMIT ? OFFSET ?
            """
            rows = conn.execute(query_sql, tuple(params + [max(1, int(limit)), max(0, int(offset))])).fetchall()

            items = []
            for r in rows:
                raw_roles = (r["roles"] or "").strip()
                roles_list = [x.strip() for x in raw_roles.split(",") if x.strip()]
                display_name = r["name"] or r["push_name"] or r["phone_number"] or r["jid"]
                items.append({
                    "jid": r["jid"],
                    "name": display_name,
                    "phone": r["phone_number"] or "",
                    "push_name": r["push_name"] or "",
                    "is_group": bool(r["is_group"]),
                    "notes": r["notes"] or "",
                    "roles": roles_list,
                })

            return {
                "items": items,
                "total": total,
                "limit": limit,
                "offset": offset,
            }

    def lookup_recipients_by_roles(
        self,
        roles: list[str],
        target_audience: str = "both",
    ) -> list[dict[str, Any]]:
        """Resolve all distinct contacts/groups matching any of the specified roles and target audience.
        If roles list is empty, targets all recipients matching target_audience without role restriction.
        """
        clean_roles = [r.strip() for r in roles if r and r.strip()]
        audience = (target_audience or "both").lower()

        where_clauses = ["c.jid != 'status@broadcast'"]
        params: list[Any] = []

        if audience == "contacts":
            where_clauses.append("c.is_group = 0")
            where_clauses.append("c.jid LIKE '%@s.whatsapp.net'")
            where_clauses.append("c.jid NOT LIKE '%:%'")
            if not clean_roles:
                where_clauses.append("""(
                    c.jid IN (SELECT jid FROM entity_roles)
                    OR (
                        c.name IS NOT NULL
                        AND TRIM(c.name) != ''
                        AND c.name NOT GLOB '+[0-9]*'
                        AND (c.phone_number IS NULL OR TRIM(c.name) != TRIM(c.phone_number))
                        AND TRIM(c.name) != TRIM(c.jid)
                    )
                )""")
        elif audience == "groups":
            where_clauses.append("c.is_group = 1")

        if clean_roles:
            placeholders = ",".join("?" for _ in clean_roles)
            where_clauses.append(f"c.jid IN (SELECT jid FROM entity_roles WHERE role IN ({placeholders}))")
            params.extend(clean_roles)

        where_sql = " WHERE " + " AND ".join(where_clauses)

        query = f"""
            SELECT c.jid, c.name, c.push_name, c.phone_number, c.is_group,
                   (SELECT GROUP_CONCAT(DISTINCT er.role) FROM entity_roles er WHERE er.jid = c.jid) as roles
            FROM contacts c
            {where_sql}
            ORDER BY COALESCE(NULLIF(c.name, ''), NULLIF(c.push_name, ''), c.phone_number, c.jid) COLLATE NOCASE ASC
        """

        with self._get_connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            seen_clean_jids = set()
            out = []
            for r in rows:
                raw_jid = r["jid"] or ""
                # Strip companion device part if present (e.g. 919850567766:34@s.whatsapp.net -> 919850567766@s.whatsapp.net)
                if ":" in raw_jid and "@" in raw_jid:
                    u, srv = raw_jid.split("@", 1)
                    raw_jid = f"{u.split(':')[0]}@{srv}"
                if raw_jid in seen_clean_jids:
                    continue
                seen_clean_jids.add(raw_jid)
                roles_arr = [x.strip() for x in (r["roles"] or "").split(",") if x.strip()]
                name = r["name"] or r["push_name"] or r["phone_number"] or raw_jid
                phone = r["phone_number"] or (raw_jid.split("@")[0] if not r["is_group"] else "")
                out.append({
                    "jid": raw_jid,
                    "name": name,
                    "phone": phone,
                    "is_group": bool(r["is_group"]),
                    "roles": roles_arr,
                })
            return out

    def lookup_recipients_autocomplete(self, query: str = "", limit: int = 25) -> list[dict[str, Any]]:
        """Search contacts and groups for the specific recipients picker dropdown."""
        q = (query or "").strip().lower()
        params: list[Any] = []
        where = "WHERE c.jid != 'status@broadcast'"
        if q:
            pattern = f"%{q}%"
            where += " AND (LOWER(COALESCE(c.name, '')) LIKE ? OR LOWER(COALESCE(c.push_name, '')) LIKE ? OR LOWER(COALESCE(c.phone_number, '')) LIKE ? OR LOWER(c.jid) LIKE ?)"
            params.extend([pattern, pattern, pattern, pattern])

        sql = f"""
            SELECT c.jid, c.name, c.push_name, c.phone_number, c.is_group,
                   (SELECT GROUP_CONCAT(er.role, ',') FROM entity_roles er WHERE er.jid = c.jid) as roles
            FROM contacts c
            {where}
            ORDER BY COALESCE(NULLIF(c.name, ''), NULLIF(c.push_name, ''), c.phone_number, c.jid) COLLATE NOCASE ASC
            LIMIT ?
        """
        params.append(max(1, int(limit)))

        with self._get_connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
            out = []
            for r in rows:
                roles_arr = [x.strip() for x in (r["roles"] or "").split(",") if x.strip()]
                name = r["name"] or r["push_name"] or r["phone_number"] or r["jid"]
                out.append({
                    "jid": r["jid"],
                    "name": name,
                    "phone": r["phone_number"] or "",
                    "is_group": bool(r["is_group"]),
                    "type": "group" if r["is_group"] else "contact",
                    "roles": roles_arr,
                })
            return out

    def create_scheduled_campaign(
        self,
        message: str,
        scheduled_time: str,
        scheduled_ts: int,
        target_audience: str = "both",
        roles: list[str] | None = None,
        specific_recipients: list[dict[str, Any]] | None = None,
        attachment_path: str = "",
        attachment_filename: str = "",
        attachment_mimetype: str = "",
    ) -> str:
        """Register a future scheduled campaign in SQLite."""
        camp_id = f"sched_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"
        now_ts = int(time.time())
        roles_json = json.dumps(roles or [])
        recipients_json = json.dumps(specific_recipients or [])

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO scheduled_campaigns (
                    id, message, attachment_path, attachment_filename, attachment_mimetype,
                    target_audience, roles, specific_recipients, scheduled_time, scheduled_ts,
                    status, created_at, sent_at, total_targets, sent_count, failed_count, log_details
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, 0, 0, 0, 0, '[]')
                """,
                (
                    camp_id,
                    (message or "").strip(),
                    (attachment_path or "").strip(),
                    (attachment_filename or "").strip(),
                    (attachment_mimetype or "").strip(),
                    (target_audience or "both").strip(),
                    roles_json,
                    recipients_json,
                    (scheduled_time or "").strip(),
                    int(scheduled_ts),
                    now_ts,
                ),
            )
            conn.commit()
            return camp_id

    def get_scheduled_campaigns(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """List scheduled campaigns sorted by scheduled time."""
        params: list[Any] = []
        where = ""
        if status:
            where = "WHERE status = ?"
            params.append(status.strip())

        sql = f"""
            SELECT id, message, attachment_path, attachment_filename, attachment_mimetype,
                   target_audience, roles, specific_recipients, scheduled_time, scheduled_ts,
                   status, created_at, sent_at, total_targets, sent_count, failed_count, log_details
            FROM scheduled_campaigns
            {where}
            ORDER BY CASE WHEN status = 'pending' THEN 0 ELSE 1 END, scheduled_ts ASC
            LIMIT ?
        """
        params.append(max(1, int(limit)))

        with self._get_connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
            out = []
            for r in rows:
                try:
                    roles_list = json.loads(r["roles"] or "[]")
                except Exception:
                    roles_list = []
                try:
                    recipients_list = json.loads(r["specific_recipients"] or "[]")
                except Exception:
                    recipients_list = []
                try:
                    logs_list = json.loads(r["log_details"] or "[]")
                except Exception:
                    logs_list = []

                out.append({
                    "id": r["id"],
                    "message": r["message"] or "",
                    "attachment_path": r["attachment_path"] or "",
                    "attachment_name": r["attachment_filename"] or "",
                    "attachment_mimetype": r["attachment_mimetype"] or "",
                    "target_audience": r["target_audience"] or "both",
                    "roles": roles_list,
                    "specific_recipients": recipients_list,
                    "scheduled_time": r["scheduled_time"] or "",
                    "scheduled_ts": int(r["scheduled_ts"] or 0),
                    "status": r["status"] or "pending",
                    "created_at": int(r["created_at"] or 0),
                    "sent_at": int(r["sent_at"] or 0),
                    "total_targets": int(r["total_targets"] or 0),
                    "sent_count": int(r["sent_count"] or 0),
                    "failed_count": int(r["failed_count"] or 0),
                    "logs": logs_list,
                })
            return out

    def get_scheduled_campaign(self, camp_id: str) -> dict[str, Any] | None:
        """Get single scheduled campaign by ID."""
        if not camp_id:
            return None
        with self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT id, message, attachment_path, attachment_filename, attachment_mimetype,
                       target_audience, roles, specific_recipients, scheduled_time, scheduled_ts,
                       status, created_at, sent_at, total_targets, sent_count, failed_count, log_details
                FROM scheduled_campaigns
                WHERE id = ?
                """,
                (camp_id.strip(),),
            ).fetchone()
            if not row:
                return None
            return {
                "id": row["id"],
                "message": row["message"] or "",
                "attachment_path": row["attachment_path"] or "",
                "attachment_name": row["attachment_filename"] or "",
                "attachment_mimetype": row["attachment_mimetype"] or "",
                "target_audience": row["target_audience"] or "both",
                "roles": json.loads(row["roles"] or "[]"),
                "specific_recipients": json.loads(row["specific_recipients"] or "[]"),
                "scheduled_time": row["scheduled_time"] or "",
                "scheduled_ts": int(row["scheduled_ts"] or 0),
                "status": row["status"] or "pending",
                "created_at": int(row["created_at"] or 0),
                "sent_at": int(row["sent_at"] or 0),
                "total_targets": int(row["total_targets"] or 0),
                "sent_count": int(row["sent_count"] or 0),
                "failed_count": int(row["failed_count"] or 0),
                "logs": json.loads(row["log_details"] or "[]"),
            }

    def get_due_scheduled_campaigns(self, now_ts: int | None = None) -> list[dict[str, Any]]:
        """Fetch pending campaigns whose scheduled timestamp has arrived."""
        ts = int(time.time()) if now_ts is None else int(now_ts)
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, message, attachment_path, attachment_filename, attachment_mimetype,
                       target_audience, roles, specific_recipients, scheduled_time, scheduled_ts,
                       status, created_at, sent_at, total_targets, sent_count, failed_count, log_details
                FROM scheduled_campaigns
                WHERE status = 'pending' AND scheduled_ts <= ?
                ORDER BY scheduled_ts ASC
                """,
                (ts,),
            ).fetchall()
            out = []
            for row in rows:
                out.append({
                    "id": row["id"],
                    "message": row["message"] or "",
                    "attachment_path": row["attachment_path"] or "",
                    "attachment_name": row["attachment_filename"] or "",
                    "attachment_mimetype": row["attachment_mimetype"] or "",
                    "target_audience": row["target_audience"] or "both",
                    "roles": json.loads(row["roles"] or "[]"),
                    "specific_recipients": json.loads(row["specific_recipients"] or "[]"),
                    "scheduled_time": row["scheduled_time"] or "",
                    "scheduled_ts": int(row["scheduled_ts"] or 0),
                    "status": row["status"] or "pending",
                })
            return out

    def update_scheduled_campaign_status(
        self,
        camp_id: str,
        status: str,
        sent_count: int = 0,
        failed_count: int = 0,
        total_targets: int = 0,
        logs: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Update campaign execution state and delivery counts."""
        now_ts = int(time.time())
        logs_json = json.dumps(logs or [])
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE scheduled_campaigns
                SET status = ?,
                    sent_count = ?,
                    failed_count = ?,
                    total_targets = ?,
                    log_details = ?,
                    sent_at = CASE WHEN ? IN ('sent', 'failed') THEN ? ELSE sent_at END
                WHERE id = ?
                """,
                (
                    status,
                    int(sent_count),
                    int(failed_count),
                    int(total_targets),
                    logs_json,
                    status,
                    now_ts,
                    camp_id,
                ),
            )
            conn.commit()
            return True

    def cancel_scheduled_campaign(self, camp_id: str) -> bool:
        """Cancel a pending scheduled campaign."""
        with self._get_connection() as conn:
            cur = conn.execute(
                "UPDATE scheduled_campaigns SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
                (camp_id,),
            )
            conn.commit()
            return bool(cur.rowcount > 0)

    def delete_scheduled_campaign(self, camp_id: str) -> bool:
        """Delete a campaign record from scheduled queue."""
        with self._get_connection() as conn:
            cur = conn.execute("DELETE FROM scheduled_campaigns WHERE id = ?", (camp_id,))
            conn.commit()
            return bool(cur.rowcount > 0)

    def record_campaign_history(
        self,
        message: str,
        target_audience: str,
        roles: list[str],
        recipients_count: int,
        sent_count: int,
        failed_count: int,
        duration_sec: float,
        started_at: int,
        finished_at: int,
        logs: list[dict[str, Any]],
        attachment_path: str = "",
        attachment_filename: str = "",
        campaign_type: str = "instant",
        camp_id: str | None = None,
    ) -> str:
        """Log a completed campaign in the audit history."""
        cid = camp_id or f"camp_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO campaign_history (
                    id, campaign_type, message, attachment_path, attachment_filename,
                    roles, target_audience, recipients_count, sent_count, failed_count,
                    duration_sec, started_at, finished_at, logs
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cid,
                    campaign_type,
                    (message or "").strip(),
                    attachment_path or "",
                    attachment_filename or "",
                    json.dumps(roles or []),
                    target_audience or "both",
                    int(recipients_count),
                    int(sent_count),
                    int(failed_count),
                    round(float(duration_sec), 2),
                    int(started_at),
                    int(finished_at),
                    json.dumps(logs or []),
                ),
            )
            conn.commit()
            return cid

    def get_campaign_history(self, query: str = "", limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """Fetch past campaign execution records."""
        q = (query or "").strip().lower()
        where = ""
        params: list[Any] = []
        if q:
            pattern = f"%{q}%"
            where = "WHERE (LOWER(id) LIKE ? OR LOWER(message) LIKE ? OR LOWER(roles) LIKE ? OR LOWER(logs) LIKE ?)"
            params.extend([pattern, pattern, pattern, pattern])

        with self._get_connection() as conn:
            count_row = conn.execute(f"SELECT COUNT(*) FROM campaign_history {where}", tuple(params)).fetchone()
            total = count_row[0] if count_row else 0

            sql = f"""
                SELECT id, campaign_type, message, attachment_path, attachment_filename,
                       roles, target_audience, recipients_count, sent_count, failed_count,
                       duration_sec, started_at, finished_at, logs
                FROM campaign_history
                {where}
                ORDER BY started_at DESC
                LIMIT ? OFFSET ?
            """
            rows = conn.execute(sql, tuple(params + [max(1, int(limit)), max(0, int(offset))])).fetchall()
            items = []
            for r in rows:
                try:
                    roles_list = json.loads(r["roles"] or "[]")
                except Exception:
                    roles_list = []
                try:
                    logs_list = json.loads(r["logs"] or "[]")
                except Exception:
                    logs_list = []

                items.append({
                    "id": r["id"],
                    "campaign_type": r["campaign_type"] or "instant",
                    "message": r["message"] or "",
                    "attachment_name": r["attachment_filename"] or "",
                    "attachment_path": r["attachment_path"] or "",
                    "roles": roles_list,
                    "target_audience": r["target_audience"] or "both",
                    "recipients_count": int(r["recipients_count"] or 0),
                    "sent_count": int(r["sent_count"] or 0),
                    "failed_count": int(r["failed_count"] or 0),
                    "duration_sec": float(r["duration_sec"] or 0),
                    "started_at": int(r["started_at"] or 0),
                    "finished_at": int(r["finished_at"] or 0),
                    "date_str": datetime.fromtimestamp(int(r["started_at"] or 0)).strftime("%Y-%m-%d %H:%M:%S"),
                    "logs": logs_list,
                })

            return {"items": items, "total": total, "limit": limit, "offset": offset}

    def export_workbook_data(self) -> dict[str, list[dict[str, Any]]]:
        """Collect all Contacts, Groups, and Roles data for Excel workbook generation."""
        contacts = self.list_entities_with_roles(is_group=False, limit=50000)["items"]
        groups = self.list_entities_with_roles(is_group=True, limit=50000)["items"]
        roles = self.get_roles()
        return {
            "contacts": contacts,
            "groups": groups,
            "roles": roles,
        }

    def import_workbook_data(
        self,
        contacts_or_dict: Any,
        groups_rows: list[dict[str, Any]] | None = None,
        roles_names: list[str] | None = None,
        import_action: str = "merge",
        mode: str | None = None,
    ) -> dict[str, int]:
        """Import Contacts, Groups, and Roles from parsed workbook data."""
        if isinstance(contacts_or_dict, dict):
            data_dict = contacts_or_dict
            contacts_rows = data_dict.get("contacts", [])
            groups_rows = data_dict.get("groups", [])
            raw_roles = data_dict.get("roles", [])
            r_names = []
            for r in raw_roles:
                if isinstance(r, dict):
                    if r.get("name"):
                        self.add_role(r["name"], r.get("color", "#00c896"))
                    r_names.append(r.get("name") or "")
                elif r:
                    r_names.append(str(r))
            roles_names = [r for r in r_names if r]
            action = (mode or import_action or "merge").lower()
        else:
            contacts_rows = contacts_or_dict or []
            groups_rows = groups_rows or []
            roles_names = roles_names or []
            action = (mode or import_action or "merge").lower()

        stats = {"roles": 0, "contacts": 0, "groups": 0, "roles_added": 0, "contacts_updated": 0, "groups_updated": 0}

        with self._get_connection() as conn:
            if action == "replace":
                conn.execute("DELETE FROM entity_roles")
                logger.info("Import action 'replace': cleared existing entity_roles")

        # 1. Add roles
        for r_name in roles_names:
            if self.add_role(r_name):
                stats["roles_added"] += 1
                stats["roles"] += 1

        # 2. Upsert contacts
        for c in contacts_rows:
            phone = str(c.get("phone") or c.get("phone_or_jid") or c.get("phone_number") or "").strip()
            name = str(c.get("name") or "").strip()
            notes = str(c.get("notes") or "").strip()
            roles_val = c.get("roles") or []
            if isinstance(roles_val, str):
                roles_val = [x.strip() for x in roles_val.split(",") if x.strip()]

            # Determine JID
            jid = str(c.get("jid") or "").strip()
            if not jid and "@" in phone:
                jid = phone
            elif not jid and phone:
                clean_phone = re.sub(r"\D", "", phone)
                if clean_phone:
                    jid = f"{clean_phone}@s.whatsapp.net"

            if jid:
                self.save_contact_with_roles(
                    jid=jid,
                    name=name,
                    phone_number=phone if "@" not in phone else "",
                    roles=roles_val,
                    notes=notes,
                    is_group=False,
                )
                stats["contacts_updated"] += 1
                stats["contacts"] += 1

        # 3. Upsert groups
        for g in groups_rows:
            jid = str(g.get("jid") or "").strip()
            name = str(g.get("name") or "").strip()
            notes = str(g.get("notes") or "").strip()
            roles_val = g.get("roles") or []
            if isinstance(roles_val, str):
                roles_val = [x.strip() for x in roles_val.split(",") if x.strip()]

            if jid and (jid.endswith("@g.us") or "@" not in jid):
                if not jid.endswith("@g.us"):
                    jid = f"{jid}@g.us"
                self.save_contact_with_roles(
                    jid=jid,
                    name=name,
                    phone_number="",
                    roles=roles_val,
                    notes=notes,
                    is_group=True,
                )
                stats["groups_updated"] += 1
                stats["groups"] += 1

        return stats
