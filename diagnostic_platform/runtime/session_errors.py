"""Session-domain runtime exceptions."""

from __future__ import annotations

from typing import Any


def _status_text(session_status: Any) -> str:
    raw = str(getattr(session_status, "value", session_status) or "").strip()
    if raw.startswith("SessionStatus."):
        return raw.split(".", 1)[1].lower()
    return raw


class SessionNotRunningError(ValueError):
    """Raised when a running-session-only operation is requested elsewhere."""

    def __init__(self, session_status: Any) -> None:
        self.session_status = _status_text(session_status)
        super().__init__(f"Session not running (status={self.session_status})")
