"""Business-session action helpers for AI, navigation, and live diagnostics flows."""

from __future__ import annotations

import logging
from typing import Any, Callable

from diagnostic_platform.action_schema import ActionStep, GDS2Action
from diagnostic_platform.contracts import (
    BackendCapability,
    DiagnosticBackend,
    UnsupportedCapabilityError,
)
from diagnostic_platform.session_models import SessionStatus
from diagnostic_platform.safe_utils import (
    mapping_or_empty as _mapping_or_empty,
    status_value as _status_value,
    strip_optional_text as _strip_optional_text,
)

from .diagnostics_runtime import (
    start_live_data_stream as start_diagnostics_live_data_stream,
    stop_live_data_stream as stop_diagnostics_live_data_stream,
)
from .navigation_runtime import (
    NavSessionStatus,
    abort_navigation_session as abort_worker_navigation_session,
    get_navigation_session as get_worker_navigation_session,
    start_navigation_session as start_worker_navigation_session,
    submit_navigation_decision as submit_worker_navigation_decision,
)
from .session_state import (
    ai_session_id,
    bind_ai_session,
    bind_navigation_session,
    clear_ai_binding,
    clear_navigation_binding,
    live_data_active,
    navigation_session_id,
    set_session_current_page,
    set_live_data_active,
    set_session_selection,
)
from diagnostic_platform.session_observability import emit_session_runtime_event
from .session_errors import SessionNotRunningError
from .worker_runtime import WorkerRuntime

logger = logging.getLogger(__name__)
_CLEAR_DTCS_READY_PAGES = {"data_list", "data_display", "sub_data_list"}


def ensure_session_capability(session: Any, capability: BackendCapability) -> None:
    """Validate one running session supports the requested capability."""
    if _status_value(session.status) != SessionStatus.RUNNING.value:
        raise SessionNotRunningError(_status_value(session.status))
    available = {
        str(item)
        for item in (getattr(session, "capabilities", None) or [])
    }
    if capability.value not in available:
        backend_name = getattr(session, "backend_name", None) or "unknown"
        raise UnsupportedCapabilityError(capability, backend_name)


def ensure_running_gds2_session(session: Any, *, capability: str) -> None:
    """Backward-compatible wrapper while callers migrate to capability checks."""
    capability_map = {
        "dtc read": BackendCapability.READ_DTCS,
        "live data": BackendCapability.LIVE_DATA,
        "ai diagnosis": BackendCapability.AI_DATA_COLLECTION,
        "navigation": BackendCapability.NAVIGATION,
    }
    ensure_session_capability(
        session,
        capability_map.get(_strip_optional_text(capability).lower(), BackendCapability.CORE_SESSION),
    )


def resolve_session_vehicle_context(
    session: Any,
    data: dict[str, Any],
    *,
    backend: Any,
) -> dict[str, str]:
    """Resolve VIN/module/data-category from request, session, then backend state."""
    vin = _strip_optional_text(data.get("vin")) or _strip_optional_text(session.context.vin)
    module = _strip_optional_text(data.get("module")) or _strip_optional_text(
        getattr(session, "selected_module", "")
    )
    data_category = _strip_optional_text(data.get("data_category")) or _strip_optional_text(
        getattr(session, "selected_data_category", "")
    )

    if vin and module and data_category:
        return {
            "vin": vin,
            "module": module,
            "data_category": data_category,
        }

    state = None
    try:
        state = backend.get_state()
    except Exception:
        state = None

    state_extra = getattr(state, "extra", {}) if state is not None else {}
    if not isinstance(state_extra, dict):
        state_extra = {}

    if not vin:
        vin = _strip_optional_text(state_extra.get("vin"))
    if not module:
        module = _strip_optional_text(
            getattr(state, "current_module", "")
        ) or _strip_optional_text(state_extra.get("module"))
    if not data_category:
        data_category = _strip_optional_text(
            getattr(state, "current_data_category", "")
        ) or _strip_optional_text(state_extra.get("data_category"))

    return {
        "vin": vin,
        "module": module,
        "data_category": data_category,
    }


def _detect_backend_page(
    backend: Any,
    *,
    state: Any | None = None,
    fallback: str = "",
) -> str:
    """Return the current backend page, falling back to backend state when needed."""
    try:
        page = backend.detect_current_page()
        return str(getattr(page, "value", page) or fallback)
    except (AttributeError, UnsupportedCapabilityError, NotImplementedError):
        try:
            resolved_state = state if state is not None else backend.get_state()
        except AttributeError:
            return fallback
        return str(getattr(resolved_state, "current_page", fallback) or fallback)


