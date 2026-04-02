"""Session orchestration API blueprint (Phase G3).

Provides endpoints for session lifecycle, SSE event streaming,
user decision submission, and abort.  Operates on the in-memory
SessionOrchestrator and does not touch existing diagnostics routes.
"""

# pyright: reportMissingImports=false

import logging
from typing import Any

from flask import Blueprint, Response, jsonify, request

from diagnostic_platform.contracts import BackendCapability, UnsupportedCapabilityError
from diagnostic_platform.runtime.session_actions import (
    ensure_session_capability,
    execute_backend_action,
    select_data_category_action,
    select_module_action,
)
from diagnostic_platform.runtime.session_decisions import (
    raise_branch_decision,
    submit_session_decision,
)
from diagnostic_platform.runtime.session_lifecycle import (
    abort_business_session,
    build_session_status_payload,
    start_business_session,
)
from diagnostic_platform.runtime.worker_runtime import (
    OperationCancelledError,
    WorkerBusyError,
)
from diagnostic_platform.runtime.session_preflight import (
    run_start_diagnostics,
)
from diagnostic_platform.runtime.session_streams import (
    iter_session_events,
)

from src.gds2_orchestration.planner import BranchDecisionRequiredError
from src.gds2_orchestration.session_orchestrator import (
    SessionContext,
    SessionStatus,
)
from server.api import session_ai_handlers
from server.api.session_dependencies import (
    _runtime,
    get_adapter,
    get_backend as _get_backend,
    get_data_viewer as _get_data_viewer,
    get_executor,
    get_orchestrator,
    get_ai_engine as _get_ai_engine,
    reset_executor,
    set_data_viewer_getter,
    set_orchestrator,
)
from server.api import session_live_data_handlers
from server.api import session_navigation_handlers
logger = logging.getLogger(__name__)

session_bp = Blueprint("session", __name__, url_prefix="/api/session")


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


# ---------------------------------------------------------------------------
# POST /api/session/start
# ---------------------------------------------------------------------------

