import re
import json
import time
import copy
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Set, Any
from dataclasses import dataclass, field
from neonize.proto.Neonize_pb2 import JID
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message

try:
    from neonize.utils.jid import JIDToNonAD, build_jid
except ImportError:
    JIDToNonAD = None
    build_jid = None


@dataclass
class RichMessageInfo:
    text: str
    media_type: str = "text"
    media_filename: str = ""
    media_mimetype: str = ""
    media_size: int = 0
    media_metadata: dict[str, Any] = field(default_factory=dict)
    clean_text: str = ""
    quoted_msg_id: str = ""
    quoted_participant: str = ""
    quoted_media_type: str = ""
    quoted_filename: str = ""
    quoted_snippet: str = ""


class MessageDeduplicator:
    """In-memory cache to prevent processing bot replies or duplicate messages."""

    def __init__(self, ttl_seconds: int = 120):
        self._sent_ids: Set[str] = set()
        self._timestamps: dict[str, float] = {}
        self.ttl = ttl_seconds

    def add(self, msg_id: str) -> None:
        now = time.time()
        self._sent_ids.add(msg_id)
        self._timestamps[msg_id] = now
        self._cleanup(now)

    def contains(self, msg_id: str) -> bool:
        return msg_id in self._sent_ids

    def _cleanup(self, now: float) -> None:
        expired = [mid for mid, ts in self._timestamps.items() if now - ts > self.ttl]
        for mid in expired:
            self._sent_ids.discard(mid)
            self._timestamps.pop(mid, None)


def clean_phone_number(phone: str) -> str:
    """Remove non-digits from a phone number."""
    return re.sub(r"\D", "", phone)


def normalize_timestamp(ts: int | float | None) -> int:
    """Normalize timestamp to seconds if given in milliseconds."""
    if not ts:
        return int(time.time())
    ts = int(ts)
    if ts > 10_000_000_000:
        return ts // 1000
    return ts


def jid_to_str(jid: JID | None) -> str:
    """Format JID object to canonical user@server string."""
    if not jid:
        return ""
    user = getattr(jid, "User", "")
    server = getattr(jid, "Server", "s.whatsapp.net")
    return f"{user}@{server}" if user else ""


def clean_recipient_jid(dest: Any) -> Any:
    """Normalize a JID object or phone/JID string to ensure no device part is present (Device=0, RawAgent=0).

    WhatsApp's protocol (whatsmeow / neonize) requires that message recipients (direct or self-chat)
    be a user JID with no device part. Linked companion clients have Device > 0 on device.JID,
    so sending directly to device.JID fails with:
    'message recipient must be a user JID with no device part'.
    """
    if dest is None:
        return None

    # Handle protobuf JID instance or duck-typed object with Device attribute
    if isinstance(dest, JID) or hasattr(dest, "Device"):
        cleaned = JIDToNonAD(dest) if JIDToNonAD is not None else copy.deepcopy(dest)
        if hasattr(cleaned, "Device"):
            cleaned.Device = 0
        if hasattr(cleaned, "RawAgent"):
            cleaned.RawAgent = 0
        user = getattr(cleaned, "User", "")
        if ":" in user:
            cleaned.User = user.split(":", 1)[0]
        return cleaned

    s = str(dest).strip()
    if not s:
        return None

    if "@" in s:
        user_part, srv = s.rsplit("@", 1)
        if ":" in user_part:
            user_part = user_part.split(":", 1)[0]
        if "." in user_part and srv != "g.us":
            user_part = user_part.split(".", 1)[0]
        if build_jid is not None:
            return build_jid(user_part, srv)
        return JID(User=user_part, Server=srv, Device=0, RawAgent=0, Integrator=0, IsEmpty=False)

    if ":" in s:
        s = s.split(":", 1)[0]
    if build_jid is not None:
        return build_jid(s)
    return JID(User=s, Server="s.whatsapp.net", Device=0, RawAgent=0, Integrator=0, IsEmpty=False)


def format_bytes(num_bytes: int | None) -> str:
    """Format bytes to human readable string (KB, MB, GB)."""
    if not num_bytes:
        return ""
    for unit in ["B", "KB", "MB", "GB"]:
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:3.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} TB"


def format_duration(seconds: int | None) -> str:
    """Format duration in seconds to human readable time string."""
    if not seconds:
        return "0s"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    elif m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def parse_vcard(vcard_text: str) -> dict[str, str]:
    """Parse common fields from vCard text."""
    data = {}
    if not vcard_text:
        return data

    fn_match = re.search(r"FN:(.+)", vcard_text, re.IGNORECASE)
    if fn_match:
        data["name"] = fn_match.group(1).strip()

    phone_match = re.search(r"TEL[^:]*:\s*(\+?[0-9\s\-()]+)", vcard_text, re.IGNORECASE)
    if phone_match:
        data["phone"] = phone_match.group(1).strip()

    org_match = re.search(r"ORG:\s*(.+)", vcard_text, re.IGNORECASE)
    if org_match:
        data["organization"] = org_match.group(1).strip()

    email_match = re.search(r"EMAIL[^:]*:\s*(.+)", vcard_text, re.IGNORECASE)
    if email_match:
        data["email"] = email_match.group(1).strip()

    waid_match = re.search(r"waid=([0-9]+)", vcard_text, re.IGNORECASE)
    if waid_match:
        data["waid"] = waid_match.group(1).strip()

    return data


def unwrap_message(msg: Message) -> Message:
    """Unwrap ephemeral, viewOnce, deviceSent, or document-caption message containers."""
    while True:
        if msg.ephemeralMessage.ListFields():
            msg = msg.ephemeralMessage.message
        elif msg.viewOnceMessage.ListFields():
            msg = msg.viewOnceMessage.message
        elif msg.viewOnceMessageV2.ListFields():
            msg = msg.viewOnceMessageV2.message
        elif hasattr(msg, "viewOnceMessageV2Extension") and msg.viewOnceMessageV2Extension.ListFields():
            msg = msg.viewOnceMessageV2Extension.message
        elif hasattr(msg, "deviceSentMessage") and msg.deviceSentMessage.ListFields():
            msg = msg.deviceSentMessage.message
        elif msg.documentWithCaptionMessage.ListFields():
            msg = msg.documentWithCaptionMessage.message
        else:
            break
    return msg


