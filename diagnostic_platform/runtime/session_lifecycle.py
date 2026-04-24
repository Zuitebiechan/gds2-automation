"""Business-session lifecycle helpers."""

from __future__ import annotations

import logging
from typing import Any, Callable

from diagnostic_platform.session_models import SessionContext

from .session_actions import abort_active_execution
from diagnostic_platform.session_observability import emit_session_runtime_event
from .session_backends import summarize_backend_state
from .session_preflight import get_session_network_snapshot
from .session_state import (
    bind_business_session,
    clear_business_session,
    session_binding_payload,
)
from .worker_runtime import WorkerRuntime

logger = logging.getLogger(__name__)


def _rollback_failed_session_start(
    runtime: WorkerRuntime,
    *,
    orchestrator: Any,
    session: Any,
) -> None:
    session_id = getattr(session, "session_id", None)
    if not session_id:
        return

    try:
        clear_business_session(runtime, session_id)
    except Exception:
        logger.exception(
            "Failed to clear worker binding during start rollback for session %s",
            session_id,
        )

    rollback = getattr(orchestrator, "_rollback_start_session", None)
    if callable(rollback):
        try:
            rollback(session_id)
            return
        except Exception:
            logger.exception(
                "Failed to discard session %s during start rollback",
                session_id,
            )

    abort = getattr(orchestrator, "abort_session", None)
    if callable(abort):
        try:
            abort(session_id, "Session start failed before worker binding completed")
        except Exception:
            logger.exception(
                "Failed to abort session %s during start rollback fallback",
                session_id,
            )


def start_business_session(
    runtime: WorkerRuntime,
    *,
    orchestrator: Any,
    context: SessionContext,
) -> dict[str, Any]:
    """Start and bind one business session."""
    session = orchestrator.start_session(context)
    try:
        bind_business_session(runtime, session)
    except Exception:
        logger.warning(
            "SESSION %s start rollback after worker binding failure",
            getattr(session, "session_id", "unknown"),
            exc_info=True,
        )
        _rollback_failed_session_start(
            runtime,
            orchestrator=orchestrator,
            session=session,
        )
        raise
    logger.info(
        "SESSION %s started brand=%s backend=%s status=%s",
        session.session_id,
        context.brand,
        session.backend_name,
        session.status.value,
    )
    emit_session_runtime_event(
        "session.lifecycle.started",
        runtime=runtime,
        session=session,
        operation_kind="session.start",
        reason="session_started",
        brand=context.brand,
        backend_name=session.backend_name,
    )
    payload = {
        "success": True,
        "session_id": session.session_id,
        "status": session.status.value,
        "backend_name": session.backend_name,
        "workflow": session.backend_name,
        "capabilities": list(getattr(session, "capabilities", []) or []),
    }
    if session.pending_decision is not None:
        payload["decision"] = session.pending_decision.to_dict()
    return payload


def abort_business_session(
    runtime: WorkerRuntime,
    *,
    orchestrator: Any,
    session_id: str,
    reason: str,
    get_ai_engine: Callable[[], Any],
) -> dict[str, Any]:
    """Abort one business session and clear worker bindings."""
    session = orchestrator.get_session(session_id)
    runtime.cancel_operation(session_id)
    abort_active_execution(runtime, session, get_ai_engine=get_ai_engine)
    session = orchestrator.abort_session(session_id, reason)
    clear_business_session(runtime, session_id)
    logger.info("SESSION %s aborted reason=%s", session_id, reason or "user")
    return {
        "success": True,
        "session_id": session.session_id,
        "status": session.status.value,
    }


def build_session_status_payload(
    runtime: WorkerRuntime,
    *,
    orchestrator: Any,
    backend: Any,
    session_id: str,
) -> dict[str, Any]:
    """Build the public status payload for one business session."""
    session = orchestrator.get_session(session_id)
    backend_state_summary = summarize_backend_state(backend)
    network_snapshot = get_session_network_snapshot(
        orchestrator=orchestrator,
        backend=backend,
        session_id=session_id,
    )
    return {
        "success": True,
        **session.to_dict(),
        "backend_state_summary": backend_state_summary,
        **session_binding_payload(runtime, session_id),
        **network_snapshot,
    }
