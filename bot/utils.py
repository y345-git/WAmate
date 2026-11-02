import re
import json
import time
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Set, Any
from dataclasses import dataclass, field
from neonize.proto.Neonize_pb2 import JID
from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message


@dataclass
class RichMessageInfo:
    text: str
    media_type: str = "text"
    media_filename: str = ""
    media_mimetype: str = ""
    media_size: int = 0
    media_metadata: dict[str, Any] = field(default_factory=dict)


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
    """Unwrap ephemeral, viewOnce, or template message containers."""
    while True:
        if msg.ephemeralMessage.ListFields():
            msg = msg.ephemeralMessage.message
        elif msg.viewOnceMessage.ListFields():
            msg = msg.viewOnceMessage.message
        elif msg.viewOnceMessageV2.ListFields():
            msg = msg.viewOnceMessageV2.message
        elif msg.documentWithCaptionMessage.ListFields():
            msg = msg.documentWithCaptionMessage.message
        else:
            break
    return msg


def extract_rich_message_info(raw_msg: Message) -> RichMessageInfo:
    """Extract comprehensive metadata, media attributes, and searchable text from any WhatsApp message."""
    msg = unwrap_message(raw_msg)

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
        body = ext.text or ""
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

        # Extract snippet of quoted message if this was a reply
        quote_snippet = ""
        if ext.contextInfo and ext.contextInfo.quotedMessage:
            qm = ext.contextInfo.quotedMessage
            if qm.conversation:
                quote_snippet = qm.conversation
            elif qm.extendedTextMessage and qm.extendedTextMessage.text:
                quote_snippet = qm.extendedTextMessage.text
            elif qm.documentMessage and qm.documentMessage.fileName:
                quote_snippet = f"Document: {qm.documentMessage.fileName}"
            elif qm.imageMessage and qm.imageMessage.caption:
                quote_snippet = f"Image: {qm.imageMessage.caption}"

        if quote_snippet:
            short_quote = (quote_snippet[:60] + "...") if len(quote_snippet) > 60 else quote_snippet
            text = f"{body} [Replying to: \"{short_quote}\"]"
        else:
            text = body

        metadata = {}
        if title or desc:
            metadata["link_title"] = title
            metadata["link_description"] = desc

        return RichMessageInfo(text=text, media_type="text", media_metadata=metadata)

    # 11. Plain Conversation
    if msg.conversation:
        return RichMessageInfo(text=msg.conversation, media_type="text")

    return RichMessageInfo(text="", media_type="text")


def extract_document_text(file_path: str, max_chars: int = 15000) -> str:
    """Extract readable text from a PDF, Word DOCX, or text file."""
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

    # 2. Word DOCX Documents
    elif suffix == ".docx":
        try:
            with zipfile.ZipFile(str(path)) as z:
                xml_content = z.read("word/document.xml")
                tree = ET.fromstring(xml_content)
                namespaces = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
                texts = [node.text for node in tree.iterfind(".//w:t", namespaces) if node.text]
                if not texts:
                    return "The Word document appears to be empty."
                full_text = " ".join(texts)
                return full_text[:max_chars]
        except Exception as e:
            return f"Failed to extract text from DOCX: {e}"

    # 3. Plain Text, CSV, JSON, XML, Code
    elif suffix in [".txt", ".csv", ".json", ".xml", ".html", ".py", ".md", ".log"]:
        try:
            return path.read_text(encoding="utf-8", errors="ignore")[:max_chars]
        except Exception as e:
            return f"Failed to read text file: {e}"

    else:
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