def extract_protocol_action(raw_msg: Message) -> dict[str, Any] | None:
    """Detect WhatsApp protocol control messages (Delete-for-Everyone revokes, edits, system events).

    Returns a dict describing the action or None if this is a normal user message.
    """
    current = raw_msg
    for _ in range(8):
        if current.protocolMessage.ListFields():
            break
        if current.ephemeralMessage.ListFields():
            current = current.ephemeralMessage.message
        elif hasattr(current, "editedMessage") and current.editedMessage.ListFields():
            current = current.editedMessage.message
        elif hasattr(current, "deviceSentMessage") and current.deviceSentMessage.ListFields():
            current = current.deviceSentMessage.message
        elif current.viewOnceMessage.ListFields():
            current = current.viewOnceMessage.message
        elif current.viewOnceMessageV2.ListFields():
            current = current.viewOnceMessageV2.message
        elif hasattr(current, "viewOnceMessageV2Extension") and current.viewOnceMessageV2Extension.ListFields():
            current = current.viewOnceMessageV2Extension.message
        else:
            break

    if not current.protocolMessage.ListFields():
        return None

    proto = current.protocolMessage
    has_type = hasattr(proto, "HasField") and proto.HasField("type")
    ptype = int(proto.type) if has_type else -1
    has_key = hasattr(proto, "HasField") and proto.HasField("key")
    target_id = (proto.key.ID or "").strip() if (has_key or proto.key.ID) else ""
    remote_jid = (proto.key.remoteJID or "").strip() if (has_key or proto.key.remoteJID) else ""
    ts_ms = int(getattr(proto, "timestampMS", 0) or 0)

    # 0 = ProtocolMessage.Type.REVOKE (Delete for Everyone)
    if ptype == 0 and target_id:
        return {
            "action": "revoke",
            "target_msg_id": target_id,
            "remote_jid": remote_jid,
            "timestamp_ms": ts_ms,
        }

    # 14 = ProtocolMessage.Type.MESSAGE_EDIT
    if ptype == 14 and target_id:
        edited_payload = proto.editedMessage if (hasattr(proto, "HasField") and proto.HasField("editedMessage")) else None
        rich_info = extract_rich_message_info(edited_payload) if edited_payload is not None else RichMessageInfo()
        return {
            "action": "edit",
            "target_msg_id": target_id,
            "remote_jid": remote_jid,
            "new_text": rich_info.text.strip(),
            "rich_info": rich_info,
            "timestamp_ms": ts_ms,
        }

    # Other pure protocol notifications (history sync notifications, ephemeral settings, key shares)
    top_fields = {f.name for f, _ in current.ListFields()}
    if top_fields.issubset({"protocolMessage", "messageContextInfo"}):
        return {
            "action": "system",
            "target_msg_id": target_id,
            "protocol_type": ptype,
            "timestamp_ms": ts_ms,
        }
    return None


def _find_context_info(msg: Message) -> Any:
    """Locate protobuf ContextInfo if this message quotes or replies to an earlier message."""
    for attr in (
        "extendedTextMessage",
        "imageMessage",
        "videoMessage",
        "documentMessage",
        "audioMessage",
        "stickerMessage",
        "contactMessage",
        "locationMessage",
    ):
        sub = getattr(msg, attr, None)
        if sub and sub.ListFields():
            ctx = getattr(sub, "contextInfo", None)
            if ctx and ctx.ListFields():
                return ctx
    return None


def _extract_quote_context(ctx_info: Any) -> dict[str, str]:
    """Extract quoted stanza ID, participant, media type, filename, and snippet from ContextInfo."""
    result = {
        "quoted_msg_id": "",
        "quoted_participant": "",
        "quoted_media_type": "",
        "quoted_filename": "",
        "quoted_snippet": "",
    }
    if not ctx_info or not ctx_info.ListFields():
        return result

    result["quoted_msg_id"] = str(getattr(ctx_info, "stanzaID", "") or "").strip()
    result["quoted_participant"] = str(getattr(ctx_info, "participant", "") or "").strip()

    raw_qm = getattr(ctx_info, "quotedMessage", None)
    if not raw_qm or not raw_qm.ListFields():
        return result

    qm = unwrap_message(raw_qm)
    if qm.conversation:
        result["quoted_media_type"] = "text"
        result["quoted_snippet"] = qm.conversation.strip()
    elif qm.extendedTextMessage and qm.extendedTextMessage.text:
        result["quoted_media_type"] = "text"
        result["quoted_snippet"] = qm.extendedTextMessage.text.strip()
    elif qm.documentMessage.ListFields():
        doc = qm.documentMessage
        fname = (doc.fileName or doc.title or "").strip()
        cap = (doc.caption or "").strip()
        result["quoted_media_type"] = "document"
        result["quoted_filename"] = fname
        result["quoted_snippet"] = f"Document: {fname or 'file'}" + (f" ({cap})" if cap else "")
    elif qm.imageMessage.ListFields():
        cap = (qm.imageMessage.caption or "").strip()
        result["quoted_media_type"] = "image"
        result["quoted_snippet"] = f"Image: {cap}" if cap else "Image (photo/screenshot)"
    elif qm.videoMessage.ListFields():
        cap = (qm.videoMessage.caption or "").strip()
        result["quoted_media_type"] = "video"
        result["quoted_snippet"] = f"Video: {cap}" if cap else "Video"
    elif qm.audioMessage.ListFields():
        is_ptt = bool(qm.audioMessage.PTT)
        result["quoted_media_type"] = "voice_note" if is_ptt else "audio"
        result["quoted_snippet"] = "Voice Note" if is_ptt else "Audio File"
    elif qm.locationMessage.ListFields():
        loc = qm.locationMessage
        result["quoted_media_type"] = "location"
        result["quoted_snippet"] = f"Location: {loc.name or loc.address or 'Shared Location'}"
    elif qm.contactMessage.ListFields():
        result["quoted_media_type"] = "contact"
        result["quoted_snippet"] = f"Contact: {qm.contactMessage.displayName or 'vCard'}"
    else:
        for p_attr in ("pollCreationMessage", "pollCreationMessageV2", "pollCreationMessageV3"):
            poll_obj = getattr(qm, p_attr, None)
            if poll_obj and poll_obj.ListFields():
                result["quoted_media_type"] = "poll"
                result["quoted_snippet"] = f"Poll: {poll_obj.name or 'Untitled Poll'}"
                break

    return result


