"""Shared navigation execution runtime for navigate and session APIs."""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from diagnostic_platform.safe_utils import (
    display_text as _display_text,
    json_dumps_safe as _json_sse_data,
    mapping_or_empty as _mapping_or_empty,
    status_value as _status_value,
    strip_optional_text as _strip_optional_text,
)

from .errors import OperationCancelledError
from .navigation_errors import (
    NavigationDecisionMismatchError,
    NavigationNotAwaitingDecisionError,
    NavigationSessionTerminatedError,
)
from .worker_runtime import WorkerRuntime

logger = logging.getLogger(__name__)

_MAX_NAVIGATION_STEPS = 48
_DECISION_POLL_INTERVAL_SEC = 0.25
_LOADING_POLL_INTERVAL_SEC = 0.5


class NavSessionStatus(Enum):
    RUNNING = "running"
    AWAITING_DECISION = "awaiting_decision"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


@dataclass
class NavSession:
    """In-memory navigation session."""

    session_id: str
    goal: str
    status: NavSessionStatus = NavSessionStatus.RUNNING
    event_queue: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=500))
    decision_queue: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=1))
    thread: threading.Thread | None = None
    final_state: dict[str, Any] | None = None
    current_page: str = ""
    pending_decision_id: str | None = None
    pending_items: list[Any] = field(default_factory=list)
    error: str | None = None
    cleanup_callback: Any = field(default=None, repr=False)
    cancel_event: Any = field(default_factory=threading.Event, repr=False)
    controller_factory: Any = field(default=None, repr=False)

    def cancel(self) -> None:
        self.cancel_event.set()

    def check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise OperationCancelledError(f"Navigation session {self.session_id} cancelled")


def get_navigation_session(runtime: WorkerRuntime, session_id: str) -> NavSession:
    """Return one internal navigation session by id."""
    return runtime.get_navigation_session(session_id)


def start_navigation_session(
    runtime: WorkerRuntime,
    goal: str = "Navigate to Data Display",
    *,
    controller_factory: Callable[[], Any] | None = None,
) -> NavSession:
    """Create and start one internal navigation session."""
    normalized_goal = _strip_optional_text(goal) or "Navigate to Data Display"
    session_id = uuid.uuid4().hex[:16]
    session = NavSession(session_id=session_id, goal=normalized_goal)
    session.cleanup_callback = runtime.schedule_navigation_session_cleanup
    session.controller_factory = controller_factory

    thread = threading.Thread(
        target=_run_graph_thread,
        args=(session,),
        daemon=True,
        name=f"nav-graph-{session_id}",
    )
    session.thread = thread
    runtime.set_navigation_session(session_id, session)
    thread.start()

    logger.info("NAV session=%s started goal=%s", session_id, normalized_goal)
    return session


def submit_navigation_decision(
    runtime: WorkerRuntime,
    session_id: str,
    *,
    decision_id: str = "",
    selected_item: str,
) -> dict[str, Any]:
    """Resume one paused navigation session with a selected item."""
    session = get_navigation_session(runtime, session_id)
    session_status = _status_value(session.status)

    if session_status != NavSessionStatus.AWAITING_DECISION.value:
        raise NavigationNotAwaitingDecisionError(session_status)

    if decision_id and session.pending_decision_id and decision_id != session.pending_decision_id:
        raise NavigationDecisionMismatchError(session.pending_decision_id, decision_id)

    session.status = NavSessionStatus.RUNNING
    session.pending_decision_id = None
    session.pending_items = []
    session.decision_queue.put({"selected_item": selected_item})
    logger.info("NAV session=%s decision=%s selected=%s", session_id, decision_id or "-", selected_item)

    return {
        "success": True,
        "session_id": session_id,
        "selected_item": selected_item,
    }


