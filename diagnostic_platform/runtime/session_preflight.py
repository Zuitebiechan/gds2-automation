"""Session preflight and diagnostics-start helpers."""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from diagnostic_platform.contracts import BackendCapability
from diagnostic_platform.session_models import (
    DecisionGate,
    DecisionOption,
    SessionStatus,
)
from diagnostic_platform.safe_utils import mapping_or_empty as _mapping_or_empty

from .session_backends import ensure_session_capability
from .session_state import (
    clear_ai_binding,
    clear_navigation_binding,
    set_live_data_active,
    set_session_selection,
)
from .errors import OperationCancelledError
from .worker_runtime import WorkerRuntime

logger = logging.getLogger(__name__)


def _state_extra_mapping(state: Any) -> dict[str, Any]:
    """Return backend state.extra when it is a mapping, otherwise an empty mapping."""
    extra = getattr(state, "extra", {})
    return extra if isinstance(extra, dict) else {}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def network_quality_summary(snapshot: dict[str, Any] | None) -> str:
    if not isinstance(snapshot, dict):
        return "grade=unknown status=unknown epoch=None p95=n/a reason=none"
    metrics = _mapping_or_empty(snapshot.get("network_ms"))
    p95 = metrics.get("p95")
    try:
        p95_text = "n/a" if p95 is None else f"{float(p95):.1f}ms"
    except (TypeError, ValueError):
        p95_text = "n/a"
    return (
        f"grade={snapshot.get('grade')} status={snapshot.get('status')} "
        f"epoch={snapshot.get('connection_epoch')} p95={p95_text} "
        f"reason={snapshot.get('reason')} samples={snapshot.get('sample_count')} "
        f"fresh={snapshot.get('fresh')} connected={snapshot.get('connected')}"
    )


def get_effective_network_override(session: Any, connection_epoch: str | None) -> dict[str, Any] | None:
    override = getattr(session, "network_override", None)
    if not isinstance(override, dict):
        return None
    if not override.get("allowed"):
        return None
    if override.get("connection_epoch") != connection_epoch:
        return None
    return dict(override)


