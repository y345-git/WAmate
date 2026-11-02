import sys
import logging
import signal
import argparse
from pathlib import Path
from bot.config import Config
from bot.database import DatabaseManager
from bot.gemini_agent import GeminiAgent
from bot.whatsapp_client import WhatsAppBot


def setup_logging():
    log_level = getattr(logging, Config.LOG_LEVEL, logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


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
    args = parser.parse_args()

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
