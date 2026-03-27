"""Session orchestration API blueprint (Phase G3).

Provides endpoints for session lifecycle, SSE event streaming,
user decision submission, and abort.  Operates on the in-memory
SessionOrchestrator and does not touch existing diagnostics routes.
"""

# pyright: reportMissingImports=false

import json
import logging
import queue
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from flask import Blueprint, Response, jsonify, request

from backends.gds2 import GDS2DiagnosticBackend
from diagnostic_platform.sse import agent_clients, agent_lock

from src.agentic.contracts.action_schema import ActionStep, GDS2Action
from src.agentic.executor import DeterministicExecutor
from src.agentic.adapters.gds2_adapter import GDS2ActionAdapter
from src.agentic.planner import BranchDecisionRequiredError
from src.navigation import GDS2Page
from src.agentic.session_orchestrator import (
    DecisionGate,
    DecisionOption,
    SessionContext,
    SessionOrchestrator,
    SessionStatus,
)

logger = logging.getLogger(__name__)

session_bp = Blueprint("session", __name__, url_prefix="/api/session")

# Module-level orchestrator instance (in-memory, single-process).
_orchestrator = SessionOrchestrator()
_backend: GDS2DiagnosticBackend | None = None
_executor: DeterministicExecutor | None = None
_adapter: GDS2ActionAdapter | None = None
_data_viewer_getter: Callable[[], Any] | None = None
_data_viewer: Any | None = None

def get_orchestrator() -> SessionOrchestrator:
    """Return the module-level orchestrator.

    Exposed so tests can swap / reset it.
    """
    return _orchestrator


def set_orchestrator(orch: SessionOrchestrator) -> None:
    """Replace the module-level orchestrator (for testing)."""
    global _orchestrator
    _orchestrator = orch


def set_data_viewer_getter(getter: Callable[[], Any] | None) -> None:
    """Inject a lightweight viewer object for tests that bypass GDS2 startup."""
    global _backend, _data_viewer_getter, _data_viewer
    _data_viewer_getter = getter
    _data_viewer = None
    _backend = None
    reset_executor()


def _get_data_viewer() -> Any:
    """Return the injected viewer when present, otherwise the real workflow."""
    global _data_viewer
    if _data_viewer_getter is None:
        return _get_backend()._get_workflow()

    if _data_viewer is None:
        _data_viewer = _data_viewer_getter()
        if _data_viewer is None:
            raise RuntimeError("Injected data viewer getter returned None")

    return _data_viewer


def _get_backend() -> GDS2DiagnosticBackend:
    global _backend
    if _backend is None:
        _backend = GDS2DiagnosticBackend()
    return _backend


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


def _resolve_session_vehicle_context(session: Any, data: dict[str, Any]) -> dict[str, str]:
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
        state = _get_backend().get_state()
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


def _set_session_selection(
    session: Any,
    *,
    module: str | None = None,
    data_category: str | None = None,
) -> None:
    if module is not None:
        session.selected_module = module
    if data_category is not None:
        session.selected_data_category = data_category
    session.updated_at = time.time()


def _clear_navigation_binding(session: Any) -> None:
    session.active_navigation_session_id = None
    session.updated_at = time.time()


def _clear_ai_binding(session: Any) -> None:
    session.active_ai_session_id = None
    session.updated_at = time.time()


def _set_live_data_active(session: Any, active: bool) -> None:
    session.live_data_active = active
    session.updated_at = time.time()


def _abort_active_navigation(session: Any) -> None:
    nav_session_id = getattr(session, "active_navigation_session_id", None)
    if not nav_session_id:
        return

    import navigate_api

    try:
        navigate_api.abort_navigation_session(nav_session_id)
    except KeyError:
        pass
    except ValueError:
        pass
    finally:
        _clear_navigation_binding(session)


def _abort_active_ai(session: Any) -> None:
    ai_session_id = getattr(session, "active_ai_session_id", None)
    if not ai_session_id:
        return

    try:
        engine = _get_ai_engine()
        engine.abort_session(ai_session_id)
    except Exception:
        logger.exception("Failed to abort AI session for business session %s", session.session_id)
    finally:
        _clear_ai_binding(session)


def _abort_active_live_data(session: Any) -> None:
    if not getattr(session, "live_data_active", False):
        return

    try:
        import diagnostics_api

        diagnostics_api.stop_live_data_stream()
    except Exception:
        logger.exception("Failed to stop live data for business session %s", session.session_id)
    finally:
        _set_live_data_active(session, False)


