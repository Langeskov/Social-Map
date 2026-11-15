"""Application-owned rotating logs; never log business content or secrets."""
import logging
from logging.handlers import RotatingFileHandler
from app.config.settings import AppConfig


def configure_logging(config: AppConfig) -> logging.Logger:
    config.log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("app")
    logger.setLevel(config.log_level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    console = logging.StreamHandler()
    application = RotatingFileHandler(config.log_dir / "application.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    errors = RotatingFileHandler(config.log_dir / "error.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    errors.setLevel(logging.ERROR)
    for handler in (console, application, errors):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger
