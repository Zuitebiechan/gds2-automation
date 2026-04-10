"""Decision and branch-gate helpers for session APIs."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable

from diagnostic_platform.branch_planning import BranchDecisionRequiredError
from diagnostic_platform.session_models import DecisionGate, DecisionOption

from .session_actions import resume_branch_selection
from .session_preflight import (
    clear_network_override,
    get_session_network_snapshot,
    network_quality_summary,
    run_start_diagnostics,
    set_network_override,
)
from .worker_runtime import WorkerRuntime

logger = logging.getLogger(__name__)


def _session_success_payload(session: Any, **extra: Any) -> dict[str, Any]:
    """Build one common successful session API payload."""
    payload = {
        "success": True,
        "session_id": session.session_id,
        "status": session.status.value,
        "backend_name": session.backend_name,
        "capabilities": list(getattr(session, "capabilities", []) or []),
        "workflow": session.backend_name,
    }
    payload.update(extra)
    return payload


def build_branch_gate(
    *,
    domain: str,
    target: str,
    choices: list[str],
    reason: str,
    resume_action: str,
) -> DecisionGate:
    """Build a deterministic branch-resolution gate."""
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


def resume_action_for_domain(domain: str, default_action: str) -> str:
    """Map a branch domain to the action that should resume after resolution."""
    mapping = {
        "sub_module": "select_sub_module",
        "sub_category": "select_sub_category",
    }
    return mapping.get(domain, default_action)


def raise_branch_decision(
    *,
    orchestrator: Any,
    session_id: str,
    exc: BranchDecisionRequiredError,
    default_action: str,
) -> dict[str, Any]:
    """Raise a branch decision gate and return the route payload."""
    resume_action = resume_action_for_domain(
        exc.decision.domain.value,
        default_action,
    )
    gate = build_branch_gate(
        domain=exc.decision.domain.value,
        target=exc.decision.target,
        choices=exc.choices,
        reason=exc.decision.reason,
        resume_action=resume_action,
    )
    session = orchestrator.raise_decision(session_id, gate)
    return _session_success_payload(
        session,
        decision_required=True,
        decision=gate.to_dict(),
    )


def _resume_network_quality_start(
    runtime: WorkerRuntime,
    *,
    orchestrator: Any,
    backend: Any,
    session_id: str,
    decision_id: str,
    option_id: str,
) -> dict[str, Any]:
    snapshot = get_session_network_snapshot(
        orchestrator=orchestrator,
        backend=backend,
        session_id=session_id,
    )
    override = set_network_override(
        orchestrator=orchestrator,
        session_id=session_id,
        connection_epoch=snapshot.get("connection_epoch"),
    )
    logger.warning(
        "[NETWORK_GATE] session=%s decision=%s option=%s action=resume_start %s",
        session_id,
        decision_id,
        option_id,
        network_quality_summary(snapshot.get("network_quality")),
    )
    payload = run_start_diagnostics(
        runtime,
        orchestrator=orchestrator,
        backend=backend,
        session_id=session_id,
        resumed=True,
    )
    payload["network_override"] = override
    return payload


def _cancel_network_quality_start(
    *,
    orchestrator: Any,
    backend: Any,
    session: Any,
    session_id: str,
    decision_id: str,
    option_id: str,
) -> dict[str, Any]:
    logger.info(
        "[NETWORK_GATE] session=%s decision=%s option=%s action=cancel_start",
        session_id,
        decision_id,
        option_id,
    )
    clear_network_override(
        orchestrator=orchestrator,
        session_id=session_id,
        reason="user_cancelled",
    )
    return _session_success_payload(
        session,
        cancelled=True,
        **get_session_network_snapshot(
            orchestrator=orchestrator,
            backend=backend,
            session_id=session_id,
        ),
    )


def _handle_network_quality_decision(
    runtime: WorkerRuntime,
    *,
    orchestrator: Any,
    backend: Any,
    session: Any,
    session_id: str,
    decision_id: str,
    option_id: str,
) -> dict[str, Any]:
    if option_id == "continue_anyway":
        return _resume_network_quality_start(
            runtime,
            orchestrator=orchestrator,
            backend=backend,
            session_id=session_id,
            decision_id=decision_id,
            option_id=option_id,
        )
    return _cancel_network_quality_start(
        orchestrator=orchestrator,
        backend=backend,
        session=session,
        session_id=session_id,
        decision_id=decision_id,
        option_id=option_id,
    )


def _resume_branch_decision(
    runtime: WorkerRuntime,
    *,
    orchestrator: Any,
    session: Any,
    pending_gate: Any,
    session_id: str,
    option_id: str,
    get_data_viewer: Callable[[], Any],
    get_backend: Callable[[], Any],
) -> dict[str, Any]:
    try:
        resume_payload = resume_branch_selection(
            runtime,
            session,
            pending_gate=pending_gate,
            option_id=option_id,
            get_data_viewer=get_data_viewer,
            get_backend=get_backend,
            emit_progress=lambda message, details=None: orchestrator.emit_progress(
                session_id,
                message,
                details,
            ),
        )
    except BranchDecisionRequiredError as exc:
        resume_action = str(pending_gate.context.get("resume_action") or "").strip()
        return raise_branch_decision(
            orchestrator=orchestrator,
            session_id=session_id,
            exc=exc,
            default_action=resume_action,
        )

    logger.info(
        "SESSION %s resumed action=%s choice=%s",
        session_id,
        resume_payload["resume_action"],
        resume_payload["selected_choice"],
    )
    return _session_success_payload(
        session,
        resumed=True,
        resume_action=resume_payload["resume_action"],
        selected_choice=resume_payload["selected_choice"],
        result=resume_payload["result"],
    )


def submit_session_decision(
    runtime: WorkerRuntime,
    *,
    orchestrator: Any,
    backend: Any,
    session_id: str,
    decision_id: str,
    option_id: str,
    get_data_viewer: Callable[[], Any],
    get_backend: Callable[[], Any],
) -> dict[str, Any]:
    """Apply a pending session decision and return the public API payload."""
    before = orchestrator.get_session(session_id)
    pending_gate = before.pending_decision
    session = orchestrator.submit_decision(session_id, decision_id, option_id)
    logger.info("SESSION %s decision=%s option=%s", session_id, decision_id, option_id)

    if pending_gate is not None and pending_gate.kind == "network_quality":
        return _handle_network_quality_decision(
            runtime,
            orchestrator=orchestrator,
            backend=backend,
            session=session,
            session_id=session_id,
            decision_id=decision_id,
            option_id=option_id,
        )

    if pending_gate is not None and pending_gate.kind == "branch":
        return _resume_branch_decision(
            runtime,
            orchestrator=orchestrator,
            session=session,
            pending_gate=pending_gate,
            session_id=session_id,
            option_id=option_id,
            get_data_viewer=get_data_viewer,
            get_backend=get_backend,
        )

    return _session_success_payload(session)
