import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file if present
load_dotenv()


class Config:
    # Gemini Configuration
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "").strip()
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()

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

    # Idle Background OCR Configuration
    ENABLE_IDLE_OCR: bool = os.getenv("ENABLE_IDLE_OCR", "true").lower() in ("true", "1", "yes")
    IDLE_OCR_LOOKBACK_HOURS: int = int(os.getenv("IDLE_OCR_LOOKBACK_HOURS", "48"))
    IDLE_OCR_DELAY_SECONDS: int = int(os.getenv("IDLE_OCR_DELAY_SECONDS", "12"))
    IDLE_OCR_MAX_PER_RUN: int = int(os.getenv("IDLE_OCR_MAX_PER_RUN", "100"))
    IDLE_OCR_INCLUDE_STATUSES: bool = os.getenv("IDLE_OCR_INCLUDE_STATUSES", "false").lower() in ("true", "1", "yes")

    @classmethod
    def ensure_data_dir(cls) -> None:
        """Create the data directory if it does not already exist."""
        cls.DATA_DIR.mkdir(parents=True, exist_ok=True)

    @classmethod
    def validate(cls) -> None:
        """Validate critical configuration settings."""
        if not cls.GEMINI_API_KEY:
            raise ValueError(
                "GEMINI_API_KEY is not set! Please provide your Gemini API key in the .env file or environment."
            )
        cls.ensure_data_dir()