def _extract_base_message_info(msg: Message, quote_snippet: str = "") -> RichMessageInfo:
    """Extract core message fields and searchable text from an already-unwrapped Message."""

    # 1. Document Message (PDF, DOCX, ZIP, etc.)
    doc = None
    if msg.documentMessage.ListFields():
        doc = msg.documentMessage

    if doc:
        filename = (doc.fileName or doc.title or "").strip()
        mimetype = doc.mimetype or "application/octet-stream"
        size = int(doc.fileLength or 0)
        pages = int(doc.pageCount or 0)
        caption = (doc.caption or "").strip()

        # If no filename is set, synthesize a readable name from mimetype or caption
        if not filename or filename.lower() == "unnamed_document":
            ext = ""
            if "pdf" in mimetype.lower():
                ext = ".pdf"
            elif "word" in mimetype.lower() or "docx" in mimetype.lower():
                ext = ".docx"
            elif "sheet" in mimetype.lower() or "excel" in mimetype.lower():
                ext = ".xlsx"
            elif "presentation" in mimetype.lower() or "powerpoint" in mimetype.lower():
                ext = ".pptx"
            elif "zip" in mimetype.lower() or "compressed" in mimetype.lower():
                ext = ".zip"
            elif "image" in mimetype.lower():
                ext = ".jpg"

            if caption:
                safe_cap = re.sub(r'[\\/*?:"<>|]', "_", caption[:30]).strip()
                filename = f"{safe_cap}{ext if not safe_cap.endswith(ext) else ''}"
            else:
                filename = f"Document{ext}" if ext else "Document"

        size_str = format_bytes(size)
        pages_str = f", {pages} pages" if pages > 0 else ""
        caption_str = f" - Caption: {caption}" if caption else ""
        text = f"📄 [Document: {filename} ({size_str}{pages_str})]{caption_str}"

        metadata = {
            "filename": filename,
            "title": doc.title or "",
            "mimetype": mimetype,
            "size_bytes": size,
            "page_count": pages,
            "caption": caption,
        }
        return RichMessageInfo(
            text=text,
            media_type="document",
            media_filename=filename,
            media_mimetype=mimetype,
            media_size=size,
            media_metadata=metadata,
        )

    # 2. Contact Cards (vCards)
    if msg.contactMessage.ListFields():
        cm = msg.contactMessage
        display_name = cm.displayName or "Unknown Contact"
        vcard = cm.vcard or ""
        vcard_data = parse_vcard(vcard)

        phone = vcard_data.get("phone", "")
        org = vcard_data.get("organization", "")
        email = vcard_data.get("email", "")

        parts = [f"👤 [Contact Card: {display_name}"]
        if phone:
            parts.append(f"Phone: {phone}")
        if org:
            parts.append(f"Org: {org}")
        if email:
            parts.append(f"Email: {email}")
        text = " | ".join(parts) + "]"

        metadata = {
            "display_name": display_name,
            "phone": phone,
            "organization": org,
            "email": email,
            "vcard_raw": vcard,
        }
        return RichMessageInfo(
            text=text,
            media_type="contact",
            media_metadata=metadata,
        )

    # 3. Contacts Array (Multiple shared contacts)
    if msg.contactsArrayMessage.ListFields():
        cam = msg.contactsArrayMessage
        contacts = []
        names = []
        for c in cam.contacts:
            name = c.displayName or "Contact"
            vcard = parse_vcard(c.vcard or "")
            phone = vcard.get("phone", "")
            contacts.append({"name": name, "phone": phone, "vcard": c.vcard})
            names.append(f"{name} ({phone})" if phone else name)

        contacts_str = ", ".join(names)
        text = f"👥 [Shared Contacts ({len(contacts)}): {contacts_str}]"
        return RichMessageInfo(
            text=text,
            media_type="contact",
            media_metadata={"contacts_count": len(contacts), "contacts": contacts},
        )

    # 4. Poll Creation Messages (V1, V2, V3)
    poll = None
    if msg.pollCreationMessage.ListFields():
        poll = msg.pollCreationMessage
    elif msg.pollCreationMessageV2.ListFields():
        poll = msg.pollCreationMessageV2
    elif msg.pollCreationMessageV3.ListFields():
        poll = msg.pollCreationMessageV3

    if poll:
        question = poll.name or "Untitled Poll"
        options = [opt.optionName for opt in poll.options if opt.optionName]
        selectable = int(poll.selectableOptionsCount or 1)
        opt_str = " | ".join([f"{i+1}. {o}" for i, o in enumerate(options)])
        choice_type = "Multiple Choice" if selectable > 1 else "Single Choice"
        text = f"📊 [Poll: \"{question}\" | Options: {opt_str} | ({choice_type})]"

        metadata = {
            "question": question,
            "options": options,
            "selectable_options_count": selectable,
            "choice_type": choice_type,
        }
        return RichMessageInfo(
            text=text,
            media_type="poll",
            media_metadata=metadata,
        )

    # 5. Audio & Voice Notes (PTT)
    if msg.audioMessage.ListFields():
        audio = msg.audioMessage
        seconds = int(audio.seconds or 0)
        is_ptt = bool(audio.PTT)
        size = int(audio.fileLength or 0)
        mimetype = audio.mimetype or "audio/ogg"

        dur_str = format_duration(seconds)
        size_str = format_bytes(size)
        if is_ptt:
            text = f"🎙️ [Voice Note (Duration: {dur_str}, Size: {size_str})]"
            media_type = "voice_note"
        else:
            text = f"🎵 [Audio File (Duration: {dur_str}, Size: {size_str})]"
            media_type = "audio"

        metadata = {
            "duration_seconds": seconds,
            "duration_formatted": dur_str,
            "is_ptt": is_ptt,
            "mimetype": mimetype,
            "size_bytes": size,
        }
        return RichMessageInfo(
            text=text,
            media_type=media_type,
            media_mimetype=mimetype,
            media_size=size,
            media_metadata=metadata,
        )

    # 6. Image Message
    if msg.imageMessage.ListFields():
        img = msg.imageMessage
        caption = img.caption or ""
        size = int(img.fileLength or 0)
        mimetype = img.mimetype or "image/jpeg"
        w, h = int(img.width or 0), int(img.height or 0)

        dim_str = f" {w}x{h}" if (w and h) else ""
        size_str = format_bytes(size)
        caption_str = f": \"{caption}\"" if caption else ""
        text = f"🖼️ [Image{dim_str} ({size_str})]{caption_str}"

        metadata = {
            "caption": caption,
            "width": w,
            "height": h,
            "mimetype": mimetype,
            "size_bytes": size,
        }
        return RichMessageInfo(
            text=text,
            media_type="image",
            media_mimetype=mimetype,
            media_size=size,
            media_metadata=metadata,
        )

    # 7. Video Message
    if msg.videoMessage.ListFields():
        vid = msg.videoMessage
        caption = vid.caption or ""
        seconds = int(vid.seconds or 0)
        size = int(vid.fileLength or 0)
        mimetype = vid.mimetype or "video/mp4"

        dur_str = format_duration(seconds)
        size_str = format_bytes(size)
        caption_str = f": \"{caption}\"" if caption else ""
        text = f"🎥 [Video ({dur_str}, {size_str})]{caption_str}"

        metadata = {
            "caption": caption,
            "duration_seconds": seconds,
            "duration_formatted": dur_str,
            "mimetype": mimetype,
            "size_bytes": size,
        }
        return RichMessageInfo(
            text=text,
            media_type="video",
            media_mimetype=mimetype,
            media_size=size,
            media_metadata=metadata,
        )

    # 8. Location Messages
    if msg.locationMessage.ListFields():
        loc = msg.locationMessage
        name = loc.name or ""
        addr = loc.address or ""
        lat, lon = loc.degreesLatitude, loc.degreesLongitude

        loc_label = name or "Shared Location"
        addr_str = f", {addr}" if addr else ""
        coords_str = f" (Coords: {lat:.4f}, {lon:.4f})" if (lat or lon) else ""
        url_str = f" - Map: {loc.URL}" if loc.URL else ""
        text = f"📍 [Location: {loc_label}{addr_str}{coords_str}]{url_str}"

        metadata = {
            "name": name,
            "address": addr,
            "latitude": lat,
            "longitude": lon,
            "url": loc.URL or "",
        }
        return RichMessageInfo(
            text=text,
            media_type="location",
            media_metadata=metadata,
        )

    if msg.liveLocationMessage.ListFields():
        live = msg.liveLocationMessage
        caption = live.caption or "Live Location"
        lat, lon = live.degreesLatitude, live.degreesLongitude
        text = f"📍 [Live Location: {caption} (Coords: {lat:.4f}, {lon:.4f})]"
        return RichMessageInfo(
            text=text,
            media_type="location",
            media_metadata={"caption": caption, "latitude": lat, "longitude": lon},
        )

    # 9. Stickers
    if msg.stickerMessage.ListFields():
        stk = msg.stickerMessage
        size = int(stk.fileLength or 0)
        size_str = format_bytes(size)
        return RichMessageInfo(
            text=f"🏷️ [Sticker ({size_str})]",
            media_type="sticker",
            media_size=size,
            media_mimetype=stk.mimetype or "image/webp",
        )

    # 10. Extended Text Message (Handles link previews & replies/quotes)
    if msg.extendedTextMessage.ListFields():
        ext = msg.extendedTextMessage
        raw_body = ext.text or ""
        body = raw_body
        title = (ext.title or "").strip()
        desc = (ext.description or "").strip()

        # Append link preview title & description so shared links, product cards, and articles are searchable
        preview_parts = []
        if title:
            preview_parts.append(title)
        if desc and desc != title:
            preview_parts.append(desc)
        if preview_parts:
            preview_str = " - ".join(preview_parts)
            body = f"{body} [Link Preview: {preview_str}]" if body else f"[Link Preview: {preview_str}]"

        if quote_snippet:
            short_quote = (quote_snippet[:60] + "...") if len(quote_snippet) > 60 else quote_snippet
            text = f"{body} [Replying to: \"{short_quote}\"]"
        else:
            text = body

        metadata = {}
        if title or desc:
            metadata["link_title"] = title
            metadata["link_description"] = desc

        return RichMessageInfo(
            text=text,
            media_type="text",
            media_metadata=metadata,
            clean_text=raw_body.strip(),
        )

    # 11. Plain Conversation
    if msg.conversation:
        return RichMessageInfo(
            text=msg.conversation,
            media_type="text",
            clean_text=msg.conversation.strip(),
        )

    return RichMessageInfo(text="", media_type="text")


