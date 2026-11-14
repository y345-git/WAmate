import hashlib
import os
import logging
import re
import shutil
import subprocess
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
from .database import DatabaseManager, DOWNLOADABLE_MEDIA_TYPES, _format_timestamp
from .gemini_agent import GeminiAgent
from .utils import (
    LOCAL_DOC_INDEXABLE_EXTS,
    MessageDeduplicator,
    RichMessageInfo,
    clean_recipient_jid,
    extract_protocol_action,
    extract_rich_message_info,
    extract_document_text,
    format_for_whatsapp,
    is_meaningful_extracted_doc_text,
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
        self.my_non_ad_jid = None
        self.last_self_chat_jid = None
        self.history_sync_completed = False

        # Link Gemini agent back to this bot client for document sending & extraction tools
        if self.gemini:
            self.gemini.bot_client = self

        # Background workers synchronization & state
        self._stop_worker_event = threading.Event()
        self._user_active_event = threading.Event()
        self._local_index_thread: threading.Thread | None = None
        self._local_index_stats = {"ocr_processed": 0, "doc_processed": 0, "last_active": None}

        # Media Storage Worker & Auto-Download (Zero Gemini API Cost - keeps media accessible after 30 days)
        self._media_storage_thread: threading.Thread | None = None
        self._storage_worker_stats = {
            "stored": 0,
            "failed_or_expired": 0,
            "last_active": "Never",
        }
        self._media_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="MediaDownloader")

        # Async Query Executor so self-chat AI queries never block Neonize's MessageEv callback
        self._query_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="QueryWorker")
        self._active_queries = 0
        self._query_state_lock = threading.Lock()

        # Background campaign scheduler worker
        self._scheduler_thread: threading.Thread | None = None

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
                self.my_non_ad_jid = clean_recipient_jid(self.my_jid)
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
                pending_protocol_actions: list[tuple[dict[str, Any], bool, str, int, str]] = []
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

                        timestamp = normalize_timestamp(wmsg.messageTimestamp)
                        proto_action = extract_protocol_action(wmsg.message)
                        if proto_action:
                            if proto_action["action"] != "system":
                                pending_protocol_actions.append(
                                    (proto_action, is_from_me, sender_name, timestamp, chat_jid)
                                )
                            continue

                        rich_info = extract_rich_message_info(wmsg.message)
                        # Only persist raw_message BLOBs for downloadable media (avoids DB bloat on text messages)
                        raw_bytes = (
                            wmsg.message.SerializeToString()
                            if (
                                rich_info.media_type in DOWNLOADABLE_MEDIA_TYPES
                                and hasattr(wmsg.message, "SerializeToString")
                            )
                            else None
                        )

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
                            for act, act_me, act_sender, act_ts, act_chat in pending_protocol_actions:
                                self._apply_protocol_action(
                                    act, act_me, act_sender, act_ts, chat_jid=act_chat, allow_notify=False
                                )
                            pending_protocol_actions.clear()

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
                for act, act_me, act_sender, act_ts, act_chat in pending_protocol_actions:
                    self._apply_protocol_action(
                        act, act_me, act_sender, act_ts, chat_jid=act_chat, allow_notify=False
                    )
                pending_protocol_actions.clear()

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

                # Intercept Delete-for-Everyone revokes, edits, and system protocol frames before normal indexing
                proto_action = extract_protocol_action(message.Message)
                if proto_action:
                    self._apply_protocol_action(
                        proto_action,
                        is_from_me=is_from_me,
                        actor_name=sender_display_name,
                        fallback_ts=timestamp,
                        chat_jid=chat_jid,
                        client=client,
                        allow_notify=True,
                    )
                    return

                rich_info = extract_rich_message_info(message.Message)
                text = rich_info.text.strip()
                raw_bytes = (
                    message.Message.SerializeToString()
                    if (
                        rich_info.media_type in DOWNLOADABLE_MEDIA_TYPES
                        and hasattr(message.Message, "SerializeToString")
                    )
                    else None
                )

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
                    self.last_self_chat_jid = clean_recipient_jid(chat) or chat
                    command_text = (rich_info.clean_text or text).strip()
                    quoted_context = self._build_quoted_context(rich_info)
                    self._handle_self_chat_message(
                        client,
                        message,
                        msg_id,
                        command_text,
                        chat,
                        quoted_context=quoted_context,
                    )

            except Exception as e:
                logger.exception("Error handling MessageEv: %s", e)

        # 5. Logged Out Event
        @self.client.event(LoggedOutEv)
        def on_logged_out(client: NewClient, event: LoggedOutEv):
            logger.warning("Session logged out from WhatsApp. Re-authentication will be required.")

    def get_self_chat_jid(self) -> Any:
        """Resolve a valid, device-clean recipient JID (Device=0) for sending messages into self-chat."""
        candidates = [
            f"{self.my_user_number}@s.whatsapp.net" if getattr(self, "my_user_number", "") else None,
            getattr(self, "my_canonical_jid", None),
            getattr(self, "my_non_ad_jid", None),
            getattr(self, "last_self_chat_jid", None),
            getattr(self, "my_jid", None),
        ]
        for candidate in candidates:
            if candidate:
                clean = clean_recipient_jid(candidate)
                if clean:
                    if hasattr(clean, "Device"):
                        clean.Device = 0
                    if hasattr(clean, "RawAgent"):
                        clean.RawAgent = 0
                    return clean
        return None

    def _begin_query(self) -> None:
        with self._query_state_lock:
            self._active_queries += 1
            self._user_active_event.set()

    def _end_query(self) -> None:
        with self._query_state_lock:
            self._active_queries = max(0, self._active_queries - 1)
            if self._active_queries == 0:
                self._user_active_event.clear()

    def _send_self_chat_reply(
        self, client: NewClient, chat_jid: Any, original_message: MessageEv, text: str
    ) -> None:
        """Send a reply to self-chat with deduplication tracking and direct-send fallback."""
        try:
            resp = client.reply_message(text, original_message)
            if resp and hasattr(resp, "ID"):
                self.dedup.add(resp.ID)
            logger.info("Sent reply to self-chat.")
        except Exception as e:
            logger.warning("reply_message failed (%s), sending directly to chat...", e)
            try:
                dest = clean_recipient_jid(chat_jid) or chat_jid
                resp = client.send_message(dest, text)
                if resp and hasattr(resp, "ID"):
                    self.dedup.add(resp.ID)
                logger.info("Sent direct message to self-chat.")
            except Exception as e2:
                logger.exception("Failed to send response to self-chat: %s", e2)

    def _apply_protocol_action(
        self,
        proto_action: dict[str, Any],
        is_from_me: bool,
        actor_name: str,
        fallback_ts: int,
        chat_jid: str = "",
        client: NewClient | None = None,
        allow_notify: bool = False,
    ) -> None:
        """Apply REVOKE (Delete-for-Everyone) or MESSAGE_EDIT events to the Anti-Delete & Edit Vault."""
        action = proto_action.get("action")
        if action == "system":
            return

        target_id = proto_action.get("target_msg_id") or ""
        if not target_id:
            return
        event_ts = normalize_timestamp(proto_action.get("timestamp_ms") or fallback_ts)

        if action == "revoke":
            preserved = self.db.mark_message_deleted(
                target_id,
                deleted_by=actor_name,
                deleted_ts=event_ts,
                chat_jid=chat_jid or proto_action.get("remote_jid", ""),
            )
            if preserved and not preserved.get("already_recorded"):
                logger.info(
                    "Anti-Delete Vault: preserved deleted %s message %s in '%s' (by %s).",
                    preserved.get("media_type", "text"),
                    target_id,
                    preserved.get("chat", chat_jid),
                    preserved.get("deleted_by", actor_name),
                )
                target_self = self.get_self_chat_jid()
                if (
                    allow_notify
                    and Config.ANTI_DELETE_NOTIFY
                    and not is_from_me
                    and chat_jid != "status@broadcast"
                    and target_self
                    and client
                ):
                    orig = (preserved.get("original_text") or "").strip()
                    orig_preview = (orig[:300] + "...") if len(orig) > 300 else (orig or "[No text content]")
                    media_note = ""
                    if preserved.get("local_path") and not str(preserved["local_path"]).startswith("["):
                        fname = preserved.get("media_filename") or preserved.get("media_type") or "media"
                        media_note = f"\n• *Media Saved 💾:* `{fname}`"
                    alert = (
                        "🛡️ *Deleted Message Preserved*\n\n"
                        f"• *Chat:* {preserved.get('chat')}\n"
                        f"• *Sender:* {preserved.get('sender')}\n"
                        f"• *Deleted At:* {preserved.get('deleted_at')}\n"
                        f"• *Original Content:* {orig_preview}{media_note}"
                    )
                    try:
                        resp = client.send_message(target_self, alert)
                        if resp and hasattr(resp, "ID"):
                            self.dedup.add(resp.ID)
                    except Exception as notify_err:
                        logger.debug("Failed to send anti-delete alert: %s", notify_err)

        elif action == "edit":
            new_text = proto_action.get("new_text", "")
            edit_res = self.db.record_message_edit(
                target_id,
                new_text=new_text,
                edited_by=actor_name,
                edited_ts=event_ts,
                chat_jid=chat_jid or proto_action.get("remote_jid", ""),
            )
            if edit_res:
                logger.info(
                    "Anti-Edit Vault: preserved original wording for edited message %s in '%s'.",
                    target_id,
                    edit_res.get("chat", chat_jid),
                )

    def _build_quoted_context(self, rich_info: RichMessageInfo) -> str:
        """Resolve full context for a WhatsApp swipe-reply / quoted message."""
        if not rich_info.quoted_msg_id and not rich_info.quoted_snippet:
            return ""

        lines = ["[Quoted Message Context]"]
        db_msg = None
        if rich_info.quoted_msg_id:
            lines.append(f"• Message ID: {rich_info.quoted_msg_id}")
            db_msg = self.db.get_message_raw(rich_info.quoted_msg_id)

        if db_msg:
            chat_label = db_msg.get("chat_name") or db_msg.get("chat_jid") or "Unknown"
            sender_label = db_msg.get("sender_name") or "Unknown"
            ts_label = _format_timestamp(db_msg.get("timestamp"))
            media_type = db_msg.get("media_type") or "text"
            filename = db_msg.get("filename") or rich_info.quoted_filename
            q_text = (db_msg.get("text") or rich_info.quoted_snippet or "").strip()

            lines.append(f"• Chat: {chat_label}")
            lines.append(f"• Sender: {sender_label}")
            lines.append(f"• Timestamp: {ts_label}")
            lines.append(f"• Media Type: {media_type}")
            if filename:
                lines.append(f"• Filename: {filename}")
            if q_text:
                trimmed = (q_text[:2000] + "...") if len(q_text) > 2000 else q_text
                lines.append(f"• Quoted Content: {trimmed}")
        else:
            if rich_info.quoted_media_type:
                lines.append(f"• Media Type: {rich_info.quoted_media_type}")
            if rich_info.quoted_filename:
                lines.append(f"• Filename: {rich_info.quoted_filename}")
            if rich_info.quoted_snippet:
                trimmed = (rich_info.quoted_snippet[:2000] + "...") if len(rich_info.quoted_snippet) > 2000 else rich_info.quoted_snippet
                lines.append(f"• Quoted Content: {trimmed}")

        return "\n".join(lines)

    @staticmethod
    def _matches_command_trigger(text_lower: str, trigger: str) -> bool:
        return text_lower == trigger or text_lower.startswith(f"{trigger} ") or text_lower.startswith(f"{trigger}\n")

    def _process_self_chat_query(
        self,
        client: NewClient,
        message: MessageEv,
        msg_id: str,
        chat_jid: Any,
        query: str,
        quoted_context: str = "",
    ) -> None:
        """Execute an AI self-chat query in the background worker pool."""
        source = message.Info.MessageSource
        chat = source.Chat
        sender = source.Sender
        dest = clean_recipient_jid(chat_jid) or chat_jid

        try:
            logger.info("Processing self-chat query: '%s'", query)
            try:
                client.send_chat_presence(
                    dest, ChatPresence.CHAT_PRESENCE_COMPOSING, ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT
                )
            except Exception as pe:
                logger.debug("Could not send typing presence indicator: %s", pe)

            try:
                react_pending = client.build_reaction(chat, sender, msg_id, "⏳")
                client.send_message(dest, react_pending)
            except Exception as re_err:
                logger.debug("Could not send pending reaction: %s", re_err)

            raw_answer = self.gemini.ask(query, quoted_context=quoted_context)
            answer = format_for_whatsapp(raw_answer)
            self._send_self_chat_reply(client, dest, message, answer)
        except Exception as e:
            logger.exception("Unhandled error in async query worker: %s", e)
            self._send_self_chat_reply(client, dest, message, f"⚠️ *Error processing request:*\n`{e}`")
        finally:
            self._end_query()
            try:
                client.send_chat_presence(
                    dest, ChatPresence.CHAT_PRESENCE_PAUSED, ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT
                )
            except Exception:
                pass
            try:
                react_done = client.build_reaction(chat, sender, msg_id, "✅")
                client.send_message(dest, react_done)
            except Exception:
                pass

    def _handle_self_chat_message(
        self,
        client: NewClient,
        message: MessageEv,
        msg_id: str,
        text: str,
        chat_jid: Any,
        quoted_context: str = "",
    ) -> None:
        """Handle commands and queries received in the self-chat."""
        if not text:
            return

        text_lower = text.lower().strip()

        # Check for !clear / !new / !forget conversation memory reset commands (before !unlink)
        clear_triggers = ["!clear", "!new", "!forget", "!reset-chat"]
        if any(self._matches_command_trigger(text_lower, ct) for ct in clear_triggers):
            if self.dedup.contains(msg_id):
                return
            self.dedup.add(msg_id)
            cleared = self.gemini.clear_history()
            reply = (
                "🧹 *Conversation Memory Cleared*\n\n"
                f"Removed {cleared} recent Q&A turn(s) from active context. "
                "Your chat database and stored media remain intact — ask a new question anytime!"
            )
            self._send_self_chat_reply(client, chat_jid, message, reply)
            return

        # Check for !help / !commands command
        help_triggers = ["!help", "!commands", "!?"]
        if any(self._matches_command_trigger(text_lower, ht) for ht in help_triggers):
            if self.dedup.contains(msg_id):
                return
            self.dedup.add(msg_id)
            help_text = (
                "🤖 *WAmate Self-Chat Commands*\n\n"
                f"• `{self.prefix} <question>` — Search chats, inspect photos/PDFs/spreadsheets, summarize messages, or ask follow-ups\n"
                "• `!get <file or topic>` — Download & send a document directly here _(aliases: `!send`, `!fetch`, `!doc`, `!file`, `!download`)_\n"
                "• *Swipe-Reply Support* — Swipe/reply to any photo, PDF, document, or message with `!ask` or `!get` to act directly on it\n"
                "• `!deleted [name]` / `!edits [name]` — View deleted-for-everyone & edited messages saved by Anti-Delete Vault _(alias: `!vault`)_\n"
                "• `!clear` — Reset conversation memory to start a fresh topic _(aliases: `!new`, `!forget`)_\n"
                "• `!status` — View indexed messages, stored 30-day-safe media, OCR/Doc index, Vault & model info\n"
                "• `!help` — Show this command guide\n"
                "• `!unlink` — Disconnect WhatsApp session _(add `delete-data` to wipe local DB)_"
            )
            self._send_self_chat_reply(client, chat_jid, message, help_text)
            return

        # Check for !deleted / !edits / !vault Anti-Delete Vault commands
        vault_triggers = [("!deleted", "deleted"), ("!edits", "edited"), ("!vault", "all")]
        matched_vault = next(
            ((trig, mode) for trig, mode in vault_triggers if self._matches_command_trigger(text_lower, trig)),
            None,
        )
        if matched_vault:
            if self.dedup.contains(msg_id):
                return
            self.dedup.add(msg_id)
            trig, mode = matched_vault
            filter_query = text[len(trig):].strip()
            items = self.db.find_deleted_or_edited_messages(
                mode=mode,
                chat_name_or_jid=filter_query,
                limit=15,
                include_statuses=("status" in filter_query.lower()),
            )
            if not items:
                scope_note = f" matching _{filter_query}_" if filter_query else ""
                reply = (
                    f"🛡️ *Anti-Delete & Edit Vault*\n\n"
                    f"No {mode if mode != 'all' else 'deleted or edited'} messages recorded yet{scope_note}.\n"
                    "_Real-time Delete-for-Everyone and message edits are automatically archived here going forward._"
                )
            else:
                lines = [f"🛡️ *Anti-Delete & Edit Vault* ({len(items)} recent)"]
                if filter_query:
                    lines[0] += f" — Filter: _{filter_query}_"
                lines.append("")
                for idx, it in enumerate(items, 1):
                    badge = "🗑️ Deleted" if it["is_deleted"] else "✏️ Edited"
                    if it["is_deleted"] and it["is_edited"]:
                        badge = "🗑️✏️ Edited & Deleted"
                    when = it["deleted_at"] or it["edited_at"] or it["sent_at"]
                    orig = (it["original_text"] or it["current_text"] or "[Empty]").replace("\n", " ")
                    if len(orig) > 180:
                        orig = orig[:180].rstrip() + "..."
                    entry = f"{idx}. *{badge}* — *{it['sender']}* in _{it['chat']}_ ({when})\n   • *Original:* {orig}"
                    if it["is_edited"] and it["current_text"] and it["current_text"] != it["original_text"]:
                        curr = it["current_text"].replace("\n", " ")
                        if len(curr) > 150:
                            curr = curr[:150].rstrip() + "..."
                        entry += f"\n   • *Latest:* {curr}"
                    if it["media_saved_on_disk"]:
                        mname = it["media_filename"] or it["media_type"]
                        entry += f"\n   • *Media Preserved 💾:* `{mname}` (ID: `{it['id']}`)"
                    lines.append(entry)
                reply = "\n".join(lines)
            self._send_self_chat_reply(client, chat_jid, message, reply)
            return

        # Check for account unlinking / logout commands in self-chat
        unlink_triggers = ["!unlink", "!logout", "!reset"]
        if any(self._matches_command_trigger(text_lower, ut) for ut in unlink_triggers):
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
        if any(self._matches_command_trigger(text_lower, st) for st in status_triggers):
            if self.dedup.contains(msg_id):
                return
            self.dedup.add(msg_id)

            stats = self.db.get_stats()
            sync_state = "Fully indexed & Real-time active ✅" if getattr(self, "history_sync_completed", False) else "Indexed (Real-time active) ✅"
            local_ocr_state = Config.describe_local_ocr(stats.get("total_local_ocr", 0))
            local_doc_state = Config.describe_local_doc_index(stats.get("total_local_docs", 0))
            web_ui_obj = getattr(self, "web_ui", None)
            web_ui_state = Config.describe_web_ui(
                running=web_ui_obj.is_running if web_ui_obj is not None else True
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
            dedup_saved = stats.get("dedup_saved_files", 0)
            dedup_note = f", {dedup_saved} deduped" if dedup_saved > 0 else ""
            storage_state = (
                f"Active 🟢 ({stored_count} items, {stored_mb} MB on disk{dedup_note})"
                if Config.ENABLE_MEDIA_STORAGE
                else "Disabled"
            )
            key_count = len(getattr(self.gemini, "_clients", []) or [1])

            reply = (
                "📊 *WAmate Second Brain Status*\n\n"
                f"• *Messages Synced:* {stats['total_messages']:,}\n"
                f"• *Contacts & Groups:* {stats['total_contacts']:,} ({stats['total_groups']} groups)\n"
                f"• *History Date Range:* {stats['oldest_date']} → {stats['newest_date']}\n"
                f"• *Stored Media (30-day safe 💾):* {storage_state}\n"
                f"• *AI Vision & Voice OCR:* On-Demand ⚡ ({stats['total_ocr_cached']} cached)\n"
                f"• *Local CPU OCR:* {local_ocr_state}\n"
                f"• *Local Doc Index:* {local_doc_state}\n"
                f"• *Anti-Delete Vault:* Active 🛡️ ({stats.get('total_deleted', 0)} deleted, {stats.get('total_edited', 0)} edited)\n"
                f"• *Web Search UI:* {web_ui_state}\n"
                f"• *Database Size:* {stats['db_size_mb']} MB\n"
                f"• *Sync Status:* {sync_state}\n"
                f"• *AI Model:* `{Config.GEMINI_MODEL}` ({key_count} API key{'s' if key_count != 1 else ''})\n\n"
                "_Send `!help` to view all commands!_"
            )
            self._send_self_chat_reply(client, chat_jid, message, reply)
            return

        matched_prefix = None
        recognized_prefixes = [self.prefix.lower(), "!get", "!send", "!fetch", "!doc", "!file", "!download"]
        for pfx in recognized_prefixes:
            if self._matches_command_trigger(text_lower, pfx):
                matched_prefix = pfx
                break

        if not matched_prefix:
            return

        # Prevent duplicate processing or processing bot echos
        if self.dedup.contains(msg_id):
            return
        self.dedup.add(msg_id)

        raw_query = text[len(matched_prefix):].strip()
        is_doc_delivery_cmd = matched_prefix in ["!get", "!send", "!fetch", "!download", "!file", "!doc"]

        # Direct action prefixes like !get, !send, !file automatically map to document delivery queries
        if is_doc_delivery_cmd:
            if raw_query:
                query = f"send me the {raw_query}" if not raw_query.lower().startswith(("send", "get", "fetch", "deliver")) else raw_query
            elif quoted_context:
                query = "send me the quoted file or document shown below"
            else:
                query = ""
        else:
            if raw_query:
                query = raw_query
            elif quoted_context:
                query = "Analyze, summarize, or explain the quoted message/media below."
            else:
                query = ""

        if not query:
            hint = (
                "🤖 *WhatsApp AI Assistant*\n\n"
                "Ask me anything about your chats or request files directly!\n\n"
                f"• `{self.prefix} get me the latest invoices`\n"
                f"• `{self.prefix} send me the project proposal pdf`\n"
                "• `!get invoices`\n"
                f"• `{self.prefix} what did Alex message me?`\n"
                f"• `{self.prefix} summarize what happened today`\n"
                "• Send `!help` for all commands"
            )
            self._send_self_chat_reply(client, chat_jid, message, hint)
            return

        # Dispatch off the Neonize event callback onto the background query worker pool
        self._begin_query()
        try:
            self._query_executor.submit(
                self._process_self_chat_query,
                client,
                message,
                msg_id,
                chat_jid,
                query,
                quoted_context,
            )
        except Exception as e:
            self._end_query()
            logger.exception("Failed to submit self-chat query to worker pool: %s", e)

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

            # Also fetch and sync joined WhatsApp groups
            self.sync_joined_groups()
            return synced
        except Exception as e:
            logger.exception("Error syncing address book contacts from whatsmeow session: %s", e)
            return 0

    def sync_joined_groups(self) -> int:
        """Fetch all groups joined by this account via neonize client and ensure they exist in contacts."""
        try:
            if not getattr(self, "client", None) or not hasattr(self.client, "get_joined_groups"):
                return 0
            groups = self.client.get_joined_groups()
            if not groups:
                return 0
            to_upsert = []
            for g in groups:
                g_jid = jid_to_str(getattr(g, "JID", None))
                g_name = ""
                if hasattr(g, "GroupName") and getattr(g.GroupName, "Name", None):
                    g_name = g.GroupName.Name
                elif getattr(g, "Name", None):
                    g_name = g.Name
                if g_jid:
                    to_upsert.append({
                        "jid": g_jid,
                        "name": g_name or None,
                        "is_group": True,
                    })
            if to_upsert:
                self.db.upsert_contacts_batch(to_upsert)
                logger.info("Synchronized %d joined WhatsApp groups into database.", len(to_upsert))
                return len(to_upsert)
        except Exception as exc:
            logger.debug("sync_joined_groups notice: %s", exc)
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

            file_hash = hashlib.sha256(binary_data).hexdigest()
            existing_path = self.db.find_existing_local_path_by_hash(file_hash)
            if existing_path:
                self.db.update_message_local_path(msg_id, existing_path, media_hash=file_hash)
                logger.info("Deduplicated media file (SHA-256 match) for %s -> %s", msg_id, existing_path)
                self._maybe_index_local_media(msg_id, existing_path, mimetype)
                return existing_path, filename, mimetype, None

            with open(file_path, "wb") as f:
                f.write(binary_data)

            self.db.update_message_local_path(msg_id, str(file_path), media_hash=file_hash)
            logger.info("Successfully downloaded media file: %s (%d bytes)", file_path, len(binary_data))
            self._maybe_index_local_media(msg_id, str(file_path), mimetype)
            return str(file_path), filename, mimetype, None
        except Exception as e:
            err_str = str(e).strip()
            # Mark ALL download failures in database so it NEVER endlessly retries dead links
            self.db.update_message_local_path(msg_id, f"[DOWNLOAD_FAILED: {err_str[:40]}]")
            logger.debug("Media download failed for message %s: %s", msg_id, err_str)
            return None, filename, mimetype, f"WhatsApp download failed: {err_str}"

    def _resolve_document_matches(
        self, query: str = "", message_id: str = "", limit: int = 15
    ) -> list[dict[str, Any]]:
        """Resolve document candidate(s) by exact WhatsApp message_id first, or search query."""
        if message_id:
            raw = self.db.get_message_raw(message_id.strip())
            if raw:
                return [
                    {
                        "id": raw["id"],
                        "filename": raw.get("filename") or "document",
                        "chat": raw.get("chat_name") or raw.get("chat_jid") or "Unknown",
                    }
                ]
        if query:
            return self.db.find_documents(query=query, limit=limit)
        return []

    def send_document_by_query(
        self, query: str = "", target_jid: Any = None, max_count: int = 1, message_id: str = ""
    ) -> str:
        """Find matching document(s), download them, and send the actual file(s) directly into self-chat.
        If a candidate file link has expired on WhatsApp CDN (410), automatically tries subsequent matches.
        """
        dest_jid = clean_recipient_jid(target_jid) if target_jid else self.get_self_chat_jid()
        if not dest_jid:
            return "Cannot send document: self-chat destination JID is not yet available."

        matches = self._resolve_document_matches(query=query, message_id=message_id, limit=15)
        if not matches:
            target_desc = message_id or query
            return f"No document matching '{target_desc}' found in your chats."

        # Support sending multiple files for plural requests ("invoices", "bills", "files", "all")
        plural_keywords = ["invoices", "bills", "files", "documents", "pdfs", "all", "reports", "forms"]
        if not message_id and max_count <= 1 and any(pw in query.lower() for pw in plural_keywords):
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

    def send_media_by_id_to_self(self, msg_id: str) -> tuple[bool, str]:
        """Deliver a stored file, media attachment, or message snippet directly into WhatsApp self-chat."""
        row = self.db.get_message_media_info(msg_id)
        if not row:
            return False, "Message not found."

        dest_jid = clean_recipient_jid(self.get_self_chat_jid())
        if not dest_jid:
            return False, "WhatsApp self-chat is not connected yet."
        if hasattr(dest_jid, "Device"):
            dest_jid.Device = 0
        if hasattr(dest_jid, "RawAgent"):
            dest_jid.RawAgent = 0

        mt = (row.get("media_type") or "text").lower()
        chat_name = row.get("chat_name") or row.get("chat_jid") or "Unknown"
        sender_name = "You" if row.get("is_from_me") else (row.get("sender_name") or chat_name)
        text_body = (row.get("text") or "").strip()

        if mt in ("text", "link", "contact", "poll", "location"):
            if not text_body:
                return False, "Message has no text or attachment to send."
            quote_msg = f"📌 *{sender_name}* in _{chat_name}_\n\n{text_body}"
            try:
                resp = self.client.send_message(dest_jid, quote_msg)
                if resp and hasattr(resp, "ID"):
                    self.dedup.add(resp.ID)
                return True, "Sent message snippet to WhatsApp self-chat."
            except Exception as exc:
                logger.exception("Failed sending snippet %s to self-chat: %s", msg_id, exc)
                return False, f"Failed to send message: {exc}"

        file_path, real_filename, mimetype, err = self.download_media_by_id(msg_id)
        if err or not file_path:
            return False, err or "Media file is unavailable on disk."

        fname = real_filename or row.get("media_filename") or Path(file_path).name
        mime = mimetype or row.get("media_mimetype") or "application/octet-stream"
        caption = f"📎 *{fname}*\nChat: _{chat_name}_"
        if text_body and text_body != fname:
            caption += f"\n{text_body[:300]}"

        try:
            resp = None
            if (mt == "image" or mime.startswith("image/")) and mt != "sticker":
                resp = self.client.send_image(to=dest_jid, file=file_path, caption=caption)
            elif mt == "video" or mime.startswith("video/"):
                resp = self.client.send_video(to=dest_jid, file=file_path, caption=caption)
            elif mt in ("voice_note", "audio") or mime.startswith("audio/"):
                resp = self.client.send_audio(to=dest_jid, file=file_path, ptt=(mt == "voice_note"))
            elif mt == "sticker":
                resp = self.client.send_sticker(to=dest_jid, file=file_path)
            else:
                resp = self.client.send_document(
                    to=dest_jid,
                    file=file_path,
                    filename=fname,
                    mimetype=mime,
                    caption=caption,
                )
            if resp and hasattr(resp, "ID"):
                self.dedup.add(resp.ID)
            logger.info("Sent %s '%s' to WhatsApp self-chat from Web UI.", mt, fname)
            return True, f"Sent '{fname}' to WhatsApp self-chat."
        except Exception as exc:
            logger.exception("Failed sending media %s to self-chat: %s", msg_id, exc)
            return False, f"WhatsApp send failed: {exc}"

    def send_broadcast_message(
        self,
        recipients: list[dict[str, Any]],
        message: str,
        attachment_path: str = "",
        attachment_filename: str = "",
        attachment_mimetype: str = "",
        min_delay: float = 1.0,
        max_delay: float = 2.0,
    ) -> dict[str, Any]:
        """Broadcast a message and optional attachment to multiple recipients with safe pacing."""
        import random
        start_ts = int(time.time())
        t0 = time.monotonic()
        sent_count = 0
        failed_count = 0
        logs = []

        total = len(recipients)
        msg_text = (message or "").strip()
        att_path = Path(attachment_path) if attachment_path and Path(attachment_path).is_file() else None

        for idx, rec in enumerate(recipients):
            if self._stop_worker_event.is_set():
                logs.append({
                    "jid": rec.get("jid", ""),
                    "name": rec.get("name", ""),
                    "phone": rec.get("phone", ""),
                    "status": "cancelled",
                    "error": "Bot stopped",
                    "timestamp": int(time.time()),
                })
                failed_count += 1
                continue

            raw_jid = rec.get("jid") or rec.get("phone") or ""
            dest = clean_recipient_jid(raw_jid)
            rec_name = rec.get("name") or rec.get("push_name") or rec.get("phone") or str(raw_jid)
            phone = rec.get("phone") or ""

            if not dest:
                logs.append({
                    "jid": str(raw_jid),
                    "name": rec_name,
                    "phone": phone,
                    "status": "failed",
                    "error": "Invalid destination JID",
                    "timestamp": int(time.time()),
                })
                failed_count += 1
                continue

            try:
                resp = None
                if att_path:
                    fname = attachment_filename or att_path.name
                    mime = attachment_mimetype or "application/octet-stream"
                    if mime.startswith("image/"):
                        resp = self.client.send_image(to=dest, file=str(att_path), caption=msg_text)
                    elif mime.startswith("video/"):
                        resp = self.client.send_video(to=dest, file=str(att_path), caption=msg_text)
                    elif mime.startswith("audio/"):
                        resp = self.client.send_audio(to=dest, file=str(att_path))
                        if msg_text:
                            self.client.send_message(dest, msg_text)
                    else:
                        resp = self.client.send_document(
                            to=dest,
                            file=str(att_path),
                            filename=fname,
                            mimetype=mime,
                            caption=msg_text,
                        )
                else:
                    resp = self.client.send_message(dest, msg_text)

                if resp and hasattr(resp, "ID"):
                    self.dedup.add(resp.ID)

                sent_count += 1
                logs.append({
                    "jid": str(raw_jid),
                    "name": rec_name,
                    "phone": phone,
                    "status": "sent",
                    "error": "",
                    "timestamp": int(time.time()),
                })
                logger.info("Broadcast sent [%d/%d] to %s (%s)", idx + 1, total, rec_name, raw_jid)
            except Exception as exc:
                failed_count += 1
                logs.append({
                    "jid": str(raw_jid),
                    "name": rec_name,
                    "phone": phone,
                    "status": "failed",
                    "error": str(exc),
                    "timestamp": int(time.time()),
                })
                logger.warning("Broadcast failed [%d/%d] to %s: %s", idx + 1, total, raw_jid, exc)

            # Safe inter-message delay
            if idx < total - 1 and not self._stop_worker_event.is_set():
                delay = random.uniform(min_delay, max_delay)
                self._stop_worker_event.wait(delay)

        elapsed = time.monotonic() - t0
        return {
            "total": total,
            "sent": sent_count,
            "failed": failed_count,
            "duration": elapsed,
            "started_at": start_ts,
            "finished_at": int(time.time()),
            "logs": logs,
        }

    def dispatch_scheduled_campaign(self, camp_id: str) -> tuple[bool, str]:
        """Execute a scheduled campaign immediately."""
        camp = self.db.get_scheduled_campaign(camp_id)
        if not camp:
            return False, "Campaign not found"
        if camp["status"] in ("sent", "sending", "cancelled"):
            return False, f"Campaign is already {camp['status']}"

        self.db.update_scheduled_campaign_status(camp_id, "sending")
        audience = camp.get("target_audience", "both")
        recipients = []

        if audience == "specific":
            recipients = camp.get("specific_recipients", [])
        else:
            roles = camp.get("roles", [])
            recipients = self.db.lookup_recipients_by_roles(roles, target_audience=audience)

        if not recipients:
            self.db.update_scheduled_campaign_status(
                camp_id, "failed", sent_count=0, failed_count=0, total_targets=0,
                logs=[{"error": "No matching recipients found for selected roles/audience"}]
            )
            return False, "No matching recipients found"

        result = self.send_broadcast_message(
            recipients=recipients,
            message=camp.get("message", ""),
            attachment_path=camp.get("attachment_path", ""),
            attachment_filename=camp.get("attachment_name", ""),
            attachment_mimetype=camp.get("attachment_mimetype", ""),
        )

        final_status = "sent" if result["sent"] > 0 else "failed"
        self.db.update_scheduled_campaign_status(
            camp_id,
            status=final_status,
            sent_count=result["sent"],
            failed_count=result["failed"],
            total_targets=result["total"],
            logs=result["logs"],
        )

        # Record in campaign history
        self.db.record_campaign_history(
            message=camp.get("message", ""),
            target_audience=audience,
            roles=camp.get("roles", []),
            recipients_count=result["total"],
            sent_count=result["sent"],
            failed_count=result["failed"],
            duration_sec=result["duration"],
            started_at=result["started_at"],
            finished_at=result["finished_at"],
            logs=result["logs"],
            attachment_path=camp.get("attachment_path", ""),
            attachment_filename=camp.get("attachment_name", ""),
            campaign_type="scheduled",
            camp_id=camp_id,
        )

        return True, f"Broadcast complete: {result['sent']} sent, {result['failed']} failed."

    def trigger_scheduled_campaign_now(self, camp_id: str) -> tuple[bool, str]:
        """Manually trigger immediate execution of a scheduled campaign."""
        return self.dispatch_scheduled_campaign(camp_id)

    def _start_scheduler_worker(self) -> None:
        """Start the background campaign scheduler thread."""
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            return
        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop,
            name="BroadcastScheduler",
            daemon=True,
        )
        self._scheduler_thread.start()
        logger.info("Broadcast scheduler background thread launched.")

    def _scheduler_loop(self) -> None:
        """Background loop that regularly checks and executes due scheduled campaigns."""
        while not self._stop_worker_event.is_set():
            if self._stop_worker_event.wait(15.0):
                break
            try:
                now_ts = int(time.time())
                due = self.db.get_due_scheduled_campaigns(now_ts)
                for camp in due:
                    if self._stop_worker_event.is_set():
                        break
                    logger.info("Executing due scheduled campaign: %s", camp["id"])
                    self.dispatch_scheduled_campaign(camp["id"])
            except Exception as e:
                logger.debug("Error in scheduler loop: %s", e)

    def read_and_ocr_document(
        self, query: str = "", specific_question: str = "", message_id: str = ""
    ) -> str:
        """Find matching document, download it, and extract structured text (Excel, Word, PowerPoint, PDF, CSV, Text) or run Gemini Vision OCR on scanned PDFs."""
        matches = self._resolve_document_matches(query=query, message_id=message_id, limit=10)
        if not matches:
            target_desc = message_id or query
            return f"No document matching '{target_desc}' found in your chats."

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

    def read_document_by_query(self, query: str = "", message_id: str = "") -> str:
        """Alias for backward compatibility."""
        return self.read_and_ocr_document(query=query, message_id=message_id)

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

    def _maybe_index_local_ocr(
        self,
        msg_id: str,
        file_path: str,
        mimetype: str,
        path_cache: dict[str, str] | None = None,
    ) -> bool:
        """Run zero-API-cost offline tesseract OCR on stored image files and mark completion in DB."""
        if not Config.ENABLE_LOCAL_OCR:
            return False
        ext = os.path.splitext(file_path)[1].lower() if file_path else ""
        is_image = bool(
            (mimetype and mimetype.lower().startswith("image/"))
            or ext in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
        )
        if not is_image:
            return False
        if not file_path or not os.path.exists(file_path):
            self.db.mark_local_ocr_done(msg_id, "")
            return False
        tesseract_bin = Config.get_tesseract_path()
        if not tesseract_bin:
            return False

        if path_cache is not None and file_path in path_cache:
            cleaned = path_cache[file_path]
            self.db.mark_local_ocr_done(msg_id, cleaned)
            if len(cleaned) >= 8:
                self._local_index_stats["ocr_processed"] += 1
                self._local_index_stats["last_active"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                return True
            return False

        try:
            proc = subprocess.run(
                [tesseract_bin, file_path, "stdout", "--psm", "6"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            cleaned = " ".join(proc.stdout.split()) if proc.returncode == 0 and proc.stdout else ""
            if path_cache is not None:
                path_cache[file_path] = cleaned
            self.db.mark_local_ocr_done(msg_id, cleaned)
            if len(cleaned) >= 8:
                self._local_index_stats["ocr_processed"] += 1
                self._local_index_stats["last_active"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                logger.debug("Indexed local CPU OCR text (%d chars) for %s", len(cleaned), msg_id)
                return True
        except Exception as e:
            logger.debug("Local tesseract OCR skipped for %s: %s", msg_id, e)
            self.db.mark_local_ocr_done(msg_id, "")
        return False

    def _maybe_index_local_doc(
        self,
        msg_id: str,
        file_path: str,
        path_cache: dict[str, str] | None = None,
    ) -> bool:
        """Run zero-API-cost pure-Python text extraction on stored documents and mark completion in DB."""
        if not Config.ENABLE_LOCAL_DOC_INDEX:
            return False
        if not file_path or not os.path.exists(file_path):
            self.db.mark_local_doc_done(msg_id, "")
            return False
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in LOCAL_DOC_INDEXABLE_EXTS:
            self.db.mark_local_doc_done(msg_id, "")
            return False
        try:
            if os.path.getsize(file_path) > 25 * 1024 * 1024:
                self.db.mark_local_doc_done(msg_id, "")
                return False
        except OSError:
            self.db.mark_local_doc_done(msg_id, "")
            return False

        if path_cache is not None and file_path in path_cache:
            cleaned = path_cache[file_path]
            self.db.mark_local_doc_done(msg_id, cleaned)
            if len(cleaned) >= 20:
                self._local_index_stats["doc_processed"] += 1
                self._local_index_stats["last_active"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                return True
            return False

        try:
            raw_text = extract_document_text(file_path, max_chars=8000)
            cleaned = " ".join(raw_text.split()) if is_meaningful_extracted_doc_text(raw_text) else ""
            if path_cache is not None:
                path_cache[file_path] = cleaned
            self.db.mark_local_doc_done(msg_id, cleaned)
            if len(cleaned) >= 20:
                self._local_index_stats["doc_processed"] += 1
                self._local_index_stats["last_active"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                logger.debug("Indexed local document text (%d chars) for %s", len(cleaned), msg_id)
                return True
        except Exception as e:
            logger.debug("Local document indexing skipped for %s: %s", msg_id, e)
            self.db.mark_local_doc_done(msg_id, "")
        return False

    def _maybe_index_local_media(self, msg_id: str, file_path: str, mimetype: str) -> None:
        """Index newly downloaded image or document locally without spending Gemini quota."""
        ext = os.path.splitext(file_path)[1].lower() if file_path else ""
        if (mimetype and mimetype.lower().startswith("image/")) or ext in {
            ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"
        }:
            self._maybe_index_local_ocr(msg_id, file_path, mimetype)
        elif ext in LOCAL_DOC_INDEXABLE_EXTS:
            self._maybe_index_local_doc(msg_id, file_path)

    def _transcribe_single_audio_item(
        self,
        msg_id: str,
        chat_name: str,
        sender_name: str,
        timestamp: str,
        default_mime: str = "audio/ogg",
        user_question: str = "",
    ) -> str | None:
        """Transcribe one voice note or audio clip (cache-first) and return formatted output."""
        cached = self.db.get_ocr_cache(msg_id)
        if cached:
            logger.info("Found cached transcript for audio %s (instant local lookup)", msg_id)
            return (
                f"--- Voice Note Transcript ({msg_id}) from '{chat_name}' ({timestamp}) [Cached] ---\n"
                f"Sender: {sender_name}\n\n"
                f"{cached}\n"
                f"--- End of Transcript ---"
            )

        file_path, real_filename, detected_mime, err = self.download_media_by_id(msg_id)
        if err or not file_path:
            return None

        try:
            with open(file_path, "rb") as f:
                audio_bytes = f.read()
            if not audio_bytes:
                return None
            mime = detected_mime or default_mime or "audio/ogg"
            transcript = self.gemini.transcribe_audio_content(
                audio_bytes, mimetype=mime, question=user_question
            )
            if transcript and not transcript.startswith("Audio transcription error"):
                self.db.save_ocr_cache(msg_id, transcript, mimetype=mime)
            return (
                f"--- Voice Note Transcript ({real_filename or msg_id}) from '{chat_name}' ({timestamp}) ---\n"
                f"Sender: {sender_name}\n\n"
                f"{transcript}\n"
                f"--- End of Transcript ---"
            )
        except Exception as e:
            logger.exception("Failed to transcribe audio %s: %s", msg_id, e)
            return None

    def transcribe_voice_note(
        self,
        message_id: str = "",
        contact_or_chat: str = "",
        topic_or_query: str = "",
        user_question: str = "",
    ) -> str:
        """Find a voice note or audio message, read/download it, and transcribe spoken words via Gemini."""
        if message_id:
            msg_id = message_id.strip()
            raw = self.db.get_message_raw(msg_id)
            if not raw:
                return f"No voice note or audio message found in database with ID '{msg_id}'."
            res = self._transcribe_single_audio_item(
                msg_id=msg_id,
                chat_name=raw.get("chat_name") or raw.get("chat_jid") or "Unknown",
                sender_name=raw.get("sender_name") or "Unknown",
                timestamp=_format_timestamp(raw.get("timestamp")),
                default_mime=raw.get("mimetype") or "audio/ogg",
                user_question=user_question,
            )
            return res or f"Could not download or transcribe audio message '{msg_id}' (media may have expired)."

        candidates: list[dict[str, Any]] = []
        for mtype in ("voice_note", "audio"):
            candidates.extend(
                self.db.find_voice_notes_and_media(
                    media_type=mtype,
                    query=topic_or_query,
                    chat_name_or_jid=contact_or_chat,
                    limit=10,
                )
            )
        if not candidates and contact_or_chat and topic_or_query:
            for mtype in ("voice_note", "audio"):
                candidates.extend(
                    self.db.find_voice_notes_and_media(
                        media_type=mtype, query="", chat_name_or_jid=contact_or_chat, limit=10
                    )
                )

        if not candidates:
            target_desc = contact_or_chat or topic_or_query or "your chats"
            return f"No voice notes or audio messages found matching '{target_desc}'."

        seen_ids: set[str] = set()
        outputs: list[str] = []
        for item in candidates:
            mid = item["id"]
            if mid in seen_ids:
                continue
            seen_ids.add(mid)
            res = self._transcribe_single_audio_item(
                msg_id=mid,
                chat_name=item.get("chat") or "Unknown",
                sender_name=item.get("sender") or "Unknown",
                timestamp=item.get("timestamp") or "",
                user_question=user_question,
            )
            if res:
                outputs.append(res)
                if len(outputs) >= 3:
                    break

        if outputs:
            return "\n\n".join(outputs)
        return "Found matching voice note(s), but could not download them (links may have expired on WhatsApp)."

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
            if Config.STATUS_MEDIA_RETENTION_DAYS > 0:
                try:
                    pruned = self.db.prune_expired_status_media(Config.STATUS_MEDIA_RETENTION_DAYS)
                    if pruned:
                        logger.info("Media Retention: pruned %d expired status file(s) older than %d days.", pruned, Config.STATUS_MEDIA_RETENTION_DAYS)
                except Exception as pe:
                    logger.debug("Media Retention check skipped: %s", pe)

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

    def _start_local_index_worker(self) -> None:
        """Launch zero-API-cost Local Background Indexing worker for stored images and documents."""
        tesseract_bin = Config.get_tesseract_path() if Config.ENABLE_LOCAL_OCR else None
        ocr_ready = bool(Config.ENABLE_LOCAL_OCR and tesseract_bin)
        doc_ready = bool(Config.ENABLE_LOCAL_DOC_INDEX)

        if Config.ENABLE_LOCAL_OCR and not tesseract_bin:
            logger.warning(
                "ENABLE_LOCAL_OCR=true, but 'tesseract' binary was not found in PATH! "
                "Image CPU OCR is paused. Install it on Ubuntu/Debian via: sudo apt update && sudo apt install -y tesseract-ocr "
                "(On-Demand Gemini Vision OCR still works during '!ask' queries.)"
            )
        elif not Config.ENABLE_LOCAL_OCR:
            logger.info(
                "Local CPU Image OCR disabled (ENABLE_LOCAL_OCR=false). On-Demand AI Vision & Voice OCR remains active during '!ask' queries."
            )

        if not ocr_ready and not doc_ready:
            logger.info("Local Document Text Indexing disabled (ENABLE_LOCAL_DOC_INDEX=false).")
            return

        if self._local_index_thread and self._local_index_thread.is_alive():
            return

        self._stop_worker_event.clear()
        self._local_index_thread = threading.Thread(
            target=self._local_index_worker, daemon=True, name="LocalIndexWorker"
        )
        self._local_index_thread.start()
        logger.info(
            "Local Background Index Worker started (image_ocr=%s, doc_index=%s, 0 Gemini API calls).",
            "ON" if ocr_ready else "OFF",
            "ON" if doc_ready else "OFF",
        )

    def _local_index_worker(self) -> None:
        """Background thread that gently backfills offline image OCR and local document text for stored files."""
        logger.info("Local Background Index worker active. Waiting 25s for startup warmup...")
        if self._stop_worker_event.wait(25.0):
            return

        path_cache: dict[str, str] = {}
        while not self._stop_worker_event.is_set():
            if self._user_active_event.is_set():
                if self._stop_worker_event.wait(3.0):
                    break
                continue

            tesseract_bin = Config.get_tesseract_path() if Config.ENABLE_LOCAL_OCR else None
            ocr_candidates: list[dict[str, Any]] = []
            doc_candidates: list[dict[str, Any]] = []

            if tesseract_bin:
                try:
                    ocr_candidates = self.db.get_local_ocr_candidates(
                        limit=25, self_jid=self.my_canonical_jid or ""
                    )
                except Exception as e:
                    logger.warning("Local CPU OCR: error querying candidates: %s", e)

            if Config.ENABLE_LOCAL_DOC_INDEX:
                try:
                    doc_candidates = self.db.get_local_doc_candidates(
                        limit=25, self_jid=self.my_canonical_jid or ""
                    )
                except Exception as e:
                    logger.warning("Local Doc Index: error querying candidates: %s", e)

            if not ocr_candidates and not doc_candidates:
                if self._stop_worker_event.wait(60.0):
                    break
                continue

            logger.info(
                "Local Background Index batch: %d image(s), %d document(s) queued offline...",
                len(ocr_candidates),
                len(doc_candidates),
            )
            if len(path_cache) > 2000:
                path_cache.clear()

            work_queue = [("ocr", item) for item in ocr_candidates] + [("doc", item) for item in doc_candidates]
            for kind, item in work_queue:
                if self._stop_worker_event.is_set():
                    break
                while self._user_active_event.is_set():
                    if self._stop_worker_event.wait(3.0):
                        break
                if self._stop_worker_event.is_set():
                    break

                msg_id = item["id"]
                file_path = item.get("local_path") or ""
                if kind == "ocr":
                    mimetype = item.get("media_mimetype") or "image/jpeg"
                    self._maybe_index_local_ocr(msg_id, file_path, mimetype, path_cache=path_cache)
                else:
                    self._maybe_index_local_doc(msg_id, file_path, path_cache=path_cache)

                # Yield CPU slightly between local indexing calls
                if self._stop_worker_event.wait(0.2):
                    break

    def stop(self) -> None:
        """Stop background workers and disconnect the WhatsApp client."""
        logger.info("Stopping WhatsApp bot and background workers...")
        self._stop_worker_event.set()
        if self._media_storage_thread and self._media_storage_thread.is_alive():
            self._media_storage_thread.join(timeout=2.0)
        if self._local_index_thread and self._local_index_thread.is_alive():
            self._local_index_thread.join(timeout=2.0)
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            self._scheduler_thread.join(timeout=2.0)
        try:
            self._media_executor.shutdown(wait=False)
        except Exception:
            pass
        try:
            self._query_executor.shutdown(wait=False)
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

        # Launch zero-API Local Background Index worker (doc text extraction + optional image CPU OCR)
        self._start_local_index_worker()

        # Launch broadcast campaign scheduler worker
        self._start_scheduler_worker()

        logger.info("Starting WhatsApp client connection...")
        self.client.connect()

    def unlink(self, delete_data: bool = False) -> str:
        """Unlink the WhatsApp account, remove session credentials, and optionally delete all saved messages/data."""
        logger.info("Unlinking WhatsApp account (delete_data=%s)...", delete_data)
        self._stop_worker_event.set()
        for executor in (self._media_executor, self._query_executor):
            try:
                executor.shutdown(wait=False)
            except Exception:
                pass
        if self._media_storage_thread and self._media_storage_thread.is_alive():
            self._media_storage_thread.join(timeout=2.0)
        if self._local_index_thread and self._local_index_thread.is_alive():
            self._local_index_thread.join(timeout=2.0)
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            self._scheduler_thread.join(timeout=2.0)

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
