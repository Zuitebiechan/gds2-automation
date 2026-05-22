"""Shared observability helpers for cloud-side session/runtime flows."""

from __future__ import annotations

import logging
from typing import Any

from diagnostic_platform.observability import (
    LogContext,
    emit_event,
    flush_product_log_writers,
    get_cloud_observability_root,
    get_product_log_writer,
    read_active_session_snapshot,
)
from diagnostic_platform.proxy_local_live_data import (
    write_proxy_local_live_data_session_state,
)

logger = logging.getLogger(__name__)
_SESSION_TERMINAL_EVENT_TYPES = {
    "session.lifecycle.completed",
    "session.lifecycle.aborted",
    "session.lifecycle.failed",
}
_PROXY_LOCAL_LIVE_DATA_STATE_EVENTS = {
    "session.live_data.started",
    "session.live_data.stopped",
    "live_data.stream.error",
    *_SESSION_TERMINAL_EVENT_TYPES,
}


def _safe_connection_epoch(runtime: Any | None, session_id: str | None, backend: Any | None) -> str | None:
    if runtime is not None and session_id:
        getter = getattr(runtime, "get_connection_epoch", None)
        if callable(getter):
            try:
                value = getter(session_id)
                if value:
                    return str(value)
            except Exception:
                pass

    if backend is not None:
        preflight = getattr(backend, "preflight", None)
        if callable(preflight):
            try:
                snapshot = preflight() or {}
                value = snapshot.get("connection_epoch")
                if value:
                    return str(value)
            except Exception:
                pass
    return None


def build_session_log_context(
    *,
    runtime: Any | None = None,
    session: Any | None = None,
    backend: Any | None = None,
    operation_kind: str = "",
    page: str | None = None,
    module: str | None = None,
    data_category: str | None = None,
    connection_epoch: str | None = None,
) -> LogContext:
    session_id = getattr(session, "session_id", None)
    snapshot = read_active_session_snapshot() or {}
    return LogContext(
        session_id=str(session_id) if session_id else str(snapshot.get("session_id") or "") or None,
        connection_epoch=(
            connection_epoch
            or _safe_connection_epoch(runtime, session_id, backend)
            or (str(snapshot.get("connection_epoch")) if snapshot.get("connection_epoch") is not None else None)
        ),
        operation_kind=operation_kind or None,
        page=(
            page
            if page is not None
            else str(getattr(session, "current_page", "") or "") or str(snapshot.get("current_page") or "") or None
        ),
        module=(
            module
            if module is not None
            else str(getattr(session, "selected_module", "") or "") or str(snapshot.get("selected_module") or "") or None
        ),
        data_category=(
            data_category
            if data_category is not None
            else str(getattr(session, "selected_data_category", "") or "")
            or str(snapshot.get("selected_data_category") or "")
            or None
        ),
    )


def emit_session_runtime_event(
    event_type: str,
    *,
    runtime: Any | None = None,
    session: Any | None = None,
    backend: Any | None = None,
    operation_kind: str = "",
    status: str = "ok",
    failure_code: str | None = None,
    failure_domain: str = "unknown",
    reason: str | None = None,
    impact_scope: str = "session_runtime",
    page: str | None = None,
    module: str | None = None,
    data_category: str | None = None,
    connection_epoch: str | None = None,
    **extra: object,
) -> dict[str, Any]:
    context = build_session_log_context(
        runtime=runtime,
        session=session,
        backend=backend,
        operation_kind=operation_kind,
        page=page,
        module=module,
        data_category=data_category,
        connection_epoch=connection_epoch,
    )
    payload = emit_event(
        get_product_log_writer("session_runtime"),
        component="session_runtime",
        event_type=event_type,
        context=context,
        status=status,
        failure_code=failure_code,
        failure_domain=failure_domain,
        reason=reason,
        impact_scope=impact_scope,
        **extra,
    )
    if event_type in _PROXY_LOCAL_LIVE_DATA_STATE_EVENTS:
        _write_proxy_local_live_data_state(event_type, payload)
    if event_type in _SESSION_TERMINAL_EVENT_TYPES:
        try:
            from diagnostic_platform.observability_artifacts import start_session_artifact_materialization

            start_session_artifact_materialization(
                cloud_root=get_cloud_observability_root(),
                session_id=str(payload.get("session_id") or "").strip() or None,
                connection_epoch=str(payload.get("connection_epoch") or "").strip() or None,
                triggering_event_type=None,
                flush_callback=flush_product_log_writers,
            )
        except Exception:
            logger.debug(
                "Failed to queue explicit terminal session trace refresh for event_type=%s session_id=%s",
                event_type,
                payload.get("session_id"),
                exc_info=True,
            )
    return payload


def _write_proxy_local_live_data_state(
    event_type: str,
    payload: dict[str, Any],
) -> None:
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        return
    try:
        write_proxy_local_live_data_session_state(
            session_id=session_id,
            connection_epoch=(
                str(payload.get("connection_epoch"))
                if payload.get("connection_epoch") is not None
                else None
            ),
            live_data_active=(event_type == "session.live_data.started"),
            source_event_type=event_type,
            operation_kind=str(payload.get("operation_kind") or "") or None,
            current_page=str(payload.get("page") or "") or None,
            selected_module=str(payload.get("module") or "") or None,
            selected_data_category=str(payload.get("data_category") or "") or None,
        )
    except Exception:
        logger.debug(
            "Failed to update proxy-local live-data session state for event_type=%s session_id=%s",
            event_type,
            session_id,
            exc_info=True,
        )


def emit_gds2_ui_event(
    event_type: str,
    *,
    runtime: Any | None = None,
    session: Any | None = None,
    backend: Any | None = None,
    operation_kind: str = "",
    status: str = "ok",
    failure_code: str | None = None,
    failure_domain: str = "gds2_ui_or_agent",
    reason: str | None = None,
    impact_scope: str = "gds2_ui_or_agent",
    page: str | None = None,
    module: str | None = None,
    data_category: str | None = None,
    connection_epoch: str | None = None,
    **extra: object,
) -> dict[str, Any]:
    return emit_event(
        get_product_log_writer("gds2_ui_or_agent"),
        component="gds2_ui_or_agent",
        event_type=event_type,
        context=build_session_log_context(
            runtime=runtime,
            session=session,
            backend=backend,
            operation_kind=operation_kind,
            page=page,
            module=module,
            data_category=data_category,
            connection_epoch=connection_epoch,
        ),
        status=status,
        failure_code=failure_code,
        failure_domain=failure_domain,
        reason=reason,
        impact_scope=impact_scope,
        **extra,
    )


def emit_collector_event(
    event_type: str,
    *,
    status: str = "ok",
    failure_code: str | None = None,
    failure_domain: str = "gds2_ui_or_agent",
    reason: str | None = None,
    impact_scope: str = "agent_data_collector",
    **extra: object,
) -> dict[str, Any]:
    return emit_event(
        get_product_log_writer("agent_data_collector"),
        component="agent_data_collector",
        event_type=event_type,
        context=build_session_log_context(operation_kind="agent_data_collection"),
        status=status,
        failure_code=failure_code,
        failure_domain=failure_domain,
        reason=reason,
        impact_scope=impact_scope,
        **extra,
    )


__all__ = [
    "build_session_log_context",
    "emit_collector_event",
    "emit_gds2_ui_event",
    "emit_session_runtime_event",
]
