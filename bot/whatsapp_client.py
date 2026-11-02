import logging
import re
import time
from typing import Any
import segno
from pathlib import Path
from neonize.client import NewClient
from neonize.events import ConnectedEv, HistorySyncEv, MessageEv, LoggedOutEv
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message
from neonize.utils import ClientName

from .config import Config
from .database import DatabaseManager
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

        # Link Gemini agent back to this bot client for document sending & extraction tools
        self.gemini.bot_client = self

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
                logger.info("Processing HistorySync batch (type: %s)...", sync_type_name)

                # Sync pushnames to contacts table
                for pn in data.pushnames:
                    if pn.ID and pn.pushname:
                        self.db.upsert_contact(jid=pn.ID, push_name=pn.pushname)

                # Sync conversations and messages
                total_synced_msgs = 0
                for conv in data.conversations:
                    chat_jid = conv.ID
                    if not chat_jid:
                        continue

                    chat_name = conv.name or conv.displayName or ""
                    is_group = chat_jid.endswith("@g.us")

                    self.db.upsert_contact(jid=chat_jid, name=chat_name, is_group=is_group)

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

                            # Determine sender name:
                            sender_name = (getattr(wmsg, "pushName", "") or "").strip()
                            if not sender_name and sender_jid and sender_jid != chat_jid:
                                c_info = self.db.get_contact(sender_jid)
                                if c_info:
                                    sender_name = c_info.get("name") or c_info.get("push_name") or ""
                            if not sender_name:
                                sender_name = sender_jid.split("@")[0] if ("@" in sender_jid and not sender_jid.endswith("@g.us")) else "Participant"
                        else:
                            sender_jid = chat_jid
                            sender_name = (getattr(wmsg, "pushName", "") or "").strip() or chat_name

                        rich_info = extract_rich_message_info(wmsg.message)
                        timestamp = normalize_timestamp(wmsg.messageTimestamp)
                        raw_bytes = wmsg.message.SerializeToString() if hasattr(wmsg.message, "SerializeToString") else None

                        inserted = self.db.insert_message(
                            msg_id=msg_id,
                            chat_jid=chat_jid,
                            chat_name=chat_name,
                            sender_jid=sender_jid,
                            sender_name=sender_name,
                            text=rich_info.text,
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
                        if inserted:
                            total_synced_msgs += 1

                logger.info("HistorySync: successfully indexed %d historical messages.", total_synced_msgs)
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
                        sender_display_name = c_info.get("name") or c_info.get("push_name") or ""
                if not sender_display_name:
                    sender_display_name = sender_jid.split("@")[0] if ("@" in sender_jid and not is_group) else ("You" if is_from_me else "Participant")

                # Index message with full rich media metadata into database
                self.db.insert_message(
                    msg_id=msg_id,
                    chat_jid=chat_jid,
                    chat_name=None,
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
                f"• `{self.prefix} what did Patil Ayush message me?`\n"
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

        logger.info("Processing self-chat query: '%s'", query)

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

    def sync_address_book_contacts(self) -> int:
        """Sync contacts from whatsmeow's session store (whatsmeow_contacts) into messages database.
        whatsmeow captures all contacts saved in the user's phone contact book (full_name).
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
            s_conn.close()

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

            self.db.backfill_contact_names()
            logger.info("Synchronized %d phone address book contacts into database.", synced)
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
            elif "ogg" in mimetype.lower():
                safe_filename += ".ogg"
            elif "audio" in mimetype.lower():
                safe_filename += ".m4a"
            elif "video" in mimetype.lower():
                safe_filename += ".mp4"

        file_path = downloads_dir / f"{msg_id}_{safe_filename}"

        if file_path.exists() and file_path.stat().st_size > 0:
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
            binary_data = self.client.download_any(msg)
            if not binary_data:
                return None, filename, mimetype, "WhatsApp download returned empty data (media link may have expired on servers)."

            with open(file_path, "wb") as f:
                f.write(binary_data)

            logger.info("Successfully downloaded media file: %s (%d bytes)", file_path, len(binary_data))
            return str(file_path), filename, mimetype, None
        except Exception as e:
            logger.exception("Failed to download media for message %s: %s", msg_id, e)
            return None, filename, mimetype, f"WhatsApp download failed: {str(e)}"

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
        """Find matching document, download it, and extract text or run Gemini Multimodal OCR on scanned PDFs."""
        matches = self.db.find_documents(query=query, limit=10)
        if not matches:
            return f"No document matching '{query}' found in your chats."

        for doc_info in matches:
            msg_id = doc_info["id"]
            filename = doc_info.get("filename") or "document"
            chat_name = doc_info.get("chat") or "Unknown"

            file_path, real_filename, mimetype, err = self.download_media_by_id(msg_id)
            if err or not file_path:
                logger.warning("Could not download '%s' to read: %s (checking next match)", filename, err)
                continue

            # First, attempt local text extraction
            text = extract_document_text(file_path, max_chars=12000)

            # Check if this document is a scanned image, has low text, or user requested OCR/detailed inspection
            is_scanned = (
                not text
                or "scanned images without selectable text" in text.lower()
                or len(text.strip()) < 100
                or any(k in (query + " " + specific_question).lower() for k in ["ocr", "scan", "scanned", "image", "table", "tax", "gst", "total", "amount"])
            )

            if is_scanned and mimetype == "application/pdf":
                logger.info("Running Gemini Vision OCR on document %s...", real_filename or filename)
                try:
                    with open(file_path, "rb") as f:
                        file_bytes = f.read()
                    ocr_prompt = specific_question or f"Transcribe and analyze all details, tables, items, and figures in this document ({real_filename or filename}):"
                    ocr_text = self.gemini.ocr_file_content(file_bytes, mimetype=mimetype, prompt=ocr_prompt)
                    return (
                        f"--- Document Analysis & AI Vision OCR: {real_filename or filename} (Chat: '{chat_name}') ---\n"
                        f"{ocr_text}\n"
                        f"--- End of Document OCR ---"
                    )
                except Exception as e:
                    logger.warning("Gemini Vision OCR on %s failed: %s, falling back to text extraction", real_filename or filename, e)

            return (
                f"--- Document Content: {real_filename or filename} (from chat '{chat_name}') ---\n"
                f"{text}\n"
                f"--- End of Document Content ---"
            )

        return f"Found documents matching '{query}', but could not download their contents (media links may have expired on WhatsApp servers)."

    def read_document_by_query(self, query: str) -> str:
        """Alias for backward compatibility."""
        return self.read_and_ocr_document(query)

    def inspect_and_ocr_image(self, query_or_contact: str = "", user_question: str = "") -> str:
        """Find image or photo in chats, download it, and run Gemini Vision OCR to transcribe text and analyze content."""
        images = []
        if query_or_contact:
            # 1. Search by chat_name_or_jid (matches both DM and messages sent by the contact)
            images = self.db.find_voice_notes_and_media(media_type="image", query="", chat_name_or_jid=query_or_contact, limit=6)
            if not images:
                # 2. Try matching query keyword in captions
                images = self.db.find_voice_notes_and_media(media_type="image", query=query_or_contact, limit=6)

        if not images:
            images = self.db.find_voice_notes_and_media(media_type="image", query="", limit=5)

        if not images:
            return f"No images found matching '{query_or_contact}' in your chats."

        inspections = []
        for img in images:
            msg_id = img["id"]
            chat_name = img.get("chat") or "Unknown"
            caption = img.get("caption") or ""
            timestamp = img.get("timestamp") or ""

            file_path, real_filename, mimetype, err = self.download_media_by_id(msg_id)
            if err or not file_path:
                logger.warning("Could not download image %s: %s (trying next image)", msg_id, err)
                continue

            try:
                with open(file_path, "rb") as f:
                    img_bytes = f.read()

                logger.info("Running Gemini Vision OCR on image %s from chat '%s'...", msg_id, chat_name)
                ocr_prompt = user_question or f"Transcribe all text, numbers, receipts, handwritten notes, colors, patterns, and visual details from this image (shared in chat '{chat_name}'):"
                analysis = self.gemini.ocr_file_content(img_bytes, mimetype=mimetype or "image/jpeg", prompt=ocr_prompt)

                inspections.append(
                    f"--- Image Analysis ({real_filename or msg_id}) from chat '{chat_name}' ({timestamp}) ---\n"
                    f"Caption: {caption if caption else 'None'}\n\n"
                    f"{analysis}\n"
                    f"--- End of Image Analysis ---"
                )
                if len(inspections) >= 3:
                    break
            except Exception as e:
                logger.exception("Failed to run OCR on image %s: %s", msg_id, e)
                continue

        if inspections:
            return "\n\n".join(inspections)

        return f"Found matching images, but could not download them from WhatsApp servers (links may have expired)."

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

        logger.info("Starting WhatsApp client connection...")
        self.client.connect()
