"""HTTP-facing request/response helpers for the API layer."""

from __future__ import annotations

from typing import Any

INTERNAL_SERVER_ERROR_MESSAGE = "Internal server error"
JSON_OBJECT_REQUIRED_MESSAGE = "JSON request body must be an object"


class RequestPayloadError(ValueError):
    """Raised when a request body is not the expected JSON object payload."""


def require_json_object(request_obj: Any | None = None) -> dict[str, Any]:
    """Return the current request JSON payload as an object."""
    if request_obj is None:
        from flask import request as request_obj

    get_json = getattr(request_obj, "get_json", None)
    if callable(get_json):
        try:
            payload = get_json(silent=True)
        except TypeError:
            payload = get_json()
    else:
        payload = getattr(request_obj, "json", None)

    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise RequestPayloadError(JSON_OBJECT_REQUIRED_MESSAGE)
    return payload


def read_text_mapping_field(
    mapping: Any,
    field: str,
    *,
    default: str = "",
) -> str:
    """Read one stripped text value from a mapping-like object."""
    getter = getattr(mapping, "get", None)
    value = getter(field, default) if callable(getter) else default
    if value is None:
        value = default
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value.strip()


def internal_error_payload(*, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a sanitized 500-class JSON payload."""
    payload: dict[str, Any] = {
        "success": False,
        "error": INTERNAL_SERVER_ERROR_MESSAGE,
    }
    if extra:
        payload.update(extra)
    return payload
