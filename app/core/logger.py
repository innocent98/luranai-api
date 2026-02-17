import os
import sys
from typing import Any

from loguru import logger

from app.core.config import settings


def setup_logging() -> Any:
    """Configure logging based on environment."""
    logger.remove()

    log_level = settings.LOG_LEVEL.upper() if settings.LOG_LEVEL else "INFO"
    if settings.ENVIRONMENT == "development" and settings.LOG_LEVEL.lower() == "info":
        log_level = "DEBUG"

    if settings.LOG_TO_FILE:
        log_dir = os.path.dirname(settings.LOG_FILE_PATH) or "."
        os.makedirs(log_dir, exist_ok=True)
        logger.add(
            settings.LOG_FILE_PATH,
            rotation=f"{settings.LOG_FILE_ROTATION_MB} MB",
            retention=f"{settings.LOG_FILE_RETENTION_DAYS} days",
            compression=settings.LOG_FILE_COMPRESSION or None,
            level=log_level,
            enqueue=True,
            backtrace=False,
            diagnose=False,
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
        )

    if settings.ENVIRONMENT == "development":
        # Development: Human-readable colored output
        logger.add(
            sys.stderr,
            format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
            level=log_level,
            colorize=True,
        )
    else:
        # Production: JSON logs to stdout
        logger.add(
            sys.stdout,
            level=log_level,
            serialize=True,
            enqueue=True,
            backtrace=False,
            diagnose=False,
        )

        # Errors to stderr
        logger.add(
            sys.stderr,
            level="ERROR",
            serialize=True,
            enqueue=True,
            backtrace=False,
            diagnose=False,
        )

    return logger


log = setup_logging()