def extract_rich_message_info(raw_msg: Message) -> RichMessageInfo:
    """Extract comprehensive metadata, media attributes, and searchable text from any WhatsApp message."""
    msg = unwrap_message(raw_msg)
    quote_ctx = _extract_quote_context(_find_context_info(msg))
    info = _extract_base_message_info(msg, quote_snippet=quote_ctx["quoted_snippet"])
    if quote_ctx["quoted_msg_id"] or quote_ctx["quoted_snippet"]:
        info.quoted_msg_id = quote_ctx["quoted_msg_id"]
        info.quoted_participant = quote_ctx["quoted_participant"]
        info.quoted_media_type = quote_ctx["quoted_media_type"]
        info.quoted_filename = quote_ctx["quoted_filename"]
        info.quoted_snippet = quote_ctx["quoted_snippet"]
        if quote_ctx["quoted_msg_id"]:
            info.media_metadata["quoted_msg_id"] = quote_ctx["quoted_msg_id"]
    if not info.clean_text:
        info.clean_text = info.text.strip()
    return info


def _extract_xlsx_text(path: Path, max_chars: int = 18000) -> str:
    """Extract structured spreadsheet data, sheets, and tables from an Excel file (.xlsx, .xlsm, .xltx, .xls)."""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
        sheets_out = []
        total_chars = 0
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            rows_data = []
            for row_idx, row in enumerate(ws.iter_rows(values_only=True)):
                if row_idx >= 150:
                    rows_data.append("... [Additional rows truncated for brevity] ...")
                    break
                # Skip empty rows
                if not any(c is not None and str(c).strip() != "" for c in row):
                    continue
                cells = [str(c).strip() if c is not None else "" for c in row]
                while cells and cells[-1] == "":
                    cells.pop()
                if cells:
                    rows_data.append(" | ".join(cells))

            if rows_data:
                sheet_text = f"=== Sheet: '{sheet_name}' ({len(rows_data)} rows) ===\n" + "\n".join(rows_data)
                sheets_out.append(sheet_text)
                total_chars += len(sheet_text)
                if total_chars > max_chars:
                    break
        wb.close()
        if sheets_out:
            return "\n\n".join(sheets_out)[:max_chars]
        return "The Excel spreadsheet contains no readable data."
    except Exception as e:
        # Fallback to reading shared strings XML from ZIP archive
        try:
            with zipfile.ZipFile(str(path)) as z:
                shared_strings = []
                if "xl/sharedStrings.xml" in z.namelist():
                    tree = ET.fromstring(z.read("xl/sharedStrings.xml"))
                    for si in tree.findall(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t"):
                        if si.text:
                            shared_strings.append(si.text.strip())
                if shared_strings:
                    return f"[Extracted from Excel Shared Strings ({path.name})]\n" + "\n".join(shared_strings)[:max_chars]
        except Exception:
            pass
        return f"Failed to extract text from Excel file: {e}"


def _extract_docx_text(path: Path, max_chars: int = 18000) -> str:
    """Extract structured paragraphs, headings, and tables from a Word document (.docx, .doc)."""
    try:
        import docx
        doc = docx.Document(str(path))
        elements = []
        total_chars = 0

        for element in doc.element.body:
            tag = element.tag.split("}")[-1] if "}" in element.tag else element.tag
            if tag == "p":
                p = docx.text.paragraph.Paragraph(element, doc)
                txt = p.text.strip()
                if txt:
                    if p.style and "heading" in p.style.name.lower():
                        elements.append(f"## {txt}")
                    else:
                        elements.append(txt)
                    total_chars += len(txt)
            elif tag == "tbl":
                table = docx.table.Table(element, doc)
                table_rows = []
                for row in table.rows:
                    cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
                    if any(cells):
                        table_rows.append(" | ".join(cells))
                if table_rows:
                    tbl_str = "\n".join(table_rows)
                    elements.append(f"--- Table ---\n{tbl_str}\n--- End Table ---")
                    total_chars += len(tbl_str)

            if total_chars > max_chars:
                break

        if elements:
            return "\n\n".join(elements)[:max_chars]
    except Exception:
        pass

    # Fallback to raw XML extraction from word/document.xml
    try:
        with zipfile.ZipFile(str(path)) as z:
            xml_content = z.read("word/document.xml")
            tree = ET.fromstring(xml_content)
            namespaces = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            texts = [node.text for node in tree.iterfind(".//w:t", namespaces) if node.text]
            if texts:
                return " ".join(texts)[:max_chars]
    except Exception as ze:
        return f"Failed to extract text from Word document: {ze}"
    return "The Word document appears to be empty."


def _extract_pptx_text(path: Path, max_chars: int = 18000) -> str:
    """Extract slide titles, bullet points, tables, and notes from a PowerPoint presentation (.pptx, .ppt)."""
    try:
        from pptx import Presentation
        prs = Presentation(str(path))
        slides_out = []
        total_chars = 0

        for slide_num, slide in enumerate(prs.slides, start=1):
            slide_lines = []
            title = ""
            if slide.shapes.title and slide.shapes.title.text:
                title = slide.shapes.title.text.strip()
                slide_lines.append(f"Title: {title}")

            for shape in slide.shapes:
                if shape == slide.shapes.title:
                    continue
                if shape.has_text_frame:
                    for paragraph in shape.text_frame.paragraphs:
                        txt = paragraph.text.strip()
                        if txt and txt != title:
                            slide_lines.append(f"• {txt}")
                elif shape.has_table:
                    table_rows = []
                    for row in shape.table.rows:
                        cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
                        if any(cells):
                            table_rows.append(" | ".join(cells))
                    if table_rows:
                        slide_lines.append("[Table]:\n" + "\n".join(table_rows))

            if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
                notes = slide.notes_slide.notes_text_frame.text.strip()
                if notes:
                    slide_lines.append(f"[Speaker Notes]: {notes}")

            if slide_lines:
                slide_header = f"=== Slide {slide_num}" + (f": {title}" if title else "") + " ==="
                slide_text = f"{slide_header}\n" + "\n".join(slide_lines)
                slides_out.append(slide_text)
                total_chars += len(slide_text)
                if total_chars > max_chars:
                    break

        if slides_out:
            return "\n\n".join(slides_out)[:max_chars]
        return "The PowerPoint presentation contains no text."
    except Exception as e:
        # Fallback to XML extraction from ZIP archive
        try:
            with zipfile.ZipFile(str(path)) as z:
                slide_files = sorted([f for f in z.namelist() if f.startswith("ppt/slides/slide") and f.endswith(".xml")])
                slides_text = []
                for sfile in slide_files:
                    tree = ET.fromstring(z.read(sfile))
                    texts = [n.text for n in tree.iterfind(".//{http://schemas.openxmlformats.org/drawingml/2006/main}t") if n.text]
                    if texts:
                        slides_text.append(f"--- {Path(sfile).stem} ---\n" + "\n".join(texts))
                if slides_text:
                    return "\n\n".join(slides_text)[:max_chars]
        except Exception:
            pass
        return f"Failed to extract text from PowerPoint presentation: {e}"


def _read_plain_text(path: Path, max_chars: int = 18000) -> str:
    """Read a text file trying multiple common encodings."""
    for enc in ["utf-8", "utf-8-sig", "latin-1", "cp1252", "utf-16"]:
        try:
            content = path.read_text(encoding=enc, errors="replace")
            if content:
                return content[:max_chars]
        except Exception:
            continue
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:max_chars]
    except Exception as e:
        return f"Failed to read text file: {e}"


def _extract_vcf_text(path: Path, max_chars: int = 18000) -> str:
    """Extract contact details from a vCard (.vcf) file."""
    try:
        raw = _read_plain_text(path, max_chars=max_chars)
        cards = []
        curr = {}
        for line in raw.splitlines():
            line = line.strip()
            if line == "BEGIN:VCARD":
                curr = {}
            elif line == "END:VCARD":
                if curr:
                    card_str = f"👤 *Contact: {curr.get('FN') or curr.get('N') or 'Unnamed'}*\n"
                    if "TEL" in curr:
                        card_str += f"• Phone: {curr['TEL']}\n"
                    if "EMAIL" in curr:
                        card_str += f"• Email: {curr['EMAIL']}\n"
                    if "ORG" in curr:
                        card_str += f"• Organization: {curr['ORG']}\n"
                    if "TITLE" in curr:
                        card_str += f"• Title: {curr['TITLE']}\n"
                    if "NOTE" in curr:
                        card_str += f"• Note: {curr['NOTE']}\n"
                    cards.append(card_str.strip())
            elif ":" in line:
                k, v = line.split(":", 1)
                k_clean = k.split(";")[0].upper()
                if k_clean in ("FN", "N", "TEL", "EMAIL", "ORG", "TITLE", "NOTE"):
                    curr[k_clean] = v.strip()
        if cards:
            return "\n\n".join(cards)[:max_chars]
        return raw[:max_chars]
    except Exception as e:
        return f"Failed to parse vCard file: {e}"


