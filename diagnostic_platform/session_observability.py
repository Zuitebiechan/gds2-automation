"""Shared observability helpers for cloud-side session/runtime flows."""

from __future__ import annotations

from typing import Any

from diagnostic_platform.observability import (
    LogContext,
    emit_event,
    get_product_log_writer,
    read_active_session_snapshot,
)


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
    return emit_event(
        get_product_log_writer("session_runtime"),
        component="session_runtime",
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
