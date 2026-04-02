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
from typing import TYPE_CHECKING, Any

from .worker_runtime import WorkerRuntime
from .worker_runtime import OperationCancelledError

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from src.navigation import NavigationController


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
    cancel_event: Any = field(default_factory=threading.Event, repr=False)

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
) -> NavSession:
    """Create and start one internal navigation session."""
    normalized_goal = (goal or "Navigate to Data Display").strip()
    session_id = uuid.uuid4().hex[:16]
    session = NavSession(session_id=session_id, goal=normalized_goal)

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

    if session.status != NavSessionStatus.AWAITING_DECISION:
        raise ValueError(
            f"Session is not awaiting a decision (status={session.status.value})"
        )

    if decision_id and session.pending_decision_id and decision_id != session.pending_decision_id:
        raise ValueError(
            f"Decision ID mismatch: expected '{session.pending_decision_id}', got '{decision_id}'"
        )

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

    if session.status in (
        NavSessionStatus.COMPLETED,
        NavSessionStatus.FAILED,
        NavSessionStatus.ABORTED,
    ):
        raise ValueError(f"Session already terminated (status={session.status.value})")

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
    return {
        "success": True,
        "session_id": session_id,
        "status": session.status.value,
    }


def build_navigation_status_payload(session_id: str, session: NavSession) -> dict[str, Any]:
    """Build the direct navigation status payload."""
    payload: dict[str, Any] = {
        "success": True,
        "session_id": session_id,
        "status": session.status.value,
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
    yield f"event: connected\ndata: {json.dumps({'session_id': session_id})}\n\n"

    while True:
        try:
            event = session.event_queue.get(timeout=1)
        except queue.Empty:
            yield ": keepalive\n\n"
            if session.thread and not session.thread.is_alive():
                while not session.event_queue.empty():
                    try:
                        event = session.event_queue.get_nowait()
                        event_type = event.get("type", "progress")
                        yield f"event: {event_type}\ndata: {json.dumps(event)}\n\n"
                    except queue.Empty:
                        break
                if session.status in (
                    NavSessionStatus.COMPLETED,
                    NavSessionStatus.FAILED,
                    NavSessionStatus.ABORTED,
                ):
                    yield (
                        "event: done\n"
                        f"data: {json.dumps({'type': 'done', 'status': session.status.value, 'error': session.error})}\n\n"
                    )
                    return
            continue

        event_type = event.get("type", "progress")
        if event_type == "progress":
            session.current_page = event.get("page", session.current_page)
        elif event_type == "decision_required":
            session.status = NavSessionStatus.AWAITING_DECISION
            session.pending_decision_id = event.get("decision_id")
            session.pending_items = event.get("items", [])

        yield f"event: {event_type}\ndata: {json.dumps(event)}\n\n"
        if event_type in ("done", "error"):
            return


def _page_value(page: Any) -> str:
    if hasattr(page, "value"):
        return str(page.value)
    return str(page or "unknown")


def _create_navigation_controller() -> "NavigationController":
    from src.navigation import NavigationController

    return NavigationController()


def _emit_progress(session: NavSession, *, page: str, action: str, node: str | None = None) -> None:
    try:
        session.event_queue.put(
            {
                "type": "progress",
                "node": node or page,
                "page": page,
                "action": action,
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
    if not items:
        raise RuntimeError(f"No selectable items available on page '{page}'")

    decision_id = uuid.uuid4().hex[:12]
    event = {
        "type": "decision_required",
        "decision_id": decision_id,
        "page": page,
        "items": list(items),
    }
    session.status = NavSessionStatus.AWAITING_DECISION
    session.pending_decision_id = decision_id
    session.pending_items = list(items)
    session.event_queue.put(event)

    while True:
        session.check_cancelled()
        try:
            payload = session.decision_queue.get(timeout=_DECISION_POLL_INTERVAL_SEC)
        except queue.Empty:
            continue

        selected_item = str((payload or {}).get("selected_item") or "").strip()
        if not selected_item:
            if session.cancel_event.is_set():
                raise OperationCancelledError(f"Navigation session {session.session_id} cancelled")
            continue

        session.status = NavSessionStatus.RUNNING
        session.pending_decision_id = None
        session.pending_items = []
        return selected_item


def _run_navigation_session(
    session: NavSession,
    *,
    controller: Any | None = None,
) -> dict[str, Any]:
    controller = controller or _create_navigation_controller()
    if hasattr(controller, "set_cancel_checker"):
        controller.set_cancel_checker(session.check_cancelled)

    navigation_history: list[dict[str, Any]] = []
    selections: dict[str, str] = {}

    for _ in range(_MAX_NAVIGATION_STEPS):
        session.check_cancelled()
        current_page = _page_value(controller.detect_current_page())
        session.current_page = current_page

        if current_page == "data_display":
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

        if current_page == "loading":
            _emit_progress(session, page=current_page, action="wait_for_loading")
            navigation_history.append({"page": current_page, "action": "wait_for_loading", "success": True})
            _wait_with_cancellation(session, _LOADING_POLL_INTERVAL_SEC)
            continue

        if current_page == "main_menu":
            _emit_progress(session, page=current_page, action="click_diagnostics")
            next_page = _require_success(controller.click_button("Diagnostics"), "click Diagnostics")
            navigation_history.append(
                {
                    "page": current_page,
                    "action": "click_diagnostics",
                    "to_page": next_page,
                    "success": True,
                }
            )
            session.current_page = next_page
            continue

        if current_page == "vehicle_selection":
            _emit_progress(session, page=current_page, action="click_enter")
            next_page = _require_success(controller.click_enter(), "click Enter")
            navigation_history.append(
                {
                    "page": current_page,
                    "action": "click_enter",
                    "to_page": next_page,
                    "success": True,
                }
            )
            session.current_page = next_page
            continue

        if current_page == "diagnostics_menu":
            _emit_progress(session, page=current_page, action="select_module_diagnostics")
            next_page = _require_success(
                controller.select_list_item("Module Diagnostics"),
                "select Module Diagnostics",
            )
            navigation_history.append(
                {
                    "page": current_page,
                    "action": "select_module_diagnostics",
                    "to_page": next_page,
                    "success": True,
                }
            )
            session.current_page = next_page
            continue

        if current_page == "module_list":
            items = _read_page_items(controller)
            selected_module = _await_navigation_decision(
                session,
                page=current_page,
                items=items,
            )
            selections["module"] = selected_module
            _emit_progress(session, page=current_page, action="select_module")
            result = controller.select_list_item(selected_module)
            next_page = _require_success(result, f"select module '{selected_module}'")
            context = _navigation_result_context(result)
            if context.get("module"):
                selections["module"] = str(context["module"])
            navigation_history.append(
                {
                    "page": current_page,
                    "action": "select_module",
                    "selected_item": selected_module,
                    "to_page": next_page,
                    "success": True,
                }
            )
            session.current_page = next_page
            continue

        if current_page == "module_submenu":
            data_display_item = _resolve_data_display_item(controller)
            _emit_progress(session, page=current_page, action="select_data_display")
            next_page = _require_success(
                controller.select_list_item(data_display_item),
                f"select '{data_display_item}'",
            )
            navigation_history.append(
                {
                    "page": current_page,
                    "action": "select_data_display",
                    "selected_item": data_display_item,
                    "to_page": next_page,
                    "success": True,
                }
            )
            session.current_page = next_page
            continue

        if current_page == "data_list":
            items = _read_page_items(controller)
            selected_category = _await_navigation_decision(
                session,
                page=current_page,
                items=items,
            )
            selections["data_category"] = selected_category
            _emit_progress(session, page=current_page, action="select_data_category")
            result = controller.select_list_item(selected_category)
            next_page = _require_success(result, f"select data category '{selected_category}'")
            context = _navigation_result_context(result)
            if context.get("data_category"):
                selections["data_category"] = str(context["data_category"])
            navigation_history.append(
                {
                    "page": current_page,
                    "action": "select_data_category",
                    "selected_item": selected_category,
                    "to_page": next_page,
                    "success": True,
                }
            )
            session.current_page = next_page
            continue

        if current_page == "sub_data_list":
            items = _read_page_items(controller)
            selected_sub_category = _await_navigation_decision(
                session,
                page=current_page,
                items=items,
            )
            selections["sub_category"] = selected_sub_category
            _emit_progress(session, page=current_page, action="select_sub_category")
            if hasattr(controller, "select_sub_category"):
                result = controller.select_sub_category(selected_sub_category)
            else:
                result = controller.select_list_item(selected_sub_category)
            next_page = _require_success(result, f"select sub-category '{selected_sub_category}'")
            context = _navigation_result_context(result)
            if context.get("sub_category"):
                selections["sub_category"] = str(context["sub_category"])
            navigation_history.append(
                {
                    "page": current_page,
                    "action": "select_sub_category",
                    "selected_item": selected_sub_category,
                    "to_page": next_page,
                    "success": True,
                }
            )
            session.current_page = next_page
            continue

        if current_page == "j2534_disconnect":
            _emit_progress(session, page=current_page, action="recover_connection")
            if not hasattr(controller, "recover_data_display_connection"):
                raise RuntimeError("Navigation controller cannot recover J2534 disconnects")
            recovery = controller.recover_data_display_connection(
                data_category=selections.get("data_category")
            )
            next_page = _require_success(recovery, "recover Data Display connection")
            navigation_history.append(
                {
                    "page": current_page,
                    "action": "recover_connection",
                    "to_page": next_page,
                    "success": True,
                }
            )
            session.current_page = next_page
            continue

        raise RuntimeError(f"Unsupported navigation page: {current_page}")

    raise RuntimeError("Navigation exceeded max steps before reaching Data Display")


def _run_graph_thread(session: NavSession) -> None:
    """Background thread that runs the deterministic navigation runtime."""
    try:
        final = _run_navigation_session(session)
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
