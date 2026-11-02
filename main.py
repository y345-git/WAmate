import sys
import logging
import signal
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


def main():
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