def _extract_ics_text(path: Path, max_chars: int = 18000) -> str:
    """Extract calendar meeting/event details from an iCalendar (.ics) file."""
    try:
        raw = _read_plain_text(path, max_chars=max_chars)
        events = []
        curr = {}
        for line in raw.splitlines():
            line = line.strip()
            if line == "BEGIN:VEVENT":
                curr = {}
            elif line == "END:VEVENT":
                if curr:
                    ev_str = f"📅 *Event: {curr.get('SUMMARY', 'Untitled Event')}*\n"
                    if "DTSTART" in curr:
                        ev_str += f"• Starts: {curr['DTSTART']}\n"
                    if "DTEND" in curr:
                        ev_str += f"• Ends: {curr['DTEND']}\n"
                    if "LOCATION" in curr:
                        ev_str += f"• Location: {curr['LOCATION']}\n"
                    if "DESCRIPTION" in curr:
                        ev_str += f"• Description: {curr['DESCRIPTION']}\n"
                    events.append(ev_str.strip())
            elif ":" in line:
                k, v = line.split(":", 1)
                k_clean = k.split(";")[0].upper()
                if k_clean in ("SUMMARY", "DTSTART", "DTEND", "LOCATION", "DESCRIPTION"):
                    curr[k_clean] = v.strip()
        if events:
            return "\n\n".join(events)[:max_chars]
        return raw[:max_chars]
    except Exception as e:
        return f"Failed to parse calendar invite file: {e}"


def _extract_ipynb_text(path: Path, max_chars: int = 18000) -> str:
    """Extract code and markdown cells from a Jupyter Notebook (.ipynb)."""
    try:
        raw = _read_plain_text(path, max_chars=max_chars * 2)
        data = json.loads(raw)
        cells_out = []
        for idx, cell in enumerate(data.get("cells", []), 1):
            ctype = cell.get("cell_type", "code")
            src = "".join(cell.get("source", [])).strip()
            if src:
                cells_out.append(f"[{ctype.upper()} Cell {idx}]:\n{src}")
        if cells_out:
            return f"=== Jupyter Notebook: {path.name} ({len(cells_out)} cells) ===\n\n" + "\n\n".join(cells_out)[:max_chars]
        return "The Jupyter Notebook contains no cells or text."
    except Exception as e:
        return f"Failed to parse Jupyter Notebook: {e}"


def _extract_opendocument_text(path: Path, max_chars: int = 18000) -> str:
    """Extract text from OpenDocument formats (.odt, .ods, .odp)."""
    try:
        with zipfile.ZipFile(str(path)) as z:
            if "content.xml" in z.namelist():
                tree = ET.fromstring(z.read("content.xml"))
                texts = [node.text for node in tree.iter() if node.text and node.text.strip()]
                if texts:
                    return f"=== OpenDocument: {path.name} ===\n" + "\n".join(texts)[:max_chars]
        return "OpenDocument file appears empty."
    except Exception as e:
        return f"Failed to parse OpenDocument file: {e}"


def _extract_rtf_text(path: Path, max_chars: int = 18000) -> str:
    """Extract plain text from a Rich Text Format (.rtf) file."""
    try:
        raw = _read_plain_text(path, max_chars=max_chars * 2)
        clean = re.sub(r"\\[a-zA-Z0-9-]+\s?", " ", raw)
        clean = re.sub(r"[{}]", "", clean)
        clean = " ".join(clean.split())
        return clean[:max_chars] if clean else "The RTF document is empty."
    except Exception as e:
        return f"Failed to extract RTF text: {e}"


