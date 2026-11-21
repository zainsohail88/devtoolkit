"""Logging set-up from the LOG_LEVEL environment variable."""

from __future__ import annotations

import logging

from messages import UNKNOWN_LOG_LEVEL
from settings import DEFAULT_LOG_LEVEL, LOG_FORMAT, LOG_LEVELS, LOGGER_NAME

logger = logging.getLogger(LOGGER_NAME)


def resolve_log_level(value: str | None) -> tuple[str, str | None]:
    """Return (level, warning). An unknown LOG_LEVEL falls back to INFO and keeps running."""
    level = (value or DEFAULT_LOG_LEVEL).strip().upper()
    if level in LOG_LEVELS:
        return level, None
    allowed = ", ".join(name.lower() for name in LOG_LEVELS)
    return DEFAULT_LOG_LEVEL, UNKNOWN_LOG_LEVEL.format(value=value, allowed=allowed)


def configure_logging(value: str | None) -> str:
    """Set up logging from a LOG_LEVEL value and return the level used."""
    level, warning = resolve_log_level(value)
    logging.basicConfig(level=level, format=LOG_FORMAT)
    if warning:
        logger.warning(warning)
    return level