def _read_backend_state_and_page(backend: Any) -> tuple[Any, str]:
    """Return backend state plus the best-effort current page value."""
    state = backend.get_state()
    return state, _detect_backend_page(backend, state=state)


def _maybe_select_module_for_session(
    session: Any,
    *,
    backend: Any,
    state: Any,
    current_page: str,
    module_name: str,
) -> tuple[Any, str]:
    """Select one module only when the backend is not already on it."""
    if not module_name or getattr(state, "current_module", "") == module_name:
        return state, current_page

    backend.select_module(module_name)
    set_session_selection(session, module=module_name)
    state = backend.get_state()
    return state, _detect_backend_page(backend, state=state)


def _maybe_select_data_category_for_session(
    session: Any,
    *,
    backend: Any,
    state: Any,
    data_category: str,
    force: bool = False,
) -> None:
    """Select one data category only when the backend is not already on it."""
    if (
        not data_category
        or (not force and getattr(state, "current_data_category", "") == data_category)
    ):
        return

    backend.select_data_category(data_category)
    set_session_selection(session, data_category=data_category)


def _ensure_clear_dtcs_allowed(runtime: WorkerRuntime, session: Any) -> None:
    """Reject clear-DTC requests that conflict with active worker-side flows."""
    if live_data_active(runtime, session.session_id):
        raise RuntimeError("Cannot clear DTCs while live data streaming is active")
    if navigation_session_id(runtime, session.session_id):
        raise RuntimeError("Cannot clear DTCs while navigation is active")
    if ai_session_id(runtime, session.session_id):
        raise RuntimeError("Cannot clear DTCs while AI diagnosis is active")


def _clear_dtcs_context_flags(session: Any, data: dict[str, Any]) -> tuple[bool, bool]:
    """Return whether request/session context should drive pre-clear reselection."""
    explicit_module = _strip_optional_text(data.get("module"))
    explicit_data_category = _strip_optional_text(data.get("data_category"))
    explicit_context_requested = bool(explicit_module or explicit_data_category)

    remembered_module = _strip_optional_text(getattr(session, "selected_module", ""))
    remembered_data_category = _strip_optional_text(getattr(session, "selected_data_category", ""))
    remembered_context_available = bool(remembered_module or remembered_data_category)
    return explicit_context_requested, remembered_context_available


def _clear_dtcs_explicit_context_is_redundant(
    session: Any,
    data: dict[str, Any],
    *,
    state: Any,
    current_page: str,
) -> bool:
    """Return whether explicit clear-DTC context only restates the active Data Display target."""
    if current_page != "data_display":
        return False

    explicit_module = _strip_optional_text(data.get("module"))
    explicit_data_category = _strip_optional_text(data.get("data_category"))
    if not (explicit_module or explicit_data_category):
        return False

    known_modules = {
        _strip_optional_text(getattr(state, "current_module", "")),
        _strip_optional_text(getattr(session, "selected_module", "")),
    }
    known_modules.discard("")

    known_categories = {
        _strip_optional_text(getattr(state, "current_data_category", "")),
        _strip_optional_text(getattr(session, "selected_data_category", "")),
    }
    known_categories.discard("")

    if explicit_module and explicit_module not in known_modules:
        return False
    if explicit_data_category and explicit_data_category not in known_categories:
        return False
    return True


def _apply_explicit_clear_dtcs_context(
    session: Any,
    data: dict[str, Any],
    *,
    backend: Any,
    state: Any,
    current_page: str,
) -> tuple[Any, str]:
    """Apply request-provided module/category context before clearing DTCs."""
    context = resolve_session_vehicle_context(session, data, backend=backend)
    state, current_page = _maybe_select_module_for_session(
        session,
        backend=backend,
        state=state,
        current_page=current_page,
        module_name=context.get("module", ""),
    )
    _maybe_select_data_category_for_session(
        session,
        backend=backend,
        state=state,
        data_category=context.get("data_category", ""),
    )
    return state, current_page


def _apply_remembered_clear_dtcs_context(
    session: Any,
    data: dict[str, Any],
    *,
    backend: Any,
    state: Any,
    current_page: str,
) -> tuple[Any, str]:
    """Apply remembered session context when clear-DTC runs away from Data Display."""
    context = resolve_session_vehicle_context(session, data, backend=backend)
    module_name = context.get("module", "")
    data_category = context.get("data_category", "")

    if module_name and current_page not in _CLEAR_DTCS_READY_PAGES:
        state, current_page = _maybe_select_module_for_session(
            session,
            backend=backend,
            state=state,
            current_page=current_page,
            module_name=module_name,
        )

    if data_category and current_page != "data_display":
        _maybe_select_data_category_for_session(
            session,
            backend=backend,
            state=state,
            data_category=data_category,
            force=True,
        )
    return state, current_page