def get_executor() -> DeterministicExecutor:
    """Return the module-level executor, lazily wired with the GDS2 adapter.

    Creates a DeterministicExecutor + GDS2ActionAdapter on first call,
    using the DataViewerWorkflow wrapped by GDS2DiagnosticBackend.
    """
    global _executor, _adapter
    if _executor is None:
        # Transitional pattern: session_api still depends on the executor chain,
        # so we reach through the backend to reuse its underlying workflow until
        # this module is migrated to backend.execute_action().
        workflow = _get_backend()._get_workflow()
        _adapter = GDS2ActionAdapter(workflow)
        _executor = DeterministicExecutor()
        _adapter.register_all(_executor)
        logger.debug("Session API executor wired with GDS2 adapter")
    return _executor


def get_adapter() -> GDS2ActionAdapter | None:
    """Return the current adapter (available after get_executor() is called)."""
    return _adapter


def reset_executor() -> None:
    """Reset executor/adapter (for testing or when DataViewerWorkflow changes)."""
    global _executor, _adapter
    _executor = None
    _adapter = None

def _build_branch_gate(
    *,
    domain: str,
    target: str,
    choices: list[str],
    reason: str,
    resume_action: str,
) -> DecisionGate:
    options = [
        DecisionOption(
            option_id=f"branch_{idx}",
            label=choice,
            description=f"{domain} candidate",
        )
        for idx, choice in enumerate(choices)
    ]
    option_map = {opt.option_id: opt.label for opt in options}
    fallback_option_id = options[0].option_id if options else None

    return DecisionGate(
        decision_id=uuid.uuid4().hex[:12],
        prompt=(
            f"Ambiguous {domain} selection for '{target}'. "
            "Please choose one option to continue."
        ),
        options=options,
        kind="branch",
        context={
            "domain": domain,
            "target": target,
            "reason": reason,
            "option_map": option_map,
            "resume_action": resume_action,
        },
        timeout_sec=120.0,
        fallback_option_id=fallback_option_id,
    )


def _resume_action_for_domain(domain: str, default_action: str) -> str:
    """Map branch domain to resume action for /api/session/decision."""
    mapping = {
        "sub_module": "select_sub_module",
        "sub_category": "select_sub_category",
    }
    return mapping.get(domain, default_action)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _network_quality_summary(snapshot: dict[str, Any] | None) -> str:
    if not isinstance(snapshot, dict):
        return "grade=unknown status=unknown epoch=None p95=n/a reason=none"
    metrics = snapshot.get("network_ms") or {}
    p95 = metrics.get("p95")
    p95_text = "n/a" if p95 is None else f"{float(p95):.1f}ms"
    return (
        f"grade={snapshot.get('grade')} status={snapshot.get('status')} "
        f"epoch={snapshot.get('connection_epoch')} p95={p95_text} "
        f"reason={snapshot.get('reason')} samples={snapshot.get('sample_count')} "
        f"fresh={snapshot.get('fresh')} connected={snapshot.get('connected')}"
    )


def _get_session_network_snapshot(session_id: str) -> dict[str, Any]:
    session = get_orchestrator().get_session(session_id)
    if session.workflow != "gds2":
        return {
            "network_quality": None,
            "network_override": None,
            "connection_epoch": None,
        }

    preflight = _get_backend().preflight()
    network_quality = preflight.get("network_quality")
    connection_epoch = preflight.get("connection_epoch")
    effective_override = _get_effective_network_override(session, connection_epoch)
    raw_override = getattr(session, "network_override", None)
    if isinstance(raw_override, dict) and effective_override is None and raw_override.get("allowed"):
        logger.info(
            "[NETWORK_GATE] session=%s override_invalidated old_epoch=%s new_epoch=%s",
            session_id,
            raw_override.get("connection_epoch"),
            connection_epoch,
        )
        _clear_network_override(session_id, reason="epoch_changed")
    return {
        "network_quality": network_quality,
        "network_override": effective_override,
        "connection_epoch": connection_epoch,
    }


def _get_effective_network_override(session: Any, connection_epoch: str | None) -> dict[str, Any] | None:
    override = getattr(session, "network_override", None)
    if not isinstance(override, dict):
        return None
    if not override.get("allowed"):
        return None
    if override.get("connection_epoch") != connection_epoch:
        return None
    return dict(override)


def _set_network_override(session_id: str, connection_epoch: str | None) -> dict[str, Any]:
    orch = get_orchestrator()
    session = orch.get_session(session_id)
    override = {
        "allowed": True,
        "confirmed_at": _utc_now_iso(),
        "connection_epoch": connection_epoch,
        "reason": "user_confirmed_high_latency",
    }
    session.network_override = override
    session.updated_at = time.time()
    logger.info(
        "[NETWORK_GATE] session=%s override_set epoch=%s confirmed_at=%s",
        session_id,
        connection_epoch,
        override["confirmed_at"],
    )
    return override


def _clear_network_override(session_id: str, reason: str = "cleared") -> None:
    session = get_orchestrator().get_session(session_id)
    if session.network_override is not None:
        logger.info(
            "[NETWORK_GATE] session=%s override_cleared reason=%s previous_epoch=%s",
            session_id,
            reason,
            session.network_override.get("connection_epoch")
            if isinstance(session.network_override, dict)
            else None,
        )
    session.network_override = None
    session.updated_at = time.time()


