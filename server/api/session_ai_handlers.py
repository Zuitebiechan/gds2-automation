"""AI diagnosis handlers for the session API layer."""

from __future__ import annotations

import logging
from typing import Any

from diagnostic_platform.contracts import (
    BackendCapability,
    UnsupportedCapabilityError,
)
from diagnostic_platform.runtime.session_actions import (
    ensure_session_capability,
    resolve_ai_event_stream,
    resolve_session_vehicle_context,
    retry_ai_diagnosis,
    start_ai_diagnosis,
)
from diagnostic_platform.runtime.session_streams import iter_ai_events

from server.api.http_utils import internal_error_payload
from server.api.session_dependencies import (
    _runtime,
    get_ai_engine,
    get_backend,
    get_orchestrator,
)

logger = logging.getLogger(__name__)


def _read_text_field(
    data: dict[str, Any],
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


def _normalize_vehicle_context_data(data: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(data)
    for field in ("vin", "module", "data_category"):
        normalized[field] = _read_text_field(data, field)
    return normalized


def start_ai_diagnose(data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    try:
        session_id = _read_text_field(data, "session_id")
        if not session_id:
            return {"success": False, "error": "session_id required"}, 400

        orch = get_orchestrator()
        session = orch.get_session(session_id)
        ensure_session_capability(session, BackendCapability.AI_DATA_COLLECTION)
        normalized_data = _normalize_vehicle_context_data(data)

        vehicle_context = resolve_session_vehicle_context(
            session,
            normalized_data,
            backend=get_backend(),
        )
        vehicle_context["brand"] = session.context.brand
        if session.context.model:
            vehicle_context["model"] = session.context.model
        data_category = vehicle_context["data_category"]
        if not data_category:
            return {"success": False, "error": "data_category required"}, 400

        backend = get_backend()
        engine = get_ai_engine()
        diagnostic_payload = backend.collect_ai_payload(
            vehicle_context=vehicle_context,
            data_category=data_category,
            collection_seconds=getattr(engine, "collection_seconds", 30),
        )
        ai_session_id = start_ai_diagnosis(
            _runtime(),
            session,
            vehicle_context=vehicle_context,
            data_category=data_category,
            engine=engine,
            diagnostic_payload=diagnostic_payload,
            emit_progress=lambda message: orch.emit_progress(session_id, message),
        )
        logger.info(
            "SESSION %s ai_diagnose started ai_session=%s module=%s category=%s",
            session_id,
            ai_session_id,
            vehicle_context.get("module") or "-",
            data_category,
        )
        return {
            "success": True,
            "session_id": session_id,
            "ai_session_id": ai_session_id,
            "message": "AI diagnosis started. Subscribe to /api/session/ai_diagnose/events for progress.",
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
        logger.exception("session_ai_diagnose failed")
        return internal_error_payload(), 500


def stream_ai_diagnose_events(session_id: str, *, sse_response):
    if not session_id:
        return {"success": False, "error": "session_id required"}, 400

    try:
        session = get_orchestrator().get_session(session_id)
        ensure_session_capability(session, BackendCapability.AI_DATA_COLLECTION)
        ai_session_id, event_queue = resolve_ai_event_stream(
            _runtime(),
            session,
            engine=get_ai_engine(),
        )

        logger.info(
            "SESSION %s ai_diagnose events bound ai_session=%s",
            session_id,
            ai_session_id,
        )

        return sse_response(
            iter_ai_events(
                runtime=_runtime(),
                session=session,
                session_id=session_id,
                event_queue=event_queue,
            )
        )

    except KeyError as exc:
        return {"success": False, "error": str(exc)}, 404
    except LookupError as exc:
        return {"success": False, "error": str(exc)}, 404
    except UnsupportedCapabilityError as exc:
        return {"success": False, "error": str(exc)}, 501
    except Exception as exc:
        logger.exception("session_ai_diagnose_events failed")
        return internal_error_payload(), 500


def retry_ai_diagnose(data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    try:
        session_id = _read_text_field(data, "session_id")
        cached_payload_id = _read_text_field(data, "cached_payload_id")

        if not session_id:
            return {"success": False, "error": "session_id required"}, 400
        if not cached_payload_id:
            return {"success": False, "error": "cached_payload_id required"}, 400

        orch = get_orchestrator()
        session = orch.get_session(session_id)
        ensure_session_capability(session, BackendCapability.AI_DATA_COLLECTION)
        normalized_data = _normalize_vehicle_context_data(data)

        vehicle_context = resolve_session_vehicle_context(
            session,
            normalized_data,
            backend=get_backend(),
        )
        engine = get_ai_engine()
        ai_session_id = retry_ai_diagnosis(
            _runtime(),
            session,
            cached_payload_id=cached_payload_id,
            vehicle_context=vehicle_context,
            engine=engine,
            emit_progress=lambda message: orch.emit_progress(session_id, message),
        )
        logger.info(
            "SESSION %s ai_diagnose retry started ai_session=%s payload=%s",
            session_id,
            ai_session_id,
            cached_payload_id,
        )
        return {
            "success": True,
            "session_id": session_id,
            "ai_session_id": ai_session_id,
            "message": "Retry started. Subscribe to /api/session/ai_diagnose/events for progress.",
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
        logger.exception("session_ai_diagnose_retry failed")
        return internal_error_payload(), 500