def _clear_dtcs_result_payload(clear_result: Any, *, page_context: str) -> dict[str, Any]:
    """Build the normalized clear-DTC result payload."""
    return {
        "success": bool(getattr(clear_result, "success", True)),
        "cleared_count": int(getattr(clear_result, "cleared_count", 0) or 0),
        "message": str(getattr(clear_result, "message", "") or "Clear DTCs completed"),
        "page_context": page_context,
    }


def start_ai_diagnosis(
    runtime: WorkerRuntime,
    session: Any,
    *,
    vehicle_context: dict[str, Any],
    data_category: str,
    engine: Any,
    diagnostic_payload: Any,
    emit_progress: Callable[[str], None],
) -> str:
    """Start and bind one AI diagnosis session."""
    if engine.is_active:
        raise RuntimeError("AI diagnosis already in progress")

    if not hasattr(engine, "start_session_from_payload"):
        raise RuntimeError("AI engine does not support payload-based sessions")
    engine_vehicle_context = dict(vehicle_context)
    engine_vehicle_context.setdefault("session_id", session.session_id)
    ai_sid = engine.start_session_from_payload(engine_vehicle_context, diagnostic_payload)
    bind_ai_session(runtime, session, ai_sid)
    set_session_selection(
        session,
        module=vehicle_context.get("module", ""),
        data_category=data_category,
        runtime=runtime,
    )
    emit_progress(
        f"AI diagnosis started: {vehicle_context.get('module') or '-'} / {data_category}"
    )
    emit_session_runtime_event(
        "session.ai.started",
        runtime=runtime,
        session=session,
        operation_kind="ai.start",
        reason="ai_started",
        module=vehicle_context.get("module", ""),
        data_category=data_category,
        ai_session_id=ai_sid,
    )
    return ai_sid


def retry_ai_diagnosis(
    runtime: WorkerRuntime,
    session: Any,
    *,
    cached_payload_id: str,
    vehicle_context: dict[str, Any],
    engine: Any,
    emit_progress: Callable[[str], None],
) -> str:
    """Retry AI diagnosis using a cached payload and bind the new session."""
    if engine.is_active:
        raise RuntimeError("AI diagnosis already in progress")

    engine_vehicle_context = dict(vehicle_context)
    engine_vehicle_context.setdefault("session_id", session.session_id)
    ai_sid = engine.retry_with_cached(cached_payload_id, engine_vehicle_context)
    bind_ai_session(runtime, session, ai_sid)
    set_session_selection(
        session,
        module=vehicle_context.get("module", ""),
        data_category=vehicle_context.get("data_category", ""),
        runtime=runtime,
    )
    emit_progress(
        "AI diagnosis retry started: "
        f"{vehicle_context.get('module') or '-'} / "
        f"{vehicle_context.get('data_category') or '-'}"
    )
    emit_session_runtime_event(
        "session.ai.retry_started",
        runtime=runtime,
        session=session,
        operation_kind="ai.retry",
        reason="ai_retry_started",
        module=vehicle_context.get("module", ""),
        data_category=vehicle_context.get("data_category", ""),
        ai_session_id=ai_sid,
        cached_payload_id=cached_payload_id,
    )
    return ai_sid


def resolve_ai_event_stream(
    runtime: WorkerRuntime,
    session: Any,
    *,
    engine: Any,
) -> tuple[str, Any]:
    """Resolve the bound AI session id and its event queue."""
    ai_sid = ai_session_id(runtime, session.session_id)
    if not ai_sid:
        raise LookupError(f"No active AI session for {session.session_id}")

    event_queue = engine.get_event_queue(ai_sid)
    if event_queue is None:
        runtime.clear_ai_session(session.session_id)
        raise LookupError(f"AI session {ai_sid} not found")

    return ai_sid, event_queue


def handle_ai_stream_terminal_event(runtime: WorkerRuntime, session: Any, message: str) -> bool:
    """Clear AI binding when one terminal SSE event is observed."""
    if not isinstance(message, str):
        return False
    if message.startswith("event: done\n") or message.startswith("event: error\n"):
        if message.startswith("event: error\n"):
            emit_session_runtime_event(
                "ai.stream.error",
                runtime=runtime,
                session=session,
                operation_kind="ai.stream",
                status="error",
                failure_code="ai_stream_error",
                failure_domain="session_runtime",
                reason="stream_error",
            )
        clear_ai_binding(runtime, session)
        return True
    return False