def _build_network_quality_gate(network_quality: dict[str, Any] | None) -> DecisionGate:
    payload = dict(network_quality or {})
    reason = str(payload.get("reason") or "network quality is blocked")
    connection_epoch = payload.get("connection_epoch")
    return DecisionGate(
        decision_id=uuid.uuid4().hex[:12],
        prompt=(
            "Tunnel quality is currently blocked. "
            f"Reason: {reason}. Continue anyway?"
        ),
        options=[
            DecisionOption(
                option_id="continue_anyway",
                label="Continue anyway",
                description="Start diagnostics for this tunnel epoch anyway",
            ),
            DecisionOption(
                option_id="cancel",
                label="Cancel",
                description="Do not start diagnostics until the tunnel stabilizes",
            ),
        ],
        kind="network_quality",
        context={
            "network_quality": payload,
            "connection_epoch": connection_epoch,
        },
        timeout_sec=120.0,
        fallback_option_id="cancel",
    )


def _network_signature(payload: dict[str, Any]) -> tuple[Any, ...]:
    quality = payload.get("network_quality") or {}
    return (
        quality.get("grade"),
        quality.get("status"),
        quality.get("reason"),
        payload.get("connection_epoch"),
        payload.get("network_override", {}).get("connection_epoch")
        if isinstance(payload.get("network_override"), dict)
        else None,
    )


