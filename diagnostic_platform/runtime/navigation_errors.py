"""Navigation-domain runtime exceptions."""

from __future__ import annotations

from typing import Any


def _status_text(session_status: Any) -> str:
    return str(getattr(session_status, "value", session_status) or "").strip()


class NavigationDecisionError(ValueError):
    """Base class for navigation decision failures."""

    error_code = "navigation_decision_invalid"
    http_status = 400


class NavigationNotAwaitingDecisionError(NavigationDecisionError):
    """Raised when a decision is submitted outside an awaiting-decision state."""

    error_code = "navigation_not_awaiting_decision"
    http_status = 409

    def __init__(self, session_status: Any) -> None:
        self.session_status = _status_text(session_status)
        super().__init__(f"Session is not awaiting a decision (status={self.session_status})")


class NavigationSessionTerminatedError(NavigationDecisionError):
    """Raised when a terminal navigation session is mutated."""

    error_code = "navigation_session_terminated"
    http_status = 409

    def __init__(self, session_status: Any) -> None:
        self.session_status = _status_text(session_status)
        super().__init__(f"Session already terminated (status={self.session_status})")


class NavigationDecisionMismatchError(NavigationDecisionError):
    """Raised when a submitted decision id does not match the pending gate."""

    error_code = "navigation_decision_mismatch"
    http_status = 400

    def __init__(self, expected_decision_id: str, actual_decision_id: str) -> None:
        self.expected_decision_id = expected_decision_id
        self.actual_decision_id = actual_decision_id
        super().__init__(
            f"Decision ID mismatch: expected '{expected_decision_id}', got '{actual_decision_id}'"
        )
