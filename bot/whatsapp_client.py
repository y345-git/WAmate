import os
import logging
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any
import segno
from pathlib import Path
from neonize.client import NewClient
from neonize.events import ConnectedEv, HistorySyncEv, MessageEv, LoggedOutEv
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message
from neonize.utils.enum import ChatPresence, ChatPresenceMedia
from neonize.utils import ClientName

from .config import Config
from .database import DatabaseManager, _format_timestamp
from .gemini_agent import GeminiAgent
from .utils import (
    MessageDeduplicator,
    extract_rich_message_info,
    extract_document_text,
    format_for_whatsapp,
    jid_to_str,
    clean_phone_number,
    normalize_timestamp,
)

logger = logging.getLogger(__name__)


class WhatsAppBot:
    """Manages WhatsApp connection, message indexing, and self-chat AI querying."""

    def __init__(
        self,
        session_db_path: Path,
        db: DatabaseManager,
        gemini: GeminiAgent,
        command_prefix: str = "!ask",
        phone_number: str = "",
    ):
        self.session_db_path = str(session_db_path)
        self.db = db
        self.gemini = gemini
        self.prefix = command_prefix.strip()
        self.phone_number = clean_phone_number(phone_number)
        self.dedup = MessageDeduplicator()

        self.client = NewClient(self.session_db_path)
        self.my_jid = None
        self.my_user_number = ""
        self.my_canonical_jid = ""
        self.last_self_chat_jid = None
        self.history_sync_completed = False

        # Link Gemini agent back to this bot client for document sending & extraction tools
        self.gemini.bot_client = self

        # Idle Background OCR Worker synchronization & state
        self._stop_worker_event = threading.Event()
        self._user_active_event = threading.Event()
        self._idle_worker_thread: threading.Thread | None = None
        self._idle_worker_stats = {"processed": 0, "last_active": None}

        # Media Storage Worker & Auto-Download (Zero Gemini API Cost - keeps media accessible after 30 days)
        self._media_storage_thread: threading.Thread | None = None
        self._storage_worker_stats = {
            "stored": 0,
            "failed_or_expired": 0,
            "last_active": "Never",
        }
        self._media_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="MediaDownloader")

        self._register_handlers()

    def _register_handlers(self) -> None:
        """Register event listeners for Neonize client."""

        # 1. QR Code Handler (Called when pairing via QR code is required)
        @self.client.qr
        def on_qr(client: NewClient, data: bytes):
            qr_string = data.decode("utf-8")
            print("\n" + "=" * 60)
            print("  SCAN THIS QR CODE IN WHATSAPP TO LINK YOUR BOT:")
            print("=" * 60 + "\n")
            try:
                qr = segno.make(qr_string)
                qr.terminal(compact=True)
            except Exception:
                print(f"Raw QR payload: {qr_string}")
            print("\n" + "=" * 60 + "\n")

        # 2. Connection Established
        @self.client.event(ConnectedEv)
        def on_connected(client: NewClient, event: ConnectedEv):
            try:
                device = client.get_me()
                self.my_jid = device.JID
                self.my_user_number = self.my_jid.User
                self.my_canonical_jid = f"{self.my_user_number}@s.whatsapp.net"
                push_name = device.PushName or "You"

                self.my_lid_number = ""
                self.my_lid_jid = ""
                if hasattr(device, "LID") and getattr(device.LID, "User", None):
                    self.my_lid_number = device.LID.User
                    self.my_lid_jid = f"{self.my_lid_number}@lid"
                    logger.info("Detected Account LID: %s", self.my_lid_jid)

                logger.info("Successfully connected to WhatsApp!")
                logger.info("Logged in as: %s (Phone: %s)", push_name, self.my_canonical_jid)
                logger.info("Bot is active! Send '%s <question>' in your self-chat (Message yourself).", self.prefix)

                # Record self in contacts table
                self.db.upsert_contact(
                    jid=self.my_canonical_jid,
                    name="You (Self)",
                    push_name=push_name,
                    phone_number=self.my_user_number,
                    is_group=False,
                )
                if self.my_lid_jid:
                    self.db.upsert_contact(
                        jid=self.my_lid_jid,
                        name="You (Self LID)",
                        push_name=push_name,
                        phone_number=self.my_user_number,
                        is_group=False,
                    )

                # Sync user's phone address book contacts from whatsmeow session store
                self.sync_address_book_contacts()
            except Exception as e:
                logger.exception("Error during ConnectedEv post-processing: %s", e)

        # 3. History Sync (Captures past messages, contacts, and group chats)
        @self.client.event(HistorySyncEv)
        def on_history_sync(client: NewClient, event: HistorySyncEv):
            try:
                data = event.Data
                sync_type_name = data.HistorySyncType.Name(data.syncType) if hasattr(data, "syncType") else "UNKNOWN"
                chunk_order = getattr(data, "chunkOrder", 0)
                progress = getattr(data, "progress", 0)
                # Calculate total messages in this sync batch
                total_msgs_in_batch = sum(len(conv.messages) for conv in data.conversations)
                num_chats = len(data.conversations)

                prog_str = f" [Progress: {progress}%]" if progress > 0 else ""
                chunk_str = f"chunk #{chunk_order} " if chunk_order > 0 else ""
                logger.info(
                    "Processing HistorySync %s(type: %s%s) — %d messages across %d chats...",
                    chunk_str, sync_type_name, prog_str, total_msgs_in_batch, num_chats
                )

                # Batch sync pushnames to contacts table
                pushnames_to_sync = [{"jid": pn.ID, "push_name": pn.pushname} for pn in data.pushnames if pn.ID and pn.pushname]
                if pushnames_to_sync:
                    self.db.upsert_contacts_batch(pushnames_to_sync)

                # Batch sync chat headers to contacts table
                chats_to_sync = [
                    {"jid": conv.ID, "name": conv.name or conv.displayName or "", "is_group": conv.ID.endswith("@g.us")}
                    for conv in data.conversations if conv.ID
                ]
                if chats_to_sync:
                    self.db.upsert_contacts_batch(chats_to_sync)

                # Local in-memory contact cache for fast name lookups during this batch
                contact_name_cache: dict[str, str] = {}

                # Sync messages with live counter in batches of 500
                start_time = time.time()
                pending_messages = []
                processed_count = 0
                total_synced_msgs = 0
                for conv in data.conversations:
                    chat_jid = conv.ID
                    if not chat_jid:
                        continue

                    chat_name = conv.name or conv.displayName or ""
                    is_group = chat_jid.endswith("@g.us")

                    for msg_wrapper in conv.messages:
                        wmsg = msg_wrapper.message
                        key = wmsg.key
                        msg_id = key.ID
                        if not msg_id:
                            continue

                        is_from_me = bool(key.fromMe)
                        if is_from_me:
                            sender_jid = self.my_canonical_jid or (getattr(self, "my_jid", None) or "You")
                            sender_name = "You"
                        elif is_group:
                            # In WebMessageInfo, participant is stored in wmsg.participant or key.participant
                            sender_jid = (getattr(wmsg, "participant", "") or getattr(key, "participant", "") or "").strip()
                            if not sender_jid:
                                sender_jid = chat_jid

                            # Determine sender name with cache
                            sender_name = (getattr(wmsg, "pushName", "") or "").strip()
                            if not sender_name and sender_jid and sender_jid != chat_jid:
                                if sender_jid in contact_name_cache:
                                    sender_name = contact_name_cache[sender_jid]
                                else:
                                    c_info = self.db.get_contact(sender_jid)
                                    if c_info:
                                        sender_name = c_info.get("name") or c_info.get("push_name") or ""
                                    contact_name_cache[sender_jid] = sender_name
                            if not sender_name:
                                sender_name = sender_jid.split("@")[0] if ("@" in sender_jid and not sender_jid.endswith("@g.us")) else "Participant"
                        else:
                            sender_jid = chat_jid
                            push_name = (getattr(wmsg, "pushName", "") or "").strip()
                            sender_name = push_name or chat_name
                            if not sender_name and "@s.whatsapp.net" in chat_jid:
                                sender_name = chat_jid.split("@")[0]
                            if not chat_name:
                                chat_name = sender_name or (chat_jid.split("@")[0] if "@s.whatsapp.net" in chat_jid else "")

                        rich_info = extract_rich_message_info(wmsg.message)
                        timestamp = normalize_timestamp(wmsg.messageTimestamp)
                        raw_bytes = wmsg.message.SerializeToString() if hasattr(wmsg.message, "SerializeToString") else None

                        pending_messages.append({
                            "msg_id": msg_id,
                            "chat_jid": chat_jid,
                            "chat_name": chat_name,
                            "sender_jid": sender_jid,
                            "sender_name": sender_name,
                            "text": rich_info.text,
                            "timestamp": timestamp,
                            "is_from_me": is_from_me,
                            "is_group": is_group,
                            "media_type": rich_info.media_type,
                            "media_filename": rich_info.media_filename,
                            "media_mimetype": rich_info.media_mimetype,
                            "media_size": rich_info.media_size,
                            "media_metadata": rich_info.media_metadata,
                            "raw_message": raw_bytes,
                        })
                        processed_count += 1

                        # Flush batch of 500 messages & log live progress counter
                        if len(pending_messages) >= 500:
                            self.db.insert_messages_batch(pending_messages)
                            total_synced_msgs += len(pending_messages)
                            pending_messages.clear()

                            elapsed = time.time() - start_time
                            rate = processed_count / elapsed if elapsed > 0 else 0
                            pct = (processed_count / total_msgs_in_batch * 100) if total_msgs_in_batch > 0 else 100
                            logger.info(
                                "[HistorySync Counter] %d / %d messages (%.1f%%) — %.0f msgs/sec",
                                processed_count, total_msgs_in_batch, pct, rate
                            )

                # Flush any remaining messages in the batch
                if pending_messages:
                    self.db.insert_messages_batch(pending_messages)
                    total_synced_msgs += len(pending_messages)
                    pending_messages.clear()

                total_elapsed = time.time() - start_time
                total_rate = total_synced_msgs / total_elapsed if total_elapsed > 0 else 0
                logger.info(
                    "HistorySync: successfully indexed %d / %d historical messages in %.2fs (%.0f msgs/sec).",
                    total_synced_msgs, total_msgs_in_batch, total_elapsed, total_rate
                )

                if progress >= 100:
                    self.history_sync_completed = True
                    stats = self.db.get_stats()
                    logger.info(
                        "🎉 HistorySync 100%% complete! All historical messages indexed (%d total in database across %d contacts).",
                        stats["total_messages"], stats["total_contacts"]
                    )

                # Keep phone address book contacts updated
                self.sync_address_book_contacts()
            except Exception as e:
                logger.exception("Error processing HistorySync event: %s", e)

        # 4. Real-time Incoming & Outgoing Messages
        @self.client.event(MessageEv)
        def on_message(client: NewClient, message: MessageEv):
            try:
                info = message.Info
                source = info.MessageSource
                chat = source.Chat
                sender = source.Sender
                is_from_me = bool(source.IsFromMe)
                is_group = bool(source.IsGroup)

                chat_jid = jid_to_str(chat)
                sender_jid = jid_to_str(sender)
                push_name = info.Pushname or ""
                msg_id = info.ID
                timestamp = normalize_timestamp(info.Timestamp)

                rich_info = extract_rich_message_info(message.Message)
                text = rich_info.text.strip()
                raw_bytes = message.Message.SerializeToString() if hasattr(message.Message, "SerializeToString") else None

                # Store contact info
                if push_name:
                    self.db.upsert_contact(jid=sender_jid, push_name=push_name, is_group=is_group)

                sender_display_name = "You" if is_from_me else push_name
                if not is_from_me and not sender_display_name and sender_jid:
                    c_info = self.db.get_contact(sender_jid)
                    if c_info:
                        sender_display_name = c_info.get("name") or c_info.get("push_name") or c_info.get("phone_number") or ""
                if not sender_display_name:
                    sender_display_name = sender_jid.split("@")[0] if ("@" in sender_jid and not is_group) else ("You" if is_from_me else "Participant")

                # Resolve chat display name for 1-on-1 DMs with unsaved contacts
                chat_display_name = None
                if not is_group:
                    c_chat = self.db.get_contact(chat_jid)
                    if c_chat:
                        chat_display_name = c_chat.get("name") or c_chat.get("push_name") or c_chat.get("phone_number")
                    if not chat_display_name:
                        chat_display_name = sender_display_name if not is_from_me else (chat_jid.split("@")[0] if "@s.whatsapp.net" in chat_jid else None)

                # Index message with full rich media metadata into database
                self.db.insert_message(
                    msg_id=msg_id,
                    chat_jid=chat_jid,
                    chat_name=chat_display_name,
                    sender_jid=sender_jid,
                    sender_name=sender_display_name,
                    text=text,
                    timestamp=timestamp,
                    is_from_me=is_from_me,
                    is_group=is_group,
                    media_type=rich_info.media_type,
                    media_filename=rich_info.media_filename,
                    media_mimetype=rich_info.media_mimetype,
                    media_size=rich_info.media_size,
                    media_metadata=rich_info.media_metadata,
                    raw_message=raw_bytes,
                )

                # Real-time Media Auto-Storage (Zero Gemini API Cost)
                # Automatically save incoming media while fresh so it remains accessible forever even after 30 days
                if Config.ENABLE_MEDIA_STORAGE and raw_bytes and rich_info.media_type != "text":
                    types_list = [t.strip() for t in Config.MEDIA_STORAGE_TYPES.split(",") if t.strip()]
                    is_all_types = "all" in types_list or "any" in types_list or not types_list
                    if is_all_types or rich_info.media_type in types_list:
                        max_bytes = Config.MEDIA_STORAGE_MAX_SIZE_MB * 1024 * 1024
                        if Config.MEDIA_STORAGE_MAX_SIZE_MB <= 0 or (rich_info.media_size or 0) <= max_bytes:
                            if Config.MEDIA_STORAGE_INCLUDE_STATUSES or chat_jid != "status@broadcast":
                                self._media_executor.submit(self._auto_download_media_task, msg_id)

                # Determine if this message is inside the user's self-chat ("Message yourself")
                is_self_chat = False
                if is_from_me:
                    # In WhatsApp self-chat:
                    # - chat_jid and sender_jid are identical (e.g. your LID or your JID)
                    # - OR chat.User matches your phone number or your account LID
                    if chat_jid and sender_jid and (chat_jid == sender_jid):
                        is_self_chat = True
                    elif self.my_user_number and (chat.User == self.my_user_number or chat_jid == self.my_canonical_jid):
                        is_self_chat = True
                    elif getattr(self, "my_lid_number", "") and (chat.User == self.my_lid_number or chat_jid == getattr(self, "my_lid_jid", "")):
                        is_self_chat = True

                # Handle command ONLY in self-chat sent by me
                if is_self_chat and is_from_me:
                    self.last_self_chat_jid = chat
                    self._handle_self_chat_message(client, message, msg_id, text, chat)

            except Exception as e:
                logger.exception("Error handling MessageEv: %s", e)

        # 5. Logged Out Event
        @self.client.event(LoggedOutEv)
        def on_logged_out(client: NewClient, event: LoggedOutEv):
            logger.warning("Session logged out from WhatsApp. Re-authentication will be required.")

    def _handle_self_chat_message(
        self, client: NewClient, message: MessageEv, msg_id: str, text: str, chat_jid: Any
    ) -> None:
        """Handle commands and queries received in the self-chat."""
        if not text:
            return

        text_lower = text.lower().strip()

        # Check for account unlinking / logout commands in self-chat
        unlink_triggers = ["!unlink", "!logout", "!reset"]
        if any(text_lower.startswith(ut) for ut in unlink_triggers):
            if self.dedup.contains(msg_id):
                return
            self.dedup.add(msg_id)

            delete_data = any(k in text_lower for k in ["delete-data", "--delete-data", "purge", "all", "wipe", "reset", "clear-data"])

            if delete_data:
                reply = (
                    "⚠️ *Unlinking Account & Wiping Data...*\n\n"
                    "• WhatsApp account has been disconnected and unlinked.\n"
                    "• All saved message history, contacts, and downloaded files have been deleted.\n"
                    "• To link a new account, start `python3 main.py` in your terminal to scan a new QR code or enter a pairing code."
                )
            else:
                reply = (
                    "⚠️ *Unlinking Account...*\n\n"
                    "• WhatsApp account has been disconnected and unlinked.\n"
                    "• Your saved message history is preserved in the local database.\n"
                    "• To link a new account, start `python3 main.py` in your terminal to scan a new QR code or enter a pairing code."
                )

            try:
                client.reply_message(reply, message)
                time.sleep(1.5)
            except Exception as e:
                logger.warning("Failed to send unlink confirmation: %s", e)

            self.unlink(delete_data=delete_data)
            logger.info("Account unlinked via self-chat command. Process terminating...")
            os._exit(0)
            return

        # Check for !status / !stats / !info commands in self-chat
        status_triggers = ["!status", "!stats", "!info"]
        if any(text_lower == st or text_lower.startswith(f"{st} ") for st in status_triggers):
            if self.dedup.contains(msg_id):
                return
            self.dedup.add(msg_id)

            stats = self.db.get_stats()
            sync_state = "Fully indexed & Real-time active ✅" if getattr(self, "history_sync_completed", False) else "Indexed (Real-time active) ✅"
            idle_state = (
                f"Active 🟢 ({self._idle_worker_stats['processed']} pre-cached, budget: {Config.IDLE_OCR_DAILY_BUDGET}/day)"
                if Config.ENABLE_IDLE_OCR
                else "Disabled (On-Demand only ⚡)"
            )

            # Compute disk usage of stored media files
            stored_mb = 0.0
            if Config.DOWNLOADS_DIR.exists():
                try:
                    stored_bytes = sum(f.stat().st_size for f in Config.DOWNLOADS_DIR.glob("*") if f.is_file())
                    stored_mb = round(stored_bytes / (1024 * 1024), 2)
                except Exception:
                    pass

            stored_count = stats.get("total_stored_media", 0)
            storage_state = (
                f"Active 🟢 ({stored_count} files, {stored_mb} MB stored on disk)"
                if Config.ENABLE_MEDIA_STORAGE
                else "Disabled"
            )

            reply = (
                "📊 *WAmate Second Brain Status*\n\n"
                f"• *Messages Synced:* {stats['total_messages']:,}\n"
                f"• *Contacts & Groups:* {stats['total_contacts']:,} ({stats['total_groups']} groups)\n"
                f"• *History Date Range:* {stats['oldest_date']} → {stats['newest_date']}\n"
                f"• *Stored Media (30-day safe 💾):* {storage_state}\n"
                f"• *OCR Cached Media:* {stats['total_ocr_cached']}\n"
                f"• *Idle OCR Worker:* {idle_state}\n"
                f"• *Database Size:* {stats['db_size_mb']} MB\n"
                f"• *Sync Status:* {sync_state}\n"
                f"• *AI Model:* `{Config.GEMINI_MODEL}`\n\n"
                "_Send `!ask <question>` to search or query your messages!_"
            )
            try:
                client.reply_message(reply, message)
            except Exception:
                try:
                    client.send_message(chat_jid, reply)
                except Exception as e:
                    logger.warning("Failed to send status reply: %s", e)
            return

        matched_prefix = None
        recognized_prefixes = [self.prefix.lower(), "!get", "!send", "!fetch", "!doc", "!file", "!download"]
        for pfx in recognized_prefixes:
            if text_lower.startswith(pfx):
                matched_prefix = pfx
                break

        if not matched_prefix:
            return

        # Prevent duplicate processing or processing bot echos
        if self.dedup.contains(msg_id):
            return
        self.dedup.add(msg_id)

        raw_query = text[len(matched_prefix):].strip()
        # Direct action prefixes like !get, !send, !file automatically map to document delivery queries
        if matched_prefix in ["!get", "!send", "!fetch", "!download", "!file", "!doc"]:
            query = f"send me the {raw_query}" if not raw_query.lower().startswith(("send", "get", "fetch", "deliver")) else raw_query
        else:
            query = raw_query

        if not query:
            hint = (
                f"🤖 *WhatsApp AI Assistant*\n\n"
                f"Ask me anything about your chats or request files directly!\n\n"
                f"• `{self.prefix} get me the latest invoices`\n"
                f"• `{self.prefix} send me the Reimagine 2026 pdf`\n"
                f"• `!get invoices`\n"
                f"• `{self.prefix} what did Alex message me?`\n"
                f"• `{self.prefix} summarize what happened today`"
            )
            try:
                resp = client.reply_message(hint, message)
                if resp and hasattr(resp, "ID"):
                    self.dedup.add(resp.ID)
            except Exception:
                resp = client.send_message(chat_jid, hint)
                if resp and hasattr(resp, "ID"):
                    self.dedup.add(resp.ID)
            return

        # Pause any background idle workers while user is actively querying
        self._user_active_event.set()
        logger.info("Processing self-chat query: '%s'", query)

        # Show real-time typing indicator and hourglass reaction in WhatsApp self-chat
        source = message.Info.MessageSource
        chat = source.Chat
        sender = source.Sender

        try:
            client.send_chat_presence(chat_jid, ChatPresence.CHAT_PRESENCE_COMPOSING, ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT)
        except Exception as pe:
            logger.debug("Could not send typing presence indicator: %s", pe)

        try:
            react_pending = client.build_reaction(chat, sender, msg_id, "⏳")
            client.send_message(chat_jid, react_pending)
        except Exception as re_err:
            logger.debug("Could not send pending reaction: %s", re_err)

        try:
            # Generate answer using Gemini with access to the database
            raw_answer = self.gemini.ask(query)
            answer = format_for_whatsapp(raw_answer)

            # Reply strictly in self-chat
            try:
                resp = client.reply_message(answer, message)
                if resp and hasattr(resp, "ID"):
                    self.dedup.add(resp.ID)
                logger.info("Sent reply to self-chat.")
            except Exception as e:
                logger.warning("reply_message failed (%s), sending directly to chat...", e)
                try:
                    resp = client.send_message(chat_jid, answer)
                    if resp and hasattr(resp, "ID"):
                        self.dedup.add(resp.ID)
                    logger.info("Sent direct message to self-chat.")
                except Exception as e2:
                    logger.exception("Failed to send response to self-chat: %s", e2)
        finally:
            self._user_active_event.clear()
            # Clear typing indicator and update reaction to checkmark
            try:
                client.send_chat_presence(chat_jid, ChatPresence.CHAT_PRESENCE_PAUSED, ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT)
            except Exception:
                pass
            try:
                react_done = client.build_reaction(chat, sender, msg_id, "✅")
                client.send_message(chat_jid, react_done)
            except Exception:
                pass

    def sync_address_book_contacts(self) -> int:
        """Sync contacts and LID mappings from whatsmeow's session store into messages database.
        whatsmeow captures all contacts saved in the user's phone contact book (full_name)
        and the cryptographic LID-to-phone number map.
        """
        import sqlite3
        if not Path(self.session_db_path).exists():
            return 0
        try:
            s_conn = sqlite3.connect(self.session_db_path)
            sc = s_conn.cursor()
            contacts = sc.execute(
                "SELECT their_jid, full_name, first_name, push_name, business_name FROM whatsmeow_contacts"
            ).fetchall()

            synced = 0
            for their_jid, full_name, first_name, push_name, business_name in contacts:
                if not their_jid:
                    continue
                name = (full_name or first_name or business_name or "").strip()
                pname = (push_name or "").strip()
                phone = their_jid.split("@")[0] if "@" in their_jid else ""
                is_group = their_jid.endswith("@g.us")

                if name or pname:
                    self.db.upsert_contact(
                        jid=their_jid,
                        name=name or None,
                        push_name=pname or None,
                        phone_number=phone if not is_group else None,
                        is_group=is_group,
                    )
                    synced += 1

            # Sync LID to phone number mappings
            try:
                lid_rows = sc.execute("SELECT lid, pn FROM whatsmeow_lid_map").fetchall()
                lid_contacts = []
                for lid, pn in lid_rows:
                    lid_jid = f"{lid}@lid"
                    pn_jid = f"{pn}@s.whatsapp.net"
                    c = self.db.get_contact(pn_jid)
                    name = (c.get("name") or c.get("push_name")) if c else None
                    lid_contacts.append({
                        "jid": lid_jid,
                        "name": name,
                        "phone_number": pn,
                        "is_group": False,
                    })
                if lid_contacts:
                    self.db.upsert_contacts_batch(lid_contacts)
                    synced += len(lid_contacts)
            except Exception as le:
                logger.debug("LID map sync notice: %s", le)

            s_conn.close()

            self.db.backfill_contact_names()
            logger.info("Synchronized %d phone contacts & LID mappings into database.", synced)
            return synced
        except Exception as e:
            logger.exception("Error syncing address book contacts from whatsmeow session: %s", e)
            return 0

    def download_media_by_id(self, msg_id: str) -> tuple[str | None, str | None, str | None, str | None]:
        """Download media file for a given message ID from WhatsApp or return cached file.
        Returns: (file_path, filename, mimetype, error_message)
        """
        row = self.db.get_message_raw(msg_id)
        if not row:
            return None, None, None, f"Message ID '{msg_id}' not found in database."

        filename = row["filename"] or f"file_{msg_id}"
        mimetype = row["mimetype"] or "application/octet-stream"
        raw_blob = row["raw_message"]

        downloads_dir = Config.DOWNLOADS_DIR
        downloads_dir.mkdir(parents=True, exist_ok=True)
        safe_filename = re.sub(r'[\\/*?:"<>|]', "_", filename)
        if not Path(safe_filename).suffix:
            if "jpeg" in mimetype.lower() or "jpg" in mimetype.lower():
                safe_filename += ".jpg"
            elif "png" in mimetype.lower():
                safe_filename += ".png"
            elif "pdf" in mimetype.lower():
                safe_filename += ".pdf"
            elif "spreadsheet" in mimetype.lower() or "excel" in mimetype.lower():
                safe_filename += ".xlsx"
            elif "wordprocessingml" in mimetype.lower() or "msword" in mimetype.lower():
                safe_filename += ".docx"
            elif "presentationml" in mimetype.lower() or "powerpoint" in mimetype.lower():
                safe_filename += ".pptx"
            elif "csv" in mimetype.lower():
                safe_filename += ".csv"
            elif "ogg" in mimetype.lower():
                safe_filename += ".ogg"
            elif "audio" in mimetype.lower():
                safe_filename += ".m4a"
            elif "video" in mimetype.lower():
                safe_filename += ".mp4"
            elif "webp" in mimetype.lower():
                safe_filename += ".webp"
            elif "gif" in mimetype.lower():
                safe_filename += ".gif"

        # Check if already downloaded locally
        if row.get("local_path") and not str(row["local_path"]).startswith("["):
            lp = Path(str(row["local_path"]))
            if lp.exists() and lp.stat().st_size > 0:
                return str(lp), filename, mimetype, None

        file_path = downloads_dir / f"{msg_id}_{safe_filename}"

        if file_path.exists() and file_path.stat().st_size > 0:
            if not row.get("local_path"):
                self.db.update_message_local_path(msg_id, str(file_path))
            return str(file_path), filename, mimetype, None

        if not raw_blob:
            return (
                None,
                filename,
                mimetype,
                f"Document '{filename}' was indexed without its media payload. "
                "Past documents require a fresh sync to download. New incoming documents can be downloaded immediately.",
            )

        try:
            msg = Message()
            msg.ParseFromString(raw_blob)

            # Check if this protobuf message actually contains any media attachment
            attachment_fields = [
                "imageMessage", "documentMessage", "videoMessage", "audioMessage",
                "stickerMessage", "documentWithCaptionMessage"
            ]
            has_attachment = any(msg.HasField(f) for f in attachment_fields if hasattr(msg, "HasField"))
            if not has_attachment:
                self.db.update_message_local_path(msg_id, "[NO_ATTACHMENT]")
                return None, filename, mimetype, "No media attachment found in message payload."

            binary_data = self.client.download_any(msg)
            if not binary_data:
                self.db.update_message_local_path(msg_id, "[EXPIRED_ON_WHATSAPP]")
                return None, filename, mimetype, "WhatsApp download returned empty data (media link may have expired on servers)."

            with open(file_path, "wb") as f:
                f.write(binary_data)

            self.db.update_message_local_path(msg_id, str(file_path))
            logger.info("Successfully downloaded media file: %s (%d bytes)", file_path, len(binary_data))
            return str(file_path), filename, mimetype, None
        except Exception as e:
            err_str = str(e).strip()
            # Mark ALL download failures in database so it NEVER endlessly retries dead links
            self.db.update_message_local_path(msg_id, f"[DOWNLOAD_FAILED: {err_str[:40]}]")
            logger.debug("Media download failed for message %s: %s", msg_id, err_str)
            return None, filename, mimetype, f"WhatsApp download failed: {err_str}"

    def send_document_by_query(self, query: str, target_jid: Any = None, max_count: int = 1) -> str:
        """Find matching document(s), download them, and send the actual file(s) directly into self-chat.
        If a candidate file link has expired on WhatsApp CDN (410), automatically tries subsequent matches.
        """
        dest_jid = target_jid or getattr(self, "last_self_chat_jid", None) or self.my_jid
        if not dest_jid:
            return "Cannot send document: self-chat destination JID is not yet available."

        matches = self.db.find_documents(query=query, limit=15)
        if not matches:
            return f"No document matching '{query}' found in your chats."

        # Support sending multiple files for plural requests ("invoices", "bills", "files", "all")
        plural_keywords = ["invoices", "bills", "files", "documents", "pdfs", "all", "reports", "forms"]
        if max_count <= 1 and any(pw in query.lower() for pw in plural_keywords):
            target_send = 3
        else:
            target_send = max(1, max_count)

        delivered: list[str] = []
        expired_count = 0

        for doc_info in matches:
            if len(delivered) >= target_send:
                break

            msg_id = doc_info["id"]
            filename = doc_info.get("filename") or "document"
            chat_name = doc_info.get("chat") or "Unknown"

            file_path, real_filename, mimetype, err = self.download_media_by_id(msg_id)
            if err or not file_path:
                logger.warning("Could not download '%s': %s (trying next match)", filename, err)
                if "410" in str(err) or "expired" in str(err).lower():
                    expired_count += 1
                continue

            try:
                caption = f"📄 *{real_filename or filename}*\nChat: _{chat_name}_"
                self.client.send_document(
                    to=dest_jid,
                    file=file_path,
                    filename=real_filename or filename,
                    mimetype=mimetype or "application/octet-stream",
                    caption=caption,
                )
                delivered.append(real_filename or filename)
                logger.info("Delivered document '%s' to self-chat", real_filename or filename)
                time.sleep(0.5)
            except Exception as e:
                logger.exception("Failed to send document '%s' to self-chat: %s", filename, e)

        if delivered:
            files_str = "\n".join([f"• *{f}*" for f in delivered])
            expired_note = f"\n_(Note: {expired_count} older file(s) had expired on WhatsApp servers and were skipped)._" if expired_count > 0 else ""
            return f"Successfully sent {len(delivered)} document(s) directly to your WhatsApp chat:\n{files_str}{expired_note}"
        else:
            if expired_count > 0:
                return (
                    f"Found {len(matches)} matching document(s) for '{query}', but their media download links have expired "
                    f"on WhatsApp's servers (status 410 - links typically expire after 30 days). "
                    f"Newer documents can be downloaded and delivered immediately!"
                )
            return f"Found matching documents for '{query}', but could not download them from WhatsApp."

    def read_and_ocr_document(self, query: str, specific_question: str = "") -> str:
        """Find matching document, download it, and extract structured text (Excel, Word, PowerPoint, PDF, CSV, Text) or run Gemini Vision OCR on scanned PDFs."""
        matches = self.db.find_documents(query=query, limit=10)
        if not matches:
            return f"No document matching '{query}' found in your chats."

        for doc_info in matches:
            msg_id = doc_info["id"]
            filename = doc_info.get("filename") or "document"
            chat_name = doc_info.get("chat") or "Unknown"

            # Check if this document has cached extracted text or OCR analysis
            cached_text = self.db.get_ocr_cache(msg_id)
            if cached_text:
                logger.info("Found cached text/analysis for document %s (instant local lookup)", filename)
                return (
                    f"--- Document Content: {filename} (Chat: '{chat_name}') [Cached] ---\n"
                    f"{cached_text}\n"
                    f"--- End of Document Content ---"
                )

            file_path, real_filename, mimetype, err = self.download_media_by_id(msg_id)
            if err or not file_path:
                logger.warning("Could not download '%s' to read: %s (checking next match)", filename, err)
                continue

            # Extract structured text (supports Excel .xlsx, Word .docx, PowerPoint .pptx, PDF, CSV, Code, Text)
            text = extract_document_text(file_path, max_chars=18000)

            # Check if this document is a scanned image PDF without selectable text, requiring Gemini Vision OCR
            is_scanned = (
                mimetype == "application/pdf"
                and (
                    not text
                    or "scanned images without selectable text" in text.lower()
                    or len(text.strip()) < 100
                    or any(k in (query + " " + specific_question).lower() for k in ["ocr", "scan", "scanned", "image", "table", "tax", "gst", "total", "amount"])
                )
            )

            if is_scanned and mimetype == "application/pdf":
                logger.info("Running Gemini Vision OCR on scanned document %s...", real_filename or filename)
                try:
                    with open(file_path, "rb") as f:
                        file_bytes = f.read()
                    ocr_prompt = specific_question or f"Transcribe and analyze all details, tables, items, and figures in this document ({real_filename or filename}):"
                    ocr_text = self.gemini.ocr_file_content(file_bytes, mimetype=mimetype, prompt=ocr_prompt)
                    self.db.save_ocr_cache(msg_id, ocr_text, mimetype=mimetype)
                    return (
                        f"--- Document Analysis & AI Vision OCR: {real_filename or filename} (Chat: '{chat_name}') ---\n"
                        f"{ocr_text}\n"
                        f"--- End of Document OCR ---"
                    )
                except Exception as e:
                    logger.warning("Gemini Vision OCR on %s failed: %s, falling back to text extraction", real_filename or filename, e)

            # Cache the extracted document text so future queries and FTS search find it instantly
            if text and not text.startswith("Failed to extract"):
                self.db.save_ocr_cache(msg_id, text, mimetype=mimetype or "")

            return (
                f"--- Document Content: {real_filename or filename} (from chat '{chat_name}') ---\n"
                f"{text}\n"
                f"--- End of Document Content ---"
            )

        return f"Found documents matching '{query}', but could not download their contents (media links may have expired on WhatsApp servers)."

    def read_document_by_query(self, query: str) -> str:
        """Alias for backward compatibility."""
        return self.read_and_ocr_document(query)

    def inspect_and_ocr_image(
        self,
        message_id: str = "",
        contact_or_chat: str = "",
        topic_or_query: str = "",
        user_question: str = "",
        query_or_contact: str = "",
    ) -> str:
        """Find an image or photo in chats, download it, and run Gemini Vision OCR to transcribe text and analyze content."""
        # Normalize arguments for flexibility
        if query_or_contact and not contact_or_chat and not message_id:
            cleaned_input = query_or_contact.strip()
            # If the user passed a message ID into query_or_contact
            if len(cleaned_input) >= 16 and " " not in cleaned_input and self.db.get_message_raw(cleaned_input):
                message_id = cleaned_input
            else:
                contact_or_chat = cleaned_input

        # 1. If an exact message_id was provided, inspect that specific image directly
        if message_id:
            msg_id = message_id.strip()
            raw_info = self.db.get_message_raw(msg_id)
            if not raw_info:
                return f"No message found in the database with ID '{msg_id}'."

            chat_name = raw_info.get("chat_name") or raw_info.get("chat_jid") or "Unknown"
            sender_name = raw_info.get("sender_name") or "Unknown"
            timestamp = _format_timestamp(raw_info.get("timestamp"))
            caption = raw_info.get("text") or ""
            filename = raw_info.get("filename") or msg_id

            # Instant return if OCR was previously cached
            cached_analysis = self.db.get_ocr_cache(msg_id)
            if cached_analysis:
                logger.info("Found cached OCR analysis for image %s (instant local lookup)", msg_id)
                return (
                    f"--- Image Analysis ({filename}) from chat '{chat_name}' ({timestamp}) [Cached] ---\n"
                    f"Sender: {sender_name}\n"
                    f"Caption: {caption if caption else 'None'}\n\n"
                    f"{cached_analysis}\n"
                    f"--- End of Image Analysis ---"
                )

            file_path, real_filename, mimetype, err = self.download_media_by_id(msg_id)
            if err or not file_path:
                return f"Found image {msg_id}, but could not download it from WhatsApp: {err}"

            try:
                with open(file_path, "rb") as f:
                    img_bytes = f.read()

                logger.info("Running Gemini Vision OCR on exact image %s from chat '%s'...", msg_id, chat_name)
                ocr_prompt = (
                    user_question
                    or f"Transcribe all text, numbers, receipts, amounts, handwritten notes, colors, patterns, and visual details from this image (shared by '{sender_name}' in chat '{chat_name}' at {timestamp}):"
                )
                analysis = self.gemini.ocr_file_content(
                    img_bytes, mimetype=mimetype or "image/jpeg", prompt=ocr_prompt
                )
                self.db.save_ocr_cache(msg_id, analysis, mimetype=mimetype or "image/jpeg")

                return (
                    f"--- Image Analysis ({real_filename or msg_id}) from chat '{chat_name}' ({timestamp}) ---\n"
                    f"Sender: {sender_name}\n"
                    f"Caption: {caption if caption else 'None'}\n\n"
                    f"{analysis}\n"
                    f"--- End of Image Analysis ---"
                )
            except Exception as e:
                logger.exception("Failed to run OCR on image %s: %s", msg_id, e)
                return f"Error analyzing image {msg_id}: {str(e)}"

        # 2. Search candidate images
        images = []
        if topic_or_query and contact_or_chat:
            images = self.db.find_voice_notes_and_media(
                media_type="image", query=topic_or_query, chat_name_or_jid=contact_or_chat, limit=10
            )

        if not images and contact_or_chat:
            images = self.db.find_voice_notes_and_media(
                media_type="image", query="", chat_name_or_jid=contact_or_chat, limit=20
            )

        if not images and topic_or_query:
            images = self.db.find_voice_notes_and_media(
                media_type="image", query=topic_or_query, limit=10
            )

        if not images:
            images = self.db.find_voice_notes_and_media(media_type="image", query="", limit=10)

        if not images:
            target_desc = f"'{topic_or_query}' in '{contact_or_chat}'" if (topic_or_query and contact_or_chat) else f"'{contact_or_chat or topic_or_query}'"
            return f"No images found matching {target_desc} in your chats."

        # If user asked a specific question or topic, prioritize candidate images whose caption or text matches keywords
        search_terms = set(re.findall(r"\w+", (topic_or_query + " " + user_question).lower()))
        for stop in ["what", "when", "where", "the", "did", "image", "photo", "show", "send", "tell", "details"]:
            search_terms.discard(stop)

        if search_terms:
            def _image_relevance(img_dict: dict[str, Any]) -> int:
                cap = (img_dict.get("caption") or img_dict.get("description") or "").lower()
                return sum(2 for term in search_terms if term in cap)

            images.sort(key=_image_relevance, reverse=True)

        inspections = []
        for img in images:
            msg_id = img["id"]
            chat_name = img.get("chat") or "Unknown"
            caption = img.get("caption") or img.get("description") or ""
            timestamp = img.get("timestamp") or ""

            # Check if this image was previously OCR'd
            cached_analysis = self.db.get_ocr_cache(msg_id)
            if cached_analysis:
                logger.info("Using cached OCR analysis for image %s", msg_id)
                inspections.append(
                    f"--- Image Analysis ({msg_id}) from chat '{chat_name}' ({timestamp}) [Cached] ---\n"
                    f"Caption: {caption if caption else 'None'}\n\n"
                    f"{cached_analysis}\n"
                    f"--- End of Image Analysis ---"
                )
                if len(inspections) >= 4:
                    break
                continue

            file_path, real_filename, mimetype, err = self.download_media_by_id(msg_id)
            if err or not file_path:
                logger.warning("Could not download image %s: %s (trying next image)", msg_id, err)
                continue

            try:
                with open(file_path, "rb") as f:
                    img_bytes = f.read()

                logger.info("Running Gemini Vision OCR on image %s from chat '%s'...", msg_id, chat_name)
                ocr_prompt = (
                    user_question
                    or f"Transcribe all text, numbers, receipts, handwritten notes, colors, patterns, and visual details from this image (shared in chat '{chat_name}'):"
                )
                analysis = self.gemini.ocr_file_content(
                    img_bytes, mimetype=mimetype or "image/jpeg", prompt=ocr_prompt
                )
                self.db.save_ocr_cache(msg_id, analysis, mimetype=mimetype or "image/jpeg")

                inspections.append(
                    f"--- Image Analysis ({real_filename or msg_id}) from chat '{chat_name}' ({timestamp}) ---\n"
                    f"Caption: {caption if caption else 'None'}\n\n"
                    f"{analysis}\n"
                    f"--- End of Image Analysis ---"
                )
                if len(inspections) >= 4:
                    break
            except Exception as e:
                logger.exception("Failed to run OCR on image %s: %s", msg_id, e)
                continue

        if inspections:
            return "\n\n".join(inspections)

        return "Found matching images, but could not download them from WhatsApp servers (links may have expired)."

    def _auto_download_media_task(self, msg_id: str) -> None:
        """Background worker task to immediately pre-download an incoming media file while link is fresh."""
        try:
            file_path, filename, mimetype, err = self.download_media_by_id(msg_id)
            if file_path and not err:
                self._storage_worker_stats["stored"] += 1
                self._storage_worker_stats["last_active"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                size_kb = round(os.path.getsize(file_path) / 1024, 1) if os.path.exists(file_path) else 0
                logger.info("Real-Time Media Storage: Auto-downloaded '%s' (%s KB, msg: %s) [30-day protected].", filename, size_kb, msg_id)
        except Exception as e:
            logger.debug("Real-Time Media Storage error for %s: %s", msg_id, e)

    def _start_media_storage_worker(self) -> None:
        """Launch the background media storage worker thread if enabled (Zero Gemini API Cost)."""
        if not Config.ENABLE_MEDIA_STORAGE:
            logger.info("Media Storage Worker is disabled (ENABLE_MEDIA_STORAGE=false).")
            return

        if self._media_storage_thread and self._media_storage_thread.is_alive():
            return

        self._stop_worker_event.clear()
        self._media_storage_thread = threading.Thread(
            target=self._media_storage_worker, daemon=True, name="MediaStorageWorker"
        )
        self._media_storage_thread.start()
        logger.info(
            "Media Storage Worker started (lookback: %dd, max size: %dMB, delay: %.1fs). "
            "Pre-downloads raw media to disk with 0 Gemini API calls so files remain accessible forever after 30 days.",
            Config.MEDIA_STORAGE_LOOKBACK_DAYS,
            Config.MEDIA_STORAGE_MAX_SIZE_MB,
            Config.MEDIA_STORAGE_DELAY_SECONDS,
        )

    def _media_storage_worker(self) -> None:
        """Background thread that pre-downloads and stores raw media files to disk while idle.
        Uses ZERO Gemini API calls! Permanently protects media from WhatsApp's 30-day CDN link expiration.
        """
        logger.info("Media Storage Worker active. Waiting 15s for connection warmup...")
        if self._stop_worker_event.wait(15.0):
            return

        types_tuple = tuple(t.strip() for t in Config.MEDIA_STORAGE_TYPES.split(",") if t.strip())
        max_bytes = Config.MEDIA_STORAGE_MAX_SIZE_MB * 1024 * 1024 if Config.MEDIA_STORAGE_MAX_SIZE_MB > 0 else 0

        while not self._stop_worker_event.is_set():
            if not getattr(self.client, "is_connected", False):
                if self._stop_worker_event.wait(5.0):
                    break
                continue

            # If user is currently querying self-chat, pause and yield
            if self._user_active_event.is_set():
                if self._stop_worker_event.wait(3.0):
                    break
                continue

            try:
                candidates = self.db.get_unstored_media_candidates(
                    lookback_days=Config.MEDIA_STORAGE_LOOKBACK_DAYS,
                    limit=50,
                    media_types=types_tuple,
                    max_size_bytes=max_bytes,
                    include_statuses=Config.MEDIA_STORAGE_INCLUDE_STATUSES,
                    self_jid=self.my_canonical_jid or "",
                )
            except Exception as e:
                logger.warning("Media Storage Worker: error querying candidates: %s", e)
                candidates = []

            if not candidates:
                # No candidates to store; wait 60s before checking again
                if self._stop_worker_event.wait(60.0):
                    break
                continue

            logger.info(
                "Media Storage Worker: found %d unstored media item(s) from past %d days.",
                len(candidates),
                Config.MEDIA_STORAGE_LOOKBACK_DAYS,
            )

            for item in candidates:
                if self._stop_worker_event.is_set():
                    break

                # If user starts querying, pause immediately
                while self._user_active_event.is_set():
                    if self._stop_worker_event.wait(3.0):
                        break

                if self._stop_worker_event.is_set():
                    break

                msg_id = item["id"]
                chat_name = item.get("chat_name") or item.get("chat_jid") or "Unknown"
                media_type = item.get("media_type") or "media"
                filename = item.get("media_filename") or msg_id

                file_path, real_filename, mimetype, err = self.download_media_by_id(msg_id)
                if file_path and not err:
                    self._storage_worker_stats["stored"] += 1
                    self._storage_worker_stats["last_active"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    size_kb = round(os.path.getsize(file_path) / 1024, 1) if os.path.exists(file_path) else 0
                    logger.info(
                        "Media Storage: Saved %s '%s' (%s KB) locally from '%s' [30-day protected].",
                        media_type,
                        real_filename or filename,
                        size_kb,
                        chat_name,
                    )
                else:
                    self._storage_worker_stats["failed_or_expired"] += 1
                    logger.debug("Media Storage: Skipping un-downloadable %s %s (%s)", media_type, msg_id, err)

                # Gentle delay to avoid socket congestion
                if self._stop_worker_event.wait(Config.MEDIA_STORAGE_DELAY_SECONDS):
                    break

            # Sleep 30 seconds between candidate batches
            if self._stop_worker_event.wait(30.0):
                break

    def _start_idle_ocr_worker(self) -> None:
        """Launch the background idle OCR worker thread if enabled."""
        if not Config.ENABLE_IDLE_OCR:
            logger.info("Idle Background OCR is disabled (ENABLE_IDLE_OCR=false).")
            return

        if self._idle_worker_thread and self._idle_worker_thread.is_alive():
            return

        self._stop_worker_event.clear()
        self._idle_worker_thread = threading.Thread(
            target=self._idle_ocr_worker, daemon=True, name="IdleOCRWorker"
        )
        self._idle_worker_thread.start()
        logger.info(
            "Idle Background OCR Worker started (lookback: %dh, delay: %ds, max/cycle: %d, daily budget: %d).",
            Config.IDLE_OCR_LOOKBACK_HOURS,
            Config.IDLE_OCR_DELAY_SECONDS,
            Config.IDLE_OCR_MAX_PER_RUN,
            Config.IDLE_OCR_DAILY_BUDGET,
        )

    def _idle_ocr_worker(self) -> None:
        """Background thread that pre-caches OCR analysis for recent images while idle."""
        logger.info("Idle OCR worker active. Waiting 20s for connection warmup...")
        # Warmup delay
        if self._stop_worker_event.wait(20.0):
            return

        current_day = datetime.now().date()
        daily_ocr_count = 0

        while not self._stop_worker_event.is_set():
            today = datetime.now().date()
            if today != current_day:
                current_day = today
                daily_ocr_count = 0
                logger.info("Idle OCR: Daily quota budget reset for new day (%s).", current_day)

            # Check if daily budget has been reached
            if daily_ocr_count >= Config.IDLE_OCR_DAILY_BUDGET:
                logger.info(
                    "Idle OCR: Daily quota budget reached (%d/%d requests today). Pausing background worker until midnight.",
                    daily_ocr_count,
                    Config.IDLE_OCR_DAILY_BUDGET,
                )
                if self._stop_worker_event.wait(1800.0):
                    break
                continue
            # If client is not connected, wait
            if not getattr(self.client, "is_connected", False):
                if self._stop_worker_event.wait(5.0):
                    break
                continue

            # If user is currently issuing commands in self-chat, yield immediately
            if self._user_active_event.is_set():
                if self._stop_worker_event.wait(3.0):
                    break
                continue

            # Fetch uncached candidates from the last lookback_hours (strictly prioritizing self-chat, DMs, and groups before statuses)
            try:
                candidates = self.db.get_uncached_media_candidates(
                    lookback_hours=Config.IDLE_OCR_LOOKBACK_HOURS,
                    limit=Config.IDLE_OCR_MAX_PER_RUN,
                    include_statuses=Config.IDLE_OCR_INCLUDE_STATUSES,
                    self_jid=self.my_canonical_jid or "",
                )
            except Exception as e:
                logger.warning("Idle OCR: error querying uncached candidates: %s", e)
                candidates = []

            if not candidates:
                # No candidates to process; sleep 60s before checking again
                if self._stop_worker_event.wait(60.0):
                    break
                continue

            logger.info(
                "Idle OCR Worker found %d uncached media item(s) from past %d hours.",
                len(candidates),
                Config.IDLE_OCR_LOOKBACK_HOURS,
            )

            processed_in_cycle = 0
            for item in candidates:
                if self._stop_worker_event.is_set():
                    break

                # If user starts querying, pause immediately and yield
                while self._user_active_event.is_set():
                    logger.debug("Idle OCR paused: user is actively querying self-chat.")
                    if self._stop_worker_event.wait(3.0):
                        break

                if self._stop_worker_event.is_set():
                    break

                msg_id = item["id"]
                chat_name = item.get("chat_name") or item.get("chat_jid") or "Unknown"
                sender_name = item.get("sender_name") or "Unknown"
                mimetype = item.get("media_mimetype") or "image/jpeg"

                # Check if already cached (could have been processed on-demand)
                if self.db.get_ocr_cache(msg_id):
                    continue

                # Attempt to download image bytes
                file_path, real_filename, detected_mimetype, err = self.download_media_by_id(msg_id)
                if err or not file_path:
                    err_str = str(err).lower()
                    if any(k in err_str for k in ["410", "expired", "payload", "empty"]):
                        self.db.save_ocr_cache(msg_id, "[MEDIA_EXPIRED]", mimetype=mimetype)
                        logger.debug("Idle OCR: Marked expired image %s as [MEDIA_EXPIRED]", msg_id)
                    else:
                        self.db.save_ocr_cache(msg_id, "[OCR_SKIPPED]", mimetype=mimetype)
                        logger.debug("Idle OCR: Skipping un-downloadable media %s (%s)", msg_id, err)
                    continue

                try:
                    with open(file_path, "rb") as f:
                        img_bytes = f.read()

                    if not img_bytes:
                        self.db.save_ocr_cache(msg_id, "[MEDIA_EXPIRED]", mimetype=mimetype)
                        continue

                    prompt = (
                        "Transcribe and visually describe all content, objects, text, tables, and colors in this image in detail:\n"
                        "1. Full text transcription (headings, labels, receipts, handwritten notes, printed text, numbers, codes, prices, dates, totals).\n"
                        "2. Visual objects & scene description (items, products, clothing types/colors/patterns, devices, documents, people, locations).\n"
                        "3. Key details (brands, logos, model numbers, payment/transaction IDs, addresses, status indicators).\n"
                        "Keep it structured and concise for search indexing."
                    )

                    analysis = self.gemini.ocr_file_content(
                        img_bytes, mimetype=detected_mimetype or mimetype, prompt=prompt
                    )

                    if analysis and not analysis.startswith("OCR processing error"):
                        self.db.save_ocr_cache(msg_id, analysis, mimetype=detected_mimetype or mimetype)
                        processed_in_cycle += 1
                        daily_ocr_count += 1
                        self._idle_worker_stats["processed"] += 1
                        self._idle_worker_stats["last_active"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        chat_jid = item.get("chat_jid") or ""
                        is_group = bool(item.get("is_group"))
                        chat_type = "DM" if not is_group else "Group"
                        if chat_jid == "status@broadcast":
                            chat_type = "Status Story"
                        elif chat_jid == self.my_canonical_jid or chat_jid == getattr(self, "my_lid_jid", ""):
                            chat_type = "Self-Chat"

                        logger.info(
                            "Idle OCR: Pre-cached %s image %s from '%s' (%d/%d in cycle, %d/%d today).",
                            chat_type,
                            msg_id,
                            chat_name,
                            processed_in_cycle,
                            len(candidates),
                            daily_ocr_count,
                            Config.IDLE_OCR_DAILY_BUDGET,
                        )

                        if daily_ocr_count >= Config.IDLE_OCR_DAILY_BUDGET:
                            logger.info(
                                "Idle OCR: Reached daily budget limit (%d/%d requests). Stopping cycle.",
                                daily_ocr_count,
                                Config.IDLE_OCR_DAILY_BUDGET,
                            )
                            break
                    else:
                        logger.warning("Idle OCR: Gemini returned error or empty for %s: %s", msg_id, analysis)
                        err_text = (analysis or "").lower()
                        if "resource_exhausted" in err_text or "429" in err_text or "quota" in err_text:
                            logger.warning(
                                "Idle OCR: Rate limit / daily quota reached (%s). Pausing idle background worker for 2 hours to protect user queries.",
                                analysis,
                            )
                            if self._stop_worker_event.wait(7200.0):
                                break
                            break
                        time.sleep(5.0)

                except Exception as ex:
                    logger.warning("Idle OCR error on image %s: %s", msg_id, ex)

                # Rate-limit delay before processing next item
                if self._stop_worker_event.wait(float(Config.IDLE_OCR_DELAY_SECONDS)):
                    break

            # Sleep 60 seconds between scan cycles
            if self._stop_worker_event.wait(60.0):
                break

    def stop(self) -> None:
        """Stop background workers and disconnect the WhatsApp client."""
        logger.info("Stopping WhatsApp bot and background workers...")
        self._stop_worker_event.set()
        if self._media_storage_thread and self._media_storage_thread.is_alive():
            self._media_storage_thread.join(timeout=2.0)
        if self._idle_worker_thread and self._idle_worker_thread.is_alive():
            self._idle_worker_thread.join(timeout=2.0)
        try:
            self._media_executor.shutdown(wait=False)
        except Exception:
            pass
        try:
            if hasattr(self, "client") and self.client.is_connected:
                self.client.disconnect()
        except Exception as e:
            logger.debug("Error disconnecting client: %s", e)

    def start(self) -> None:
        """Start the WhatsApp bot connection."""
        # If phone number is configured and client is not yet logged in, generate Pairing Code
        if self.phone_number and not self.client.is_logged_in:
            logger.info("Requesting WhatsApp pairing code for phone number: %s...", self.phone_number)
            try:
                code = self.client.PairPhone(
                    self.phone_number,
                    show_push_notification=True,
                    client_name=ClientName.LINUX,
                )
                print("\n" + "=" * 65)
                print(f"   WHATSAPP PAIRING CODE: {code}")
                print("=" * 65)
                print("Instructions:")
                print("1. Open WhatsApp on your phone.")
                print("2. Go to Settings > Linked Devices > Link a Device.")
                print("3. Tap 'Link with phone number instead'.")
                print(f"4. Enter the 8-character code: {code}\n")
                print("=" * 65 + "\n")
            except Exception as e:
                logger.error("Failed to generate pairing code: %s. Falling back to QR code...", e)

        # Launch media storage pre-download worker (Zero Gemini API Cost - keeps media accessible after 30 days)
        self._start_media_storage_worker()

        # Launch idle background OCR pre-caching worker (if manually enabled)
        self._start_idle_ocr_worker()

        logger.info("Starting WhatsApp client connection...")
        self.client.connect()

    def unlink(self, delete_data: bool = False) -> str:
        """Unlink the WhatsApp account, remove session credentials, and optionally delete all saved messages/data."""
        logger.info("Unlinking WhatsApp account (delete_data=%s)...", delete_data)
        self._stop_worker_event.set()
        if self._media_storage_thread and self._media_storage_thread.is_alive():
            self._media_storage_thread.join(timeout=2.0)
        if self._idle_worker_thread and self._idle_worker_thread.is_alive():
            self._idle_worker_thread.join(timeout=2.0)

        # 1. Server-side logout via neonize
        try:
            if hasattr(self, "client") and self.client.is_logged_in:
                self.client.logout()
        except Exception as e:
            logger.warning("Error during client.logout(): %s", e)

        try:
            if hasattr(self, "client") and self.client.is_connected:
                self.client.disconnect()
        except Exception as e:
            logger.warning("Error during client.disconnect(): %s", e)

        # 2. Remove session sqlite database files
        session_path = Path(self.session_db_path)
        for p in [session_path, Path(f"{session_path}-shm"), Path(f"{session_path}-wal")]:
            if p.exists():
                try:
                    p.unlink()
                    logger.info("Deleted session file: %s", p)
                except Exception as e:
                    logger.warning("Could not delete %s: %s", p, e)

        # 3. Optionally delete messages database and downloads
        if delete_data:
            msg_db_path = Path(Config.MESSAGES_DB_PATH)
            for p in [msg_db_path, Path(f"{msg_db_path}-shm"), Path(f"{msg_db_path}-wal")]:
                if p.exists():
                    try:
                        p.unlink()
                        logger.info("Deleted messages database: %s", p)
                    except Exception as e:
                        logger.warning("Could not delete %s: %s", p, e)

            downloads_dir = Path(Config.DOWNLOADS_DIR)
            if downloads_dir.exists():
                count = 0
                for f in downloads_dir.glob("*"):
                    if f.is_file():
                        try:
                            f.unlink()
                            count += 1
                        except Exception:
                            pass
                logger.info("Cleaned %d downloaded media files.", count)

        msg = "Account successfully unlinked."
        if delete_data:
            msg += " All saved messages, contacts, and downloaded files have been deleted."
        else:
            msg += " Saved messages and contact history have been preserved."
        return msg
