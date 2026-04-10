"""Shared low-level normalization helpers for platform/runtime code."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def strip_optional_text(value: Any) -> str:
    """Return one stripped text value or an empty string for non-strings."""
    return value.strip() if isinstance(value, str) else ""


def display_text(value: Any, *, default: str = "") -> str:
    """Return one user/display-safe text value."""
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return default
    return str(value).strip()


def mapping_or_empty(value: Any) -> dict[str, Any]:
    """Return one mapping value or an empty mapping for malformed payloads."""
    return value if isinstance(value, dict) else {}


def status_value(status: Any, *, default: str = "unknown") -> str:
    """Return one normalized status string."""
    if isinstance(status, str):
        return status
    value = getattr(status, "value", status)
    return str(value or default)


def sse_message_or_none(message: Any) -> str | None:
    """Return one SSE message string or None for malformed queue entries."""
    if isinstance(message, str):
        return message
    if isinstance(message, bytes):
        try:
            return message.decode("utf-8")
        except UnicodeDecodeError:
            logger.warning("Dropping undecodable SSE message bytes")
            return None
    logger.warning("Dropping malformed SSE message type=%s", type(message).__name__)
    return None


def json_dumps_safe(payload: Any, *, sort_keys: bool = False) -> str:
    """Serialize one payload without crashing on unexpected value types."""
    return json.dumps(payload, ensure_ascii=False, default=str, sort_keys=sort_keys)
