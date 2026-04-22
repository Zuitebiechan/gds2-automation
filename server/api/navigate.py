"""Deterministic navigation API blueprint.

Provides endpoints for running the guided navigation loop via HTTP/SSE,
with decision_required / decision submission over the network.

Endpoints:
    POST /api/navigate/start      - Start navigation session
    GET  /api/navigate/events      - SSE event stream
    POST /api/navigate/decision     - Submit user decision
    GET  /api/navigate/status       - Query session status
    POST /api/navigate/abort        - Abort running session
"""

from typing import Any

from flask import Blueprint, Response, jsonify, request
from diagnostic_platform.runtime.navigation_runtime import (
    NavSession,
    abort_navigation_session as abort_runtime_navigation_session,
    build_navigation_status_payload,
    get_navigation_session as get_runtime_navigation_session,
    iter_navigation_session_events,
    submit_navigation_decision as submit_runtime_navigation_decision,
)
from diagnostic_platform.runtime.worker_runtime import get_worker_runtime
from server.api.http_utils import (
    RequestPayloadError,
    read_text_mapping_field,
    require_json_object,
)

navigate_bp = Blueprint("navigate", __name__, url_prefix="/api/navigate")


def _read_text_field(
    data: dict[str, Any],
    field: str,
    *,
    default: str = "",
) -> str:
    return read_text_mapping_field(data, field, default=default)


def _runtime():
    return get_worker_runtime()


def _get_session(session_id: str) -> NavSession:
    return get_runtime_navigation_session(_runtime(), session_id)


def _get_active_navigation_handle(*, required: bool = True):
    bundle = _runtime().get_active_backend_bundle()
    if bundle is None or bundle.navigation_handle is None:
        if required:
            raise RuntimeError("No active backend navigation runtime is available")
        return None
    return bundle.navigation_handle


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
    return _get_active_navigation_handle().start_navigation_session(_runtime(), goal)


def get_navigation_session(session_id: str) -> NavSession:
    """Return an internal navigation session by id."""
    handle = _get_active_navigation_handle(required=False)
    if handle is not None:
        return handle.get_navigation_session(_runtime(), session_id)
    return _get_session(session_id)


def submit_navigation_decision(
    session_id: str,
    *,
    decision_id: str = "",
    selected_item: str,
) -> dict[str, Any]:
    """Resume a paused navigation session with a selected item."""
    handle = _get_active_navigation_handle(required=False)
    if handle is not None:
        return handle.submit_navigation_decision(
            _runtime(),
            session_id,
            decision_id=decision_id,
            selected_item=selected_item,
        )
    return submit_runtime_navigation_decision(
        _runtime(),
        session_id,
        decision_id=decision_id,
        selected_item=selected_item,
    )


def abort_navigation_session(session_id: str) -> dict[str, Any]:
    """Abort a running or paused internal navigation session."""
    handle = _get_active_navigation_handle(required=False)
    if handle is not None:
        return handle.abort_navigation_session(_runtime(), session_id)
    return abort_runtime_navigation_session(_runtime(), session_id)


# ---------------------------------------------------------------------------
# POST /api/navigate/start
# ---------------------------------------------------------------------------

@navigate_bp.route("/start", methods=["POST"])
def navigate_start():
    """Start a new deterministic navigation session."""
    try:
        data = require_json_object(request)
        goal = _read_text_field(data, "goal", default="Navigate to Data Display")
        session = start_navigation_session(goal)
    except RequestPayloadError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"success": False, "error": str(exc)}), 409

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
    try:
        session_id = read_text_mapping_field(request.args, "session_id")
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
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
    try:
        data = require_json_object(request)
        session_id = _read_text_field(data, "session_id")
        decision_id = _read_text_field(data, "decision_id")
        selected_item = _read_text_field(data, "selected_item")
    except RequestPayloadError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

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
    try:
        session_id = read_text_mapping_field(request.args, "session_id")
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
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
    try:
        data = require_json_object(request)
        session_id = _read_text_field(data, "session_id")
    except RequestPayloadError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    if not session_id:
        return jsonify({"success": False, "error": "session_id required"}), 400

    try:
        return jsonify(abort_navigation_session(session_id))
    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
