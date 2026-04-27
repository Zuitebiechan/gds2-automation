"""HTTP-facing request/response helpers for the API layer."""

from __future__ import annotations

import re
from typing import Any

from diagnostic_platform.runtime.navigation_errors import NavigationDecisionError
from diagnostic_platform.runtime.session_errors import SessionNotRunningError

INTERNAL_SERVER_ERROR_MESSAGE = "Internal server error"
JSON_OBJECT_REQUIRED_MESSAGE = "JSON request body must be an object"
_SESSION_NOT_RUNNING_RE = re.compile(r"Session not running \(status=([^)]+)\)")


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


def error_payload(
    message: str,
    *,
    error_code: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a machine-readable JSON error payload."""
    payload: dict[str, Any] = {
        "success": False,
        "error": str(message),
    }
    if error_code:
        payload["error_code"] = error_code
    if extra:
        payload.update(extra)
    return payload


def session_not_running_payload(session_status: str) -> dict[str, Any]:
    """Return one stable payload for operations that require a running session."""
    status_text = _normalize_session_status_text(session_status)
    return error_payload(
        f"Session not running (status={status_text})",
        error_code="session_not_running",
        extra={"session_status": status_text},
    )


def session_state_error_payload(error: Any) -> tuple[dict[str, Any], int]:
    """Classify common session state errors into stable payloads."""
    if isinstance(error, SessionNotRunningError):
        return session_not_running_payload(error.session_status), 409

    text = str(error)
    match = _SESSION_NOT_RUNNING_RE.search(text)
    if match:
        return session_not_running_payload(match.group(1)), 409
    return error_payload(text), 400


def navigation_decision_error_payload(error: Any) -> tuple[dict[str, Any], int]:
    """Classify navigation decision errors into stable payloads."""
    if isinstance(error, NavigationDecisionError):
        return (
            error_payload(str(error), error_code=error.error_code),
            error.http_status,
        )

    text = str(error)
    lowered = text.lower()
    if "not awaiting a decision" in lowered:
        return error_payload(text, error_code="navigation_not_awaiting_decision"), 409
    if "already terminated" in lowered or "terminated" in lowered:
        return error_payload(text, error_code="navigation_session_terminated"), 409
    if "decision id mismatch" in lowered:
        return error_payload(text, error_code="navigation_decision_mismatch"), 400
    return error_payload(text, error_code="navigation_decision_invalid"), 400


def _normalize_session_status_text(session_status: Any) -> str:
    raw = str(session_status or "").strip()
    if raw.startswith("SessionStatus."):
        return raw.split(".", 1)[1].lower()
    return raw
