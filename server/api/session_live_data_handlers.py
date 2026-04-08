"""Live-data and DTC handlers for the session API layer."""

from __future__ import annotations

import logging
from typing import Any

from diagnostic_platform.contracts import BackendCapability, UnsupportedCapabilityError
from diagnostic_platform.runtime.session_actions import (
    clear_dtcs,
    ensure_session_capability,
    handle_live_data_stream_terminal_event,
    read_dtcs,
    start_live_data,
    stop_live_data,
)
from diagnostic_platform.runtime.session_state import (
    live_data_active as is_live_data_active,
)
from diagnostic_platform.runtime.session_streams import iter_scoped_agent_events
from diagnostic_platform.sse import session_agent_stream_scope
from src.gds2_orchestration.session_orchestrator import SessionStatus

from server.api.session_dependencies import (
    _runtime,
    get_backend,
    get_orchestrator,
)

logger = logging.getLogger(__name__)


def read_session_dtcs(data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    session_id = (data.get("session_id") or "").strip()

    if not session_id:
        return {"success": False, "error": "session_id required"}, 400

    try:
        orch = get_orchestrator()
        session = orch.get_session(session_id)
        ensure_session_capability(session, BackendCapability.READ_DTCS)
        result = read_dtcs(
            session,
            data,
            backend=get_backend(),
            emit_progress=lambda message: orch.emit_progress(session_id, message),
        )
        logger.info(
            "SESSION %s dtcs read count=%s page=%s",
            session_id,
            result["dtc_count"],
            result["page_context"],
        )
        return {
            "success": True,
            "session_id": session_id,
            "result": result,
        }, 200

    except KeyError as exc:
        return {"success": False, "error": str(exc)}, 404
    except UnsupportedCapabilityError as exc:
        return {"success": False, "error": str(exc)}, 501
    except ValueError as exc:
        return {"success": False, "error": str(exc)}, 400
    except Exception as exc:
        logger.exception("session_dtcs failed")
        return {"success": False, "error": str(exc)}, 500


def clear_session_dtcs(data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    session_id = (data.get("session_id") or "").strip()

    if not session_id:
        return {"success": False, "error": "session_id required"}, 400

    try:
        orch = get_orchestrator()
        session = orch.get_session(session_id)
        ensure_session_capability(session, BackendCapability.CLEAR_DTCS)
        result = clear_dtcs(
            _runtime(),
            session,
            data,
            backend=get_backend(),
            emit_progress=lambda message: orch.emit_progress(session_id, message),
        )
        logger.info(
            "SESSION %s clear_dtcs cleared=%s page=%s",
            session_id,
            result["cleared_count"],
            result["page_context"],
        )
        return {
            "success": True,
            "session_id": session_id,
            "result": result,
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
        logger.exception("session_clear_dtcs failed")
        return {"success": False, "error": str(exc)}, 500


def start_live_data_session(data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    session_id = (data.get("session_id") or "").strip()
    interval_ms = int(data.get("interval_ms", 100))

    if not session_id:
        return {"success": False, "error": "session_id required"}, 400

    try:
        orch = get_orchestrator()
        session = orch.get_session(session_id)
        ensure_session_capability(session, BackendCapability.LIVE_DATA)
        data_category, payload = start_live_data(
            _runtime(),
            session,
            data,
            interval_ms=interval_ms,
            backend=get_backend(),
            stream_scope=session_agent_stream_scope(session_id),
            emit_progress=lambda message: orch.emit_progress(session_id, message),
        )
        logger.info(
            "SESSION %s live_data started category=%s interval=%sms",
            session_id,
            data_category,
            interval_ms,
        )
        return {
            "success": True,
            "session_id": session_id,
            **payload,
        }, 200

    except KeyError as exc:
        return {"success": False, "error": str(exc)}, 404
    except UnsupportedCapabilityError as exc:
        return {"success": False, "error": str(exc)}, 501
    except ValueError as exc:
        return {"success": False, "error": str(exc)}, 400
    except Exception as exc:
        logger.exception("session_live_data_start failed")
        return {"success": False, "error": str(exc)}, 500


def stream_live_data_events(session_id: str, *, sse_response):
    if not session_id:
        return {"success": False, "error": "session_id required"}, 400

    try:
        session = get_orchestrator().get_session(session_id)
        ensure_session_capability(session, BackendCapability.LIVE_DATA)
        if not is_live_data_active(_runtime(), session_id):
            return {
                "success": False,
                "error": f"No active live data stream for {session_id}",
            }, 404
    except KeyError as exc:
        return {"success": False, "error": str(exc)}, 404
    except UnsupportedCapabilityError as exc:
        return {"success": False, "error": str(exc)}, 501

    return sse_response(
        iter_scoped_agent_events(
            scope=session_agent_stream_scope(session_id),
            session_id=session_id,
            on_message=lambda message: handle_live_data_stream_terminal_event(
                _runtime(),
                session,
                message,
            ),
        )
    )


def stop_live_data_session(data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    session_id = (data.get("session_id") or "").strip()

    if not session_id:
        return {"success": False, "error": "session_id required"}, 400

    try:
        orch = get_orchestrator()
        session = orch.get_session(session_id)
        if session.status != SessionStatus.RUNNING:
            return {
                "success": False,
                "error": f"Session not running (status={session.status.value})",
            }, 409
        ensure_session_capability(session, BackendCapability.LIVE_DATA)
        payload = stop_live_data(
            _runtime(),
            session,
            backend=get_backend(),
            emit_progress=lambda message: orch.emit_progress(session_id, message),
        )
        logger.info("SESSION %s live_data stopped", session_id)
        return {
            "success": True,
            "session_id": session_id,
            **payload,
        }, 200

    except KeyError as exc:
        return {"success": False, "error": str(exc)}, 404
    except UnsupportedCapabilityError as exc:
        return {"success": False, "error": str(exc)}, 501
    except Exception as exc:
        logger.exception("session_live_data_stop failed")
        return {"success": False, "error": str(exc)}, 500
