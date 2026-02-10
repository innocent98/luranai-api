import json
import sys
from typing import Any

from loguru import logger

from app.core.config import settings


def json_formatter(record: dict) -> str:
    """Format log records as JSON for production."""
    log_record = {
        "timestamp": record["time"].isoformat(),
        "level": record["level"].name,
        "logger": record["name"],
        "function": record["function"],
        "line": record["line"],
        "message": record["message"],
    }

    if record["exception"]:
        log_record["exception"] = {
            "type": record["exception"].type.__name__,
            "value": str(record["exception"].value),
        }

    if record["extra"]:
        log_record["extra"] = record["extra"]

    return json.dumps(log_record)


def setup_logging() -> Any:
    """Configure logging based on environment."""
    logger.remove()

    log_level = "DEBUG" if settings.ENVIRONMENT == "development" else "INFO"

    if settings.ENVIRONMENT == "development":
        # Development: Human-readable colored output
        logger.add(
            sys.stderr,
            format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
            level=log_level,
            colorize=True,
        )

        # File logging for development
        logger.add(
            "logs/app.log",
            rotation="10 MB",
            retention="1 week",
            level=log_level,
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
        )
    else:
        # Production: JSON logs to stdout
        logger.add(
            sys.stdout,
            format=json_formatter,  # type: ignore[arg-type]
            level=log_level,
            serialize=True,
        )

        # Errors to stderr
        logger.add(
            sys.stderr,
            format=json_formatter,  # type: ignore[arg-type]
            level="ERROR",
            serialize=True,
        )

    return logger


log = setup_logging()