@session_bp.route("/start", methods=["POST"])
def session_start():
    """Start a new diagnostics session.

    Request body (JSON)::

        {
            "brand": "Chevrolet",      # required
            "model": "Malibu",         # optional
            "vin": "1G1ZD5...",        # optional
        }

    Response::

        {
            "success": true,
            "session_id": "abc123...",
            "status": "running",       # or "awaiting_decision"
            "backend_name": "gds2",    # primary field
            "workflow": "gds2",        # deprecated alias
        }
    """
    data = request.json or {}
    brand = (data.get("brand") or "").strip()

    if not brand:
        return jsonify({"success": False, "error": "brand is required"}), 400

    try:
        ctx = SessionContext(
            brand=brand,
            model=(data.get("model") or "").strip(),
            vin=(data.get("vin") or "").strip(),
            backend_name=(data.get("backend_name") or "").strip(),
            extra={
                k: v
                for k, v in data.items()
                if k not in ("brand", "model", "vin", "backend_name")
            },
        )
        return jsonify(
            start_business_session(
                _runtime(),
                orchestrator=get_orchestrator(),
                context=ctx,
            )
        )

    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"success": False, "error": str(exc)}), 409

    except Exception as exc:
        logger.exception("session_start failed")
        return jsonify({"success": False, "error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/session/start_diagnostics
# ---------------------------------------------------------------------------

@session_bp.route("/start_diagnostics", methods=["POST"])
def session_start_diagnostics():
    """Start GDS2 diagnostics via the deterministic executor.

    Executes START_DIAGNOSTICS through the DeterministicExecutor,
    which dispatches to the real DataViewerWorkflow.start().

    Request body (JSON)::

        {
            "session_id": "abc123..."
        }

    Response::

        {
            "success": true,
            "session_id": "abc123...",
            "result": { ... }  // modules or devices from GDS2
        }
    """
    data = request.json or {}
    session_id = (data.get("session_id") or "").strip()

    if not session_id:
        return jsonify({"success": False, "error": "session_id required"}), 400

    try:
        payload = run_start_diagnostics(
            _runtime(),
            orchestrator=get_orchestrator(),
            backend=_get_backend(),
            session_id=session_id,
        )
        if payload.get("result"):
            result = payload["result"]
            logger.info(
                "SESSION %s diagnostics started modules=%s device=%s",
                session_id,
                len(result["modules"]),
                result.get("device") or '-',
            )
        return jsonify(payload)

    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 409
    except (OperationCancelledError, WorkerBusyError) as exc:
        return jsonify({"success": False, "error": str(exc)}), 409
    except Exception as exc:
        logger.exception("session_start_diagnostics failed")
        return jsonify({"success": False, "error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/session/execute
# ---------------------------------------------------------------------------

@session_bp.route("/execute", methods=["POST"])
def session_execute():
    """Execute a single backend action through the active backend contract.

    Generic endpoint for one backend-owned action. The active backend
    validates and executes the action behind the shared session facade.

    Request body (JSON)::

        {
            "session_id": "abc123...",
            "action": "select_module",    // backend-specific action id
            "args": {"module_name": "ECM"},  // action-specific arguments
            "timeout_sec": 30.0             // optional
        }
    """
    data = request.json or {}
    session_id = (data.get("session_id") or "").strip()
    action_name = (data.get("action") or "").strip()

    if not session_id:
        return jsonify({"success": False, "error": "session_id required"}), 400
    if not action_name:
        return jsonify({"success": False, "error": "action required"}), 400

    try:
        orch = get_orchestrator()
        session = orch.get_session(session_id)
        ensure_session_capability(session, BackendCapability.GENERIC_ACTIONS)

        logger.info("SESSION %s action=%s start", session_id, action_name)

        try:
            outcome = execute_backend_action(
                session_id,
                backend=_get_backend(),
                action_name=action_name,
                action_args=data.get("args") or {},
                timeout_sec=float(data.get("timeout_sec", 30.0)),
                emit_progress=lambda message: orch.emit_progress(session_id, message),
            )
        except BranchDecisionRequiredError as exc:
            payload = raise_branch_decision(
                orchestrator=orch,
                session_id=session_id,
                exc=exc,
                default_action=action_name,
            )
            logger.info(
                "SESSION %s action=%s awaiting decision=%s domain=%s options=%s",
                session_id,
                action_name,
                payload["decision"]["decision_id"],
                exc.decision.domain.value,
                len(exc.choices),
            )
            return jsonify(payload)

        if not outcome["success"]:
            logger.warning(
                "SESSION %s action=%s failed error=%s",
                session_id,
                action_name,
                outcome["error"],
            )
            return jsonify({
                "success": False,
                "session_id": session_id,
                "action": action_name,
                "error": outcome["error"],
                "attempts": outcome["attempts"],
                "elapsed_time": outcome["elapsed_time"],
            }), 500

        logger.info(
            "SESSION %s action=%s completed attempts=%s elapsed=%.1fs",
            session_id,
            action_name,
            outcome["attempts"],
            outcome["elapsed_time"],
        )
        return jsonify({
            "success": True,
            "session_id": session_id,
            "action": action_name,
            "result": outcome["result"],
            "attempts": outcome["attempts"],
            "elapsed_time": outcome["elapsed_time"],
        })

    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404
    except UnsupportedCapabilityError as exc:
        return jsonify({"success": False, "error": str(exc)}), 501
    except ValueError as exc:
        status = 409 if "Session not running" in str(exc) else 400
        return jsonify({"success": False, "error": str(exc)}), status
    except Exception as exc:
        logger.exception("session_execute failed")
        return jsonify({"success": False, "error": str(exc)}), 500


# ---------------------------------------------------------------------------
# GET /api/session/events?session_id=...
# ---------------------------------------------------------------------------

@session_bp.route("/events")
def session_events():
    """SSE stream for session events.

    Query params:
        session_id (str): required
    """
    session_id = (request.args.get("session_id") or "").strip()
    if not session_id:
        return jsonify({"success": False, "error": "session_id required"}), 400

    try:
        return _sse_response(
            iter_session_events(
                orchestrator=get_orchestrator(),
                backend=_get_backend(),
                session_id=session_id,
            )
        )
    except LookupError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404


# ---------------------------------------------------------------------------
# POST /api/session/decision
# ---------------------------------------------------------------------------

@session_bp.route("/decision", methods=["POST"])
def session_decision():
    """Submit a decision for a pending gate.

    Request body (JSON)::

        {
            "session_id": "abc123...",
            "decision_id": "def456...",
            "option_id": "gds2"
        }
    """
    data = request.json or {}
    session_id = (data.get("session_id") or "").strip()
    decision_id = (data.get("decision_id") or "").strip()
    option_id = (data.get("option_id") or "").strip()

    if not session_id:
        return jsonify({"success": False, "error": "session_id required"}), 400
    if not decision_id:
        return jsonify({"success": False, "error": "decision_id required"}), 400
    if not option_id:
        return jsonify({"success": False, "error": "option_id required"}), 400

    try:
        return jsonify(
            submit_session_decision(
                _runtime(),
                orchestrator=get_orchestrator(),
                backend=_get_backend(),
                session_id=session_id,
                decision_id=decision_id,
                option_id=option_id,
                get_data_viewer=_get_data_viewer,
                get_backend=_get_backend,
            )
        )

    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404

    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    except Exception as exc:
        logger.exception("session_decision failed")
        return jsonify({"success": False, "error": str(exc)}), 500


@session_bp.route("/select_module", methods=["POST"])
def session_select_module():
    """Select module within a running session.

    If ambiguous, emits decision_required via SessionOrchestrator and returns
    awaiting_decision payload.
    """
    data = request.json or {}
    session_id = (data.get("session_id") or "").strip()
    module = (data.get("module") or "").strip()

    if not session_id:
        return jsonify({"success": False, "error": "session_id required"}), 400
    if not module:
        return jsonify({"success": False, "error": "module required"}), 400

    try:
        orch = get_orchestrator()
        session = orch.get_session(session_id)
        if session.status != SessionStatus.RUNNING:
            return jsonify({
                "success": False,
                "error": f"Session not running (status={session.status.value})",
            }), 409

        try:
            outcome = select_module_action(
                _runtime(),
                session,
                module=module,
                backend=_get_backend(),
                get_data_viewer=_get_data_viewer,
                get_executor=get_executor,
                get_adapter=get_adapter,
                emit_progress=lambda message: orch.emit_progress(session_id, message),
            )
        except BranchDecisionRequiredError as exc:
            payload = raise_branch_decision(
                orchestrator=orch,
                session_id=session_id,
                exc=exc,
                default_action="select_module",
            )
            logger.info(
                "SESSION %s select_module awaiting decision=%s options=%s",
                session_id,
                payload["decision"]["decision_id"],
                len(exc.choices),
            )
            return jsonify(payload)

        if not outcome["success"]:
            logger.warning("SESSION %s select_module failed error=%s", session_id, outcome["error"])
            return jsonify({
                "success": False,
                "session_id": session_id,
                "error": outcome["error"],
            }), 500

        logger.info("SESSION %s module=%s selected", session_id, module)
        return jsonify({
            "success": True,
            "session_id": session_id,
            "result": outcome["result"],
        })

    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        logger.exception("session_select_module failed")
        return jsonify({"success": False, "error": str(exc)}), 500


@session_bp.route("/select_data_category", methods=["POST"])
def session_select_data_category():
    """Select data category within a running session.

    If ambiguous, emits decision_required and waits for /decision.
    """
    data = request.json or {}
    session_id = (data.get("session_id") or "").strip()
    data_category = (data.get("data_category") or "").strip()

    if not session_id:
        return jsonify({"success": False, "error": "session_id required"}), 400
    if not data_category:
        return jsonify({"success": False, "error": "data_category required"}), 400

    try:
        orch = get_orchestrator()
        session = orch.get_session(session_id)
        if session.status != SessionStatus.RUNNING:
            return jsonify({
                "success": False,
                "error": f"Session not running (status={session.status.value})",
            }), 409

        try:
            outcome = select_data_category_action(
                _runtime(),
                session,
                data_category=data_category,
                backend=_get_backend(),
                get_data_viewer=_get_data_viewer,
                get_executor=get_executor,
                get_adapter=get_adapter,
                emit_progress=lambda message: orch.emit_progress(session_id, message),
            )
        except BranchDecisionRequiredError as exc:
            payload = raise_branch_decision(
                orchestrator=orch,
                session_id=session_id,
                exc=exc,
                default_action="select_data_category",
            )
            logger.info(
                "SESSION %s select_data_category awaiting decision=%s options=%s",
                session_id,
                payload["decision"]["decision_id"],
                len(exc.choices),
            )
            return jsonify(payload)

        if not outcome["success"]:
            logger.warning("SESSION %s select_data_category failed error=%s", session_id, outcome["error"])
            return jsonify({
                "success": False,
                "session_id": session_id,
                "error": outcome["error"],
            }), 500

        logger.info("SESSION %s data_category=%s selected", session_id, data_category)
        return jsonify({
            "success": True,
            "session_id": session_id,
            "result": outcome["result"],
        })

    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        logger.exception("session_select_data_category failed")
        return jsonify({"success": False, "error": str(exc)}), 500


@session_bp.route("/ai_diagnose", methods=["POST"])
def session_ai_diagnose():
    """Start AI diagnosis through the public session facade."""
    payload, status = session_ai_handlers.start_ai_diagnose(request.json or {})
    return jsonify(payload), status


@session_bp.route("/ai_diagnose/events")
def session_ai_diagnose_events():
    """Stream AI diagnosis SSE events through the business session id."""
    result = session_ai_handlers.stream_ai_diagnose_events(
        request.args.get("session_id", "").strip(),
        sse_response=_sse_response,
    )
    if isinstance(result, tuple):
        payload, status = result
        return jsonify(payload), status
    return result


@session_bp.route("/ai_diagnose/retry", methods=["POST"])
def session_ai_diagnose_retry():
    """Retry AI diagnosis through the public session facade."""
    payload, status = session_ai_handlers.retry_ai_diagnose(request.json or {})
    return jsonify(payload), status


@session_bp.route("/dtcs", methods=["POST"])
def session_dtcs():
    """Read DTCs through the public session facade."""
    payload, status = session_live_data_handlers.read_session_dtcs(request.json or {})
    return jsonify(payload), status


@session_bp.route("/live_data/start", methods=["POST"])
def session_live_data_start():
    """Start live data streaming through the public session facade."""
    payload, status = session_live_data_handlers.start_live_data_session(request.json or {})
    return jsonify(payload), status


@session_bp.route("/live_data/events")
def session_live_data_events():
    """SSE endpoint for session-scoped live data events."""
    result = session_live_data_handlers.stream_live_data_events(
        (request.args.get("session_id") or "").strip(),
        sse_response=_sse_response,
    )
    if isinstance(result, tuple):
        payload, status = result
        return jsonify(payload), status
    return result


@session_bp.route("/live_data/stop", methods=["POST"])
def session_live_data_stop():
    """Stop live data streaming through the public session facade."""
    payload, status = session_live_data_handlers.stop_live_data_session(request.json or {})
    return jsonify(payload), status


@session_bp.route("/navigate/start", methods=["POST"])
def session_navigate_start():
    """Start guided navigation through the public session facade."""
    payload, status = session_navigation_handlers.start_navigation_session_for_business(request.json or {})
    return jsonify(payload), status


@session_bp.route("/navigate/events")
def session_navigate_events():
    """SSE endpoint for session-scoped navigation events."""
    result = session_navigation_handlers.stream_navigation_events(
        (request.args.get("session_id") or "").strip(),
        sse_response=_sse_response,
    )
    if isinstance(result, tuple):
        payload, status = result
        return jsonify(payload), status
    return result


@session_bp.route("/navigate/decision", methods=["POST"])
def session_navigate_decision():
    """Submit a navigation decision through the public session facade."""
    payload, status = session_navigation_handlers.submit_navigation_decision_for_business(request.json or {})
    return jsonify(payload), status


@session_bp.route("/navigate/abort", methods=["POST"])
def session_navigate_abort():
    """Abort the active navigation sub-session for a business session."""
    payload, status = session_navigation_handlers.abort_navigation_session_for_business(request.json or {})
    return jsonify(payload), status


@session_bp.route("/navigate/status")
def session_navigate_status():
    """Return navigation sub-session status by business session id."""
    payload, status = session_navigation_handlers.build_navigation_status_for_business(
        (request.args.get("session_id") or "").strip()
    )
    return jsonify(payload), status


# ---------------------------------------------------------------------------
# POST /api/session/abort
# ---------------------------------------------------------------------------

@session_bp.route("/abort", methods=["POST"])
def session_abort():
    """Abort a running or awaiting session.

    Request body (JSON)::

        {
            "session_id": "abc123...",
            "reason": "User cancelled"     # optional
        }
    """
    data = request.json or {}
    session_id = (data.get("session_id") or "").strip()

    if not session_id:
        return jsonify({"success": False, "error": "session_id required"}), 400

    try:
        reason = (data.get("reason") or "").strip()
        return jsonify(
            abort_business_session(
                _runtime(),
                orchestrator=get_orchestrator(),
                session_id=session_id,
                reason=reason,
                get_ai_engine=_get_ai_engine,
            )
        )

    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404

    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    except Exception as exc:
        logger.exception("session_abort failed")
        return jsonify({"success": False, "error": str(exc)}), 500


# ---------------------------------------------------------------------------
# GET /api/session/status?session_id=...
# ---------------------------------------------------------------------------

@session_bp.route("/status")
def session_status():
    """Return current session state.

    Query params:
        session_id (str): required
    """
    session_id = (request.args.get("session_id") or "").strip()
    if not session_id:
        return jsonify({"success": False, "error": "session_id required"}), 400

    try:
        return jsonify(
            build_session_status_payload(
                _runtime(),
                orchestrator=get_orchestrator(),
                backend=_get_backend(),
                session_id=session_id,
            )
        )

    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404

    except Exception as exc:
        logger.exception("session_status failed")
        return jsonify({"success": False, "error": str(exc)}), 500
