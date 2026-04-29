"""Business-session lifecycle helpers."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import threading
from typing import Any, Callable

from diagnostic_platform.session_models import SessionContext

from .diagnostics_runtime import stop_live_data_stream as stop_diagnostics_live_data_stream
from .navigation_runtime import abort_navigation_session as abort_worker_navigation_session
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
_TERMINAL_SESSION_STATUSES = {"completed", "failed", "aborted"}


@dataclass(frozen=True)
class _AbortExecutionSnapshot:
    navigation_session_id: str | None
    ai_session_id: str | None
    live_data_active: bool
    backend: Any | None
    navigation_handle: Any | None


def _session_status_value(session: Any) -> str:
    status = getattr(session, "status", "")
    return str(getattr(status, "value", status) or "")


def _capture_abort_execution_snapshot(
    runtime: WorkerRuntime,
    *,
    session_id: str,
) -> _AbortExecutionSnapshot:
    binding = runtime.get_business_session_binding(session_id)
    bundle = runtime.get_active_backend_bundle(session_id)
    backend = bundle.backend if bundle is not None else runtime.backend
    navigation_handle = bundle.navigation_handle if bundle is not None else None
    return _AbortExecutionSnapshot(
        navigation_session_id=binding.navigation_session_id,
        ai_session_id=binding.ai_session_id,
        live_data_active=bool(binding.live_data_active),
        backend=backend,
        navigation_handle=navigation_handle,
    )


def _abort_snapshot_navigation(
    runtime: WorkerRuntime,
    *,
    session: Any,
    snapshot: _AbortExecutionSnapshot,
) -> None:
    nav_sid = snapshot.navigation_session_id
    if not nav_sid:
        return

    try:
        if snapshot.navigation_handle is not None:
            snapshot.navigation_handle.abort_navigation_session(runtime, nav_sid)
        else:
            abort_worker_navigation_session(runtime, nav_sid)
    except (LookupError, ValueError):
        pass
    except Exception:
        logger.exception(
            "Failed to abort navigation session %s for business session %s",
            nav_sid,
            getattr(session, "session_id", "unknown"),
        )
    finally:
        runtime.clear_navigation_session(getattr(session, "session_id", ""))


def _abort_snapshot_live_data(
    runtime: WorkerRuntime,
    *,
    session: Any,
    snapshot: _AbortExecutionSnapshot,
) -> None:
    if not snapshot.live_data_active:
        return

    try:
        if snapshot.backend is not None:
            stop_diagnostics_live_data_stream(runtime, backend=snapshot.backend)
    except Exception:
        logger.exception(
            "Failed to stop live data for business session %s",
            getattr(session, "session_id", "unknown"),
        )
    finally:
        session_id = getattr(session, "session_id", "")
        binding = runtime.get_business_session_binding(session_id)
        if binding.session_id == session_id:
            runtime.set_live_data_active(session_id, False)


def _abort_snapshot_ai(
    runtime: WorkerRuntime,
    *,
    session: Any,
    snapshot: _AbortExecutionSnapshot,
    get_ai_engine: Callable[[], Any],
) -> None:
    ai_sid = snapshot.ai_session_id
    if not ai_sid:
        return

    try:
        get_ai_engine().abort_session(ai_sid)
    except Exception:
        logger.exception(
            "Failed to abort AI session %s for business session %s",
            ai_sid,
            getattr(session, "session_id", "unknown"),
        )
    finally:
        runtime.clear_ai_session(getattr(session, "session_id", ""))


def _cleanup_aborted_session_execution(
    runtime: WorkerRuntime,
    *,
    session: Any,
    snapshot: _AbortExecutionSnapshot,
    get_ai_engine: Callable[[], Any],
) -> None:
    session_id = getattr(session, "session_id", "unknown")
    try:
        _abort_snapshot_navigation(runtime, session=session, snapshot=snapshot)
        _abort_snapshot_live_data(runtime, session=session, snapshot=snapshot)
        _abort_snapshot_ai(
            runtime,
            session=session,
            snapshot=snapshot,
            get_ai_engine=get_ai_engine,
        )
    except Exception:
        logger.exception("Failed to cleanup aborted session execution for %s", session_id)
    finally:
        try:
            clear_business_session(runtime, session_id)
        except Exception:
            logger.exception("Failed to clear worker binding after abort for %s", session_id)


def _start_abort_cleanup_thread(
    runtime: WorkerRuntime,
    *,
    session: Any,
    snapshot: _AbortExecutionSnapshot,
    get_ai_engine: Callable[[], Any],
) -> None:
    session_id = str(getattr(session, "session_id", "") or "unknown")
    thread = threading.Thread(
        target=_cleanup_aborted_session_execution,
        kwargs={
            "runtime": runtime,
            "session": session,
            "snapshot": snapshot,
            "get_ai_engine": get_ai_engine,
        },
        daemon=True,
        name=f"session-abort-cleanup-{session_id}",
    )
    thread.start()


def _clear_stale_worker_binding(
    runtime: WorkerRuntime,
    *,
    orchestrator: Any,
    new_session_id: str,
) -> None:
    binding = runtime.get_business_session_binding()
    bound_session_id = binding.session_id
    if not bound_session_id or bound_session_id == new_session_id:
        return

    try:
        bound_session = orchestrator.get_session(bound_session_id)
    except KeyError:
        clear_business_session(runtime, bound_session_id)
        logger.warning(
            "Cleared stale worker session binding before binding new session "
            "old_session_id=%s new_session_id=%s reason=session_not_found",
            bound_session_id,
            new_session_id,
        )
        return

    status_value = _session_status_value(bound_session)
    if status_value in _TERMINAL_SESSION_STATUSES:
        clear_business_session(runtime, bound_session_id)
        logger.warning(
            "Cleared stale worker session binding before binding new session "
            "old_session_id=%s new_session_id=%s reason=terminal_session status=%s",
            bound_session_id,
            new_session_id,
            status_value,
        )


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
        _clear_stale_worker_binding(
            runtime,
            orchestrator=orchestrator,
            new_session_id=session.session_id,
        )
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
    execution_snapshot = _capture_abort_execution_snapshot(runtime, session_id=session_id)
    runtime.cancel_operation(session_id)
    session = orchestrator.abort_session(session_id, reason)
    _start_abort_cleanup_thread(
        runtime,
        session=session,
        snapshot=execution_snapshot,
        get_ai_engine=get_ai_engine,
    )
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
