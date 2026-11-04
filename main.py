import sys
import logging
import signal
import argparse
from pathlib import Path
from bot.config import Config
from bot.database import DatabaseManager
from bot.gemini_agent import GeminiAgent
from bot.whatsapp_client import WhatsAppBot


class WhatsmeowNoiseFilter(logging.Filter):
    """Filter out noisy, harmless internal warnings from whatsmeow (status stories decryption, socket EOF on close)."""

    IGNORED_SNIPPETS = (
        "in status@broadcast",
        "failed to close WebSocket",
        "failed to read frame header: EOF",
        "no sender key for",
        "Set status notification has unexpected content",
        "received message with old counter",
        "use of closed network connection",
        "connection reset by peer",
        "broken pipe",
        "websocket: close",
        "failed to decrypt group message",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(snippet in msg for snippet in self.IGNORED_SNIPPETS)


def setup_logging():
    log_level = getattr(logging, Config.LOG_LEVEL, logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    noise_filter = WhatsmeowNoiseFilter()
    for handler in logging.root.handlers:
        handler.addFilter(noise_filter)

    for logger_name in ["whatsmeow", "Whatsmeow", "whatsmeow.Client", "whatsmeow.Client.Socket", "neonize"]:
        l = logging.getLogger(logger_name)
        l.addFilter(noise_filter)

    try:
        import neonize.utils.log as _nlog
        if hasattr(_nlog, "clientlogger"):
            _nlog.clientlogger.addFilter(noise_filter)
        if hasattr(_nlog, "dblogger"):
            _nlog.dblogger.addFilter(noise_filter)
    except Exception:
        pass


def unlink_account(delete_data: bool = False) -> None:
    """CLI utility to unlink the WhatsApp account and optionally purge all saved data."""
    session_path = Path(Config.SESSION_DB_PATH)
    msg_db_path = Path(Config.MESSAGES_DB_PATH)
    downloads_dir = Path(Config.DOWNLOADS_DIR)

    print("\n" + "=" * 65)
    print("   WAmate - Account Unlink & Reset Utility")
    print("=" * 65)

    # 1. Delete session credentials
    deleted_session = False
    for p in [session_path, Path(f"{session_path}-shm"), Path(f"{session_path}-wal")]:
        if p.exists():
            try:
                p.unlink()
                deleted_session = True
                print(f"[OK] Removed session file: {p.name}")
            except Exception as e:
                print(f"[ERROR] Could not delete {p}: {e}")

    if not deleted_session:
        print("[INFO] No active session file found (already unlinked).")

    # 2. Optionally delete messages database and downloads
    if delete_data:
        deleted_db = False
        for p in [msg_db_path, Path(f"{msg_db_path}-shm"), Path(f"{msg_db_path}-wal")]:
            if p.exists():
                try:
                    p.unlink()
                    deleted_db = True
                    print(f"[OK] Removed database: {p.name}")
                except Exception as e:
                    print(f"[ERROR] Could not delete {p}: {e}")

        if downloads_dir.exists():
            count = 0
            for f in downloads_dir.glob("*"):
                if f.is_file():
                    try:
                        f.unlink()
                        count += 1
                    except Exception:
                        pass
            print(f"[OK] Purged {count} cached media files from {downloads_dir}.")

    print("=" * 65)
    if delete_data:
        print(" [SUCCESS] WhatsApp account unlinked and all data wiped clean.")
    else:
        print(" [SUCCESS] WhatsApp account unlinked (message history preserved).")
    print(" To link a new account, start WAmate normally:")
    print("   python3 main.py")
    print("=" * 65 + "\n")


def main():
    parser = argparse.ArgumentParser(description="WAmate - WhatsApp Self-Chat AI Second Brain")
    parser.add_argument(
        "--unlink",
        action="store_true",
        help="Unlink current WhatsApp account by deleting session credentials",
    )
    parser.add_argument(
        "--delete-data",
        action="store_true",
        help="When unlinking, also delete all indexed messages and downloaded files",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Unlink account and wipe all local databases and media completely",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip confirmation prompt and proceed immediately",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print database and indexing stats then exit",
    )
    args = parser.parse_args()

    if args.status:
        db = DatabaseManager(Config.MESSAGES_DB_PATH)
        stats = db.get_stats()

        stored_mb = 0.0
        if Config.DOWNLOADS_DIR.exists():
            try:
                stored_bytes = sum(f.stat().st_size for f in Config.DOWNLOADS_DIR.glob("*") if f.is_file())
                stored_mb = round(stored_bytes / (1024 * 1024), 2)
            except Exception:
                pass

        print("\n" + "=" * 60)
        print("   WAmate - Database & Indexing Status")
        print("=" * 60)
        print(f" • Messages Synced:      {stats['total_messages']:,}")
        print(f" • Contacts & Groups:    {stats['total_contacts']:,} ({stats['total_groups']} groups)")
        print(f" • History Date Range:   {stats['oldest_date']} -> {stats['newest_date']}")
        print(f" • Stored Media Files:   {stats.get('total_stored_media', 0)} ({stored_mb} MB on disk, 30-day protected)")
        print(f" • Media Storage Worker: {'Enabled (lookback: ' + str(Config.MEDIA_STORAGE_LOOKBACK_DAYS) + ' days)' if Config.ENABLE_MEDIA_STORAGE else 'Disabled'}")
        print(f" • OCR Media Cache:      {stats['total_ocr_cached']}")
        print(f" • Idle OCR Worker:      {'Enabled (lookback: ' + str(Config.IDLE_OCR_LOOKBACK_HOURS) + 'h, budget: ' + str(Config.IDLE_OCR_DAILY_BUDGET) + '/day)' if Config.ENABLE_IDLE_OCR else 'Disabled (On-Demand only)'}")
        print(f" • Database File Size:   {stats['db_size_mb']} MB")
        print("=" * 60 + "\n")
        sys.exit(0)

    if args.reset:
        args.unlink = True
        args.delete_data = True

    if args.unlink:
        if not args.yes:
            prompt_msg = (
                "Are you sure you want to unlink the account AND delete all saved messages/media? [y/N]: "
                if args.delete_data
                else "Are you sure you want to unlink the current WhatsApp account? (Session will be cleared) [y/N]: "
            )
            try:
                confirm = input(prompt_msg).strip().lower()
                if confirm not in ("y", "yes"):
                    print("Operation canceled.")
                    sys.exit(0)
            except (KeyboardInterrupt, EOFError):
                print("\nOperation canceled.")
                sys.exit(0)

        unlink_account(delete_data=args.delete_data)
        sys.exit(0)

    setup_logging()
    logger = logging.getLogger("main")

    print("""
    ================================================================
       __          __                      _       
       \\ \\        / /                     | |      
        \\ \\  /\\  / /__ _ _ __ ___   __ _| |_ ___ 
         \\ \\/  \\/ / _` | '_ ` _ \\ / _` | __/ _ \\
          \\  /\\  / (_| | | | | | | (_| | ||  __/
           \\/  \\/ \\__,_|_| |_| |_|\\__,_|\\__\\___|
                                                 
       WAmate - WhatsApp Self-Chat AI Second Brain
              powered by Google Gemini & Neonize
    ================================================================
    """)

    try:
        Config.validate()
    except ValueError as e:
        logger.error(str(e))
        print("\n[ERROR] Missing configuration:")
        print("Please create a .env file with your GEMINI_API_KEY.")
        print("You can copy .env.example to .env to get started.\n")
        sys.exit(1)

    logger.info("Initializing SQLite database at: %s", Config.MESSAGES_DB_PATH)
    db = DatabaseManager(Config.MESSAGES_DB_PATH)

    logger.info("Initializing Gemini Agent (model: %s)...", Config.GEMINI_MODEL)
    gemini = GeminiAgent(
        api_key=Config.GEMINI_API_KEY,
        model_name=Config.GEMINI_MODEL,
        db=db,
    )

    logger.info("Initializing WhatsApp Bot...")
    bot = WhatsAppBot(
        session_db_path=Config.SESSION_DB_PATH,
        db=db,
        gemini=gemini,
        command_prefix=Config.COMMAND_PREFIX,
        phone_number=Config.PHONE_NUMBER,
    )

    def shutdown(signum, frame):
        logger.info("Received termination signal, shutting down gracefully...")
        for log_name in ["whatsmeow", "Whatsmeow", "whatsmeow.Client", "whatsmeow.Client.Socket", "neonize"]:
            logging.getLogger(log_name).setLevel(logging.CRITICAL)
        try:
            import neonize.utils.log as _nlog
            if hasattr(_nlog, "clientlogger"):
                _nlog.clientlogger.setLevel(logging.CRITICAL)
            if hasattr(_nlog, "dblogger"):
                _nlog.dblogger.setLevel(logging.CRITICAL)
        except Exception:
            pass
        try:
            bot.stop()
        except Exception:
            try:
                bot.client.disconnect()
            except Exception:
                pass
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        bot.start()
    except KeyboardInterrupt:
        logger.info("Application stopped by user.")
    except Exception as e:
        logger.exception("Fatal error running WhatsApp Bot: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