def handle_live_data_stream_terminal_event(
    runtime: WorkerRuntime,
    session: Any,
    message: str,
) -> bool:
    """Clear live-data activity when one terminal SSE event is observed."""
    if not isinstance(message, str):
        return False
    if message.startswith("event: done\n") or message.startswith("event: error\n"):
        if message.startswith("event: error\n"):
            emit_session_runtime_event(
                "live_data.stream.error",
                runtime=runtime,
                session=session,
                operation_kind="live_data.stream",
                status="error",
                failure_code="live_data_stream_error",
                failure_domain="session_runtime",
                reason="stream_error",
            )
        set_live_data_active(runtime, session, False)
        return True
    return False


def start_navigation(
    runtime: WorkerRuntime,
    session: Any,
    *,
    goal: str,
    backend: Any | None = None,
    emit_progress: Callable[[str], None],
) -> Any:
    """Start and bind one navigation sub-session."""
    active_operation = runtime.current_operation_name()
    if active_operation is not None:
        raise RuntimeError(
            f"Worker busy with active operation '{active_operation}'"
        )
    if navigation_session_id(runtime, session.session_id):
        raise RuntimeError("Navigation already in progress for this session")

    nav_handle = _session_navigation_handle(runtime, session)
    if nav_handle is None and backend is not None:
        navigation_runtime_getter = getattr(backend, "get_navigation_runtime", None)
        if callable(navigation_runtime_getter):
            nav_handle = navigation_runtime_getter()
        else:
            backend_name = getattr(backend, "name", None) or type(backend).__name__
            raise RuntimeError(
                f"Backend '{backend_name}' does not expose a navigation runtime"
            )

    if nav_handle is None and backend is not None:
        raise RuntimeError("No active backend navigation runtime is available for this session")

    if nav_handle is not None:
        nav_session = nav_handle.start_navigation_session(runtime, goal)
    else:
        nav_session = start_worker_navigation_session(runtime, goal)
    bind_navigation_session(runtime, session, nav_session.session_id)
    emit_progress(f"Navigation started: {goal}")
    emit_session_runtime_event(
        "session.navigation.started",
        runtime=runtime,
        session=session,
        backend=backend,
        operation_kind="navigation.start",
        reason=goal,
        navigation_session_id=nav_session.session_id,
    )
    return nav_session


def _session_navigation_handle(runtime: WorkerRuntime, session: Any) -> Any | None:
    session_id = _strip_optional_text(getattr(session, "session_id", None))
    if not session_id:
        return None
    bundle = runtime.get_active_backend_bundle(session_id)
    if bundle is None:
        return None
    return bundle.navigation_handle


def resolve_navigation(runtime: WorkerRuntime, session: Any) -> tuple[str, Any]:
    """Resolve the bound navigation sub-session."""
    nav_sid = navigation_session_id(runtime, session.session_id)
    if not nav_sid:
        raise LookupError(f"No active navigation session for {session.session_id}")

    nav_handle = _session_navigation_handle(runtime, session)
    if nav_handle is not None:
        return nav_sid, nav_handle.get_navigation_session(runtime, nav_sid)
    return nav_sid, get_worker_navigation_session(runtime, nav_sid)


def apply_navigation_event(runtime: WorkerRuntime, session: Any, nav_session: Any, event: dict[str, Any]) -> str:
    """Apply one navigation SSE event to worker/session state."""
    event = _mapping_or_empty(event)
    event_type = event.get("type", "progress")
    if event_type == "progress":
        nav_session.current_page = event.get("page", nav_session.current_page)
        if nav_session.current_page:
            set_session_current_page(session, nav_session.current_page, runtime=runtime)
    elif event_type == "decision_required":
        nav_session.status = NavSessionStatus.AWAITING_DECISION
        nav_session.pending_decision_id = event.get("decision_id")
        nav_session.pending_items = event.get("items", [])
        emit_session_runtime_event(
            "session.navigation.decision_required",
            runtime=runtime,
            session=session,
            operation_kind="navigation.decision_required",
            reason="decision_required",
            navigation_decision_id=nav_session.pending_decision_id,
            items=list(nav_session.pending_items or []),
        )
    elif event_type == "done":
        nav_session.current_page = event.get("page", nav_session.current_page)
        selections = _mapping_or_empty(event.get("selections"))
        module = _strip_optional_text(selections.get("module"))
        data_category = _strip_optional_text(
            selections.get("data_category")
        ) or _strip_optional_text(selections.get("selected_item"))
        if module or data_category:
            set_session_selection(
                session,
                module=module if module else None,
                data_category=data_category if data_category else None,
                runtime=runtime,
            )
        if nav_session.current_page:
            set_session_current_page(session, nav_session.current_page, runtime=runtime)
        clear_navigation_binding(runtime, session)
        emit_session_runtime_event(
            "session.navigation.completed",
            runtime=runtime,
            session=session,
            operation_kind="navigation.complete",
            reason="navigation_completed",
            final_page=str(nav_session.current_page or ""),
        )
    elif event_type == "error":
        clear_navigation_binding(runtime, session)
        emit_session_runtime_event(
            "session.navigation.failed",
            runtime=runtime,
            session=session,
            operation_kind="navigation.failed",
            status="error",
            failure_code="navigation_error",
            failure_domain="session_runtime",
            reason=str(event.get("error") or "navigation_error"),
        )

    return event_type