def _run_start_diagnostics(session_id: str, *, resumed: bool = False) -> dict[str, Any]:
    orch = get_orchestrator()
    session = orch.get_session(session_id)
    if session.status != SessionStatus.RUNNING:
        raise ValueError(f"Session not running (status={session.status.value})")
    if session.workflow != "gds2":
        raise ValueError(f"Session workflow is '{session.workflow}', not 'gds2'")

    network_snapshot = _get_session_network_snapshot(session_id)
    network_quality = network_snapshot["network_quality"]
    effective_override = network_snapshot["network_override"]
    logger.info(
        "[NETWORK_GATE] session=%s phase=preflight resumed=%s override=%s %s",
        session_id,
        resumed,
        bool(effective_override),
        _network_quality_summary(network_quality),
    )

    if (
        isinstance(network_quality, dict)
        and str(network_quality.get("grade") or "").lower() == "block"
        and effective_override is None
    ):
        gate = _build_network_quality_gate(network_quality)
        session = orch.raise_decision(session_id, gate)
        logger.warning(
            "[NETWORK_GATE] session=%s decision_required decision_id=%s kind=%s %s",
            session_id,
            gate.decision_id,
            gate.kind,
            _network_quality_summary(network_quality),
        )
        return {
            "success": True,
            "session_id": session_id,
            "status": session.status.value,
            "workflow": session.workflow,
            "decision_required": True,
            "decision": gate.to_dict(),
            **network_snapshot,
        }

    orch.emit_progress(session_id, "Starting GDS2 diagnostics...")
    backend = _get_backend()
    start_result = backend.start() or {}
    state = backend.get_state()
    modules = start_result.get("modules") if isinstance(start_result, dict) else None
    if not isinstance(modules, list) or not modules:
        modules = backend.get_modules()
    result = {
        "modules": modules,
        "vin": (
            start_result.get("vin")
            if isinstance(start_result, dict) and start_result.get("vin")
            else state.extra.get("vin")
        ),
        "device": (
            start_result.get("device")
            if isinstance(start_result, dict) and start_result.get("device")
            else state.extra.get("device")
        ),
    }
    _set_session_selection(session, module="", data_category="")
    _clear_navigation_binding(session)
    _clear_ai_binding(session)
    _set_live_data_active(session, False)
    orch.emit_progress(session_id, "GDS2 diagnostics started", result)
    logger.info(
        "[NETWORK_GATE] session=%s decision=allow resumed=%s override=%s modules=%s device=%s %s",
        session_id,
        resumed,
        bool(effective_override),
        len(result["modules"]),
        result.get("device") or "-",
        _network_quality_summary(network_quality),
    )

    payload = {
        "success": True,
        "session_id": session_id,
        "status": session.status.value,
        "workflow": session.workflow,
        "result": result,
        **network_snapshot,
    }
    if resumed:
        payload["resumed"] = True
        payload["resume_action"] = "start_diagnostics"
    return payload


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
        orch = get_orchestrator()
        session = orch.start_session(ctx)
        logger.info(
            "SESSION %s started brand=%s workflow=%s status=%s",
            session.session_id,
            brand,
            session.workflow,
            session.status.value,
        )

        payload = {
            "success": True,
            "session_id": session.session_id,
            "status": session.status.value,
            "workflow": session.workflow,
        }

        # Include the pending decision so the client can render it immediately
        if session.pending_decision is not None:
            payload["decision"] = session.pending_decision.to_dict()

        return jsonify(payload)

    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

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
        payload = _run_start_diagnostics(session_id)
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
        # Validate action name
        try:
            action = GDS2Action(action_name)
        except ValueError:
            valid = [a.value for a in GDS2Action]
            return jsonify({
                "success": False,
                "error": f"Unknown action '{action_name}'. Valid: {valid}",
            }), 400

        orch = get_orchestrator()
        session = orch.get_session(session_id)
        if session.status != SessionStatus.RUNNING:
            return jsonify({
                "success": False,
                "error": f"Session not running (status={session.status.value})",
            }), 409

        executor = get_executor()
        adapter = get_adapter()
        assert adapter is not None, "Adapter not initialized"

        step = ActionStep(
            action=action,
            args=data.get("args") or {},
            timeout_sec=float(data.get("timeout_sec", 30.0)),
        )

        # Get current UI state from real GDS2
        ui_state = adapter.get_current_ui_state()

        # Guarded fallback for transient UNKNOWN during Device Explorer popup.
        # This commonly occurs right after start_diagnostics when native dialog
        # is visible but Java page detection hasn't stabilized yet.
        if action == GDS2Action.CONNECT_DEVICE and ui_state.current_page == "unknown":
            orch.emit_progress(
                session_id,
                "Current page unknown; retrying detection before connect_device...",
            )
            ui_state = adapter.get_current_ui_state()
            if ui_state.current_page == "unknown":
                try:
                    from src.native import DeviceExplorerController

                    if DeviceExplorerController().is_visible():
                        ui_state.current_page = "device_explorer"
                        orch.emit_progress(
                            session_id,
                            "Device Explorer detected via native check; continuing connect_device.",
                        )
                except Exception:
                    # Leave UNKNOWN as-is; policy guard will block unsafe action.
                    pass

        orch.emit_progress(session_id, f"Executing {action_name}...")
        logger.info("SESSION %s action=%s start", session_id, action_name)

        # Handle BranchDecisionRequiredError for select_module/select_data_category
        try:
            exec_result = executor.execute_step(step, ui_state)
        except BranchDecisionRequiredError as exc:
            resume_action = _resume_action_for_domain(
                exc.decision.domain.value,
                action_name,
            )
            gate = _build_branch_gate(
                domain=exc.decision.domain.value,
                target=exc.decision.target,
                choices=exc.choices,
                reason=exc.decision.reason,
                resume_action=resume_action,
            )
            session = orch.raise_decision(session_id, gate)
            logger.info(
                "SESSION %s action=%s awaiting decision=%s domain=%s options=%s",
                session_id,
                action_name,
                gate.decision_id,
                exc.decision.domain.value,
                len(exc.choices),
            )
            return jsonify({
                "success": True,
                "session_id": session.session_id,
                "status": session.status.value,
                "decision_required": True,
                "decision": gate.to_dict(),
            })

        if not exec_result.success:
            orch.emit_progress(session_id, f"{action_name} failed: {exec_result.error}")
            logger.warning(
                "SESSION %s action=%s failed error=%s",
                session_id,
                action_name,
                exec_result.error,
            )
            return jsonify({
                "success": False,
                "session_id": session_id,
                "action": action_name,
                "error": exec_result.error,
                "attempts": exec_result.attempts,
                "elapsed_time": exec_result.elapsed_time,
            }), 500

        orch.emit_progress(session_id, f"{action_name} completed")
        logger.info(
            "SESSION %s action=%s completed attempts=%s elapsed=%.1fs",
            session_id,
            action_name,
            exec_result.attempts,
            exec_result.elapsed_time,
        )
        return jsonify({
            "success": True,
            "session_id": session_id,
            "action": action_name,
            "result": exec_result.metadata,
            "attempts": exec_result.attempts,
            "elapsed_time": exec_result.elapsed_time,
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

    orch = get_orchestrator()
    event_queue = orch.get_event_queue(session_id)
    if event_queue is None:
        return jsonify({"success": False, "error": f"Session {session_id} not found"}), 404

    def generate():
        yield f"event: connected\ndata: {json.dumps({'session_id': session_id})}\n\n"
        last_network_signature: tuple[Any, ...] | None = None
        try:
            last_network_signature = _network_signature(_get_session_network_snapshot(session_id))
        except Exception:
            last_network_signature = None

        while True:
            try:
                # Tick timeout fallback for pending decisions before waiting.
                try:
                    orch.check_decision_timeout(session_id)
                except Exception:
                    pass

                message = event_queue.get(timeout=1)

                # A pending decision may have timed out before this queued
                # decision_required event is consumed. In that case the session
                # has already advanced out of AWAITING_DECISION and the stale
                # decision_required should not be shown to the client.
                if message.startswith("event: decision_required\n"):
                    try:
                        session = orch.get_session(session_id)
                    except KeyError:
                        break
                    if session.status != SessionStatus.AWAITING_DECISION:
                        continue

                yield message

                # Terminal events — close the stream
                if message.startswith("event: done\n"):
                    break
                if message.startswith("event: error\n"):
                    # error is followed by done in fail_session, but
                    # standalone error events do not close the stream
                    pass

            except queue.Empty:
                try:
                    network_snapshot = _get_session_network_snapshot(session_id)
                    signature = _network_signature(network_snapshot)
                    if last_network_signature is None:
                        last_network_signature = signature
                    elif signature != last_network_signature:
                        last_network_signature = signature
                        logger.info(
                            "[NETWORK_GATE] session=%s sse=network_quality_changed override=%s %s",
                            session_id,
                            bool(network_snapshot.get("network_override")),
                            _network_quality_summary(network_snapshot.get("network_quality")),
                        )
                        yield (
                            "event: network_quality_changed\n"
                            f"data: {json.dumps({'session_id': session_id, **network_snapshot})}\n\n"
                        )
                        continue
                except KeyError:
                    break
                except Exception:
                    pass
                yield ": keepalive\n\n"

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
        orch = get_orchestrator()
        before = orch.get_session(session_id)
        pending_gate = before.pending_decision
        session = orch.submit_decision(session_id, decision_id, option_id)
        logger.info("SESSION %s decision=%s option=%s", session_id, decision_id, option_id)

        if pending_gate is not None and pending_gate.kind == "network_quality":
            if option_id == "continue_anyway":
                snapshot = _get_session_network_snapshot(session_id)
                override = _set_network_override(session_id, snapshot.get("connection_epoch"))
                logger.warning(
                    "[NETWORK_GATE] session=%s decision=%s option=%s action=resume_start %s",
                    session_id,
                    decision_id,
                    option_id,
                    _network_quality_summary(snapshot.get("network_quality")),
                )
                payload = _run_start_diagnostics(session_id, resumed=True)
                payload["network_override"] = override
                return jsonify(payload)

            logger.info(
                "[NETWORK_GATE] session=%s decision=%s option=%s action=cancel_start",
                session_id,
                decision_id,
                option_id,
            )
            _clear_network_override(session_id, reason="user_cancelled")
            return jsonify({
                "success": True,
                "session_id": session.session_id,
                "status": session.status.value,
                "workflow": session.workflow,
                "cancelled": True,
                **_get_session_network_snapshot(session_id),
            })

        if pending_gate is not None and pending_gate.kind == "branch":
            option_map = pending_gate.context.get("option_map", {})
            selected_choice = option_map.get(option_id)
            if not selected_choice:
                raise ValueError(f"Invalid branch option_id: {option_id}")

            resume_action = str(pending_gate.context.get("resume_action") or "").strip()
            if not resume_action:
                raise ValueError("Missing resume_action in branch decision context")

            try:
                if _data_viewer_getter is not None:
                    viewer = _get_data_viewer()
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
                    backend = _get_backend()
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
            except BranchDecisionRequiredError as exc:
                nested_resume_action = _resume_action_for_domain(
                    exc.decision.domain.value,
                    resume_action,
                )
                nested_gate = _build_branch_gate(
                    domain=exc.decision.domain.value,
                    target=exc.decision.target,
                    choices=exc.choices,
                    reason=exc.decision.reason,
                    resume_action=nested_resume_action,
                )
                # session is RUNNING after submit; raise next gate
                session = orch.raise_decision(session_id, nested_gate)
                return jsonify({
                    "success": True,
                    "session_id": session.session_id,
                    "status": session.status.value,
                    "workflow": session.workflow,
                    "decision_required": True,
                    "decision": nested_gate.to_dict(),
                })

            orch.emit_progress(
                session_id,
                f"Resumed via branch decision: {resume_action} -> {selected_choice}",
                {
                    "resume_action": resume_action,
                    "selected_choice": selected_choice,
                },
            )
            if resume_action in {"select_module", "select_sub_module"}:
                _set_session_selection(session, module=selected_choice)
            if resume_action in {"select_data_category", "select_sub_category"}:
                _set_session_selection(session, data_category=selected_choice)
            logger.info(
                "SESSION %s resumed action=%s choice=%s",
                session_id,
                resume_action,
                selected_choice,
            )

            return jsonify({
                "success": True,
                "session_id": session.session_id,
                "status": session.status.value,
                "workflow": session.workflow,
                "resumed": True,
                "resume_action": resume_action,
                "selected_choice": selected_choice,
                "result": resume_result,
            })

        return jsonify({
            "success": True,
            "session_id": session.session_id,
            "status": session.status.value,
            "workflow": session.workflow,
        })

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
            if _data_viewer_getter is not None:
                result = _get_data_viewer().select_module(module)
                exec_result = None
            else:
                executor = get_executor()
                adapter = get_adapter()
                assert adapter is not None, "Adapter not initialized"

                step = ActionStep(
                    action=GDS2Action.SELECT_MODULE,
                    args={"module_name": module},
                    timeout_sec=30.0,
                )

                ui_state = adapter.get_current_ui_state()
                exec_result = executor.execute_step(step, ui_state)
        except BranchDecisionRequiredError as exc:
            resume_action = _resume_action_for_domain(
                exc.decision.domain.value,
                "select_module",
            )
            gate = _build_branch_gate(
                domain=exc.decision.domain.value,
                target=exc.decision.target,
                choices=exc.choices,
                reason=exc.decision.reason,
                resume_action=resume_action,
            )
            session = orch.raise_decision(session_id, gate)
            logger.info(
                "SESSION %s select_module awaiting decision=%s options=%s",
                session_id,
                gate.decision_id,
                len(exc.choices),
            )
            return jsonify({
                "success": True,
                "session_id": session.session_id,
                "status": session.status.value,
                "workflow": session.workflow,
                "decision_required": True,
                "decision": gate.to_dict(),
            })

        if exec_result is not None and not exec_result.success:
            orch.emit_progress(session_id, f"select_module failed: {exec_result.error}")
            logger.warning("SESSION %s select_module failed error=%s", session_id, exec_result.error)
            return jsonify({
                "success": False,
                "session_id": session_id,
                "error": exec_result.error,
            }), 500

        _set_session_selection(session, module=module, data_category="")
        orch.emit_progress(session_id, f"Module selected: {module}")
        logger.info("SESSION %s module=%s selected", session_id, module)
        return jsonify({
            "success": True,
            "session_id": session_id,
            "result": result if exec_result is None else exec_result.metadata,
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
            if _data_viewer_getter is not None:
                result = _get_data_viewer().select_data_category(data_category)
                exec_result = None
            else:
                executor = get_executor()
                adapter = get_adapter()
                assert adapter is not None, "Adapter not initialized"

                step = ActionStep(
                    action=GDS2Action.SELECT_DATA_CATEGORY,
                    args={"category_name": data_category},
                    timeout_sec=30.0,
                )

                ui_state = adapter.get_current_ui_state()
                exec_result = executor.execute_step(step, ui_state)
        except BranchDecisionRequiredError as exc:
            resume_action = _resume_action_for_domain(
                exc.decision.domain.value,
                "select_data_category",
            )
            gate = _build_branch_gate(
                domain=exc.decision.domain.value,
                target=exc.decision.target,
                choices=exc.choices,
                reason=exc.decision.reason,
                resume_action=resume_action,
            )
            session = orch.raise_decision(session_id, gate)
            logger.info(
                "SESSION %s select_data_category awaiting decision=%s options=%s",
                session_id,
                gate.decision_id,
                len(exc.choices),
            )
            return jsonify({
                "success": True,
                "session_id": session.session_id,
                "status": session.status.value,
                "workflow": session.workflow,
                "decision_required": True,
                "decision": gate.to_dict(),
            })

        if exec_result is not None and not exec_result.success:
            orch.emit_progress(session_id, f"select_data_category failed: {exec_result.error}")
            logger.warning("SESSION %s select_data_category failed error=%s", session_id, exec_result.error)
            return jsonify({
                "success": False,
                "session_id": session_id,
                "error": exec_result.error,
            }), 500

        _set_session_selection(session, data_category=data_category)
        orch.emit_progress(session_id, f"Data category selected: {data_category}")
        logger.info("SESSION %s data_category=%s selected", session_id, data_category)
        return jsonify({
            "success": True,
            "session_id": session_id,
            "result": result if exec_result is None else exec_result.metadata,
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
        if session.status != SessionStatus.RUNNING:
            return jsonify({
                "success": False,
                "error": f"Session not running (status={session.status.value})",
            }), 409
        if session.workflow != "gds2":
            return jsonify({
                "success": False,
                "error": f"AI diagnosis is not supported for workflow={session.workflow}",
            }), 409

        vehicle_context = _resolve_session_vehicle_context(session, data)
        data_category = vehicle_context["data_category"]
        if not data_category:
            return jsonify({"success": False, "error": "data_category required"}), 400

        engine = _get_ai_engine()
        if engine.is_active:
            return jsonify({
                "success": False,
                "error": "AI diagnosis already in progress",
            }), 409

        ai_session_id = engine.start_session(
            vehicle_context,
            collection_guard=_make_ai_collection_guard(data_category),
        )
        session.active_ai_session_id = ai_session_id
        _set_session_selection(
            session,
            module=vehicle_context.get("module", ""),
            data_category=data_category,
        )
        orch.emit_progress(
            session_id,
            f"AI diagnosis started: {vehicle_context.get('module') or '-'} / {data_category}",
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
        ai_session_id = getattr(session, "active_ai_session_id", None)
        if not ai_session_id:
            return jsonify({
                "success": False,
                "error": f"No active AI session for {session_id}",
            }), 404

        event_queue = _get_ai_engine().get_event_queue(ai_session_id)
        if event_queue is None:
            session.active_ai_session_id = None
            return jsonify({
                "success": False,
                "error": f"AI session {ai_session_id} not found",
            }), 404

        logger.info(
            "SESSION %s ai_diagnose events bound ai_session=%s",
            session_id,
            ai_session_id,
        )

        def generate():
            yield f"event: connected\ndata: {json.dumps({'session_id': session_id})}\n\n"

            while True:
                try:
                    message = event_queue.get(timeout=60)
                    yield message
                    if message.startswith("event: done\n"):
                        _clear_ai_binding(session)
                        break
                    if message.startswith("event: error\n"):
                        _clear_ai_binding(session)
                        break
                except queue.Empty:
                    yield ": keepalive\n\n"

        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    except KeyError as exc:
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
        if session.status != SessionStatus.RUNNING:
            return jsonify({
                "success": False,
                "error": f"Session not running (status={session.status.value})",
            }), 409
        if session.workflow != "gds2":
            return jsonify({
                "success": False,
                "error": f"AI diagnosis is not supported for workflow={session.workflow}",
            }), 409

        vehicle_context = _resolve_session_vehicle_context(session, data)
        engine = _get_ai_engine()
        if engine.is_active:
            return jsonify({
                "success": False,
                "error": "AI diagnosis already in progress",
            }), 409

        ai_session_id = engine.retry_with_cached(cached_payload_id, vehicle_context)
        session.active_ai_session_id = ai_session_id
        _set_session_selection(
            session,
            module=vehicle_context.get("module", ""),
            data_category=vehicle_context.get("data_category", ""),
        )
        orch.emit_progress(
            session_id,
            f"AI diagnosis retry started: {vehicle_context.get('module') or '-'} / {vehicle_context.get('data_category') or '-'}",
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
        if session.status != SessionStatus.RUNNING:
            return jsonify({
                "success": False,
                "error": f"Session not running (status={session.status.value})",
            }), 409
        if session.workflow != "gds2":
            return jsonify({
                "success": False,
                "error": f"DTC read is not supported for workflow={session.workflow}",
            }), 409

        context = _resolve_session_vehicle_context(session, data)
        backend = _get_backend()
        state = backend.get_state()
        current_page = backend.detect_current_page()

        module_name = context.get("module", "")
        data_category = context.get("data_category", "")

        if module_name and not getattr(state, "current_module", ""):
            backend.select_module(module_name)
            _set_session_selection(session, module=module_name)
            current_page = backend.detect_current_page()

        if data_category and current_page != GDS2Page.DATA_DISPLAY.value:
            backend.select_data_category(data_category)
            _set_session_selection(session, data_category=data_category)

        dtcs = backend.read_dtcs()
        page_context = backend.detect_current_page()
        orch.emit_progress(session_id, f"Read DTCs completed ({len(dtcs)} codes)")
        logger.info("SESSION %s dtcs read count=%s page=%s", session_id, len(dtcs), page_context)
        return jsonify({
            "success": True,
            "session_id": session_id,
            "result": {
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
            },
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
        if session.status != SessionStatus.RUNNING:
            return jsonify({
                "success": False,
                "error": f"Session not running (status={session.status.value})",
            }), 409
        if session.workflow != "gds2":
            return jsonify({
                "success": False,
                "error": f"Live data is not supported for workflow={session.workflow}",
            }), 409

        context = _resolve_session_vehicle_context(session, data)
        data_category = context.get("data_category", "")
        if not data_category:
            return jsonify({"success": False, "error": "data_category required"}), 400

        import diagnostics_api

        payload = diagnostics_api.start_live_data_stream(data_category, interval_ms)
        _set_session_selection(
            session,
            module=context.get("module", ""),
            data_category=data_category,
        )
        _set_live_data_active(session, True)
        orch.emit_progress(session_id, f"Live data started: {data_category}")
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
        if not getattr(session, "live_data_active", False):
            return jsonify({
                "success": False,
                "error": f"No active live data stream for {session_id}",
            }), 404
    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404

    def generate():
        client_queue = queue.Queue(maxsize=200)

        with agent_lock:
            agent_clients.append(client_queue)

        try:
            yield f"event: connected\ndata: {json.dumps({'session_id': session_id, 'message': 'Connected to stream'})}\n\n"

            while True:
                try:
                    message = client_queue.get(timeout=30)
                    yield message
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            with agent_lock:
                if client_queue in agent_clients:
                    agent_clients.remove(client_queue)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
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

        import diagnostics_api

        payload = diagnostics_api.stop_live_data_stream()
        _set_live_data_active(session, False)
        orch.emit_progress(session_id, "Live data stopped")
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
        if session.status != SessionStatus.RUNNING:
            return jsonify({
                "success": False,
                "error": f"Session not running (status={session.status.value})",
            }), 409
        if session.workflow != "gds2":
            return jsonify({
                "success": False,
                "error": f"Navigation is not supported for workflow={session.workflow}",
            }), 409
        if session.active_navigation_session_id:
            return jsonify({
                "success": False,
                "error": "Navigation already in progress for this session",
            }), 409

        import navigate_api

        nav_session = navigate_api.start_navigation_session(goal)
        session.active_navigation_session_id = nav_session.session_id
        session.updated_at = time.time()
        orch.emit_progress(session_id, f"Navigation started: {goal}")
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
        nav_session_id = getattr(session, "active_navigation_session_id", None)
        if not nav_session_id:
            return jsonify({
                "success": False,
                "error": f"No active navigation session for {session_id}",
            }), 404

        import navigate_api

        nav_session = navigate_api.get_navigation_session(nav_session_id)
    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404
    except Exception as exc:
        logger.exception("session_navigate_events failed to bind")
        return jsonify({"success": False, "error": str(exc)}), 500

    def generate():
        yield f"event: connected\ndata: {json.dumps({'session_id': session_id})}\n\n"

        while True:
            try:
                event = nav_session.event_queue.get(timeout=1)
            except queue.Empty:
                yield ": keepalive\n\n"
                if nav_session.thread and not nav_session.thread.is_alive():
                    while not nav_session.event_queue.empty():
                        try:
                            event = nav_session.event_queue.get_nowait()
                            event_type = event.get("type", "progress")
                            yield f"event: {event_type}\ndata: {json.dumps(event)}\n\n"
                        except queue.Empty:
                            break
                    nav_status = (
                        nav_session.status
                        if isinstance(nav_session.status, str)
                        else nav_session.status.value
                    )
                    if nav_status in ("completed", "failed", "aborted"):
                        _clear_navigation_binding(session)
                        yield f"event: done\ndata: {json.dumps({'type': 'done', 'status': nav_status, 'error': nav_session.error})}\n\n"
                        return
                continue

            event_type = event.get("type", "progress")
            if event_type == "progress":
                nav_session.current_page = event.get("page", nav_session.current_page)
            elif event_type == "decision_required":
                nav_session.status = navigate_api.NavSessionStatus.AWAITING_DECISION
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
                    _set_session_selection(
                        session,
                        module=module if module else None,
                        data_category=data_category if data_category else None,
                    )
                _clear_navigation_binding(session)
            elif event_type == "error":
                _clear_navigation_binding(session)

            yield f"event: {event_type}\ndata: {json.dumps(event)}\n\n"

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
        nav_session_id = getattr(session, "active_navigation_session_id", None)
        if not nav_session_id:
            return jsonify({
                "success": False,
                "error": f"No active navigation session for {session_id}",
            }), 404

        import navigate_api

        payload = navigate_api.submit_navigation_decision(
            nav_session_id,
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
        nav_session_id = getattr(session, "active_navigation_session_id", None)
        if not nav_session_id:
            return jsonify({
                "success": False,
                "error": f"No active navigation session for {session_id}",
            }), 404

        import navigate_api

        payload = navigate_api.abort_navigation_session(nav_session_id)
        _clear_navigation_binding(session)
        return jsonify({
            "success": True,
            "session_id": session_id,
            "navigation_session_id": nav_session_id,
            "status": payload["status"],
        })

    except KeyError as exc:
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
        nav_session_id = getattr(session, "active_navigation_session_id", None)
        if not nav_session_id:
            return jsonify({
                "success": False,
                "error": f"No active navigation session for {session_id}",
            }), 404

        import navigate_api

        nav_session = navigate_api.get_navigation_session(nav_session_id)
        payload: dict[str, Any] = {
            "success": True,
            "session_id": session_id,
            "navigation_session_id": nav_session_id,
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
        return jsonify(payload)

    except KeyError as exc:
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
        orch = get_orchestrator()
        reason = (data.get("reason") or "").strip()
        session = orch.get_session(session_id)
        _abort_active_navigation(session)
        _abort_active_live_data(session)
        _abort_active_ai(session)
        session = orch.abort_session(session_id, reason)
        logger.info("SESSION %s aborted reason=%s", session_id, reason or "user")
        return jsonify({
            "success": True,
            "session_id": session.session_id,
            "status": session.status.value,
        })

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
        orch = get_orchestrator()
        session = orch.get_session(session_id)
        return jsonify({
            "success": True,
            **session.to_dict(),
            **_get_session_network_snapshot(session_id),
        })

    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404

    except Exception as exc:
        logger.exception("session_status failed")
        return jsonify({"success": False, "error": str(exc)}), 500