def _extract_zip_contents(path: Path, max_chars: int = 18000) -> str:
    """List contents of a ZIP archive and preview readable text/code files inside."""
    try:
        with zipfile.ZipFile(str(path)) as z:
            infolist = z.infolist()
            total_files = len(infolist)
            total_size_kb = sum(info.file_size for info in infolist) / 1024
            lines = [f"=== Archive: '{path.name}' ({total_files} files, {total_size_kb:.1f} KB uncompressed) ==="]

            preview_candidates = []
            for idx, info in enumerate(infolist):
                if idx < 30:
                    lines.append(f"• {info.filename} ({info.file_size / 1024:.1f} KB)")
                sfx = Path(info.filename).suffix.lower()
                if sfx in [".txt", ".md", ".py", ".json", ".csv", ".html", ".c", ".cpp", ".java", ".sql", ".js", ".ts"] and 0 < info.file_size < 50000:
                    preview_candidates.append(info.filename)

            if total_files > 30:
                lines.append(f"... and {total_files - 30} additional files.")

            for pfile in preview_candidates[:3]:
                try:
                    pcontent = z.read(pfile).decode("utf-8", errors="replace")[:2000]
                    if pcontent.strip():
                        lines.append(f"\n--- Preview: {pfile} ---\n{pcontent.strip()}\n--- End Preview ---")
                except Exception:
                    pass

            return "\n".join(lines)[:max_chars]
    except Exception as e:
        return f"Failed to inspect ZIP archive: {e}"


PLAIN_TEXT_DOC_EXTS: frozenset[str] = frozenset({
    ".txt", ".log", ".md", ".markdown", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml",
    ".html", ".htm", ".tex", ".ini", ".cfg", ".conf", ".env", ".sql", ".py", ".js", ".jsx",
    ".ts", ".tsx", ".css", ".scss", ".c", ".cpp", ".h", ".hpp", ".java", ".kt", ".cs",
    ".php", ".rb", ".go", ".rs", ".swift", ".sh", ".bash", ".bat", ".ps1", ".cmd", ".epub",
})

LOCAL_DOC_INDEXABLE_EXTS: frozenset[str] = PLAIN_TEXT_DOC_EXTS | frozenset({
    ".pdf", ".xlsx", ".xlsm", ".xltx", ".xls",
    ".docx", ".doc", ".pptx", ".ppt",
    ".odt", ".ods", ".odp", ".rtf",
    ".ipynb", ".vcf", ".ics", ".zip",
})

_NON_CONTENT_DOC_PREFIXES = (
    "Failed to ",
    "File '",
    "Document '",
    "The PDF appears to be empty",
    "The Excel spreadsheet contains no readable",
    "The Word document appears to be empty",
    "The PowerPoint presentation contains no text",
    "The Jupyter Notebook contains no",
    "OpenDocument file appears empty",
)


