"""Business-session lifecycle helpers."""

from __future__ import annotations

import logging
from typing import Any, Callable

from src.gds2_orchestration.session_orchestrator import SessionContext

from .session_actions import abort_active_execution
from .session_backends import summarize_backend_state
from .session_preflight import get_session_network_snapshot
from .session_state import (
    bind_business_session,
    clear_business_session,
    session_binding_payload,
)
from .worker_runtime import WorkerRuntime

logger = logging.getLogger(__name__)


def start_business_session(
    runtime: WorkerRuntime,
    *,
    orchestrator: Any,
    context: SessionContext,
) -> dict[str, Any]:
    """Start and bind one business session."""
    session = orchestrator.start_session(context)
    bind_business_session(runtime, session)
    logger.info(
        "SESSION %s started brand=%s backend=%s status=%s",
        session.session_id,
        context.brand,
        session.backend_name,
        session.status.value,
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
    return {
        "success": True,
        **session.to_dict(),
        "backend_state_summary": summarize_backend_state(backend),
        **session_binding_payload(runtime, session_id),
        **get_session_network_snapshot(
            orchestrator=orchestrator,
            backend=backend,
            session_id=session_id,
        ),
    }
