"""Session orchestration API blueprint (Phase G3).

Provides endpoints for session lifecycle, SSE event streaming,
user decision submission, and abort.  Operates on the in-memory
SessionOrchestrator and does not touch existing diagnostics routes.
"""

# pyright: reportMissingImports=false

import logging
import time
from typing import Any, Callable

from flask import Blueprint, Response, jsonify, request

from backends.gds2 import GDS2DiagnosticBackend
from diagnostic_platform.runtime.session_actions import (
    abort_navigation,
    execute_gds2_action,
    ensure_running_gds2_session,
    navigation_status_payload,
    read_dtcs,
    resolve_session_vehicle_context,
    resolve_ai_event_stream,
    resolve_navigation,
    retry_ai_diagnosis,
    select_data_category_action,
    select_module_action,
    start_ai_diagnosis,
    start_live_data,
    start_navigation,
    stop_live_data,
    submit_navigation_decision,
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
from diagnostic_platform.runtime.worker_runtime import get_worker_runtime
from diagnostic_platform.runtime.session_state import (
    live_data_active as is_live_data_active,
)
from diagnostic_platform.runtime.session_preflight import (
    run_start_diagnostics,
)
from diagnostic_platform.runtime.session_streams import (
    iter_ai_events,
    iter_navigation_events,
    iter_scoped_agent_events,
    iter_session_events,
)
from diagnostic_platform.sse import (
    session_agent_stream_scope,
)

from src.agentic.executor import DeterministicExecutor
from src.agentic.adapters.gds2_adapter import GDS2ActionAdapter
from src.agentic.planner import BranchDecisionRequiredError
from src.agentic.session_orchestrator import (
    SessionContext,
    SessionOrchestrator,
    SessionStatus,
)
logger = logging.getLogger(__name__)

session_bp = Blueprint("session", __name__, url_prefix="/api/session")


def _runtime():
    return get_worker_runtime()


def get_orchestrator() -> SessionOrchestrator:
    """Return the shared worker-scoped orchestrator.

    Exposed so tests can swap / reset it.
    """
    return _runtime().orchestrator


def set_orchestrator(orch: SessionOrchestrator) -> None:
    """Replace the shared worker-scoped orchestrator (for testing)."""
    _runtime().set_orchestrator(orch)


def set_data_viewer_getter(getter: Callable[[], Any] | None) -> None:
    """Inject a lightweight viewer object for tests that bypass GDS2 startup."""
    _runtime().set_data_viewer_getter(getter)


def _get_data_viewer() -> Any:
    """Return the injected viewer when present, otherwise the real workflow."""
    return _runtime().get_data_viewer(GDS2DiagnosticBackend)


def _get_backend() -> GDS2DiagnosticBackend:
    return _runtime().get_backend(GDS2DiagnosticBackend)


def _get_ai_engine():
    import diagnostics_api

    return diagnostics_api._get_ai_engine()


def _make_ai_collection_guard(data_category: str):
    import diagnostics_api

    return diagnostics_api._make_data_display_guard(
        _get_backend(),
        data_category,
        mode="ai_collect",
    )


def get_executor() -> DeterministicExecutor:
    """Return the worker-scoped executor, lazily wired with the GDS2 adapter.

    Creates a DeterministicExecutor + GDS2ActionAdapter on first call,
    using the DataViewerWorkflow wrapped by GDS2DiagnosticBackend.
    """
    if _runtime().get_adapter() is None:
        # Transitional pattern: session_api still depends on the executor chain,
        # so we reach through the backend to reuse its underlying workflow until
        # this module is migrated to backend.execute_action().
        logger.debug("Session API executor wiring with GDS2 adapter")
    return _runtime().get_executor(lambda: _get_backend()._get_workflow())


def get_adapter() -> GDS2ActionAdapter | None:
    """Return the current adapter (available after get_executor() is called)."""
    return _runtime().get_adapter()


def reset_executor() -> None:
    """Reset executor/adapter (for testing or when DataViewerWorkflow changes)."""
    _runtime().reset_executor()


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
            "workflow": "gds2",        # or null
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
            extra={k: v for k, v in data.items() if k not in ("brand", "model", "vin")},
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
    """Start GDS2 diagnostics via the agentic executor.

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
    except Exception as exc:
        logger.exception("session_start_diagnostics failed")
        return jsonify({"success": False, "error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/session/execute
# ---------------------------------------------------------------------------

@session_bp.route("/execute", methods=["POST"])
def session_execute():
    """Execute a single GDS2 action through the agentic executor.

    Generic endpoint for any GDS2Action.  The action is validated
    by the PolicyGuard against current GDS2 UI state before execution.

    Request body (JSON)::

        {
            "session_id": "abc123...",
            "action": "select_module",    // GDS2Action enum value
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
        if session.status != SessionStatus.RUNNING:
            return jsonify({
                "success": False,
                "error": f"Session not running (status={session.status.value})",
            }), 409

        logger.info("SESSION %s action=%s start", session_id, action_name)

        try:
            outcome = execute_gds2_action(
                session_id,
                action_name=action_name,
                action_args=data.get("args") or {},
                timeout_sec=float(data.get("timeout_sec", 30.0)),
                get_executor=get_executor,
                get_adapter=get_adapter,
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
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
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
    data = request.json or {}
    session_id = (data.get("session_id") or "").strip()

    if not session_id:
        return jsonify({"success": False, "error": "session_id required"}), 400

    try:
        orch = get_orchestrator()
        session = orch.get_session(session_id)
        ensure_running_gds2_session(session, capability="AI diagnosis")

        vehicle_context = resolve_session_vehicle_context(
            session,
            data,
            backend=_get_backend(),
        )
        data_category = vehicle_context["data_category"]
        if not data_category:
            return jsonify({"success": False, "error": "data_category required"}), 400

        engine = _get_ai_engine()
        ai_session_id = start_ai_diagnosis(
            _runtime(),
            session,
            vehicle_context=vehicle_context,
            data_category=data_category,
            engine=engine,
            collection_guard=_make_ai_collection_guard(data_category),
            emit_progress=lambda message: orch.emit_progress(session_id, message),
        )
        logger.info(
            "SESSION %s ai_diagnose started ai_session=%s module=%s category=%s",
            session_id,
            ai_session_id,
            vehicle_context.get("module") or "-",
            data_category,
        )
        return jsonify({
            "success": True,
            "session_id": session_id,
            "ai_session_id": ai_session_id,
            "message": "AI diagnosis started. Subscribe to /api/session/ai_diagnose/events for progress.",
        })

    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404
    except RuntimeError as exc:
        return jsonify({"success": False, "error": str(exc)}), 409
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        logger.exception("session_ai_diagnose failed")
        return jsonify({"success": False, "error": str(exc)}), 500


@session_bp.route("/ai_diagnose/events")
def session_ai_diagnose_events():
    """Stream AI diagnosis SSE events through the business session id."""
    session_id = request.args.get("session_id", "").strip()
    if not session_id:
        return jsonify({"success": False, "error": "session_id required"}), 400

    try:
        session = get_orchestrator().get_session(session_id)
        ai_session_id, event_queue = resolve_ai_event_stream(
            _runtime(),
            session,
            engine=_get_ai_engine(),
        )

        logger.info(
            "SESSION %s ai_diagnose events bound ai_session=%s",
            session_id,
            ai_session_id,
        )

        return _sse_response(
            iter_ai_events(
                runtime=_runtime(),
                session=session,
                session_id=session_id,
                event_queue=event_queue,
            )
        )

    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404
    except LookupError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404
    except Exception as exc:
        logger.exception("session_ai_diagnose_events failed")
        return jsonify({"success": False, "error": str(exc)}), 500


@session_bp.route("/ai_diagnose/retry", methods=["POST"])
def session_ai_diagnose_retry():
    """Retry AI diagnosis through the public session facade."""
    data = request.json or {}
    session_id = (data.get("session_id") or "").strip()
    cached_payload_id = (data.get("cached_payload_id") or "").strip()

    if not session_id:
        return jsonify({"success": False, "error": "session_id required"}), 400
    if not cached_payload_id:
        return jsonify({"success": False, "error": "cached_payload_id required"}), 400

    try:
        orch = get_orchestrator()
        session = orch.get_session(session_id)
        ensure_running_gds2_session(session, capability="AI diagnosis")

        vehicle_context = resolve_session_vehicle_context(
            session,
            data,
            backend=_get_backend(),
        )
        engine = _get_ai_engine()
        ai_session_id = retry_ai_diagnosis(
            _runtime(),
            session,
            cached_payload_id=cached_payload_id,
            vehicle_context=vehicle_context,
            engine=engine,
            emit_progress=lambda message: orch.emit_progress(session_id, message),
        )
        logger.info(
            "SESSION %s ai_diagnose retry started ai_session=%s payload=%s",
            session_id,
            ai_session_id,
            cached_payload_id,
        )
        return jsonify({
            "success": True,
            "session_id": session_id,
            "ai_session_id": ai_session_id,
            "message": "Retry started. Subscribe to /api/session/ai_diagnose/events for progress.",
        })

    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404
    except RuntimeError as exc:
        return jsonify({"success": False, "error": str(exc)}), 409
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        logger.exception("session_ai_diagnose_retry failed")
        return jsonify({"success": False, "error": str(exc)}), 500


@session_bp.route("/dtcs", methods=["POST"])
def session_dtcs():
    """Read DTCs through the public session facade."""
    data = request.json or {}
    session_id = (data.get("session_id") or "").strip()

    if not session_id:
        return jsonify({"success": False, "error": "session_id required"}), 400

    try:
        orch = get_orchestrator()
        session = orch.get_session(session_id)
        ensure_running_gds2_session(session, capability="DTC read")
        result = read_dtcs(
            session,
            data,
            backend=_get_backend(),
            emit_progress=lambda message: orch.emit_progress(session_id, message),
        )
        logger.info(
            "SESSION %s dtcs read count=%s page=%s",
            session_id,
            result["dtc_count"],
            result["page_context"],
        )
        return jsonify({
            "success": True,
            "session_id": session_id,
            "result": result,
        })

    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        logger.exception("session_dtcs failed")
        return jsonify({"success": False, "error": str(exc)}), 500


@session_bp.route("/live_data/start", methods=["POST"])
def session_live_data_start():
    """Start live data streaming through the public session facade."""
    data = request.json or {}
    session_id = (data.get("session_id") or "").strip()
    interval_ms = int(data.get("interval_ms", 100))

    if not session_id:
        return jsonify({"success": False, "error": "session_id required"}), 400

    try:
        orch = get_orchestrator()
        session = orch.get_session(session_id)
        ensure_running_gds2_session(session, capability="Live data")
        data_category, payload = start_live_data(
            _runtime(),
            session,
            data,
            interval_ms,
            backend=_get_backend(),
            stream_scope=session_agent_stream_scope(session_id),
            emit_progress=lambda message: orch.emit_progress(session_id, message),
        )
        logger.info(
            "SESSION %s live_data started category=%s interval=%sms",
            session_id,
            data_category,
            interval_ms,
        )
        return jsonify({
            "success": True,
            "session_id": session_id,
            **payload,
        })

    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        logger.exception("session_live_data_start failed")
        return jsonify({"success": False, "error": str(exc)}), 500


@session_bp.route("/live_data/events")
def session_live_data_events():
    """SSE endpoint for session-scoped live data events."""
    session_id = (request.args.get("session_id") or "").strip()
    if not session_id:
        return jsonify({"success": False, "error": "session_id required"}), 400

    try:
        session = get_orchestrator().get_session(session_id)
        if not is_live_data_active(_runtime(), session_id):
            return jsonify({
                "success": False,
                "error": f"No active live data stream for {session_id}",
            }), 404
    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404

    return _sse_response(
        iter_scoped_agent_events(
            scope=session_agent_stream_scope(session_id),
            session_id=session_id,
        )
    )


@session_bp.route("/live_data/stop", methods=["POST"])
def session_live_data_stop():
    """Stop live data streaming through the public session facade."""
    data = request.json or {}
    session_id = (data.get("session_id") or "").strip()

    if not session_id:
        return jsonify({"success": False, "error": "session_id required"}), 400

    try:
        orch = get_orchestrator()
        session = orch.get_session(session_id)
        if session.status != SessionStatus.RUNNING:
            return jsonify({
                "success": False,
                "error": f"Session not running (status={session.status.value})",
            }), 409
        payload = stop_live_data(
            _runtime(),
            session,
            backend=_get_backend(),
            emit_progress=lambda message: orch.emit_progress(session_id, message),
        )
        logger.info("SESSION %s live_data stopped", session_id)
        return jsonify({
            "success": True,
            "session_id": session_id,
            **payload,
        })

    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404
    except Exception as exc:
        logger.exception("session_live_data_stop failed")
        return jsonify({"success": False, "error": str(exc)}), 500


@session_bp.route("/navigate/start", methods=["POST"])
def session_navigate_start():
    """Start agentic navigation through the public session facade."""
    data = request.json or {}
    session_id = (data.get("session_id") or "").strip()
    goal = (data.get("goal") or "Navigate to Data Display").strip()

    if not session_id:
        return jsonify({"success": False, "error": "session_id required"}), 400

    try:
        orch = get_orchestrator()
        session = orch.get_session(session_id)
        ensure_running_gds2_session(session, capability="Navigation")
        nav_session = start_navigation(
            _runtime(),
            session,
            goal=goal,
            emit_progress=lambda message: orch.emit_progress(session_id, message),
        )
        session.updated_at = time.time()
        logger.info(
            "SESSION %s navigation started nav_session=%s goal=%s",
            session_id,
            nav_session.session_id,
            goal,
        )
        return jsonify({
            "success": True,
            "session_id": session_id,
            "navigation_session_id": nav_session.session_id,
            "status": nav_session.status.value,
        })

    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404
    except RuntimeError as exc:
        return jsonify({"success": False, "error": str(exc)}), 409
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        logger.exception("session_navigate_start failed")
        return jsonify({"success": False, "error": str(exc)}), 500


@session_bp.route("/navigate/events")
def session_navigate_events():
    """SSE endpoint for session-scoped navigation events."""
    session_id = (request.args.get("session_id") or "").strip()
    if not session_id:
        return jsonify({"success": False, "error": "session_id required"}), 400

    try:
        session = get_orchestrator().get_session(session_id)
        nav_session_id, nav_session = resolve_navigation(_runtime(), session)
    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404
    except LookupError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404
    except Exception as exc:
        logger.exception("session_navigate_events failed to bind")
        return jsonify({"success": False, "error": str(exc)}), 500

    return _sse_response(
        iter_navigation_events(
            runtime=_runtime(),
            session=session,
            session_id=session_id,
            nav_session=nav_session,
        )
    )


@session_bp.route("/navigate/decision", methods=["POST"])
def session_navigate_decision():
    """Submit a navigation decision through the public session facade."""
    data = request.json or {}
    session_id = (data.get("session_id") or "").strip()
    decision_id = (data.get("decision_id") or "").strip()
    selected_item = (data.get("selected_item") or "").strip()

    if not session_id:
        return jsonify({"success": False, "error": "session_id required"}), 400
    if not selected_item:
        return jsonify({"success": False, "error": "selected_item required"}), 400

    try:
        session = get_orchestrator().get_session(session_id)
        nav_session_id, payload = submit_navigation_decision(
            _runtime(),
            session,
            decision_id=decision_id,
            selected_item=selected_item,
        )
        logger.info(
            "SESSION %s navigation decision submitted nav_session=%s selected=%s",
            session_id,
            nav_session_id,
            selected_item,
        )
        return jsonify({
            "success": True,
            "session_id": session_id,
            "navigation_session_id": nav_session_id,
            "selected_item": payload["selected_item"],
        })

    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404
    except LookupError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404
    except ValueError as exc:
        message = str(exc)
        status_code = 409 if "awaiting a decision" in message else 400
        return jsonify({"success": False, "error": message}), status_code
    except Exception as exc:
        logger.exception("session_navigate_decision failed")
        return jsonify({"success": False, "error": str(exc)}), 500


@session_bp.route("/navigate/abort", methods=["POST"])
def session_navigate_abort():
    """Abort the active navigation sub-session for a business session."""
    data = request.json or {}
    session_id = (data.get("session_id") or "").strip()

    if not session_id:
        return jsonify({"success": False, "error": "session_id required"}), 400

    try:
        session = get_orchestrator().get_session(session_id)
        nav_session_id, payload = abort_navigation(_runtime(), session)
        return jsonify({
            "success": True,
            "session_id": session_id,
            "navigation_session_id": nav_session_id,
            "status": payload["status"],
        })

    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404
    except LookupError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        logger.exception("session_navigate_abort failed")
        return jsonify({"success": False, "error": str(exc)}), 500


@session_bp.route("/navigate/status")
def session_navigate_status():
    """Return navigation sub-session status by business session id."""
    session_id = (request.args.get("session_id") or "").strip()
    if not session_id:
        return jsonify({"success": False, "error": "session_id required"}), 400

    try:
        session = get_orchestrator().get_session(session_id)
        payload = navigation_status_payload(_runtime(), session)
        return jsonify({
            "success": True,
            "session_id": session_id,
            **payload,
        })

    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404
    except LookupError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404
    except Exception as exc:
        logger.exception("session_navigate_status failed")
        return jsonify({"success": False, "error": str(exc)}), 500


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
