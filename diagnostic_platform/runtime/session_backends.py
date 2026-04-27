"""Backend resolution and capability checks for business sessions."""

from __future__ import annotations

from typing import Any

from diagnostic_platform.backend_registry import get_backend_registry
from diagnostic_platform.contracts import (
    ActiveBackendBundle,
    BackendCapability,
    BackendDescriptor,
    UnsupportedCapabilityError,
)
from diagnostic_platform.session_models import SessionStatus

from .session_errors import SessionNotRunningError
from .worker_runtime import WorkerRuntime


def capability_value(capability: BackendCapability | str) -> str:
    return capability.value if isinstance(capability, BackendCapability) else str(capability)


def session_capabilities(session: Any, descriptor: BackendDescriptor | None = None) -> list[str]:
    capabilities = list(getattr(session, "capabilities", []) or [])
    if capabilities:
        return capabilities
    if descriptor is not None:
        return descriptor.capability_values()
    return []


def ensure_session_capability(
    session: Any,
    capability: BackendCapability | str,
    *,
    descriptor: BackendDescriptor | None = None,
) -> None:
    if session.status != SessionStatus.RUNNING:
        raise SessionNotRunningError(session.status)

    capability_name = capability_value(capability)
    capabilities = session_capabilities(session, descriptor)
    if capability_name not in capabilities:
        raise UnsupportedCapabilityError(
            capability if isinstance(capability, BackendCapability) else BackendCapability(capability_name),
            getattr(session, "backend_name", None) or "manual",
        )


def summarize_backend_state(backend: Any) -> dict[str, Any] | None:
    if backend is None or not hasattr(backend, "get_state"):
        return None
    try:
        state = backend.get_state()
    except Exception:
        return None
    return {
        "current_page": getattr(state, "current_page", None),
        "is_connected": getattr(state, "is_connected", None),
        "current_module": getattr(state, "current_module", None),
        "current_data_category": getattr(state, "current_data_category", None),
        "vehicle_dtc_status": (
            dict(getattr(state, "extra", {}).get("vehicle_dtc_status") or {})
            if isinstance(getattr(state, "extra", None), dict)
            else {}
        ),
    }


def resolve_backend_for_session(
    runtime: WorkerRuntime,
    session: Any,
    *,
    registry=None,
) -> ActiveBackendBundle:
    registry = registry or get_backend_registry()
    backend_name = str(getattr(session, "backend_name", "") or "").strip()
    if not backend_name or backend_name == "manual":
        raise UnsupportedCapabilityError(BackendCapability.CORE_SESSION, backend_name or "manual")

    descriptor = registry.get_descriptor(backend_name)
    return runtime.ensure_backend_bundle(
        session.session_id,
        descriptor=descriptor,
        backend_factory=lambda: registry.get_by_name(backend_name),
    )
