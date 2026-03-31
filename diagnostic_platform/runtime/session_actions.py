"""Business-session action helpers for AI, navigation, and live diagnostics flows."""

from __future__ import annotations

import logging
from typing import Any, Callable

from src.agentic.contracts.action_schema import ActionStep, GDS2Action
from src.agentic.session_orchestrator import SessionStatus
from src.navigation import GDS2Page

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
    set_live_data_active,
    set_session_selection,
)
from .worker_runtime import WorkerRuntime

logger = logging.getLogger(__name__)


def ensure_running_gds2_session(session: Any, *, capability: str) -> None:
    """Validate the session is in a runnable GDS2 state."""
    if session.status != SessionStatus.RUNNING:
        raise ValueError(f"Session not running (status={session.status.value})")
    if session.workflow != "gds2":
        raise ValueError(f"{capability} is not supported for workflow={session.workflow}")


def resolve_session_vehicle_context(
    session: Any,
    data: dict[str, Any],
    *,
    backend: Any,
) -> dict[str, str]:
    """Resolve VIN/module/data-category from request, session, then backend state."""
    vin = (data.get("vin") or session.context.vin or "").strip()
    module = (data.get("module") or getattr(session, "selected_module", "") or "").strip()
    data_category = (
        data.get("data_category")
        or getattr(session, "selected_data_category", "")
        or ""
    ).strip()

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
        vin = (state_extra.get("vin") or "").strip()
    if not module:
        module = (
            getattr(state, "current_module", "")
            or state_extra.get("module")
            or ""
        ).strip()
    if not data_category:
        data_category = (
            getattr(state, "current_data_category", "")
            or state_extra.get("data_category")
            or ""
        ).strip()

    return {
        "vin": vin,
        "module": module,
        "data_category": data_category,
    }