def abort_navigation_session(runtime: WorkerRuntime, session_id: str) -> dict[str, Any]:
    """Abort one running or paused navigation session."""
    session = get_navigation_session(runtime, session_id)
    session_status = _status_value(session.status)

    if session_status in (
        NavSessionStatus.COMPLETED.value,
        NavSessionStatus.FAILED.value,
        NavSessionStatus.ABORTED.value,
    ):
        raise NavigationSessionTerminatedError(session_status)

    session.status = NavSessionStatus.ABORTED
    session.error = "Aborted by user"
    session.cancel()

    try:
        session.decision_queue.put_nowait({"selected_item": ""})
    except queue.Full:
        pass

    try:
        session.event_queue.put_nowait({"type": "error", "error": "Aborted by user"})
    except queue.Full:
        pass

    logger.info("NAV session=%s aborted", session_id)
    if callable(session.cleanup_callback):
        session.cleanup_callback(session_id)
    else:
        runtime.schedule_navigation_session_cleanup(session_id)
    return {
        "success": True,
        "session_id": session_id,
        "status": _status_value(session.status),
    }


def build_navigation_status_payload(session_id: str, session: NavSession) -> dict[str, Any]:
    """Build the direct navigation status payload."""
    payload: dict[str, Any] = {
        "success": True,
        "session_id": session_id,
        "status": _status_value(session.status),
        "goal": session.goal,
        "current_page": session.current_page,
    }
    if session.pending_decision_id:
        payload["pending_decision"] = {
            "decision_id": session.pending_decision_id,
            "items": session.pending_items,
        }
    if session.error:
        payload["error"] = session.error
    return payload


def iter_navigation_session_events(session_id: str, session: NavSession) -> Iterator[str]:
    """Yield SSE events for one direct navigation session."""
    yield f"event: connected\ndata: {_json_sse_data({'session_id': session_id})}\n\n"

    while True:
        try:
            event = session.event_queue.get(timeout=1)
        except queue.Empty:
            yield ": keepalive\n\n"
            if session.thread and not session.thread.is_alive():
                while not session.event_queue.empty():
                    try:
                        event = _mapping_or_empty(session.event_queue.get_nowait())
                        event_type = event.get("type", "progress")
                        yield f"event: {event_type}\ndata: {_json_sse_data(event)}\n\n"
                    except queue.Empty:
                        break
                if _status_value(session.status) in (
                    NavSessionStatus.COMPLETED.value,
                    NavSessionStatus.FAILED.value,
                    NavSessionStatus.ABORTED.value,
                ):
                    yield (
                        "event: done\n"
                        f"data: {_json_sse_data({'type': 'done', 'status': _status_value(session.status), 'error': session.error})}\n\n"
                    )
                    return
            continue

        event = _mapping_or_empty(event)
        event_type = event.get("type", "progress")
        if event_type == "progress":
            session.current_page = event.get("page", session.current_page)
        elif event_type == "decision_required":
            session.status = NavSessionStatus.AWAITING_DECISION
            session.pending_decision_id = event.get("decision_id")
            session.pending_items = event.get("items", [])

        yield f"event: {event_type}\ndata: {_json_sse_data(event)}\n\n"
        if event_type in ("done", "error"):
            return


def _page_value(page: Any) -> str:
    if hasattr(page, "value"):
        return str(page.value)
    return str(page or "unknown")


def _emit_progress(session: NavSession, *, page: str, action: str, node: str | None = None) -> None:
    normalized_page = _display_text(page, default="unknown") or "unknown"
    normalized_action = _display_text(action, default="progress") or "progress"
    normalized_node = _display_text(node, default=normalized_page) or normalized_page
    try:
        session.event_queue.put(
            {
                "type": "progress",
                "node": normalized_node,
                "page": normalized_page,
                "action": normalized_action,
            }
        )
    except Exception:
        logger.debug("NAV session=%s failed to enqueue progress event", session.session_id, exc_info=True)


def _read_page_items(controller: Any) -> list[Any]:
    if hasattr(controller, "wait_for_list"):
        try:
            items = controller.wait_for_list()
            if items:
                return list(items)
        except OperationCancelledError:
            raise
        except Exception:
            logger.debug("Falling back from wait_for_list()", exc_info=True)
    if hasattr(controller, "get_list_items"):
        items = controller.get_list_items(0)
        return list(items or [])
    snapshot = controller.get_snapshot() if hasattr(controller, "get_snapshot") else {}
    return list(snapshot.get("lists", []) or [])


def _navigation_result_success(result: Any) -> bool:
    if isinstance(result, dict):
        return bool(result.get("success"))
    return bool(getattr(result, "success", False))