def set_network_override(*, orchestrator: Any, session_id: str, connection_epoch: str | None) -> dict[str, Any]:
    session = orchestrator.get_session(session_id)
    override = {
        "allowed": True,
        "confirmed_at": utc_now_iso(),
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


def clear_network_override(*, orchestrator: Any, session_id: str, reason: str = "cleared") -> None:
    session = orchestrator.get_session(session_id)
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


def get_session_network_snapshot(*, orchestrator: Any, backend: Any, session_id: str) -> dict[str, Any]:
    session = orchestrator.get_session(session_id)
    preflight = getattr(backend, "preflight", None)
    if not callable(preflight):
        return {
            "network_quality": None,
            "network_override": None,
            "connection_epoch": None,
        }

    snapshot = _mapping_or_empty(preflight())
    raw_network_quality = snapshot.get("network_quality")
    network_quality = raw_network_quality if isinstance(raw_network_quality, dict) else None
    connection_epoch = snapshot.get("connection_epoch")
    effective_override = get_effective_network_override(session, connection_epoch)
    raw_override = getattr(session, "network_override", None)
    if isinstance(raw_override, dict) and effective_override is None and raw_override.get("allowed"):
        logger.info(
            "[NETWORK_GATE] session=%s override_invalidated old_epoch=%s new_epoch=%s",
            session_id,
            raw_override.get("connection_epoch"),
            connection_epoch,
        )
        clear_network_override(
            orchestrator=orchestrator,
            session_id=session_id,
            reason="epoch_changed",
        )
    return {
        "network_quality": network_quality,
        "network_override": effective_override,
        "connection_epoch": connection_epoch,
    }


def build_network_quality_gate(network_quality: dict[str, Any] | None) -> DecisionGate:
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


def network_signature(payload: dict[str, Any]) -> tuple[Any, ...]:
    quality = _mapping_or_empty(payload.get("network_quality"))
    return (
        quality.get("grade"),
        quality.get("status"),
        quality.get("reason"),
        payload.get("connection_epoch"),
        payload.get("network_override", {}).get("connection_epoch")
        if isinstance(payload.get("network_override"), dict)
        else None,
    )


def reset_backend_startup_state(backend: Any, *, session_id: str, reason: str) -> None:
    resetter = getattr(backend, "reset_startup_state", None)
    if not callable(resetter):
        return
    try:
        resetter()
        logger.info("SESSION %s startup state reset after %s", session_id, reason)
    except Exception:
        logger.exception("SESSION %s startup reset failed after %s", session_id, reason)


def _session_response_payload(session: Any) -> dict[str, Any]:
    """Build one common session API payload envelope."""
    return {
        "success": True,
        "session_id": session.session_id,
        "status": session.status.value,
        "backend_name": session.backend_name,
        "capabilities": list(getattr(session, "capabilities", []) or []),
        "workflow": session.backend_name,
    }


def _call_backend_start(start_method: Any, *, operation: Any) -> dict[str, Any]:
    """Start one backend with cooperative-cancel support when accepted."""
    try:
        result = start_method(cancel_checker=operation.check_cancelled) or {}
    except TypeError:
        result = start_method() or {}
    return result if isinstance(result, dict) else {}


def _build_device_selection_result(start_payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return one device-selection payload when startup stops at device explorer."""
    devices = start_payload.get("devices")
    if not isinstance(devices, list) or not devices:
        return None
    return {
        "devices": devices,
        "at_device_explorer": bool(start_payload.get("at_device_explorer")),
        "device_connected": bool(start_payload.get("device_connected")),
    }


def _build_module_selection_result(
    backend: Any,
    *,
    operation: Any,
    start_payload: dict[str, Any],
    state_extra: dict[str, Any],
) -> dict[str, Any]:
    """Return one module-selection payload after backend startup completes."""
    modules = start_payload.get("modules")
    if not isinstance(modules, list) or not modules:
        modules = backend.get_modules()
    operation.check_cancelled()
    return {
        "modules": modules,
        "vin": start_payload.get("vin") or state_extra.get("vin"),
        "device": start_payload.get("device") or state_extra.get("device"),
    }


def _build_start_diagnostics_result(
    backend: Any,
    *,
    operation: Any,
    start_payload: dict[str, Any],
    state_extra: dict[str, Any],
) -> dict[str, Any]:
    """Build one normalized diagnostics-start result payload."""
    device_result = _build_device_selection_result(start_payload)
    if device_result is not None:
        return device_result
    return _build_module_selection_result(
        backend,
        operation=operation,
        start_payload=start_payload,
        state_extra=state_extra,
    )


def _reset_session_runtime_state(runtime: WorkerRuntime, session: Any) -> None:
    """Clear subordinate runtime bindings after backend startup."""
    set_session_selection(session, module="", data_category="")
    clear_navigation_binding(runtime, session)
    clear_ai_binding(runtime, session)
    set_live_data_active(runtime, session, False)


def _decision_required_start_payload(
    session: Any,
    *,
    gate: DecisionGate,
    network_snapshot: dict[str, Any],
) -> dict[str, Any]:
    payload = _session_response_payload(session)
    payload.update(
        {
            "decision_required": True,
            "decision": gate.to_dict(),
        }
    )
    payload.update(network_snapshot)
    return payload


def _started_diagnostics_payload(
    session: Any,
    *,
    result: dict[str, Any],
    network_snapshot: dict[str, Any],
    resumed: bool,
) -> dict[str, Any]:
    payload = _session_response_payload(session)
    payload["result"] = result
    payload.update(network_snapshot)
    if resumed:
        payload["resumed"] = True
        payload["resume_action"] = "start_diagnostics"
    return payload


def _read_backend_start_context(
    backend: Any,
    *,
    operation: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    start_method = getattr(backend, "start")
    start_payload = _call_backend_start(start_method, operation=operation)
    operation.check_cancelled()
    state = backend.get_state()
    state_extra = _state_extra_mapping(state)
    operation.check_cancelled()
    return start_payload, state_extra


def run_start_diagnostics(
    runtime: WorkerRuntime,
    *,
    orchestrator: Any,
    backend: Any,
    session_id: str,
    resumed: bool = False,
) -> dict[str, Any]:
    session = orchestrator.get_session(session_id)
    ensure_session_capability(session, BackendCapability.CORE_SESSION)

    network_snapshot = get_session_network_snapshot(
        orchestrator=orchestrator,
        backend=backend,
        session_id=session_id,
    )
    network_quality = network_snapshot["network_quality"]
    effective_override = network_snapshot["network_override"]
    logger.info(
        "[NETWORK_GATE] session=%s phase=preflight resumed=%s override=%s %s",
        session_id,
        resumed,
        bool(effective_override),
        network_quality_summary(network_quality),
    )

    if (
        isinstance(network_quality, dict)
        and str(network_quality.get("grade") or "").lower() == "block"
        and effective_override is None
    ):
        gate = build_network_quality_gate(network_quality)
        session = orchestrator.raise_decision(session_id, gate)
        logger.warning(
            "[NETWORK_GATE] session=%s decision_required decision_id=%s kind=%s %s",
            session_id,
            gate.decision_id,
            gate.kind,
            network_quality_summary(network_quality),
        )
        return _decision_required_start_payload(
            session,
            gate=gate,
            network_snapshot=network_snapshot,
        )

    operation = runtime.start_operation(session_id, "start_diagnostics")
    try:
        orchestrator.emit_progress(
            session_id,
            f"Starting {session.backend_name or 'diagnostic'} backend...",
        )
        start_payload, state_extra = _read_backend_start_context(
            backend,
            operation=operation,
        )
        result = _build_start_diagnostics_result(
            backend,
            operation=operation,
            start_payload=start_payload,
            state_extra=state_extra,
        )
        _reset_session_runtime_state(runtime, session)
        orchestrator.emit_progress(
            session_id,
            f"{session.backend_name or 'Diagnostic'} backend started",
            result,
        )
        logger.info(
            "[NETWORK_GATE] session=%s decision=allow resumed=%s override=%s modules=%s devices=%s device=%s %s",
            session_id,
            resumed,
            bool(effective_override),
            len(result.get("modules") or []),
            len(result.get("devices") or []),
            result.get("device") or "-",
            network_quality_summary(network_quality),
        )
        return _started_diagnostics_payload(
            session,
            result=result,
            network_snapshot=network_snapshot,
            resumed=resumed,
        )
    except OperationCancelledError:
        reset_backend_startup_state(backend, session_id=session_id, reason="cancel")
        logger.info("SESSION %s start_diagnostics cancelled", session_id)
        raise
    except Exception:
        reset_backend_startup_state(backend, session_id=session_id, reason="failure")
        raise
    finally:
        runtime.finish_operation(operation)
