import sqlite3
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


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
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=60.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # Enable WAL mode for concurrent read/write and high performance
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=60000;")
        return conn

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
            ]:
                if col_name not in existing_cols:
                    conn.execute(f"ALTER TABLE messages ADD COLUMN {col_name} {col_type};")

            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages(chat_jid, timestamp DESC);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp DESC);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_jid);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_media_type ON messages(media_type);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_media_filename ON messages(media_filename);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_local_path ON messages(local_path);")

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
                CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
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

            conn.commit()
            logger.info("Database initialized successfully at %s", self.db_path)

        downloads_dir = Path(self.db_path).parent / "downloads"
        if downloads_dir.exists():
            self.reconcile_stored_downloads(downloads_dir)

    def reconcile_stored_downloads(self, downloads_dir: Path) -> int:
        """Scan downloads_dir and record local_path for any previously downloaded files."""
        if not downloads_dir.exists():
            return 0
        updated = 0
        with self._get_connection() as conn:
            for p in downloads_dir.glob("*"):
                if p.is_file() and "_" in p.name:
                    msg_id = p.name.split("_", 1)[0]
                    if len(msg_id) >= 16:
                        cur = conn.execute(
                            "UPDATE messages SET local_path = ? WHERE id = ? AND (local_path IS NULL OR local_path = '')",
                            (str(p), msg_id),
                        )
                        updated += cur.rowcount
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

    def get_uncached_media_candidates(
        self,
        lookback_hours: int = 48,
        limit: int = 50,
        include_statuses: bool = False,
        self_jid: str = "",
    ) -> list[dict[str, Any]]:
        """Fetch un-cached media messages within lookback_hours, strictly prioritizing:
        1. Self-chat messages / notes to self
        2. Direct 1-on-1 Messages (DMs with contacts or unsaved numbers)
        3. Group chats & Announcements
        4. WhatsApp Status Stories (only if include_statuses is True, processed last)
        """
        cutoff_ts = int(time.time()) - (lookback_hours * 3600)
        status_filter = "" if include_statuses else "AND m.chat_jid != 'status@broadcast'"
        sql = f"""
        SELECT m.id, m.chat_jid, m.sender_jid, m.timestamp, m.text, 
               m.media_type, m.media_filename, m.media_mimetype,
               m.chat_name, m.sender_name, m.is_group
        FROM messages m
        LEFT JOIN media_ocr_cache c ON m.id = c.msg_id
        WHERE c.msg_id IS NULL
          AND (m.media_type LIKE '%image%' OR m.media_mimetype LIKE 'image/%')
          AND m.timestamp >= ?
          AND m.raw_message IS NOT NULL
          {status_filter}
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
            rows = conn.execute(sql, (cutoff_ts, self_jid, self_jid, limit)).fetchall()
            return [dict(r) for r in rows]

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
        4. WhatsApp Status Stories (processed after chats, if include_statuses is True)
        """
        cutoff_ts = int(time.time()) - (lookback_days * 86400)
        status_filter = "" if include_statuses else "AND m.chat_jid != 'status@broadcast'"

        # If media_types is "all" or empty, match all non-text media messages
        is_all_types = not media_types or "all" in media_types or "any" in media_types
        type_filter = "AND m.media_type != 'text'" if is_all_types else f"AND m.media_type IN ({','.join('?' for _ in media_types)})"

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
        params = []
        if not is_all_types:
            params.extend(media_types)
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
            text = CASE WHEN excluded.text != '' THEN excluded.text ELSE messages.text END,
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
            text = CASE WHEN excluded.text != '' THEN excluded.text ELSE messages.text END,
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

    def update_message_local_path(self, msg_id: str, local_path: str) -> None:
        """Update the stored local filesystem path for a downloaded media message."""
        with self._get_connection() as conn:
            conn.execute("UPDATE messages SET local_path = ? WHERE id = ?", (local_path, msg_id))
            conn.commit()

    # -------------------------------------------------------------
    # Gemini Tool Helper Methods
    # -------------------------------------------------------------

    def find_contacts(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search contacts and group chats by name, push name, phone number, or JID.
        Supports both full substring and multi-word out-of-order matching (e.g. 'Patil Ayush' matches 'Ayush Patil').
        """
        cleaned_query = f"%{query.strip().lower()}%"
        words = [w for w in query.strip().lower().split() if len(w) > 1]

        with self._get_connection() as conn:
            # 1. Exact / Full substring match first
            sql_exact = """
            SELECT jid, name, push_name, phone_number, is_group, updated_at
            FROM contacts
            WHERE (LOWER(name) LIKE ?
               OR LOWER(push_name) LIKE ?
               OR phone_number LIKE ?
               OR jid LIKE ?)
              AND jid != 'status@broadcast'
            ORDER BY updated_at DESC
            LIMIT ?
            """
            rows = list(conn.execute(sql_exact, (cleaned_query, cleaned_query, cleaned_query, cleaned_query, limit)).fetchall())

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

    def get_chat_history(self, chat_jid_or_name: str, limit: int = 50) -> list[dict[str, Any]]:
        """Get recent message history with a specific contact or group in chronological order."""
        target_jid = self.resolve_chat_jid(chat_jid_or_name) or chat_jid_or_name.strip()
        like_name = f"%{chat_jid_or_name.strip().lower()}%"

        sql = """
        SELECT id, chat_jid, chat_name, sender_jid, sender_name, text, timestamp, is_from_me, is_group,
               media_type, media_filename, media_mimetype, media_size, media_metadata
        FROM messages
        WHERE chat_jid = ? OR (is_group = 0 AND (LOWER(chat_name) LIKE ? OR LOWER(sender_name) LIKE ?))
        ORDER BY timestamp DESC
        LIMIT ?
        """
        with self._get_connection() as conn:
            rows = conn.execute(sql, (target_jid, like_name, like_name, limit)).fetchall()
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

    def search_messages(self, query: str, chat_jid: str | None = None, limit: int = 25) -> list[dict[str, Any]]:
        """Perform full-text search across messages, captions, filenames, and rich media content."""
        results: list[dict[str, Any]] = []

        resolved_jid = self.resolve_chat_jid(chat_jid) if chat_jid else None
        like_filter = f"%{chat_jid.strip().lower()}%" if chat_jid else None

        # Try FTS5 first
        try:
            with self._get_connection() as conn:
                safe_fts_query = query.replace('"', '""')
                if chat_jid:
                    sql = """
                    SELECT m.id, m.chat_jid, m.chat_name, m.sender_jid, m.sender_name, m.text, m.timestamp,
                           m.is_from_me, m.media_type, m.media_filename, m.media_mimetype
                    FROM messages_fts f
                    JOIN messages m ON f.rowid = m.rowid
                    WHERE messages_fts MATCH ?
                      AND m.chat_jid != 'status@broadcast'
                      AND (m.chat_jid = ? OR m.sender_jid = ? OR LOWER(m.chat_name) LIKE ? OR LOWER(m.sender_name) LIKE ?)
                    ORDER BY m.timestamp DESC
                    LIMIT ?
                    """
                    rows = conn.execute(sql, (f'"{safe_fts_query}"', resolved_jid, resolved_jid, like_filter, like_filter, limit)).fetchall()
                else:
                    sql = """
                    SELECT m.id, m.chat_jid, m.chat_name, m.sender_jid, m.sender_name, m.text, m.timestamp,
                           m.is_from_me, m.media_type, m.media_filename, m.media_mimetype
                    FROM messages_fts f
                    JOIN messages m ON f.rowid = m.rowid
                    WHERE messages_fts MATCH ?
                      AND m.chat_jid != 'status@broadcast'
                    ORDER BY m.timestamp DESC
                    LIMIT ?
                    """
                    rows = conn.execute(sql, (f'"{safe_fts_query}"', limit)).fetchall()

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
                            "filename": r["media_filename"],
                        }
                    )
                if results:
                    return results
        except Exception as e:
            logger.warning("FTS search encountered error '%s', falling back to standard LIKE search", e)

        # Fallback to standard LIKE if FTS fails or yields no matches
        like_query = f"%{query.strip().lower()}%"
        with self._get_connection() as conn:
            if chat_jid:
                sql = """
                SELECT id, chat_jid, chat_name, sender_jid, sender_name, text, timestamp,
                       is_from_me, media_type, media_filename, media_mimetype
                FROM messages
                WHERE chat_jid != 'status@broadcast'
                  AND (LOWER(text) LIKE ? OR LOWER(media_filename) LIKE ?)
                  AND (chat_jid = ? OR sender_jid = ? OR LOWER(chat_name) LIKE ? OR LOWER(sender_name) LIKE ?)
                ORDER BY timestamp DESC
                LIMIT ?
                """
                rows = conn.execute(sql, (like_query, like_query, resolved_jid, resolved_jid, like_filter, like_filter, limit)).fetchall()
            else:
                sql = """
                SELECT id, chat_jid, chat_name, sender_jid, sender_name, text, timestamp,
                       is_from_me, media_type, media_filename, media_mimetype
                FROM messages
                WHERE chat_jid != 'status@broadcast'
                  AND (LOWER(text) LIKE ? OR LOWER(media_filename) LIKE ?)
                ORDER BY timestamp DESC
                LIMIT ?
                """
                rows = conn.execute(sql, (like_query, like_query, limit)).fetchall()

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
                        "filename": r["media_filename"],
                    }
                )
            return results

    def find_documents(self, query: str = "", chat_name_or_jid: str = "", limit: int = 15) -> list[dict[str, Any]]:
        """Search specifically for documents (PDFs, spreadsheets, docs, presentations) by filename or keyword.
        Supports stemming and flexible multi-keyword matching (e.g. 'invoices' matches 'invoice', 'gfl invoices' matches both).
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

        if query:
            # Word stemming helper
            def _stem(w: str) -> str:
                w = w.lower().strip()
                if w.endswith("ies") and len(w) > 4:
                    return w[:-3]
                if w.endswith("es") and len(w) > 3 and not w.endswith("ees"):
                    return w[:-2]
                if w.endswith("s") and len(w) > 2 and not w.endswith("ss"):
                    return w[:-1]
                return w

            stop_words = {"the", "a", "an", "me", "my", "get", "send", "give", "show", "find", "all", "recent", "latest", "please", "file", "files"}
            tokens = [w for w in re.findall(r"\w+", query.lower()) if w not in stop_words]
            stemmed = [_stem(t) for t in tokens if len(_stem(t)) >= 2]

            if stemmed:
                for st in stemmed:
                    conditions.append("(LOWER(media_filename) LIKE ? OR LOWER(text) LIKE ?)")
                    params.extend([f"%{st}%", f"%{st}%"])
            else:
                cleaned = f"%{query.strip().lower()}%"
                conditions.append("(LOWER(media_filename) LIKE ? OR LOWER(text) LIKE ?)")
                params.extend([cleaned, cleaned])

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

                results.append(
                    {
                        "id": r["id"],
                        "filename": filename,
                        "pages": meta.get("page_count", 0),
                        "mimetype": r["media_mimetype"],
                        "chat": r["chat_name"] or r["chat_jid"],
                        "sender": sender,
                        "timestamp": _format_timestamp(r["timestamp"]),
                        "caption_or_text": r["text"],
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
        self, media_type: str = "voice_note", query: str = "", chat_name_or_jid: str = "", limit: int = 15
    ) -> list[dict[str, Any]]:
        """Find voice notes, audio files, images, or videos sent in conversations.
        Searches in direct chats, matching sender names, captions, or group chats.
        """
        target_jid = self.resolve_chat_jid(chat_name_or_jid) if chat_name_or_jid else None

        conditions = ["chat_jid != 'status@broadcast'"]
        params: list[Any] = []

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

        if query:
            cleaned = f"%{query.strip().lower()}%"
            conditions.append(
                "(LOWER(text) LIKE ? OR LOWER(media_filename) LIKE ? OR id IN (SELECT msg_id FROM media_ocr_cache WHERE LOWER(ocr_text) LIKE ?))"
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
            try:
                total_stored_media = cursor.execute(
                    "SELECT COUNT(*) FROM messages WHERE local_path IS NOT NULL AND local_path NOT LIKE '[%'"
                ).fetchone()[0]
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
                "total_stored_media": total_stored_media,
                "oldest_timestamp": oldest_ts,
                "newest_timestamp": newest_ts,
                "oldest_date": _format_timestamp(oldest_ts) if oldest_ts else "N/A",
                "newest_date": _format_timestamp(newest_ts) if newest_ts else "N/A",
                "db_size_mb": db_size_mb,
            }