def navigation_terminal_payload(runtime: WorkerRuntime, session: Any, nav_session: Any) -> dict[str, Any] | None:
    """Return one synthesized terminal payload when navigation has already finished."""
    nav_status = _status_value(nav_session.status)
    if nav_status not in ("completed", "failed", "aborted"):
        return None

    clear_navigation_binding(runtime, session)
    return {
        "type": "done",
        "status": nav_status,
        "error": nav_session.error,
    }


def submit_navigation_decision(
    runtime: WorkerRuntime,
    session: Any,
    *,
    decision_id: str,
    selected_item: str,
) -> tuple[str, dict[str, Any]]:
    """Submit one pending navigation decision."""
    nav_sid, _ = resolve_navigation(runtime, session)

    nav_handle = _session_navigation_handle(runtime, session)
    if nav_handle is not None:
        payload = nav_handle.submit_navigation_decision(
            runtime,
            nav_sid,
            decision_id=decision_id,
            selected_item=selected_item,
        )
    else:
        payload = submit_worker_navigation_decision(
            runtime,
            nav_sid,
            decision_id=decision_id,
            selected_item=selected_item,
        )
    emit_session_runtime_event(
        "session.navigation.decision_submitted",
        runtime=runtime,
        session=session,
        operation_kind="navigation.decision_submit",
        reason=str(payload.get("selected_item") or selected_item),
        navigation_session_id=nav_sid,
        selected_item=str(payload.get("selected_item") or selected_item),
        decision_id=decision_id,
    )
    return nav_sid, payload


def abort_navigation(runtime: WorkerRuntime, session: Any) -> tuple[str, dict[str, Any]]:
    """Abort the active navigation sub-session."""
    nav_sid, _ = resolve_navigation(runtime, session)

    nav_handle = _session_navigation_handle(runtime, session)
    if nav_handle is not None:
        payload = nav_handle.abort_navigation_session(runtime, nav_sid)
    else:
        payload = abort_worker_navigation_session(runtime, nav_sid)
    clear_navigation_binding(runtime, session)
    emit_session_runtime_event(
        "session.navigation.aborted",
        runtime=runtime,
        session=session,
        operation_kind="navigation.abort",
        status="error",
        failure_code="aborted",
        failure_domain="session_runtime",
        reason="aborted_by_user",
        navigation_session_id=nav_sid,
    )
    return nav_sid, payload


def navigation_status_payload(runtime: WorkerRuntime, session: Any) -> dict[str, Any]:
    """Build the response payload for navigation status."""
    nav_sid, nav_session = resolve_navigation(runtime, session)
    payload: dict[str, Any] = {
        "navigation_session_id": nav_sid,
        "status": _status_value(nav_session.status),
        "goal": nav_session.goal,
        "current_page": nav_session.current_page,
    }
    if nav_session.pending_decision_id:
        payload["pending_decision"] = {
            "decision_id": nav_session.pending_decision_id,
            "items": nav_session.pending_items,
        }
    if nav_session.error:
        payload["error"] = nav_session.error
    return payload