def start_ai_diagnosis(
    runtime: WorkerRuntime,
    session: Any,
    *,
    vehicle_context: dict[str, Any],
    data_category: str,
    engine: Any,
    collection_guard: Any,
    emit_progress: Callable[[str], None],
) -> str:
    """Start and bind one AI diagnosis session."""
    if engine.is_active:
        raise RuntimeError("AI diagnosis already in progress")

    ai_sid = engine.start_session(vehicle_context, collection_guard=collection_guard)
    bind_ai_session(runtime, session, ai_sid)
    set_session_selection(
        session,
        module=vehicle_context.get("module", ""),
        data_category=data_category,
    )
    emit_progress(
        f"AI diagnosis started: {vehicle_context.get('module') or '-'} / {data_category}"
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

    ai_sid = engine.retry_with_cached(cached_payload_id, vehicle_context)
    bind_ai_session(runtime, session, ai_sid)
    set_session_selection(
        session,
        module=vehicle_context.get("module", ""),
        data_category=vehicle_context.get("data_category", ""),
    )
    emit_progress(
        "AI diagnosis retry started: "
        f"{vehicle_context.get('module') or '-'} / "
        f"{vehicle_context.get('data_category') or '-'}"
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
    if message.startswith("event: done\n") or message.startswith("event: error\n"):
        clear_ai_binding(runtime, session)
        return True
    return False


def start_navigation(
    runtime: WorkerRuntime,
    session: Any,
    *,
    goal: str,
    emit_progress: Callable[[str], None],
) -> Any:
    """Start and bind one navigation sub-session."""
    if navigation_session_id(runtime, session.session_id):
        raise RuntimeError("Navigation already in progress for this session")

    nav_session = start_worker_navigation_session(runtime, goal)
    bind_navigation_session(runtime, session, nav_session.session_id)
    emit_progress(f"Navigation started: {goal}")
    return nav_session


def resolve_navigation(runtime: WorkerRuntime, session: Any) -> tuple[str, Any]:
    """Resolve the bound navigation sub-session."""
    nav_sid = navigation_session_id(runtime, session.session_id)
    if not nav_sid:
        raise LookupError(f"No active navigation session for {session.session_id}")

    return nav_sid, get_worker_navigation_session(runtime, nav_sid)


def apply_navigation_event(runtime: WorkerRuntime, session: Any, nav_session: Any, event: dict[str, Any]) -> str:
    """Apply one navigation SSE event to worker/session state."""
    event_type = event.get("type", "progress")
    if event_type == "progress":
        nav_session.current_page = event.get("page", nav_session.current_page)
    elif event_type == "decision_required":
        nav_session.status = NavSessionStatus.AWAITING_DECISION
        nav_session.pending_decision_id = event.get("decision_id")
        nav_session.pending_items = event.get("items", [])
    elif event_type == "done":
        selections = event.get("selections") or {}
        module = str(selections.get("module") or "").strip()
        data_category = str(
            selections.get("data_category")
            or selections.get("selected_item")
            or ""
        ).strip()
        if module or data_category:
            set_session_selection(
                session,
                module=module if module else None,
                data_category=data_category if data_category else None,
            )
        clear_navigation_binding(runtime, session)
    elif event_type == "error":
        clear_navigation_binding(runtime, session)

    return event_type


def navigation_terminal_payload(runtime: WorkerRuntime, session: Any, nav_session: Any) -> dict[str, Any] | None:
    """Return one synthesized terminal payload when navigation has already finished."""
    nav_status = (
        nav_session.status
        if isinstance(nav_session.status, str)
        else nav_session.status.value
    )
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

    payload = submit_worker_navigation_decision(
        runtime,
        nav_sid,
        decision_id=decision_id,
        selected_item=selected_item,
    )
    return nav_sid, payload


def abort_navigation(runtime: WorkerRuntime, session: Any) -> tuple[str, dict[str, Any]]:
    """Abort the active navigation sub-session."""
    nav_sid, _ = resolve_navigation(runtime, session)

    payload = abort_worker_navigation_session(runtime, nav_sid)
    clear_navigation_binding(runtime, session)
    return nav_sid, payload


def navigation_status_payload(runtime: WorkerRuntime, session: Any) -> dict[str, Any]:
    """Build the response payload for navigation status."""
    nav_sid, nav_session = resolve_navigation(runtime, session)
    payload: dict[str, Any] = {
        "navigation_session_id": nav_sid,
        "status": nav_session.status.value,
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
    """Read DTCs for one running GDS2 session."""
    context = resolve_session_vehicle_context(session, data, backend=backend)
    state = backend.get_state()
    current_page = backend.detect_current_page()

    module_name = context.get("module", "")
    data_category = context.get("data_category", "")

    if module_name and not getattr(state, "current_module", ""):
        backend.select_module(module_name)
        set_session_selection(session, module=module_name)
        current_page = backend.detect_current_page()

    if data_category and current_page != GDS2Page.DATA_DISPLAY.value:
        backend.select_data_category(data_category)
        set_session_selection(session, data_category=data_category)

    dtcs = backend.read_dtcs()
    page_context = backend.detect_current_page()
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


def select_module_action(
    runtime: WorkerRuntime,
    session: Any,
    *,
    module: str,
    get_data_viewer: Callable[[], Any],
    get_executor: Callable[[], Any],
    get_adapter: Callable[[], Any],
    emit_progress: Callable[[str], None],
) -> dict[str, Any]:
    """Select one module and update session-scoped selection state."""
    if runtime.has_data_viewer_getter():
        result = get_data_viewer().select_module(module)
        exec_result = None
    else:
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

    set_session_selection(session, module=module, data_category="")
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
    get_data_viewer: Callable[[], Any],
    get_executor: Callable[[], Any],
    get_adapter: Callable[[], Any],
    emit_progress: Callable[[str], None],
) -> dict[str, Any]:
    """Select one data category and update session-scoped selection state."""
    if runtime.has_data_viewer_getter():
        result = get_data_viewer().select_data_category(data_category)
        exec_result = None
    else:
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

    set_session_selection(session, data_category=data_category)
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
    get_data_viewer: Callable[[], Any],
    get_backend: Callable[[], Any],
    emit_progress: Callable[[str, dict[str, Any] | None], None],
) -> dict[str, Any]:
    """Resume one paused branch-selection decision and update selection state."""
    option_map = pending_gate.context.get("option_map", {})
    selected_choice = option_map.get(option_id)
    if not selected_choice:
        raise ValueError(f"Invalid branch option_id: {option_id}")

    resume_action = str(pending_gate.context.get("resume_action") or "").strip()
    if not resume_action:
        raise ValueError("Missing resume_action in branch decision context")

    if runtime.has_data_viewer_getter():
        viewer = get_data_viewer()
        if resume_action == "select_module":
            resume_result = viewer.select_module(selected_choice)
        elif resume_action == "select_sub_module":
            resume_result = viewer.select_sub_module(selected_choice)
        elif resume_action == "select_data_category":
            resume_result = viewer.select_data_category(selected_choice)
        elif resume_action == "select_sub_category":
            resume_result = viewer.select_sub_category(selected_choice)
        else:
            raise ValueError(f"Unsupported resume action: {resume_action}")
    else:
        backend = get_backend()
        workflow = backend._get_workflow()
        controller = workflow.controller

        if resume_action == "select_module":
            resume_result = workflow.select_module(selected_choice)
        elif resume_action == "select_sub_module":
            resume_result = controller.select_list_item(selected_choice).to_dict()
        elif resume_action == "select_data_category":
            resume_result = workflow.select_data_category(selected_choice)
        elif resume_action == "select_sub_category":
            resume_result = controller.select_sub_category(selected_choice).to_dict()
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
        set_session_selection(session, module=selected_choice)
    if resume_action in {"select_data_category", "select_sub_category"}:
        set_session_selection(session, data_category=selected_choice)

    return {
        "resume_action": resume_action,
        "selected_choice": selected_choice,
        "result": resume_result,
    }


def execute_gds2_action(
    session_id: str,
    *,
    action_name: str,
    action_args: dict[str, Any],
    timeout_sec: float,
    get_executor: Callable[[], Any],
    get_adapter: Callable[[], Any],
    emit_progress: Callable[[str], None],
) -> dict[str, Any]:
    """Execute one generic GDS2 action through the deterministic executor."""
    try:
        action = GDS2Action(action_name)
    except ValueError:
        valid = [candidate.value for candidate in GDS2Action]
        raise ValueError(f"Unknown action '{action_name}'. Valid: {valid}") from None

    executor = get_executor()
    adapter = get_adapter()
    if adapter is None:
        raise RuntimeError("Adapter not initialized")

    step = ActionStep(
        action=action,
        args=action_args,
        timeout_sec=timeout_sec,
    )

    ui_state = adapter.get_current_ui_state()
    if action == GDS2Action.CONNECT_DEVICE and ui_state.current_page == "unknown":
        emit_progress(
            "Current page unknown; retrying detection before connect_device...",
        )
        ui_state = adapter.get_current_ui_state()
        if ui_state.current_page == "unknown":
            try:
                from src.native import DeviceExplorerController

                if DeviceExplorerController().is_visible():
                    ui_state.current_page = "device_explorer"
                    emit_progress(
                        "Device Explorer detected via native check; continuing connect_device.",
                    )
            except Exception:
                pass

    emit_progress(f"Executing {action_name}...")
    exec_result = executor.execute_step(step, ui_state)
    if not exec_result.success:
        emit_progress(f"{action_name} failed: {exec_result.error}")
        return {
            "success": False,
            "action": action_name,
            "error": exec_result.error,
            "attempts": exec_result.attempts,
            "elapsed_time": exec_result.elapsed_time,
        }

    emit_progress(f"{action_name} completed")
    return {
        "success": True,
        "action": action_name,
        "result": exec_result.metadata,
        "attempts": exec_result.attempts,
        "elapsed_time": exec_result.elapsed_time,
    }


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
    )
    set_live_data_active(runtime, session, True)
    emit_progress(f"Live data started: {data_category}")
    return data_category, payload


def stop_live_data(
    runtime: WorkerRuntime,
    session: Any,
    *,
    backend: Any,
    emit_progress: Callable[[str], None],
) -> dict[str, Any]:
    """Stop live-data streaming for one business session."""
    payload = stop_diagnostics_live_data_stream(runtime, backend=backend)
    set_live_data_active(runtime, session, False)
    emit_progress("Live data stopped")
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
        from backends.gds2 import GDS2DiagnosticBackend

        backend = runtime.get_backend(GDS2DiagnosticBackend)
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
