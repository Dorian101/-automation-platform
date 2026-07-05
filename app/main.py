from core.logger import setup_logger
from core.config import Config


def main():
    logger = setup_logger()

    logger.info(f"Starting {Config.APP_NAME}")
    logger.info(f"Debug mode: {Config.DEBUG}")


if __name__ == "__main__":
    main()