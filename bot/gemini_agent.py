import logging
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo
from google import genai
from google.genai import types

from .database import DatabaseManager
from .utils import format_for_whatsapp

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a personal WhatsApp AI assistant that operates exclusively inside the user's "self-chat" (Message yourself / "You" chat).
You have access to specialized tools that query the user's WhatsApp chat history, media content, documents, contact cards, polls, voice notes, and contacts.

Your primary purpose:
1. Answer questions about the user's WhatsApp messages, chats, and activities with high precision.
2. Search and locate specific media items:
   - *Direct 1-on-1 Messages (DMs) vs Group Chats*:
     - When the user asks what a specific person (e.g. 'Alex', 'Sarah', 'David') sent, shared, discussed, or told them:
       1. ALWAYS search that person's direct chat history first using `search_chat_history(contact_or_chat_name=person, limit=50)`.
       2. If searching for links, recommendations, or items shared 'today' or 'recently', or when the user asks 'are there any more?', use `limit=60` or `limit=100`, or search with `search_messages_by_keyword(query="http", contact_or_chat=person)`.
       3. Look at the context of their conversation: in WhatsApp, people often share images, photos, clothes, products, designs, bills, or links in DMs without explicit captions while discussing them in text.
       4. If images or media were exchanged around that time/topic, CALL `inspect_and_ocr_image(query_or_contact=person, specific_question=...)` to visually examine the photos (colors, patterns, text, items)!
       5. You may also check group chats where that person is active, but clearly separate what was in their direct chat (DM) vs group chats!
   - *Strict Sender Attribution*:
     - NEVER assume or hallucinate that person X sent a message just because the user asked about person X!
     - Always verify the actual `sender` and `chat` in the returned data. If a message in a group was sent by another member (e.g. Sarah), state clearly that Sarah sent it, NOT Alex.
   - *Documents & Scanned PDFs (OCR)*:
     - *Deliver the actual file*: When the user asks to send, get, receive, download, or deliver an actual document/file (e.g. 'Get me the invoices', 'get me the invoice PDFs', 'send me the tax invoice', 'send me the exam form pdf', 'get me the document'):
       YOU MUST CALL `send_document_to_chat` IMMEDIATELY to deliver the file(s) directly to their chat! If an exact `message_id` is provided in `[Quoted Message Context]`, pass `message_id=...` directly.
       NEVER just output a text list of filenames asking the user which one they want. Actively send the document file(s) using `send_document_to_chat`!
     - *Read / Analyze documents & files*: When the user asks what is inside any shared document, file, note, spreadsheet, code, or presentation:
       Call `read_and_ocr_document` (passing `message_id=...` directly if provided in `[Quoted Message Context]`). It automatically reads, extracts, and analyzes content from all day-to-day WhatsApp file types: plain text (.txt, .log), Markdown (.md), Excel (.xlsx, .xls), CSV/TSV, Word (.docx, .doc, .rtf, .odt), PowerPoint (.pptx, .ppt, .odp), vCard contacts (.vcf), calendar events (.ics), Jupyter notebooks (.ipynb), ZIP archives (.zip), code files (.py, .js, .cpp, .java, etc.), and PDFs (including scanned PDFs with Gemini Vision OCR)!
     - *Search only*: Only use `find_documents` when the user explicitly asks to 'list', 'search', or 'check if I have' documents without asking to get or send them.
   - *Quoted / Swipe-Reply Context & Multi-Turn Follow-Ups*:
     - When `[Quoted Message Context]` is appended to the user's message, the user swipe-replied directly to that specific WhatsApp message or media item. Prioritize inspecting, transcribing, or delivering that exact `Message ID` or quoted content immediately before searching broadly!
     - Use recent conversation history naturally for follow-up requests (e.g. 'send me the second one', 'what about GST on that?').
   - *Images, Photos, Screenshots & Visual Items (AI Vision OCR)*:
     - When the user asks about an image, photo, receipt, bill photo, payment transfer screenshot, clothes/item color, whiteboard, or visual object shared in chats:
       1. If searching for a specific payment, receipt, or topic (e.g. 'payment receipt sent to Alex', '500 dollar invoice transfer'):
          - Search using `search_messages_by_keyword(query="payment")` or `find_voice_notes_and_media(media_type="image", query="payment", contact_or_chat="Alex")`.
          - When you find the image message, pass its exact `message_id` to `inspect_and_ocr_image(message_id=..., specific_question="What is the amount transferred?")`!
          - Or pass `contact_or_chat` and `topic_or_query` directly (e.g. `inspect_and_ocr_image(contact_or_chat="Alex", topic_or_query="receipt", specific_question=...)`).
       2. Never search unrelated chats or stories (`status@broadcast`). Focus strictly on the target chat or contact.
     - `inspect_and_ocr_image` downloads the image and performs AI Vision OCR to extract all visual content, colors, text, and numbers!
   - *Contact Cards (vCards)*: Locate phone numbers, shared contacts, organizations, and business cards using `find_shared_contacts`.
   - *Unsaved Contacts & Direct Phone Numbers*:
     - Users frequently ask about messages to/from unsaved contacts, delivery agents, businesses, or specific phone numbers (e.g. 'what did +91 98765 43210 send me?', 'who sent me the OTP?', 'did an unknown number message me about the parcel?').
     - WAmate tracks phone numbers and WhatsApp profile push names for all 1-on-1 direct messages (DMs).
     - Search by phone number (or digits without spaces/country code) using `search_chat_history(contact_or_chat_name=phone_number)` or `search_messages_by_keyword(query=phone_number)`.
   - *Polls & Surveys*: Retrieve poll questions, available choices/options, and settings using `get_polls`.
   - *Voice Notes & Audio Transcription*:
     - When the user asks what someone said in a voice note, audio message, or voice recording (or swipe-replies to one): CALL `transcribe_voice_note`!
     - `transcribe_voice_note` transcribes the spoken audio (handling English, Hindi, Hinglish, or regional languages cleanly) and caches the transcript permanently for instant future searches.
   - *Voice Notes, Audio & Media Metadata*: Search for voice messages, durations, audio clips, image captions, and videos using `find_voice_notes_and_media`.
   - *General Message / Topic Search*: Use `search_messages_by_keyword` or `search_chat_history`.

