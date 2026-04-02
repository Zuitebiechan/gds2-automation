"""Diagnostics runtime helpers shared by diagnostics and session APIs."""

from __future__ import annotations

import logging
from typing import Any

from diagnostic_platform.contracts import (
    BackendCapability,
    DiagnosticBackend,
    UnsupportedCapabilityError,
)

from .worker_runtime import WorkerRuntime

logger = logging.getLogger(__name__)


def _implements_backend_extension(backend: Any, method_name: str, base_method: Any) -> bool:
    backend_method = getattr(type(backend), method_name, None)
    if backend_method is None:
        return False
    return backend_method is not base_method


def start_live_data_stream(
    runtime: WorkerRuntime,
    *,
    backend: Any,
    data_category: str,
    interval_ms: int = 100,
    stream_scope: str | None = None,
) -> dict[str, object]:
    """Start live data through a backend-owned collector or generic backend session."""
    if _implements_backend_extension(
        backend,
        "start_live_data_session",
        DiagnosticBackend.start_live_data_session,
    ):
        return backend.start_live_data_session(
            data_category=data_category,
            interval_ms=interval_ms,
            stream_scope=stream_scope or "diagnostics",
        )

    if not data_category:
        raise ValueError("Data category required")

    backend.select_data_category(data_category)
    if _implements_backend_extension(
        backend,
        "start_live_data",
        DiagnosticBackend.start_live_data,
    ):
        stream = backend.start_live_data()
        return {
            "success": True,
            "message": "Live data streaming started",
            "interval_ms": interval_ms,
            "session_id": getattr(stream, "session_id", None),
        }

    raise UnsupportedCapabilityError(BackendCapability.LIVE_DATA, backend.name)


def stop_live_data_stream(runtime: WorkerRuntime, *, backend: Any) -> dict[str, object]:
    """Stop backend-owned or generic live-data collection."""
    if _implements_backend_extension(
        backend,
        "stop_live_data_session",
        DiagnosticBackend.stop_live_data_session,
    ):
        return backend.stop_live_data_session()

    if _implements_backend_extension(
        backend,
        "stop_live_data",
        DiagnosticBackend.stop_live_data,
    ):
        backend.stop_live_data()
        return {"success": True, "message": "Live data stopped"}

    raise UnsupportedCapabilityError(BackendCapability.LIVE_DATA, backend.name)


def build_diagnostics_start_payload(*, backend: Any) -> dict[str, Any]:
    """Start diagnostics and return the ready payload."""
    backend.start()
    state = backend.get_state()
    modules = backend.get_modules()
    return {
        "success": True,
        "modules": modules,
        "vin": state.extra.get("vin"),
        "device": state.extra.get("device"),
        "backend_name": getattr(backend, "name", None),
        "capabilities": backend.descriptor.capability_values(),
    }


def read_diagnostic_dtcs(
    *,
    backend: Any,
    module_name: str,
    data_category: str,
) -> dict[str, Any]:
    """Read DTCs for the direct diagnostics API."""
    state = backend.get_state()
    current_page = getattr(state, "current_page", "")

    if module_name and not state.current_module:
        backend.select_module(module_name)
        state = backend.get_state()
        current_page = getattr(state, "current_page", current_page)

    if data_category and not getattr(state, "current_data_category", ""):
        backend.select_data_category(data_category)
        state = backend.get_state()
        current_page = getattr(state, "current_page", current_page)

    dtcs = backend.read_dtcs()
    page_context = getattr(backend.get_state(), "current_page", current_page)
    return {
        "success": True,
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
    }


def select_diagnostic_module(*, backend: Any, module: str) -> dict[str, Any]:
    """Select a module and return the category list payload."""
    backend.select_module(module)
    return {
        "success": True,
        "data_categories": backend.get_data_categories(),
    }


def start_public_ai_diagnosis(
    *,
    backend: Any,
    engine: Any,
    vehicle_context: dict[str, Any],
    data_category: str,
) -> str:
    """Start direct AI diagnosis from the diagnostics API."""
    if engine.is_active:
        raise RuntimeError("AI diagnosis already in progress")

    diagnostic_payload = backend.collect_ai_payload(
        vehicle_context=vehicle_context,
        data_category=data_category,
        collection_seconds=getattr(engine, "collection_seconds", 30),
    )
    if not hasattr(engine, "start_session_from_payload"):
        raise RuntimeError("AI engine does not support payload-based sessions")
    return engine.start_session_from_payload(vehicle_context, diagnostic_payload)


def retry_public_ai_diagnosis(
    *,
    engine: Any,
    cached_payload_id: str,
    vehicle_context: dict[str, Any],
) -> str:
    """Retry public AI diagnosis using a cached payload."""
    if engine.is_active:
        raise RuntimeError("AI diagnosis already in progress")
    return engine.retry_with_cached(cached_payload_id, vehicle_context)
