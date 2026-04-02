"""Shared runtime accessors for the session API layer."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from diagnostic_platform.backend_registry import get_backend_registry
from diagnostic_platform.runtime.worker_runtime import get_worker_runtime
from src.gds2_orchestration.session_orchestrator import SessionOrchestrator

logger = logging.getLogger(__name__)


def _runtime():
    return get_worker_runtime()


def get_orchestrator() -> SessionOrchestrator:
    """Return the shared worker-scoped orchestrator."""
    return _runtime().orchestrator


def set_orchestrator(orch: SessionOrchestrator) -> None:
    """Replace the shared worker-scoped orchestrator."""
    _runtime().set_orchestrator(orch)


def set_data_viewer_getter(getter: Callable[[], Any] | None) -> None:
    """Inject a lightweight viewer object for tests that bypass GDS2 startup."""
    _runtime().set_data_viewer_getter(getter)


def _get_bound_session():
    runtime = _runtime()
    binding = runtime.get_business_session_binding()
    if binding.session_id:
        return runtime.orchestrator.get_session(binding.session_id)

    session = runtime.orchestrator.get_active_session()
    if session is not None:
        runtime.bind_business_session(session.session_id)
    return session


def _get_session(session_id: str | None = None):
    if session_id:
        session = _runtime().orchestrator.get_session(session_id)
        _runtime().bind_business_session(session.session_id)
        return session
    return _get_bound_session()


def _resolve_active_backend_descriptor(
    session_id: str | None = None,
    *,
    required: bool = True,
):
    session = _get_session(session_id)
    if session is None:
        raise RuntimeError("No active session bound to the worker")

    backend_name = (getattr(session, "backend_name", None) or "").strip()
    if not backend_name or backend_name == "manual":
        if required:
            raise RuntimeError("Active session does not have a runnable backend")
        return session, None

    return session, get_backend_registry().get_descriptor(backend_name)


def get_data_viewer() -> Any:
    """Return the injected viewer when present, otherwise the backend guided runtime."""
    return _runtime().get_data_viewer(get_backend)


def get_backend(
    session_id: str | None = None,
    *,
    required: bool = True,
) -> Any | None:
    session, descriptor = _resolve_active_backend_descriptor(
        session_id,
        required=required,
    )
    if descriptor is None:
        return None
    bundle = _runtime().ensure_backend_bundle(
        session.session_id,
        descriptor=descriptor,
        backend_factory=lambda: get_backend_registry().get_by_name(descriptor.backend_name),
    )
    return bundle.backend


def get_ai_engine():
    from . import diagnostics as diagnostics_api

    return diagnostics_api._get_ai_engine()


def get_executor() -> Any:
    """Return the worker-scoped executor, lazily wired by the active backend."""
    if _runtime().get_adapter() is None:
        logger.debug("Session API executor wiring with backend action runtime")
    return _runtime().get_executor(lambda: get_backend().build_action_runtime())


def get_adapter() -> Any | None:
    """Return the current backend action adapter (available after get_executor())."""
    return _runtime().get_adapter()


def reset_executor() -> None:
    """Reset backend-owned executor/adapter state."""
    _runtime().reset_executor()