def read_dtcs(
    session: Any,
    data: dict[str, Any],
    *,
    backend: Any,
    emit_progress: Callable[[str], None],
) -> dict[str, Any]:
    """Read DTCs for one running backend-neutral session."""
    context = resolve_session_vehicle_context(session, data, backend=backend)
    state, current_page = _read_backend_state_and_page(backend)

    # Reading DTCs from the active Data Display should not silently re-run
    # module/category navigation just because backend context bookkeeping is blank.
    if current_page != "data_display":
        module_name = context.get("module", "")
        data_category = context.get("data_category", "")

        state, current_page = _maybe_select_module_for_session(
            session,
            backend=backend,
            state=state,
            current_page=current_page,
            module_name=module_name,
        )
        _maybe_select_data_category_for_session(
            session,
            backend=backend,
            state=state,
            data_category=data_category,
        )

    detailed_reader = getattr(backend, "read_dtcs_with_metadata", None)
    if callable(detailed_reader):
        raw_read_result = detailed_reader()
        if isinstance(raw_read_result, dict):
            read_result = dict(raw_read_result)
            dtcs_payload = list(read_result.get("dtcs") or [])
            dtc_count = int(read_result.get("dtc_count") or len(dtcs_payload))
            page_context = _strip_optional_text(read_result.get("page_context")) or _detect_backend_page(backend)
            set_session_current_page(session, page_context)
            emit_progress(f"Read DTCs completed ({dtc_count} codes)")
            payload = {
                "dtcs": dtcs_payload,
                "dtc_count": dtc_count,
                "page_context": page_context,
            }
            if read_result.get("dtc_display_mode"):
                payload["dtc_display_mode"] = read_result.get("dtc_display_mode")
            if read_result.get("vehicle_dtc_status"):
                payload["vehicle_dtc_status"] = read_result.get("vehicle_dtc_status")
            return payload

    dtcs = backend.read_dtcs()
    page_context = _detect_backend_page(backend)
    set_session_current_page(session, page_context)
    emit_progress(f"Read DTCs completed ({len(dtcs)} codes)")
    return {
        "dtcs": [
            {
                "code": dtc.code,
                "control_module": dtc.module,
                "module": dtc.module,
                "status": dtc.status,
                "description": dtc.description,
                "source_backend": dtc.source_backend,
            }
            for dtc in dtcs
        ],
        "dtc_count": len(dtcs),
        "page_context": page_context,
    }


def clear_dtcs(
    runtime: WorkerRuntime,
    session: Any,
    data: dict[str, Any],
    *,
    backend: Any,
    emit_progress: Callable[[str], None],
) -> dict[str, Any]:
    """Clear DTCs for one running backend-neutral session."""
    _ensure_clear_dtcs_allowed(runtime, session)

    operation = runtime.start_operation(session.session_id, "clear_dtcs")
    try:
        state, current_page = _read_backend_state_and_page(backend)
        explicit_context_requested, remembered_context_available = _clear_dtcs_context_flags(
            session,
            data,
        )

        if explicit_context_requested and not _clear_dtcs_explicit_context_is_redundant(
            session,
            data,
            state=state,
            current_page=current_page,
        ):
            state, current_page = _apply_explicit_clear_dtcs_context(
                session,
                data,
                backend=backend,
                state=state,
                current_page=current_page,
            )
        elif current_page != "data_display" and remembered_context_available:
            state, current_page = _apply_remembered_clear_dtcs_context(
                session,
                data,
                backend=backend,
                state=state,
                current_page=current_page,
            )

        clear_result = backend.clear_dtcs()
        page_context = _detect_backend_page(backend, fallback=current_page)

        emit_progress(
            "Clear DTCs completed "
            f"({int(getattr(clear_result, 'cleared_count', 0) or 0)} codes)"
        )
        set_session_current_page(session, page_context, runtime=runtime)
        return _clear_dtcs_result_payload(clear_result, page_context=page_context)
    finally:
        runtime.finish_operation(operation)


def select_module_action(
    runtime: WorkerRuntime,
    session: Any,
    *,
    module: str,
    backend: Any | None = None,
    get_executor: Callable[[], Any] | None = None,
    get_adapter: Callable[[], Any] | None = None,
    emit_progress: Callable[[str], None],
) -> dict[str, Any]:
    """Select one module and update session-scoped selection state."""
    if backend is not None:
        backend.select_module(module)
        result = {
            "selected_module": module,
            "data_categories": backend.get_data_categories(),
        }
        exec_result = None
    else:
        if get_executor is None or get_adapter is None:
            raise RuntimeError("Executor-backed module selection requires executor helpers")
        executor = get_executor()
        adapter = get_adapter()
        if adapter is None:
            raise RuntimeError("Adapter not initialized")

        step = ActionStep(
            action=GDS2Action.SELECT_MODULE,
            args={"module_name": module},
            timeout_sec=30.0,
        )
        ui_state = adapter.get_current_ui_state()
        exec_result = executor.execute_step(step, ui_state)

    if exec_result is not None and not exec_result.success:
        emit_progress(f"select_module failed: {exec_result.error}")
        return {
            "success": False,
            "error": exec_result.error,
        }

    set_session_selection(session, module=module, data_category="", runtime=runtime)
    emit_progress(f"Module selected: {module}")
    return {
        "success": True,
        "result": result if exec_result is None else exec_result.metadata,
    }


