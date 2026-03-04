"""Session orchestration API blueprint (Phase G3).

Provides endpoints for session lifecycle, SSE event streaming,
user decision submission, and abort.  Operates on the in-memory
SessionOrchestrator and does not touch existing diagnostics routes.
"""

# pyright: reportMissingImports=false

import json
import logging
import queue
import uuid

from flask import Blueprint, Response, jsonify, request

from src.agentic.planner import BranchDecisionRequiredError
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
_data_viewer_getter = None


def get_orchestrator() -> SessionOrchestrator:
    """Return the module-level orchestrator.

    Exposed so tests can swap / reset it.
    """
    return _orchestrator


def set_orchestrator(orch: SessionOrchestrator) -> None:
    """Replace the module-level orchestrator (for testing)."""
    global _orchestrator
    _orchestrator = orch


def _default_data_viewer_getter():
    from app import get_data_viewer

    return get_data_viewer()


def get_data_viewer():
    getter = _data_viewer_getter or _default_data_viewer_getter
    return getter()


def set_data_viewer_getter(getter) -> None:
    """Inject data-viewer getter for testing session step endpoints."""
    global _data_viewer_getter
    _data_viewer_getter = getter


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

        while True:
            try:
                # Tick timeout fallback for pending decisions before waiting.
                try:
                    orch.check_decision_timeout(session_id)
                except Exception:
                    pass

                message = event_queue.get(timeout=1)
                yield message

                # Terminal events — close the stream
                if message.startswith("event: done\n"):
                    break
                if message.startswith("event: error\n"):
                    # error is followed by done in fail_session, but
                    # standalone error events do not close the stream
                    pass

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

        if pending_gate is not None and pending_gate.kind == "branch":
            option_map = pending_gate.context.get("option_map", {})
            selected_choice = option_map.get(option_id)
            if not selected_choice:
                raise ValueError(f"Invalid branch option_id: {option_id}")

            resume_action = str(pending_gate.context.get("resume_action") or "").strip()
            if not resume_action:
                raise ValueError("Missing resume_action in branch decision context")
            viewer = get_data_viewer()

            try:
                if resume_action == "select_module":
                    resume_result = viewer.select_module(selected_choice)
                elif resume_action == "select_data_category":
                    resume_result = viewer.select_data_category(selected_choice)
                else:
                    raise ValueError(f"Unsupported resume action: {resume_action}")
            except BranchDecisionRequiredError as exc:
                nested_gate = _build_branch_gate(
                    domain=exc.decision.domain.value,
                    target=exc.decision.target,
                    choices=exc.choices,
                    reason=exc.decision.reason,
                    resume_action=resume_action,
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

        viewer = get_data_viewer()
        try:
            result = viewer.select_module(module)
        except BranchDecisionRequiredError as exc:
            gate = _build_branch_gate(
                domain=exc.decision.domain.value,
                target=exc.decision.target,
                choices=exc.choices,
                reason=exc.decision.reason,
                resume_action="select_module",
            )
            session = orch.raise_decision(session_id, gate)
            return jsonify({
                "success": True,
                "session_id": session.session_id,
                "status": session.status.value,
                "workflow": session.workflow,
                "decision_required": True,
                "decision": gate.to_dict(),
            })

        orch.emit_progress(session_id, f"Module selected: {module}")
        return jsonify({"success": True, "session_id": session_id, "result": result})

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

        viewer = get_data_viewer()
        try:
            result = viewer.select_data_category(data_category)
        except BranchDecisionRequiredError as exc:
            gate = _build_branch_gate(
                domain=exc.decision.domain.value,
                target=exc.decision.target,
                choices=exc.choices,
                reason=exc.decision.reason,
                resume_action="select_data_category",
            )
            session = orch.raise_decision(session_id, gate)
            return jsonify({
                "success": True,
                "session_id": session.session_id,
                "status": session.status.value,
                "workflow": session.workflow,
                "decision_required": True,
                "decision": gate.to_dict(),
            })

        orch.emit_progress(session_id, f"Data category selected: {data_category}")
        return jsonify({"success": True, "session_id": session_id, "result": result})

    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        logger.exception("session_select_data_category failed")
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
        session = orch.abort_session(session_id, reason)
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
        })

    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404

    except Exception as exc:
        logger.exception("session_status failed")
        return jsonify({"success": False, "error": str(exc)}), 500
