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
"""

import logging
from typing import Any

from flask import Blueprint, Response, jsonify, request
from diagnostic_platform.runtime.navigation_runtime import (
    NavSession,
    abort_navigation_session as abort_runtime_navigation_session,
    build_navigation_status_payload,
    get_navigation_session as get_runtime_navigation_session,
    iter_navigation_session_events,
    start_navigation_session as start_runtime_navigation_session,
    submit_navigation_decision as submit_runtime_navigation_decision,
)
from diagnostic_platform.runtime.worker_runtime import get_worker_runtime

logger = logging.getLogger(__name__)

navigate_bp = Blueprint("navigate", __name__, url_prefix="/api/navigate")


def _runtime():
    return get_worker_runtime()


def _get_session(session_id: str) -> NavSession:
    return get_runtime_navigation_session(_runtime(), session_id)


def _sse_response(stream) -> Response:
    return Response(
        stream,
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def start_navigation_session(goal: str = "Navigate to Data Display") -> NavSession:
    """Create and start an internal navigation session."""
    return start_runtime_navigation_session(_runtime(), goal)


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
    return submit_runtime_navigation_decision(
        _runtime(),
        session_id,
        decision_id=decision_id,
        selected_item=selected_item,
    )


def abort_navigation_session(session_id: str) -> dict[str, Any]:
    """Abort a running or paused internal navigation session."""
    return abort_runtime_navigation_session(_runtime(), session_id)


# ---------------------------------------------------------------------------
# POST /api/navigate/start
# ---------------------------------------------------------------------------

@navigate_bp.route("/start", methods=["POST"])
def navigate_start():
    """Start a new LangGraph navigation session."""
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
    """SSE event stream for a navigation session."""
    session_id = (request.args.get("session_id") or "").strip()
    if not session_id:
        return jsonify({"success": False, "error": "session_id required"}), 400

    try:
        session = _get_session(session_id)
    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404

    return _sse_response(iter_navigation_session_events(session_id, session))


# ---------------------------------------------------------------------------
# POST /api/navigate/decision
# ---------------------------------------------------------------------------

@navigate_bp.route("/decision", methods=["POST"])
def navigate_decision():
    """Submit a user decision for a paused navigation session."""
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

    return jsonify(build_navigation_status_payload(session_id, session))


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
