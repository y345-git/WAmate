import sys
import logging
import signal
import argparse
import threading
from pathlib import Path
from bot.config import Config
from bot.database import DatabaseManager
from bot.gemini_agent import GeminiAgent
from bot.web_ui import WebUIServer
from bot.whatsapp_client import WhatsAppBot


def _create_gemini_agent(db: DatabaseManager) -> GeminiAgent:
    """Instantiate the configured Gemini agent for WhatsApp and Web UI queries."""
    return GeminiAgent(
        api_key=Config.GEMINI_API_KEYS,
        model_name=Config.GEMINI_MODEL,
        db=db,
        timezone_name=Config.TZ,
        memory_turns=Config.CONVERSATION_MEMORY_TURNS,
        memory_ttl_minutes=Config.CONVERSATION_MEMORY_TTL_MINUTES,
    )


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
    parser.add_argument(
        "--prune-db",
        action="store_true",
        help="Remove redundant raw message BLOBs and VACUUM database to reclaim disk space",
    )
    parser.add_argument(
        "--web-only",
        action="store_true",
        help="Launch only the local Web Search & Media Browser UI without connecting WhatsApp",
    )
    args = parser.parse_args()

    if args.prune_db:
        db = DatabaseManager(Config.MESSAGES_DB_PATH)
        res = db.prune_redundant_raw_messages(vacuum=True)
        db.close_all()
        print("\n" + "=" * 60)
        print("   WAmate - Database Compaction & Pruning")
        print("=" * 60)
        print(f" • Rows Pruned:   {res['rows_pruned']:,}")
        print(f" • Size Before:   {res['before_mb']} MB")
        print(f" • Size After:    {res['after_mb']} MB")
        print(f" • Space Saved:   {res['saved_mb']} MB")
        print("=" * 60 + "\n")
        sys.exit(0)

    if args.status:
        db = DatabaseManager(Config.MESSAGES_DB_PATH)
        stats = db.get_stats()
        db.close_all()

        stored_mb = 0.0
        if Config.DOWNLOADS_DIR.exists():
            try:
                stored_bytes = sum(f.stat().st_size for f in Config.DOWNLOADS_DIR.glob("*") if f.is_file())
                stored_mb = round(stored_bytes / (1024 * 1024), 2)
            except Exception:
                pass

        dedup_note = f", {stats['dedup_saved_files']} deduped" if stats.get("dedup_saved_files", 0) > 0 else ""
        print("\n" + "=" * 60)
        print("   WAmate - Database & Indexing Status")
        print("=" * 60)
        print(f" • Messages Synced:      {stats['total_messages']:,}")
        print(f" • Contacts & Groups:    {stats['total_contacts']:,} ({stats['total_groups']} groups)")
        print(f" • History Date Range:   {stats['oldest_date']} -> {stats['newest_date']}")
        print(f" • Stored Media Files:   {stats.get('total_stored_media', 0)} ({stored_mb} MB on disk{dedup_note})")
        print(f" • Media Storage Worker: {'Enabled (lookback: ' + str(Config.MEDIA_STORAGE_LOOKBACK_DAYS) + ' days)' if Config.ENABLE_MEDIA_STORAGE else 'Disabled'}")
        print(f" • AI Vision & Voice OCR: On-Demand ⚡ ({stats['total_ocr_cached']} cached)")
        print(f" • Local CPU OCR:        {Config.describe_local_ocr(stats.get('total_local_ocr', 0), concise=True)}")
        print(f" • Local Doc Index:      {Config.describe_local_doc_index(stats.get('total_local_docs', 0), concise=True)}")
        print(f" • Anti-Delete Vault:    Active 🛡️ ({stats.get('total_deleted', 0)} deleted, {stats.get('total_edited', 0)} edited preserved)")
        print(f" • Web Search UI:        {Config.describe_web_ui(concise=True)}")
        print(f" • Database File Size:   {stats['db_size_mb']} MB (raw BLOB cache: {stats.get('raw_blob_mb', 0.0)} MB)")
        print(f" • Gemini API Keys:      {len(Config.GEMINI_API_KEYS)} configured")
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

    if args.web_only:
        Config.ensure_data_dir()
        logger.info("Starting WAmate in --web-only mode (WhatsApp connection disabled).")
        logger.info("Initializing SQLite database at: %s", Config.MESSAGES_DB_PATH)
        db = DatabaseManager(Config.MESSAGES_DB_PATH)
        gemini = None
        if Config.GEMINI_API_KEYS:
            logger.info(
                "Initializing Gemini Agent (model: %s, keys: %d)...",
                Config.GEMINI_MODEL,
                len(Config.GEMINI_API_KEYS),
            )
            gemini = _create_gemini_agent(db)
        else:
            logger.warning("GEMINI_API_KEYS not set; Ask AI tab disabled in --web-only mode.")

        web_ui = WebUIServer(
            db=db,
            host=Config.WEB_UI_HOST,
            port=Config.WEB_UI_PORT,
            password=Config.WEB_UI_PASSWORD,
            gemini=gemini,
            downloads_dir=Config.DOWNLOADS_DIR,
        )
        if not web_ui.start():
            logger.error("Failed to start Web UI on http://%s:%d", Config.WEB_UI_HOST, Config.WEB_UI_PORT)
            db.close_all()
            sys.exit(1)

        stop_event = threading.Event()

        def _stop_web_only(signum, frame):
            logger.info("Received termination signal, stopping Web UI...")
            stop_event.set()

        signal.signal(signal.SIGINT, _stop_web_only)
        signal.signal(signal.SIGTERM, _stop_web_only)
        try:
            while not stop_event.wait(timeout=1.0):
                pass
        finally:
            web_ui.stop()
            db.close_all()
        sys.exit(0)

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

    logger.info(
        "Initializing Gemini Agent (model: %s, keys: %d)...",
        Config.GEMINI_MODEL,
        len(Config.GEMINI_API_KEYS),
    )
    gemini = _create_gemini_agent(db)

    logger.info("Initializing WhatsApp Bot...")
    bot = WhatsAppBot(
        session_db_path=Config.SESSION_DB_PATH,
        db=db,
        gemini=gemini,
        command_prefix=Config.COMMAND_PREFIX,
        phone_number=Config.PHONE_NUMBER,
    )

    web_ui: WebUIServer | None = None
    if Config.ENABLE_WEB_UI:
        web_ui = WebUIServer(
            db=db,
            host=Config.WEB_UI_HOST,
            port=Config.WEB_UI_PORT,
            password=Config.WEB_UI_PASSWORD,
            gemini=gemini,
            bot_client=bot,
            downloads_dir=Config.DOWNLOADS_DIR,
        )
        web_ui.start()
        bot.web_ui = web_ui

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
        if web_ui is not None:
            web_ui.stop()
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
    finally:
        if web_ui is not None:
            web_ui.stop()
        db.close_all()


if __name__ == "__main__":
    main()