def select_data_category_action(
    runtime: WorkerRuntime,
    session: Any,
    *,
    data_category: str,
    backend: Any | None = None,
    get_executor: Callable[[], Any] | None = None,
    get_adapter: Callable[[], Any] | None = None,
    emit_progress: Callable[[str], None],
) -> dict[str, Any]:
    """Select one data category and update session-scoped selection state."""
    if backend is not None:
        result = {
            "selected_data_category": data_category,
            "items": backend.select_data_category(data_category),
        }
        exec_result = None
    else:
        if get_executor is None or get_adapter is None:
            raise RuntimeError("Executor-backed category selection requires executor helpers")
        executor = get_executor()
        adapter = get_adapter()
        if adapter is None:
            raise RuntimeError("Adapter not initialized")

        step = ActionStep(
            action=GDS2Action.SELECT_DATA_CATEGORY,
            args={"category_name": data_category},
            timeout_sec=30.0,
        )
        ui_state = adapter.get_current_ui_state()
        exec_result = executor.execute_step(step, ui_state)

    if exec_result is not None and not exec_result.success:
        emit_progress(f"select_data_category failed: {exec_result.error}")
        return {
            "success": False,
            "error": exec_result.error,
        }

    set_session_selection(session, data_category=data_category, runtime=runtime)
    emit_progress(f"Data category selected: {data_category}")
    return {
        "success": True,
        "result": result if exec_result is None else exec_result.metadata,
    }


def resume_branch_selection(
    runtime: WorkerRuntime,
    session: Any,
    *,
    pending_gate: Any,
    option_id: str,
    get_navigation_runtime: Callable[[], Any] | None,
    get_backend: Callable[[], Any],
    emit_progress: Callable[[str, dict[str, Any] | None], None],
) -> dict[str, Any]:
    """Resume one paused branch-selection decision and update selection state."""
    option_map = pending_gate.context.get("option_map", {})
    selected_choice = option_map.get(option_id)
    if not selected_choice:
        raise ValueError(f"Invalid branch option_id: {option_id}")

    resume_action = _strip_optional_text(pending_gate.context.get("resume_action"))
    if not resume_action:
        raise ValueError("Missing resume_action in branch decision context")

    navigation_runtime = None
    if get_navigation_runtime is not None and resume_action in {"select_module", "select_data_category"}:
        try:
            navigation_runtime = get_navigation_runtime()
        except Exception:
            navigation_runtime = None

    if navigation_runtime is not None and resume_action in {"select_module", "select_data_category"}:
        if resume_action == "select_module":
            resume_result = navigation_runtime.select_module(selected_choice)
        else:
            resume_result = navigation_runtime.select_data_category(selected_choice)
    else:
        backend = get_backend()
        if resume_action == "select_module":
            resume_result = backend.select_module(selected_choice)
        elif resume_action == "select_data_category":
            resume_result = backend.select_data_category(selected_choice)
        else:
            raise ValueError(f"Unsupported resume action: {resume_action}")

    emit_progress(
        f"Resumed via branch decision: {resume_action} -> {selected_choice}",
        {
            "resume_action": resume_action,
            "selected_choice": selected_choice,
        },
    )
    if resume_action in {"select_module", "select_sub_module"}:
        set_session_selection(session, module=selected_choice, runtime=runtime)
    if resume_action in {"select_data_category", "select_sub_category"}:
        set_session_selection(session, data_category=selected_choice, runtime=runtime)

    return {
        "resume_action": resume_action,
        "selected_choice": selected_choice,
        "result": resume_result,
    }


def execute_backend_action(
    session_id: str,
    *,
    backend: Any,
    action_name: str,
    action_args: dict[str, Any],
    timeout_sec: float,
    emit_progress: Callable[[str], None],
) -> dict[str, Any]:
    """Execute one generic backend action through the active backend contract."""
    emit_progress(f"Executing {action_name}...")
    outcome = backend.execute_action(
        action_name,
        args=action_args,
        timeout_sec=timeout_sec,
    )
    if not isinstance(outcome, dict):
        raise RuntimeError("Backend action returned a non-dict result")

    if not outcome.get("success"):
        error = str(outcome.get("error") or f"{action_name} failed")
        emit_progress(f"{action_name} failed: {error}")
        return {
            "success": False,
            "action": action_name,
            "error": error,
            "attempts": int(outcome.get("attempts") or 1),
            "elapsed_time": float(outcome.get("elapsed_time") or 0.0),
        }

    emit_progress(f"{action_name} completed")
    return {
        "success": True,
        "action": action_name,
        "result": outcome.get("result", outcome.get("metadata", {})),
        "attempts": int(outcome.get("attempts") or 1),
        "elapsed_time": float(outcome.get("elapsed_time") or 0.0),
    }