def _navigation_result_error(result: Any) -> str | None:
    if isinstance(result, dict):
        error = result.get("error") or result.get("message")
        return str(error) if error else None
    error = getattr(result, "error", None)
    return str(error) if error else None


def _navigation_result_page(result: Any, fallback: str) -> str:
    if isinstance(result, dict):
        return _page_value(result.get("page") or fallback)
    return _page_value(getattr(result, "page", None) or fallback)


def _navigation_result_context(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        context = result.get("context") or {}
    else:
        context = getattr(result, "context", {}) or {}
    return dict(context) if isinstance(context, dict) else {}


def _wait_with_cancellation(session: NavSession, seconds: float) -> None:
    deadline = time.time() + max(0.0, seconds)
    while time.time() < deadline:
        session.check_cancelled()
        time.sleep(min(0.1, max(0.0, deadline - time.time())))


def _require_success(result: Any, action: str) -> str:
    if not _navigation_result_success(result):
        error = _navigation_result_error(result) or f"{action} failed"
        raise RuntimeError(error)
    return _navigation_result_page(result, "unknown")


def _resolve_data_display_item(controller: Any) -> str:
    items = _read_page_items(controller)
    if not items:
        raise RuntimeError("No module submenu items available")

    normalizer = getattr(controller, "_normalize_text", lambda text: str(text).lower().strip())
    markers = tuple(getattr(controller, "_MODULE_SUBMENU_DISPLAY_MARKERS", ())) or ("data display",)
    normalized_markers = tuple(normalizer(marker) for marker in markers if marker)

    for item in items:
        normalized_item = normalizer(str(item))
        if any(marker in normalized_item for marker in normalized_markers):
            return str(item)
    for item in items:
        if "data display" in str(item).lower():
            return str(item)
    raise RuntimeError("Data Display option not found")


def _await_navigation_decision(session: NavSession, *, page: str, items: list[Any]) -> str:
    normalized_items = [_display_text(item) for item in items]
    normalized_items = [item for item in normalized_items if item]
    if not normalized_items:
        raise RuntimeError(f"No selectable items available on page '{page}'")

    decision_id = uuid.uuid4().hex[:12]
    event = {
        "type": "decision_required",
        "decision_id": decision_id,
        "page": _display_text(page, default="unknown") or "unknown",
        "items": normalized_items,
    }
    session.status = NavSessionStatus.AWAITING_DECISION
    session.pending_decision_id = decision_id
    session.pending_items = list(normalized_items)
    session.event_queue.put(event)

    while True:
        session.check_cancelled()
        try:
            payload = session.decision_queue.get(timeout=_DECISION_POLL_INTERVAL_SEC)
        except queue.Empty:
            continue

        selected_item = _strip_optional_text(_mapping_or_empty(payload).get("selected_item"))
        if not selected_item:
            if session.cancel_event.is_set():
                raise OperationCancelledError(f"Navigation session {session.session_id} cancelled")
            continue

        session.status = NavSessionStatus.RUNNING
        session.pending_decision_id = None
        session.pending_items = []
        return selected_item


def _append_navigation_history(
    navigation_history: list[dict[str, Any]],
    *,
    page: str,
    action: str,
    success: bool = True,
    to_page: str | None = None,
    selected_item: str | None = None,
) -> None:
    entry: dict[str, Any] = {
        "page": page,
        "action": action,
        "success": success,
    }
    if to_page is not None:
        entry["to_page"] = to_page
    if selected_item is not None:
        entry["selected_item"] = selected_item
    navigation_history.append(entry)


def _complete_navigation(
    session: NavSession,
    *,
    current_page: str,
    navigation_history: list[dict[str, Any]],
    selections: dict[str, str],
) -> dict[str, Any]:
    final = {
        "goal": session.goal,
        "current_page": current_page,
        "navigation_history": navigation_history,
        "selections": dict(selections),
        "error": None,
    }
    session.event_queue.put(
        {
            "type": "done",
            "final_page": current_page,
            "steps": len(navigation_history),
            "selections": dict(selections),
            "error": None,
        }
    )
    return final


def _run_loading_step(
    session: NavSession,
    *,
    current_page: str,
    navigation_history: list[dict[str, Any]],
) -> None:
    _emit_progress(session, page=current_page, action="wait_for_loading")
    _append_navigation_history(
        navigation_history,
        page=current_page,
        action="wait_for_loading",
    )
    _wait_with_cancellation(session, _LOADING_POLL_INTERVAL_SEC)


def _run_click_step(
    session: NavSession,
    *,
    current_page: str,
    action: str,
    operation_label: str,
    action_runner: Any,
    navigation_history: list[dict[str, Any]],
    selected_item: str | None = None,
) -> str:
    _emit_progress(session, page=current_page, action=action)
    next_page = _require_success(action_runner(), operation_label)
    _append_navigation_history(
        navigation_history,
        page=current_page,
        action=action,
        to_page=next_page,
        selected_item=selected_item,
    )
    return next_page


def _run_selection_step(
    session: NavSession,
    *,
    controller: Any,
    current_page: str,
    action: str,
    selection_key: str,
    context_key: str,
    operation_label: str,
    action_runner: Any,
    navigation_history: list[dict[str, Any]],
    selections: dict[str, str],
) -> str:
    selected_item = _await_navigation_decision(
        session,
        page=current_page,
        items=_read_page_items(controller),
    )
    selections[selection_key] = selected_item
    _emit_progress(session, page=current_page, action=action)
    result = action_runner(selected_item)
    next_page = _require_success(result, operation_label.format(selected_item=selected_item))
    context = _navigation_result_context(result)
    if context.get(context_key):
        selections[selection_key] = str(context[context_key])
    _append_navigation_history(
        navigation_history,
        page=current_page,
        action=action,
        selected_item=selected_item,
        to_page=next_page,
    )
    return next_page


def _select_sub_category(controller: Any, selected_item: str) -> Any:
    """Select one sub-category using the best available controller method."""
    if hasattr(controller, "select_sub_category"):
        return controller.select_sub_category(selected_item)
    return controller.select_list_item(selected_item)


def _recover_data_display_connection(controller: Any, *, data_category: str | None = None) -> Any:
    """Recover one Data Display connection after a J2534 disconnect."""
    if not hasattr(controller, "recover_data_display_connection"):
        raise RuntimeError("Navigation controller cannot recover J2534 disconnects")
    return controller.recover_data_display_connection(data_category=data_category)


def _open_data_display(
    session: NavSession,
    *,
    controller: Any,
    current_page: str,
    navigation_history: list[dict[str, Any]],
) -> str:
    """Open the Data Display entry from the module submenu."""
    data_display_item = _resolve_data_display_item(controller)
    return _run_click_step(
        session,
        current_page=current_page,
        action="select_data_display",
        operation_label=f"select '{data_display_item}'",
        action_runner=lambda: controller.select_list_item(data_display_item),
        navigation_history=navigation_history,
        selected_item=data_display_item,
    )


def _run_navigation_page(
    session: NavSession,
    *,
    controller: Any,
    current_page: str,
    navigation_history: list[dict[str, Any]],
    selections: dict[str, str],
) -> dict[str, Any] | str | None:
    handlers: dict[str, Any] = {
        "data_display": lambda: _complete_navigation(
            session,
            current_page=current_page,
            navigation_history=navigation_history,
            selections=selections,
        ),
        "loading": lambda: _run_loading_step(
            session,
            current_page=current_page,
            navigation_history=navigation_history,
        ),
        "main_menu": lambda: _run_click_step(
            session,
            current_page=current_page,
            action="click_diagnostics",
            operation_label="click Diagnostics",
            action_runner=lambda: controller.click_button("Diagnostics"),
            navigation_history=navigation_history,
        ),
        "vehicle_selection": lambda: _run_click_step(
            session,
            current_page=current_page,
            action="click_enter",
            operation_label="click Enter",
            action_runner=controller.click_enter,
            navigation_history=navigation_history,
        ),
        "diagnostics_menu": lambda: _run_click_step(
            session,
            current_page=current_page,
            action="select_module_diagnostics",
            operation_label="select Module Diagnostics",
            action_runner=lambda: controller.select_list_item("Module Diagnostics"),
            navigation_history=navigation_history,
        ),
        "module_list": lambda: _run_selection_step(
            session,
            controller=controller,
            current_page=current_page,
            action="select_module",
            selection_key="module",
            context_key="module",
            operation_label="select module '{selected_item}'",
            action_runner=controller.select_list_item,
            navigation_history=navigation_history,
            selections=selections,
        ),
        "module_submenu": lambda: _open_data_display(
            session,
            controller=controller,
            current_page=current_page,
            navigation_history=navigation_history,
        ),
        "data_list": lambda: _run_selection_step(
            session,
            controller=controller,
            current_page=current_page,
            action="select_data_category",
            selection_key="data_category",
            context_key="data_category",
            operation_label="select data category '{selected_item}'",
            action_runner=controller.select_list_item,
            navigation_history=navigation_history,
            selections=selections,
        ),
        "sub_data_list": lambda: _run_selection_step(
            session,
            controller=controller,
            current_page=current_page,
            action="select_sub_category",
            selection_key="sub_category",
            context_key="sub_category",
            operation_label="select sub-category '{selected_item}'",
            action_runner=lambda selected_item: _select_sub_category(controller, selected_item),
            navigation_history=navigation_history,
            selections=selections,
        ),
        "j2534_disconnect": lambda: _run_click_step(
            session,
            current_page=current_page,
            action="recover_connection",
            operation_label="recover Data Display connection",
            action_runner=lambda: _recover_data_display_connection(
                controller,
                data_category=selections.get("data_category"),
            ),
            navigation_history=navigation_history,
        ),
    }

    handler = handlers.get(current_page)
    if handler is None:
        raise RuntimeError(f"Unsupported navigation page: {current_page}")
    return handler()


def _run_navigation_session(
    session: NavSession,
    *,
    controller: Any | None = None,
) -> dict[str, Any]:
    if controller is None:
        raise RuntimeError("Navigation controller factory is required")
    if hasattr(controller, "set_cancel_checker"):
        controller.set_cancel_checker(session.check_cancelled)

    navigation_history: list[dict[str, Any]] = []
    selections: dict[str, str] = {}

    for _ in range(_MAX_NAVIGATION_STEPS):
        session.check_cancelled()
        current_page = _page_value(controller.detect_current_page())
        session.current_page = current_page
        page_result = _run_navigation_page(
            session,
            controller=controller,
            current_page=current_page,
            navigation_history=navigation_history,
            selections=selections,
        )
        if isinstance(page_result, dict):
            return page_result
        if isinstance(page_result, str):
            session.current_page = page_result

    raise RuntimeError("Navigation exceeded max steps before reaching Data Display")


def _run_graph_thread(session: NavSession) -> None:
    """Background thread that runs the deterministic navigation runtime."""
    try:
        controller = session.controller_factory() if callable(session.controller_factory) else None
        if controller is None:
            final = _run_navigation_session(session)
        else:
            final = _run_navigation_session(session, controller=controller)
        session.final_state = final
        if session.status not in (NavSessionStatus.ABORTED,):
            final_page = final.get("current_page", "unknown") if final else "unknown"
            if final.get("error") and final_page != "data_display":
                session.status = NavSessionStatus.FAILED
                session.error = final.get("error")
                logger.warning("NAV session=%s failed page=%s error=%s", session.session_id, final_page, session.error)
            else:
                session.status = NavSessionStatus.COMPLETED
                logger.info(
                    "NAV session=%s completed page=%s steps=%s",
                    session.session_id,
                    final_page,
                    len(final.get("navigation_history", [])) if final else 0,
                )
    except OperationCancelledError:
        logger.info("NAV session=%s cancellation acknowledged", session.session_id)
        if session.status != NavSessionStatus.ABORTED:
            session.status = NavSessionStatus.ABORTED
            session.error = "Aborted by user"
    except Exception as exc:
        logger.exception("Navigation graph thread failed: %s", exc)
        session.status = NavSessionStatus.FAILED
        session.error = str(exc)
        try:
            session.event_queue.put({"type": "error", "error": str(exc)})
        except Exception:
            pass
    finally:
        if session.status in (
            NavSessionStatus.COMPLETED,
            NavSessionStatus.FAILED,
            NavSessionStatus.ABORTED,
        ) and callable(session.cleanup_callback):
            session.cleanup_callback(session.session_id)
