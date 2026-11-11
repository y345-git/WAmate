import os
import shutil
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file if present
load_dotenv()


def _parse_api_keys() -> list[str]:
    """Parse GEMINI_API_KEYS (comma-separated) merged with GEMINI_API_KEY, preserving order."""
    keys: list[str] = []
    seen: set[str] = set()
    primary = os.getenv("GEMINI_API_KEY", "").strip()
    multi = os.getenv("GEMINI_API_KEYS", "")
    for raw in [primary, *multi.split(",")]:
        k = raw.strip()
        if k and k not in seen:
            seen.add(k)
            keys.append(k)
    return keys


class Config:
    # Gemini Configuration
    GEMINI_API_KEYS: list[str] = _parse_api_keys()
    GEMINI_API_KEY: str = GEMINI_API_KEYS[0] if GEMINI_API_KEYS else ""
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
    CONVERSATION_MEMORY_TURNS: int = int(os.getenv("CONVERSATION_MEMORY_TURNS", "6"))
    CONVERSATION_MEMORY_TTL_MINUTES: int = int(os.getenv("CONVERSATION_MEMORY_TTL_MINUTES", "20"))

    # WhatsApp Bot Configuration
    COMMAND_PREFIX: str = os.getenv("COMMAND_PREFIX", "!ask").strip()
    PHONE_NUMBER: str = os.getenv("PHONE_NUMBER", "").strip()

    # Storage Paths
    DATA_DIR: Path = Path(os.getenv("DATA_DIR", "./data")).resolve()
    SESSION_DB_PATH: Path = DATA_DIR / "whatsapp_session.sqlite3"
    MESSAGES_DB_PATH: Path = DATA_DIR / "whatsapp_messages.db"
    DOWNLOADS_DIR: Path = DATA_DIR / "downloads"

    # Logging & Timezone
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
    TZ: str = os.getenv("TZ", "Asia/Kolkata").strip()

    # Media Storage & Auto-Download Configuration (Zero Gemini API Cost)
    # Pre-downloads and stores ALL media files locally (images, videos, documents, voice notes, stickers, status updates)
    # so they remain permanently accessible even after WhatsApp's 30-day CDN link expiration.
    ENABLE_MEDIA_STORAGE: bool = os.getenv("ENABLE_MEDIA_STORAGE", "true").lower() in ("true", "1", "yes")
    MEDIA_STORAGE_LOOKBACK_DAYS: int = int(os.getenv("MEDIA_STORAGE_LOOKBACK_DAYS", "30"))
    MEDIA_STORAGE_MAX_SIZE_MB: int = int(os.getenv("MEDIA_STORAGE_MAX_SIZE_MB", "0"))  # 0 = Unlimited (no size limit)
    MEDIA_STORAGE_TYPES: str = os.getenv("MEDIA_STORAGE_TYPES", "all").lower()  # "all" downloads every media type
    MEDIA_STORAGE_INCLUDE_STATUSES: bool = os.getenv("MEDIA_STORAGE_INCLUDE_STATUSES", "true").lower() in ("true", "1", "yes")
    MEDIA_STORAGE_DELAY_SECONDS: float = float(os.getenv("MEDIA_STORAGE_DELAY_SECONDS", "1.5"))
    STATUS_MEDIA_RETENTION_DAYS: int = int(os.getenv("STATUS_MEDIA_RETENTION_DAYS", "0"))  # 0 = keep forever

    # Local CPU Background Indexing (0 Gemini API calls)
    # - ENABLE_LOCAL_OCR: uses system 'tesseract' CLI on stored images
    # - ENABLE_LOCAL_DOC_INDEX: uses pure-Python extractors on stored PDFs, Excel, Word, PowerPoint, CSV, and text files
    # Note: On-Demand AI Vision, PDF, and Voice Note OCR via Gemini is ALWAYS enabled during '!ask' queries.
    ENABLE_LOCAL_OCR: bool = os.getenv("ENABLE_LOCAL_OCR", "false").lower() in ("true", "1", "yes")
    ENABLE_LOCAL_DOC_INDEX: bool = os.getenv("ENABLE_LOCAL_DOC_INDEX", "true").lower() in ("true", "1", "yes")

    # Anti-Delete & Edit Vault (always preserves deleted/edited originals in SQLite; set true for live self-chat alerts)
    ANTI_DELETE_NOTIFY: bool = os.getenv("ANTI_DELETE_NOTIFY", "false").lower() in ("true", "1", "yes")

    # Lightweight Web Search & Media Browser UI (binds cleanly to localhost by default for SSH tunnel / Tailscale safety)
    ENABLE_WEB_UI: bool = os.getenv("ENABLE_WEB_UI", "true").lower() in ("true", "1", "yes")
    WEB_UI_HOST: str = os.getenv("WEB_UI_HOST", "127.0.0.1").strip() or "127.0.0.1"
    WEB_UI_PORT: int = int(os.getenv("WEB_UI_PORT", "8765").strip() or "8765")
    WEB_UI_PASSWORD: str = os.getenv("WEB_UI_PASSWORD", "").strip()

    @staticmethod
    def get_tesseract_path() -> str | None:
        """Return path to the system tesseract binary if installed, else None."""
        return shutil.which("tesseract")

    @classmethod
    def describe_local_ocr(cls, scanned_count: int | None = None, concise: bool = False) -> str:
        """Return consistent human-readable status for Local CPU OCR across !status and --status."""
        if not cls.ENABLE_LOCAL_OCR:
            return "Disabled"
        if not cls.get_tesseract_path():
            return "Missing 'tesseract' ⚠️ (run: sudo apt install -y tesseract-ocr)"
        suffix = f" ({scanned_count:,} scanned, 0 API cost)" if scanned_count is not None else " (0 API cost)"
        prefix = "Enabled" if concise else "Active 🟢"
        return f"{prefix}{suffix}"

    @classmethod
    def describe_local_doc_index(cls, indexed_count: int | None = None, concise: bool = False) -> str:
        """Return consistent human-readable status for Local Document Text Indexing across !status and --status."""
        if not cls.ENABLE_LOCAL_DOC_INDEX:
            return "Disabled"
        suffix = f" ({indexed_count:,} indexed, 0 API cost)" if indexed_count is not None else " (0 API cost)"
        prefix = "Enabled" if concise else "Active 🟢"
        return f"{prefix}{suffix}"

    @classmethod
    def describe_web_ui(cls, concise: bool = False, running: bool = True) -> str:
        """Return consistent human-readable status for the Web Search UI across !status and --status."""
        if not cls.ENABLE_WEB_UI:
            return "Disabled"
        url = f"http://{cls.WEB_UI_HOST}:{cls.WEB_UI_PORT}"
        if not running:
            return f"Configured ({url}, bind failed)"
        prefix = "Enabled" if concise else "Active 🌐"
        if cls.WEB_UI_PASSWORD:
            auth_note = "password-protected" if concise else "🔒"
        elif cls.WEB_UI_HOST in ("127.0.0.1", "localhost", "::1"):
            auth_note = "local-only"
        else:
            auth_note = "open host ⚠️"
        return f"{prefix} ({url}, {auth_note})"

    @classmethod
    def ensure_data_dir(cls) -> None:
        """Create the data directory if it does not already exist."""
        cls.DATA_DIR.mkdir(parents=True, exist_ok=True)

    @classmethod
    def validate(cls) -> None:
        """Validate critical configuration settings."""
        if not cls.GEMINI_API_KEYS:
            raise ValueError(
                "GEMINI_API_KEY (or GEMINI_API_KEYS) is not set! Please provide your Gemini API key in the .env file or environment."
            )
        cls.ensure_data_dir()
