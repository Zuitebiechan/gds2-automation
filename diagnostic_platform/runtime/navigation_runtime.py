"""Shared navigation execution runtime for navigate and session APIs."""

from __future__ import annotations

import json
import logging
import queue
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .worker_runtime import WorkerRuntime

logger = logging.getLogger(__name__)


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


def _run_graph_thread(session: NavSession) -> None:
    """Background thread that runs the LangGraph navigation graph."""
    try:
        from src.agentic.graph import run_with_event_queue

        final = run_with_event_queue(
            goal=session.goal,
            event_queue=session.event_queue,
            decision_queue=session.decision_queue,
        )
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
    except Exception as exc:
        logger.exception("Navigation graph thread failed: %s", exc)
        session.status = NavSessionStatus.FAILED
        session.error = str(exc)
        try:
            session.event_queue.put({"type": "error", "error": str(exc)})
        except Exception:
            pass
