"""Navigation handlers for the session API layer."""

from __future__ import annotations

import logging
import time

from diagnostic_platform.contracts import BackendCapability, UnsupportedCapabilityError
from diagnostic_platform.runtime.session_actions import (
    abort_navigation,
    ensure_session_capability,
    navigation_status_payload,
    resolve_navigation,
    start_navigation,
    submit_navigation_decision,
)
from diagnostic_platform.runtime.session_streams import iter_navigation_events

from server.api.http_utils import internal_error_payload
from server.api.session_dependencies import (
    _runtime,
    get_backend,
    get_orchestrator,
)

logger = logging.getLogger(__name__)


def _read_text_field(
    data: dict[str, object],
    field: str,
    *,
    default: str = "",
) -> str:
    value = data.get(field, default)
    if value is None:
        value = default
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    return value.strip()


def start_navigation_session_for_business(data: dict[str, object]) -> tuple[dict[str, object], int]:
    try:
        session_id = _read_text_field(data, "session_id")
        goal = _read_text_field(data, "goal", default="Navigate to Data Display")

        if not session_id:
            return {"success": False, "error": "session_id required"}, 400

        orch = get_orchestrator()
        session = orch.get_session(session_id)
        ensure_session_capability(session, BackendCapability.NAVIGATION)
        backend = get_backend(session_id)
        nav_session = start_navigation(
            _runtime(),
            session,
            goal=goal,
            backend=backend,
            emit_progress=lambda message: orch.emit_progress(session_id, message),
        )
        session.updated_at = time.time()
        logger.debug(
            "SESSION %s navigation started nav_session=%s goal=%s",
            session_id,
            nav_session.session_id,
            goal,
        )
        return {
            "success": True,
            "session_id": session_id,
            "navigation_session_id": nav_session.session_id,
            "status": nav_session.status.value,
        }, 200

    except KeyError as exc:
        return {"success": False, "error": str(exc)}, 404
    except UnsupportedCapabilityError as exc:
        return {"success": False, "error": str(exc)}, 501
    except RuntimeError as exc:
        return {"success": False, "error": str(exc)}, 409
    except ValueError as exc:
        return {"success": False, "error": str(exc)}, 400
    except Exception as exc:
        logger.exception("session_navigate_start failed")
        return internal_error_payload(), 500


def stream_navigation_events(session_id: str, *, sse_response):
    if not session_id:
        return {"success": False, "error": "session_id required"}, 400

    try:
        session = get_orchestrator().get_session(session_id)
        ensure_session_capability(session, BackendCapability.NAVIGATION)
        _, nav_session = resolve_navigation(_runtime(), session)
    except KeyError as exc:
        return {"success": False, "error": str(exc)}, 404
    except LookupError as exc:
        return {"success": False, "error": str(exc)}, 404
    except UnsupportedCapabilityError as exc:
        return {"success": False, "error": str(exc)}, 501
    except Exception as exc:
        logger.exception("session_navigate_events failed to bind")
        return internal_error_payload(), 500

    return sse_response(
        iter_navigation_events(
            runtime=_runtime(),
            session=session,
            session_id=session_id,
            nav_session=nav_session,
        )
    )


def submit_navigation_decision_for_business(data: dict[str, object]) -> tuple[dict[str, object], int]:
    try:
        session_id = _read_text_field(data, "session_id")
        decision_id = _read_text_field(data, "decision_id")
        selected_item = _read_text_field(data, "selected_item")

        if not session_id:
            return {"success": False, "error": "session_id required"}, 400
        if not selected_item:
            return {"success": False, "error": "selected_item required"}, 400

        session = get_orchestrator().get_session(session_id)
        ensure_session_capability(session, BackendCapability.NAVIGATION)
        nav_session_id, payload = submit_navigation_decision(
            _runtime(),
            session,
            decision_id=decision_id,
            selected_item=selected_item,
        )
        logger.debug(
            "SESSION %s navigation decision submitted nav_session=%s selected=%s",
            session_id,
            nav_session_id,
            selected_item,
        )
        return {
            "success": True,
            "session_id": session_id,
            "navigation_session_id": nav_session_id,
            "selected_item": payload["selected_item"],
        }, 200

    except KeyError as exc:
        return {"success": False, "error": str(exc)}, 404
    except LookupError as exc:
        return {"success": False, "error": str(exc)}, 404
    except UnsupportedCapabilityError as exc:
        return {"success": False, "error": str(exc)}, 501
    except ValueError as exc:
        message = str(exc)
        status_code = 409 if "awaiting a decision" in message else 400
        return {"success": False, "error": message}, status_code
    except Exception as exc:
        logger.exception("session_navigate_decision failed")
        return internal_error_payload(), 500


def abort_navigation_session_for_business(data: dict[str, object]) -> tuple[dict[str, object], int]:
    try:
        session_id = _read_text_field(data, "session_id")

        if not session_id:
            return {"success": False, "error": "session_id required"}, 400

        session = get_orchestrator().get_session(session_id)
        ensure_session_capability(session, BackendCapability.NAVIGATION)
        nav_session_id, payload = abort_navigation(_runtime(), session)
        return {
            "success": True,
            "session_id": session_id,
            "navigation_session_id": nav_session_id,
            "status": payload["status"],
        }, 200

    except KeyError as exc:
        return {"success": False, "error": str(exc)}, 404
    except LookupError as exc:
        return {"success": False, "error": str(exc)}, 404
    except UnsupportedCapabilityError as exc:
        return {"success": False, "error": str(exc)}, 501
    except ValueError as exc:
        return {"success": False, "error": str(exc)}, 400
    except Exception as exc:
        logger.exception("session_navigate_abort failed")
        return internal_error_payload(), 500


def build_navigation_status_for_business(session_id: str) -> tuple[dict[str, object], int]:
    if not session_id:
        return {"success": False, "error": "session_id required"}, 400

    try:
        session = get_orchestrator().get_session(session_id)
        ensure_session_capability(session, BackendCapability.NAVIGATION)
        payload = navigation_status_payload(_runtime(), session)
        return {
            "success": True,
            "session_id": session_id,
            **payload,
        }, 200

    except KeyError as exc:
        return {"success": False, "error": str(exc)}, 404
    except LookupError as exc:
        return {"success": False, "error": str(exc)}, 404
    except UnsupportedCapabilityError as exc:
        return {"success": False, "error": str(exc)}, 501
    except Exception as exc:
        logger.exception("session_navigate_status failed")
        return internal_error_payload(), 500