3. STRICT WHATSAPP FORMATTING RULES (MANDATORY):
   WhatsApp has very specific text styling rules and DOES NOT support standard Markdown headers, tables, or markdown links:
   - NEVER USE MARKDOWN HEADERS (#, ##, ###, ####, etc.). If you write '### Title', WhatsApp will literally print '### Title' on the user's screen!
     Instead, use bold text with a relevant emoji on its own line:
     📄 *26200792.pdf Details*
     📦 *Material Details:*
     🏢 *Company Information:*
   - ALWAYS use single asterisks for bold (*bold*), NEVER double asterisks (**bold**).
   - ALWAYS use single underscores for italics (_italics_).
   - NEVER use markdown tables (| col | col |). Instead, format all tables and structured data as clean bullet points:
     • *Item:* ScanJet Pro 2600 F11128441
     • *Quantity:* 1.000 NOS
     • *Delivery Date:* 23/10/2026
   - NEVER use markdown links like [Title](url). WhatsApp automatically turns bare URLs into clickable links. Write: *Title:* https://example.com or simply put the bare URL.
   - Use Unicode bullet points '• ' or '- ' for clear readability on mobile screens.
   - Keep answers clean, concise, mobile-friendly, and well-spaced.
   - When you send document(s) using `send_document_to_chat`, confirm to the user that the file(s) have been delivered directly to their chat above.
   - Be concise and direct. Do not hallucinate or make up messages or files not found in the tools.
   - If nothing is found, state what you searched for and offer helpful suggestions.
"""


class GeminiAgent:
    """Agent that communicates with Google Gemini and uses tools to query WhatsApp history."""

    CANDIDATE_MODELS = (
        "gemini-3.5-flash-lite",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.1-flash-lite",
        "gemini-3.8-flash",
    )

    def __init__(
        self,
        api_key: str | list[str],
        model_name: str,
        db: DatabaseManager,
        bot_client: Any = None,
        timezone_name: str = "Asia/Kolkata",
        memory_turns: int = 6,
        memory_ttl_minutes: int = 20,
    ):
        if isinstance(api_key, str):
            keys = [k.strip() for k in api_key.split(",") if k.strip()]
        else:
            keys = [str(k).strip() for k in api_key if str(k).strip()]
        if not keys:
            keys = [""]

        self._api_keys: list[str] = keys
        self._clients: list[genai.Client] = [genai.Client(api_key=k) for k in self._api_keys]
        self.client = self._clients[0]
        self._key_lock = threading.Lock()
        self._active_key_idx = 0
        self._key_cooldown_until: dict[int, float] = {}

        self.model_name = model_name
        self.db = db
        self.bot_client = bot_client
        self.timezone_name = timezone_name or "UTC"
        self.memory_turns = max(0, int(memory_turns))
        self.memory_ttl_seconds = max(0, int(memory_ttl_minutes) * 60)
        self._history: deque[tuple[float, str, str]] = deque(
            maxlen=max(1, self.memory_turns) if self.memory_turns > 0 else 1
        )
        self._history_lock = threading.Lock()

    def _get_candidate_models(self) -> list[str]:
        models = [self.model_name]
        for m in self.CANDIDATE_MODELS:
            if m not in models:
                models.append(m)
        return models

    def _run_across_keys_and_models(self, fn: Any) -> tuple[Any, Exception | None, bool]:
        """Execute a Gemini call across configured API keys (cooldown-aware) and fallback models."""
        candidate_models = self._get_candidate_models()
        last_error: Exception | None = None
        quota_exhausted = False
        now_ts = time.time()

        with self._key_lock:
            total = len(self._clients)
            start_idx = self._active_key_idx % max(1, total)
            ring = [(start_idx + i) % total for i in range(total)]
            non_cooled = [idx for idx in ring if now_ts >= self._key_cooldown_until.get(idx, 0.0)]
            cooled = [idx for idx in ring if idx not in non_cooled]
            ordered_indices = non_cooled + cooled

        for key_idx in ordered_indices:
            client = self._clients[key_idx]
            all_rate_limited = True

            for model in candidate_models:
                try:
                    result = fn(client, model)
                    with self._key_lock:
                        self._active_key_idx = key_idx
                        self._key_cooldown_until.pop(key_idx, None)
                    return result, None, False
                except Exception as e:
                    err_str = str(e)
                    last_error = e
                    is_429 = "RESOURCE_EXHAUSTED" in err_str or "429" in err_str
                    is_transient = any(
                        k in err_str for k in ("NOT_FOUND", "404", "UNAVAILABLE", "503", "500", "overloaded")
                    )
                    key_label = f"key#{key_idx + 1}" if len(self._clients) > 1 else "primary key"
                    logger.warning("Gemini request failed on %s / %s: %s", key_label, model, e)

                    if is_429:
                        quota_exhausted = True
                        time.sleep(1.5 if len(self._clients) == 1 else 0.5)
                        continue

                    all_rate_limited = False
                    if is_transient:
                        time.sleep(1.5)
                        continue

                    # Unrecoverable error (invalid argument / malformed payload)
                    return None, last_error, quota_exhausted

            if all_rate_limited and len(self._clients) > 1:
                with self._key_lock:
                    self._key_cooldown_until[key_idx] = time.time() + 300
                logger.info("All fallback models rate-limited on key#%d; rotating to next API key.", key_idx + 1)

        return None, last_error, quota_exhausted

    def clear_history(self) -> int:
        """Clear stored multi-turn conversation memory and return how many turns were removed."""
        with self._history_lock:
            count = len(self._history)
            self._history.clear()
            return count

    def _get_current_time_context(self) -> str:
        """Build live local date/time string so relative date questions resolve accurately."""
        try:
            tz = ZoneInfo(self.timezone_name)
            now = datetime.now(tz)
            tz_label = self.timezone_name
        except Exception:
            now = datetime.now().astimezone()
            tz_label = str(now.tzinfo or "local")

        return (
            f"Current Date & Time ({tz_label}): "
            f"{now.strftime('%A, %Y-%m-%d %I:%M %p')} "
            f"(Year={now.year}, Month={now.strftime('%B')}, Day={now.day}). "
            f"Always use this exact current date/time when resolving relative time expressions "
            f"like 'today', 'yesterday', 'this week', or 'last month'."
        )

    def _get_history_contents(self) -> list[types.Content]:
        """Return valid non-expired conversation turns formatted as Gemini Content objects."""
        if self.memory_turns <= 0:
            return []

        now_ts = time.time()
        with self._history_lock:
            if self.memory_ttl_seconds > 0:
                valid = [item for item in self._history if (now_ts - item[0]) <= self.memory_ttl_seconds]
                if len(valid) != len(self._history):
                    self._history.clear()
                    self._history.extend(valid)
            snapshot = list(self._history)

        contents: list[types.Content] = []
        for _, user_text, model_text in snapshot:
            contents.append(types.Content(role="user", parts=[types.Part.from_text(text=user_text)]))
            contents.append(types.Content(role="model", parts=[types.Part.from_text(text=model_text)]))
        return contents

    def _record_turn(self, user_prompt: str, model_answer: str) -> None:
        """Store a completed Q&A turn in bounded rolling memory."""
        if self.memory_turns <= 0 or not user_prompt or not model_answer:
            return
        trimmed_user = user_prompt[:2500]
        trimmed_model = model_answer[:3500]
        with self._history_lock:
            self._history.append((time.time(), trimmed_user, trimmed_model))

    def _analyze_media_bytes(
        self,
        file_bytes: bytes,
        mimetype: str,
        user_prompt: str,
        empty_msg: str,
        error_prefix: str,
    ) -> str:
        """Analyze raw image, PDF, or audio bytes using shared multi-key/model fallback."""
        part = types.Part.from_bytes(data=file_bytes, mime_type=mimetype)

        def _invoke(client: genai.Client, model: str) -> str:
            resp = client.models.generate_content(model=model, contents=[user_prompt, part])
            if resp and resp.text:
                return resp.text.strip()
            return empty_msg

        result, last_error, _ = self._run_across_keys_and_models(_invoke)
        if result is not None:
            return result

        logger.exception("%s: %s", error_prefix, last_error)
        return f"{error_prefix}: {str(last_error)}"

    def ocr_file_content(self, file_bytes: bytes, mimetype: str = "application/pdf", prompt: str = "") -> str:
        """Perform multimodal OCR on a scanned PDF, document, or image using Gemini Vision."""
        if not prompt:
            user_prompt = (
                "You are an expert OCR and document analysis engine. "
                "Carefully transcribe and extract all visible text, numbers, tables, line items, dates, names, amounts, "
                "stamps, handwritten notes, and headers from this document/image with high accuracy. "
                "Maintain structure and clearly report totals, taxes, and key points."
            )
        else:
            user_prompt = (
                f"You are an expert OCR and visual analysis engine. Examine this document/image carefully. "
                f"Extract all relevant text, numbers, line items, and visual information to address this specific question: {prompt}"
            )
        return self._analyze_media_bytes(
            file_bytes=file_bytes,
            mimetype=mimetype,
            user_prompt=user_prompt,
            empty_msg="OCR analysis completed but returned empty text.",
            error_prefix="OCR processing error",
        )

    def transcribe_audio_content(
        self, audio_bytes: bytes, mimetype: str = "audio/ogg", question: str = ""
    ) -> str:
        """Transcribe and summarize a WhatsApp voice note or audio clip using Gemini multimodal audio."""
        if question:
            user_prompt = (
                "Listen carefully to this WhatsApp voice note / audio recording. "
                "Provide an accurate verbatim transcript in the original language (with brief English translation if non-English), "
                f"and directly answer this specific question: {question}"
            )
        else:
            user_prompt = (
                "Listen carefully to this WhatsApp voice note / audio recording. "
                "Provide a clean, accurate transcript of everything spoken (preserving names, numbers, dates, and action items), "
                "and add a brief 1-2 line summary of the key takeaway."
            )
        return self._analyze_media_bytes(
            file_bytes=audio_bytes,
            mimetype=mimetype or "audio/ogg",
            user_prompt=user_prompt,
            empty_msg="Audio transcription completed but returned empty text.",
            error_prefix="Audio transcription error",
        )

    def _build_tools(self) -> list[Any]:
        """Build callable Python tool functions for Gemini."""

        def search_chat_history(contact_or_chat_name: str, limit: int = 50) -> list[dict[str, Any]]:
            """Fetch recent messages and media from a specific person or group chat in chronological order.
            Use this when the user asks about conversations with a specific contact (e.g. 'Pranav', 'Alice', 'Work Group').

            Args:
                contact_or_chat_name: Name, phone number, or JID of the contact or group chat.
                limit: Maximum number of recent messages to fetch (default: 50, up to 100).
            """
            return self.db.get_chat_history(contact_or_chat_name, limit=limit)

        def search_messages_by_keyword(query: str, contact_or_chat: str = "", limit: int = 25) -> list[dict[str, Any]]:
            """Search across WhatsApp messages, captions, and content for specific keywords, phrases, topics, or links.

            Args:
                query: Search keyword or phrase (e.g. 'meeting', 'zoom link', 'address', 'invoice', 'flight').
                contact_or_chat: Optional contact or chat name to restrict the search.
                limit: Maximum number of results to return (default: 25).
            """
            chat_jid = None
            if contact_or_chat:
                contacts = self.db.find_contacts(contact_or_chat, limit=1)
                if contacts:
                    chat_jid = contacts[0]["jid"]
            return self.db.search_messages(query, chat_jid=chat_jid, limit=limit)

        def find_documents(query: str = "", contact_or_chat: str = "", limit: int = 15) -> list[dict[str, Any]]:
            """Search specifically for document attachments (PDFs, spreadsheets, DOCX, presentations, invoices) by filename or keyword.
            Use this when the user asks to find a file, PDF, report, syllabus, ticket, or document.

            Args:
                query: Optional search keyword or filename part (e.g. 'resume', 'invoice', 'report', 'spec', 'pdf').
                contact_or_chat: Optional contact or chat name to filter by.
                limit: Maximum number of documents to return (default: 15).
            """
            return self.db.find_documents(query=query, chat_name_or_jid=contact_or_chat, limit=limit)

        def send_document_to_chat(document_query_or_name: str = "", message_id: str = "") -> str:
            """Locate and send the actual document file (PDF, DOCX, ZIP, etc.) directly into this WhatsApp self-chat.
            Use this when the user asks to send, get, receive, download, or deliver a document or file to them.

            Args:
                document_query_or_name: Document filename or topic keyword (e.g. 'Tax Invoice', 'Yash COA Exam Form', 'ITR Report').
                message_id: Optional exact WhatsApp message ID if known (for example from quoted message context).
            """
            if not self.bot_client:
                return "Document sending is not configured on this bot client."
            return self.bot_client.send_document_by_query(query=document_query_or_name, message_id=message_id)

        def read_and_ocr_document(
            document_query_or_name: str = "",
            specific_question: str = "",
            message_id: str = "",
        ) -> str:
            """Download a document (Excel spreadsheet .xlsx, Word .docx, PowerPoint .pptx, PDF, CSV, text) and extract/analyze its contents.
            Works on Excel worksheets, Word tables/paragraphs, PowerPoint slides, and both digital & scanned PDFs.
            Use this when the user asks what is inside a document, asks to check amounts/totals/dates/tables/slides, or asks to inspect/analyze any file.

            Args:
                document_query_or_name: Document filename or topic keyword (e.g. 'GDGC-WCE01.xlsx', 'report.docx', 'presentation.pptx', 'Tax Invoice.pdf').
                specific_question: Optional specific question to answer (e.g. 'What is the grand total in sheet 1?', 'Summarize slide 3', 'What are the payment terms?').
                message_id: Optional exact WhatsApp message ID if known (for example from quoted message context).
            """
            if not self.bot_client:
                return "Document reading is not configured on this bot client."
            return self.bot_client.read_and_ocr_document(
                query=document_query_or_name,
                specific_question=specific_question,
                message_id=message_id,
            )

        def read_document_content(document_query_or_name: str = "", message_id: str = "") -> str:
            """Alias for read_and_ocr_document."""
            return read_and_ocr_document(document_query_or_name=document_query_or_name, message_id=message_id)

        def inspect_and_ocr_image(
            message_id: str = "",
            contact_or_chat: str = "",
            topic_or_query: str = "",
            specific_question: str = "",
        ) -> str:
            """Inspect and run AI Vision OCR on a photo, screenshot, receipt, payment screenshot, bill, or image.
            Performs AI Vision OCR to extract text, tables, numbers, and visual content, and answers specific questions.

            Args:
                message_id: Optional exact WhatsApp message ID if you already found the image message via search. This is the fastest and most accurate method!
                contact_or_chat: Optional contact name or chat name to look for the image (e.g. 'Ajit Patil', 'Education').
                topic_or_query: Optional keyword or topic (e.g. 'paytm', 'payment', 'transfer', 'kurta', 'receipt', 'bill').
                specific_question: Specific question to answer about the image (e.g. 'What is the amount transferred?', 'What color is the dress?').
            """
            if not self.bot_client:
                return "Image inspection is not configured on this bot client."
            return self.bot_client.inspect_and_ocr_image(
                message_id=message_id,
                contact_or_chat=contact_or_chat,
                topic_or_query=topic_or_query,
                user_question=specific_question,
            )

        def find_shared_contacts(query: str = "", contact_or_chat: str = "", limit: int = 10) -> list[dict[str, Any]]:
            """Find contact cards (vCards) shared in chats by person's name, phone number, company, or profession.
            Use this when the user asks for a shared contact, phone number, doctor, plumber, electrician, etc.

            Args:
                query: Optional name, phone number, or keyword (e.g. 'Dr. Smith', 'plumber', 'driver').
                contact_or_chat: Optional chat name where the contact card was shared.
                limit: Maximum number of contacts to return (default: 10).
            """
            return self.db.find_shared_contacts(query=query, chat_name_or_jid=contact_or_chat, limit=limit)

        def get_polls(query: str = "", contact_or_chat: str = "", limit: int = 10) -> list[dict[str, Any]]:
            """Find polls and surveys created in group or direct chats, including questions and option choices.
            Use this when the user asks about polls, votes, survey options, or group decisions.

            Args:
                query: Optional keyword in the poll question or options (e.g. 'dinner', 'time', 'date').
                contact_or_chat: Optional group chat or contact name where the poll was created.
                limit: Maximum number of polls to return (default: 10).
            """
            return self.db.get_polls(query=query, chat_name_or_jid=contact_or_chat, limit=limit)

        def transcribe_voice_note(
            message_id: str = "",
            contact_or_chat: str = "",
            topic_or_query: str = "",
            specific_question: str = "",
        ) -> str:
            """Transcribe a WhatsApp voice note or audio file and optionally answer a question about it.
            Use this whenever the user asks what was said in a voice note, audio message, or voice recording.

            Args:
                message_id: Optional exact WhatsApp message ID if known (e.g. from quoted context or search results).
                contact_or_chat: Optional contact or chat name where the voice note was sent (e.g. 'Alex', 'Project Group').
                topic_or_query: Optional keyword or caption filter.
                specific_question: Optional question about what was spoken in the audio.
            """
            if not self.bot_client:
                return "Voice note transcription is not configured on this bot client."
            return self.bot_client.transcribe_voice_note(
                message_id=message_id,
                contact_or_chat=contact_or_chat,
                topic_or_query=topic_or_query,
                user_question=specific_question,
            )

        def find_voice_notes_and_media(
            media_type: str = "voice_note", query: str = "", contact_or_chat: str = "", limit: int = 15
        ) -> list[dict[str, Any]]:
            """Find voice notes, audio files, images, or videos sent in conversations.

            Args:
                media_type: Type of media to find ('voice_note', 'audio', 'image', 'video', 'location'). Default is 'voice_note'.
                query: Optional keyword or caption search.
                contact_or_chat: Optional contact or chat name to filter by.
                limit: Maximum number of items to return (default: 15).
            """
            return self.db.find_voice_notes_and_media(
                media_type=media_type, query=query, chat_name_or_jid=contact_or_chat, limit=limit
            )

        def find_contacts(query: str) -> list[dict[str, Any]]:
            """Search for contacts or group chats by name, push name, or phone number to find their exact JID.

            Args:
                query: Contact name or keyword.
            """
            return self.db.find_contacts(query, limit=10)

        def get_recent_chat_activity(hours: int = 48, limit: int = 50) -> list[dict[str, Any]]:
            """Get recent messages across all WhatsApp chats within the last N hours.
            Use this when the user asks what happened recently, for a daily recap, or who messaged recently.

            Args:
                hours: How many hours back to look (default: 48).
                limit: Maximum number of messages to fetch (default: 50).
            """
            return self.db.get_recent_activity(hours=hours, limit=limit)

        return [
            find_documents,
            send_document_to_chat,
            read_and_ocr_document,
            read_document_content,
            inspect_and_ocr_image,
            transcribe_voice_note,
            find_shared_contacts,
            get_polls,
            find_voice_notes_and_media,
            search_chat_history,
            search_messages_by_keyword,
            find_contacts,
            get_recent_chat_activity,
        ]

    def ask(self, user_query: str, quoted_context: str = "") -> str:
        """Send the user's query to Gemini with tool calling, runtime date context, multi-key rotation, and rolling memory."""
        effective_system_instruction = f"{SYSTEM_PROMPT}\n\n--- LIVE RUNTIME CONTEXT ---\n{self._get_current_time_context()}"
        prompt_to_send = f"{user_query}\n\n{quoted_context}".strip() if quoted_context else user_query
        history_contents = self._get_history_contents()

        config = types.GenerateContentConfig(
            system_instruction=effective_system_instruction,
            tools=self._build_tools(),
            temperature=0.3,  # Low temperature for factual fidelity
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                maximum_remote_calls=15
            ),
        )

        def _extract_or_synthesize(active_chat: Any, resp: Any) -> str | None:
            if resp and resp.text:
                return format_for_whatsapp(resp.text.strip())
            try:
                logger.info("Empty response text from AFC. Prompting chat for final synthesis...")
                synth_resp = active_chat.send_message(
                    "Based on all tool outputs and information gathered above, please formulate your final concise answer to the user now."
                )
                if synth_resp and synth_resp.text:
                    return format_for_whatsapp(synth_resp.text.strip())
            except Exception as synth_err:
                logger.warning("Synthesis prompt failed: %s", synth_err)
            return None

        def _invoke(client: genai.Client, model: str) -> str | None:
            chat = client.chats.create(
                model=model,
                config=config,
                history=history_contents or None,
            )
            response = chat.send_message(prompt_to_send)
            return _extract_or_synthesize(chat, response)

        result, last_error, quota_exhausted = self._run_across_keys_and_models(_invoke)
        if last_error is None:
            if result:
                self._record_turn(prompt_to_send, result)
                return result
            return "I couldn't retrieve a response from Gemini. Please try again."

        if quota_exhausted:
            return (
                "⚠️ *Gemini Daily Quota Exceeded (429)*\n\n"
                "All configured Gemini API key(s) and fallback models have reached their rate or daily RPD limit.\n\n"
                "• *Reset Time:* Free-tier daily limits reset at 00:00 UTC (05:30 IST).\n"
                "• *Background OCR:* Disabled by default to preserve your daily quota for chat queries.\n"
                "• *Solution:* Add backup keys via `GEMINI_API_KEYS=key1,key2` in `.env` or enable pay-as-you-go billing in Google AI Studio."
            )

        logger.exception("Error during Gemini generation: %s", last_error)
        return f"⚠️ *Error processing request:*\n`{str(last_error)}`"