def execute_gds2_action(
    session_id: str,
    *,
    backend: Any,
    action_name: str,
    action_args: dict[str, Any],
    timeout_sec: float,
    emit_progress: Callable[[str], None],
) -> dict[str, Any]:
    """Compatibility alias for older callers while action routing is generalized."""
    return execute_backend_action(
        session_id,
        backend=backend,
        action_name=action_name,
        action_args=action_args,
        timeout_sec=timeout_sec,
        emit_progress=emit_progress,
    )


def start_live_data(
    runtime: WorkerRuntime,
    session: Any,
    data: dict[str, Any],
    *,
    interval_ms: int,
    backend: Any,
    stream_scope: str,
    emit_progress: Callable[[str], None],
) -> tuple[str, dict[str, Any]]:
    """Start live-data streaming for one business session."""
    context = resolve_session_vehicle_context(session, data, backend=backend)
    data_category = context.get("data_category", "")
    if not data_category:
        raise ValueError("data_category required")

    if backend.__class__.start_live_data_session is not DiagnosticBackend.start_live_data_session:
        payload = backend.start_live_data_session(
            data_category=data_category,
            interval_ms=interval_ms,
            stream_scope=stream_scope,
        )
    else:
        payload = start_diagnostics_live_data_stream(
            runtime,
            backend=backend,
            data_category=data_category,
            interval_ms=interval_ms,
            stream_scope=stream_scope,
        )
    set_session_selection(
        session,
        module=context.get("module", ""),
        data_category=data_category,
        runtime=runtime,
    )
    set_live_data_active(runtime, session, True)
    emit_progress(f"Live data started: {data_category}")
    emit_session_runtime_event(
        "session.live_data.started",
        runtime=runtime,
        session=session,
        backend=backend,
        operation_kind="live_data.start",
        reason="live_data_started",
        data_category=data_category,
        module=context.get("module", ""),
        stream_scope=stream_scope,
    )
    return data_category, payload


def stop_live_data(
    runtime: WorkerRuntime,
    session: Any,
    *,
    backend: Any,
    emit_progress: Callable[[str], None],
) -> dict[str, Any]:
    """Stop live-data streaming for one business session."""
    if backend.__class__.stop_live_data_session is not DiagnosticBackend.stop_live_data_session:
        payload = backend.stop_live_data_session()
    else:
        payload = stop_diagnostics_live_data_stream(runtime, backend=backend)
    fallback_page = _strip_optional_text(getattr(session, "current_page", ""))
    try:
        page_context = _detect_backend_page(backend, fallback=fallback_page)
    except Exception:
        logger.debug("Failed to refresh backend page after stopping live data", exc_info=True)
        page_context = fallback_page
    if page_context:
        set_session_current_page(session, page_context, runtime=runtime)
    set_live_data_active(runtime, session, False)
    emit_progress("Live data stopped")
    emit_session_runtime_event(
        "session.live_data.stopped",
        runtime=runtime,
        session=session,
        backend=backend,
        operation_kind="live_data.stop",
        reason="live_data_stopped",
    )
    return payload


def abort_active_ai(runtime: WorkerRuntime, session: Any, *, get_ai_engine: Callable[[], Any]) -> None:
    """Abort the bound AI session when one exists."""
    ai_sid = ai_session_id(runtime, session.session_id)
    if not ai_sid:
        return

    try:
        get_ai_engine().abort_session(ai_sid)
    except Exception:
        logger.exception("Failed to abort AI session for business session %s", session.session_id)
    finally:
        clear_ai_binding(runtime, session)


def abort_active_live_data(runtime: WorkerRuntime, session: Any) -> None:
    """Abort the bound live-data stream when one exists."""
    if not live_data_active(runtime, session.session_id):
        return

    try:
        bundle = runtime.get_active_backend_bundle(session.session_id)
        backend = bundle.backend if bundle is not None else runtime.backend
        if backend is not None:
            stop_diagnostics_live_data_stream(runtime, backend=backend)
    except Exception:
        logger.exception("Failed to stop live data for business session %s", session.session_id)
    finally:
        set_live_data_active(runtime, session, False)


def abort_active_execution(runtime: WorkerRuntime, session: Any, *, get_ai_engine: Callable[[], Any]) -> None:
    """Abort all worker-bound execution resources for one business session."""
    try:
        abort_navigation(runtime, session)
    except (LookupError, ValueError):
        pass

    abort_active_live_data(runtime, session)
    abort_active_ai(runtime, session, get_ai_engine=get_ai_engine)
