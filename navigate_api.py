"""LangGraph Navigation API blueprint.

Provides endpoints for running the full LangGraph navigation graph
via HTTP/SSE, replacing the console-based HITL loop with a
decision_required / decision submission flow over the network.

Endpoints:
    POST /api/navigate/start      - Start navigation session
    GET  /api/navigate/events      - SSE event stream
    POST /api/navigate/decision     - Submit user decision
    GET  /api/navigate/status       - Query session status
    POST /api/navigate/abort        - Abort running session

NOTE on session management layers:
    This module uses its own NavSession / _sessions store to manage the
    LangGraph *execution* lifecycle (graph.stream + HITL pause/resume).
    It is intentionally separate from SessionOrchestrator (session_api.py)
    which manages the higher-level *business* lifecycle (brand routing,
    workflow selection, decision gates with timeout/fallback).
    The two do not share state — they serve different API surfaces.
"""

import json
import logging
import queue
import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from flask import Blueprint, Response, jsonify, request

logger = logging.getLogger(__name__)

navigate_bp = Blueprint("navigate", __name__, url_prefix="/api/navigate")


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
    thread: Optional[threading.Thread] = None
    final_state: Optional[dict] = None
    current_page: str = ""
    pending_decision_id: Optional[str] = None
    pending_items: list = field(default_factory=list)
    error: Optional[str] = None


# In-memory session store (single process).
_sessions: dict[str, NavSession] = {}
_sessions_lock = threading.Lock()


def _get_session(session_id: str) -> NavSession:
    with _sessions_lock:
        session = _sessions.get(session_id)
    if session is None:
        raise KeyError(f"Navigation session '{session_id}' not found")
    return session


def start_navigation_session(goal: str = "Navigate to Data Display") -> NavSession:
    """Create and start an internal navigation session."""
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

    with _sessions_lock:
        _sessions[session_id] = session

    thread.start()
    logger.info("NAV session=%s started goal=%s", session_id, normalized_goal)
    return session


def get_navigation_session(session_id: str) -> NavSession:
    """Return an internal navigation session by id."""
    return _get_session(session_id)


def submit_navigation_decision(
    session_id: str,
    *,
    decision_id: str = "",
    selected_item: str,
) -> dict[str, Any]:
    """Resume a paused navigation session with a selected item."""
    session = _get_session(session_id)

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


def abort_navigation_session(session_id: str) -> dict[str, Any]:
    """Abort a running or paused internal navigation session."""
    session = _get_session(session_id)

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
        logger.exception(f"Navigation graph thread failed: {exc}")
        session.status = NavSessionStatus.FAILED
        session.error = str(exc)
        try:
            session.event_queue.put({"type": "error", "error": str(exc)})
        except Exception:
            pass


# ---------------------------------------------------------------------------
# POST /api/navigate/start
# ---------------------------------------------------------------------------

@navigate_bp.route("/start", methods=["POST"])
def navigate_start():
    """Start a new LangGraph navigation session.

    Request body (JSON)::

        {
            "goal": "Navigate to Data Display"  // optional, this is the default
        }

    Response::

        {
            "success": true,
            "session_id": "abc123",
            "status": "running"
        }
    """
    data = request.json or {}
    goal = (data.get("goal") or "Navigate to Data Display").strip()

    session = start_navigation_session(goal)

    return jsonify({
        "success": True,
        "session_id": session.session_id,
        "status": session.status.value,
    })


# ---------------------------------------------------------------------------
# GET /api/navigate/events?session_id=...
# ---------------------------------------------------------------------------

@navigate_bp.route("/events")
def navigate_events():
    """SSE event stream for a navigation session.

    Query params:
        session_id (str): required

    Event types:
        progress          - Node executed a step
        decision_required - Graph paused, user must choose
        done              - Navigation complete
        error             - Something went wrong
    """
    session_id = (request.args.get("session_id") or "").strip()
    if not session_id:
        return jsonify({"success": False, "error": "session_id required"}), 400

    try:
        session = _get_session(session_id)
    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404

    def generate():
        yield f"event: connected\ndata: {json.dumps({'session_id': session_id})}\n\n"

        while True:
            try:
                event = session.event_queue.get(timeout=1)
            except queue.Empty:
                # Send keepalive
                yield ": keepalive\n\n"

                # Check if thread is still alive
                if session.thread and not session.thread.is_alive():
                    # Thread finished but no terminal event was consumed yet.
                    # Drain remaining events.
                    while not session.event_queue.empty():
                        try:
                            event = session.event_queue.get_nowait()
                            event_type = event.get("type", "progress")
                            yield f"event: {event_type}\ndata: {json.dumps(event)}\n\n"
                        except queue.Empty:
                            break
                    # If still no terminal event, synthesize one
                    if session.status in (NavSessionStatus.COMPLETED, NavSessionStatus.FAILED, NavSessionStatus.ABORTED):
                        yield f"event: done\ndata: {json.dumps({'type': 'done', 'status': session.status.value, 'error': session.error})}\n\n"
                        return
                continue

            event_type = event.get("type", "progress")

            # Track state for status endpoint
            if event_type == "progress":
                session.current_page = event.get("page", session.current_page)
            elif event_type == "decision_required":
                session.status = NavSessionStatus.AWAITING_DECISION
                session.pending_decision_id = event.get("decision_id")
                session.pending_items = event.get("items", [])

            yield f"event: {event_type}\ndata: {json.dumps(event)}\n\n"

            # Terminal events
            if event_type in ("done", "error"):
                return

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# POST /api/navigate/decision
# ---------------------------------------------------------------------------

@navigate_bp.route("/decision", methods=["POST"])
def navigate_decision():
    """Submit a user decision for a paused navigation session.

    Request body (JSON)::

        {
            "session_id": "abc123",
            "decision_id": "nav_decision_1",
            "selected_item": "ECM - Engine Control"
        }
    """
    data = request.json or {}
    session_id = (data.get("session_id") or "").strip()
    decision_id = (data.get("decision_id") or "").strip()
    selected_item = (data.get("selected_item") or "").strip()

    if not session_id:
        return jsonify({"success": False, "error": "session_id required"}), 400
    if not selected_item:
        return jsonify({"success": False, "error": "selected_item required"}), 400

    try:
        return jsonify(
            submit_navigation_decision(
                session_id,
                decision_id=decision_id,
                selected_item=selected_item,
            )
        )
    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404
    except ValueError as exc:
        message = str(exc)
        status_code = 409 if "awaiting a decision" in message or "terminated" in message else 400
        return jsonify({"success": False, "error": message}), status_code


# ---------------------------------------------------------------------------
# GET /api/navigate/status?session_id=...
# ---------------------------------------------------------------------------

@navigate_bp.route("/status")
def navigate_status():
    """Query current navigation session status."""
    session_id = (request.args.get("session_id") or "").strip()
    if not session_id:
        return jsonify({"success": False, "error": "session_id required"}), 400

    try:
        session = _get_session(session_id)
    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404

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

    return jsonify(payload)


# ---------------------------------------------------------------------------
# POST /api/navigate/abort
# ---------------------------------------------------------------------------

@navigate_bp.route("/abort", methods=["POST"])
def navigate_abort():
    """Abort a running or paused navigation session."""
    data = request.json or {}
    session_id = (data.get("session_id") or "").strip()

    if not session_id:
        return jsonify({"success": False, "error": "session_id required"}), 400

    try:
        return jsonify(abort_navigation_session(session_id))
    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