def is_meaningful_extracted_doc_text(text: str, min_len: int = 20) -> bool:
    """Return True if extract_document_text output contains real searchable text rather than errors or binary junk."""
    if not text:
        return False
    stripped = text.strip()
    if len(stripped) < min_len:
        return False
    if stripped.startswith(_NON_CONTENT_DOC_PREFIXES):
        return False
    if stripped.count("\ufffd") >= max(5, len(stripped) // 10):
        return False
    return True


def extract_document_text(file_path: str, max_chars: int = 18000) -> str:
    """Extract readable structured text from any day-to-day WhatsApp file:
    PDFs, Excel spreadsheets, Word docs, PowerPoint presentations, Jupyter notebooks,
    vCards, Calendar invites, ZIP archives, plain text, logs, markdown, and source code.
    """
    path = Path(file_path)
    if not path.exists():
        return f"File '{file_path}' does not exist on disk."

    suffix = path.suffix.lower()

    # 1. PDF Documents
    if suffix == ".pdf":
        try:
            import pypdf
            reader = pypdf.PdfReader(str(path))
            pages_text = []
            for idx, page in enumerate(reader.pages):
                txt = page.extract_text() or ""
                if txt.strip():
                    pages_text.append(f"--- Page {idx + 1} ---\n{txt.strip()}")
                if sum(len(p) for p in pages_text) > max_chars:
                    break
            if not pages_text:
                return "The PDF appears to be empty or contains scanned images without selectable text."
            full_text = "\n\n".join(pages_text)
            return full_text[:max_chars]
        except Exception as e:
            return f"Failed to extract text from PDF: {e}"

    # 2. Excel Spreadsheets (.xlsx, .xlsm, .xltx, .xls)
    elif suffix in [".xlsx", ".xlsm", ".xltx", ".xls"]:
        return _extract_xlsx_text(path, max_chars=max_chars)

    # 3. Word Documents (.docx, .doc)
    elif suffix in [".docx", ".doc"]:
        return _extract_docx_text(path, max_chars=max_chars)

    # 4. PowerPoint Presentations (.pptx, .ppt)
    elif suffix in [".pptx", ".ppt"]:
        return _extract_pptx_text(path, max_chars=max_chars)

    # 5. OpenDocument Formats (.odt, .ods, .odp)
    elif suffix in [".odt", ".ods", ".odp"]:
        return _extract_opendocument_text(path, max_chars=max_chars)

    # 6. Rich Text Format (.rtf)
    elif suffix == ".rtf":
        return _extract_rtf_text(path, max_chars=max_chars)

    # 7. Jupyter Notebooks (.ipynb)
    elif suffix == ".ipynb":
        return _extract_ipynb_text(path, max_chars=max_chars)

    # 8. vCard Contact Cards (.vcf)
    elif suffix == ".vcf":
        return _extract_vcf_text(path, max_chars=max_chars)

    # 9. Calendar Invites (.ics)
    elif suffix == ".ics":
        return _extract_ics_text(path, max_chars=max_chars)

    # 10. ZIP Archives (.zip)
    elif suffix == ".zip":
        return _extract_zip_contents(path, max_chars=max_chars)

    # 11. Plain Text, Notes, Logs, Markdown, Configs, E-books & Source Code
    elif suffix in PLAIN_TEXT_DOC_EXTS:
        return _read_plain_text(path, max_chars=max_chars)

    # 12. Fallback: try reading as plain text with multi-encoding detection before giving up
    try:
        content = _read_plain_text(path, max_chars=max_chars)
        # If the file appears to be mostly readable text (few null bytes)
        if content and "\x00" not in content[:500]:
            return content
    except Exception:
        pass

    return f"Document '{path.name}' is in binary format ({suffix}). File size: {path.stat().st_size} bytes."


def format_for_whatsapp(text: str) -> str:
    """Sanitize and format markdown text into clean, native WhatsApp-compatible syntax.

    WhatsApp supports:
      - *bold* (single asterisk)
      - _italic_ (single underscore)
      - ~strikethrough~
      - ```monospace```
      - `inline code`
      - > blockquote
      - • or - for bullets

    WhatsApp DOES NOT support:
      - Headers (#, ##, ###, ####) -> displays raw '#' characters
      - Double asterisks (**bold**) -> displays broken/extra asterisks
      - Markdown tables (| col | col |) -> displays broken pipe characters
      - Markdown links ([text](url)) -> displays raw [text](url)
    """
    if not text:
        return ""

    # 1. Protect code blocks (```...```) and inline code (`...`)
    code_blocks = []
    def _save_code_block(m):
        code_blocks.append(m.group(0))
        return f"__CODE_BLOCK_{len(code_blocks)-1}__"

    text = re.sub(r"```[\s\S]*?```", _save_code_block, text)

    inline_codes = []
    def _save_inline_code(m):
        inline_codes.append(m.group(0))
        return f"__INLINE_CODE_{len(inline_codes)-1}__"

    text = re.sub(r"`[^`\n]+`", _save_inline_code, text)

    # 2. Convert Markdown Tables into clean bullet points
    def _convert_table(match):
        table_str = match.group(0).strip()
        lines = [line.strip() for line in table_str.splitlines() if line.strip()]
        if len(lines) < 2:
            return table_str

        rows = []
        for line in lines:
            # Skip separator line like |---|---| or |:---|---:|
            if re.match(r"^\|?(\s*:?-+:?\s*\|)+\s*:?-+:?\s*\|?$", line):
                continue
            # Extract cell values
            cells = [c.strip() for c in line.strip("|").split("|")]
            rows.append(cells)

        if not rows:
            return ""

        headers = rows[0]
        data_rows = rows[1:] if len(rows) > 1 else []
        if not data_rows:
            return "\n".join([f"• {h}" for h in headers if h])

        formatted_lines = []
        # Check if it is a 2-column key-value table
        if len(headers) == 2 and headers[0].lower() in ("field", "key", "property", "item", "header", "metric", "attribute"):
            for r in data_rows:
                if len(r) >= 2 and r[0]:
                    formatted_lines.append(f"• *{r[0]}:* {r[1]}")
                elif len(r) == 1 and r[0]:
                    formatted_lines.append(f"• {r[0]}")
        else:
            for r in data_rows:
                parts = []
                for idx, cell in enumerate(r):
                    if not cell:
                        continue
                    h = headers[idx] if idx < len(headers) else ""
                    if h and h.lower() not in ("-", "--"):
                        parts.append(f"*{h}:* {cell}")
                    else:
                        parts.append(cell)
                if parts:
                    formatted_lines.append("• " + " — ".join(parts))

        return "\n".join(formatted_lines)

    table_pattern = re.compile(r"(?:^[ \t]*\|.+?\|[ \t]*$\n?){2,}", re.MULTILINE)
    text = table_pattern.sub(_convert_table, text)

    # 3. Convert Markdown Headers (e.g. ### 📄 Document Details -> *📄 Document Details*)
    def _convert_header(m):
        content = m.group("content").strip()
        # Clean any existing outer bolding from header content
        content = re.sub(r"^(\*\*|\*)(.+?)(\*\*|\*)$", r"\2", content).strip()
        return f"*{content}*"

    text = re.sub(r"^[ \t]*#{1,6}[ \t]+(?P<content>.+?)[ \t]*$", _convert_header, text, flags=re.MULTILINE)

    # 4. Standardize Bullet Lists BEFORE bolding:
    # Handles lines starting with `* ` or `- ` (with any leading indentation), converting to `• `
    def _convert_bullet(m):
        indent = m.group(1) or ""
        return f"{indent}• "

    text = re.sub(r"^([ \t]*)\*[ \t]+", _convert_bullet, text, flags=re.MULTILINE)
    text = re.sub(r"^([ \t]*)-{1,2}[ \t]+", _convert_bullet, text, flags=re.MULTILINE)

    # 5. Convert Markdown Links: [Title](url) -> *Title* (url) or url
    def _convert_link(m):
        title = m.group(1).strip()
        url = m.group(2).strip()
        if not url.startswith(("http://", "https://", "mailto:")):
            return f"*{title}*"
        if title.lower() in ("link", "here", "website", "url", "open link", "click here") or title == url:
            return url
        return f"*{title}* ({url})"

    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _convert_link, text)

    # 6. Bold & Italic syntax
    # ***text*** or **_text_** -> *_text_*
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"*_\1_*", text)
    text = re.sub(r"\*\*_(.+?)_\*\*", r"*_\1_*", text)
    text = re.sub(r"_\*\*(.+?)\*\*_", r"*_\1_*", text)
    # **bold** -> *bold*
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)

    # Clean up duplicate asterisks like **** or ***
    text = re.sub(r"\*{3,}", "*", text)

    # 7. Clean up any accidental `•  *` spacing
    text = re.sub(r"•\s{2,}\*", "• *", text)

    # 8. Restore protected inline code and code blocks
    for idx, code in enumerate(inline_codes):
        text = text.replace(f"__INLINE_CODE_{idx}__", code)

    for idx, block in enumerate(code_blocks):
        text = text.replace(f"__CODE_BLOCK_{idx}__", block)

    # 9. Normalize spacing: max 2 consecutive newlines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

