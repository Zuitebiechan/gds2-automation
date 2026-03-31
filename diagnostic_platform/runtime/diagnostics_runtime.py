"""Diagnostics runtime helpers shared by diagnostics and session APIs."""

from __future__ import annotations

import logging
from typing import Any

from src.navigation import GDS2Page
from src.streaming import AgentDataCollector

from .worker_runtime import WorkerRuntime

logger = logging.getLogger(__name__)


def make_data_display_guard(
    backend: Any,
    data_category: str,
    *,
    mode: str,
    check_interval: float = 5.0,
):
    """Build a shared Data Display guard for live and AI collectors."""
    if mode not in {"stream", "ai_collect"}:
        raise ValueError(f"Unsupported guard mode: {mode}")

    import time as _time

    last_check_ts: list[float] = [0.0]
    last_result: list[dict[str, Any] | None] = [None]

    def guard() -> dict[str, Any] | None:
        now = _time.time()
        if now - last_check_ts[0] < check_interval:
            return last_result[0]

        last_check_ts[0] = now
        page = backend.detect_current_page()
        if page == GDS2Page.DATA_DISPLAY.value:
            last_result[0] = None
            return None

        last_check_ts[0] = 0.0

        if page == GDS2Page.LOADING.value:
            return {
                "ok": True,
                "mode": mode,
                "message": "Waiting for GDS2 loading page to finish...",
            }

        if page == GDS2Page.J2534_DISCONNECT.value:
            recovery = backend._get_workflow().controller.recover_data_display_connection(
                data_category=data_category,
                allow_backtrack=True,
            )
            if recovery.success and recovery.page == GDS2Page.DATA_DISPLAY:
                recovery_method = (recovery.context or {}).get("recovery_method", "unknown")
                if mode == "ai_collect" and recovery_method == "backtrack":
                    message = "Recovered Data Display after reconnect; restarting AI collection window."
                elif mode == "ai_collect":
                    message = "Recovered temporary J2534 disconnect and returned to Data Display."
                else:
                    message = "Recovered Data Display after J2534 disconnect."
                return {
                    "ok": True,
                    "mode": mode,
                    "recovered": True,
                    "recovery_method": recovery_method,
                    "restart_collection": mode == "ai_collect" and recovery_method == "backtrack",
                    "message": message,
                }

            if mode == "ai_collect":
                return {
                    "ok": False,
                    "mode": mode,
                    "error": (
                        "Lost communication with J2534 during AI collection and could not "
                        "restore Data Display in-place. Please reconnect and restart AI Diagnostics."
                    ),
                }

            return {
                "ok": False,
                "mode": mode,
                "error": (
                    "Lost communication with J2534 and could not restore Data Display. "
                    "Please reconnect and restart live monitoring."
                ),
            }

        return {
            "ok": False,
            "mode": mode,
            "error": (
                f"Data Display guard detected page drift to {page}. "
                "Please return to Data Display and retry."
            ),
        }

    return guard


def start_live_data_stream(
    runtime: WorkerRuntime,
    *,
    backend: Any,
    data_category: str,
    interval_ms: int = 100,
    stream_scope: str | None = None,
) -> dict[str, object]:
    """Start the shared live-data collector and return the public payload."""
    from diagnostic_platform.sse import (
        DEFAULT_AGENT_STREAM_SCOPE,
        broadcast_agent_event,
        make_scoped_agent_event_callbacks,
    )

    if stream_scope is None:
        stream_scope = DEFAULT_AGENT_STREAM_SCOPE

    if not data_category:
        raise ValueError("Data category required")

    if runtime.diag_collector and runtime.diag_collector.is_running:
        if runtime.active_live_data_scope == stream_scope:
            return {
                "success": True,
                "message": "Streaming already running",
            }

        logger.info(
            "DIAG live replacing existing stream scope=%s -> %s",
            runtime.active_live_data_scope or "-",
            stream_scope,
        )
        try:
            runtime.diag_collector.stop()
        except Exception:
            pass
        runtime.diag_collector = None
        runtime.active_live_data_scope = None

    if runtime.diag_collector is not None:
        try:
            runtime.diag_collector.stop()
        except Exception:
            pass
        runtime.diag_collector = None

    backend.select_data_category(data_category)
    page_guard = make_data_display_guard(backend, data_category, mode="stream")
    callbacks = make_scoped_agent_event_callbacks(stream_scope)

    def on_guard_event(event: dict[str, object]) -> None:
        message = event.get("message")
        if message:
            broadcast_agent_event(stream_scope, "guard", {"message": message})

    runtime.diag_collector = AgentDataCollector(
        on_snapshot=callbacks["on_snapshot"],
        on_param_change=callbacks["on_param_change"],
        on_dtc_change=callbacks["on_dtc_change"],
        on_error=callbacks["on_error"],
        page_guard=page_guard,
        on_guard_event=on_guard_event,
        interval_ms=interval_ms,
    )
    runtime.diag_collector.start()
    runtime.active_live_data_scope = stream_scope

    logger.info(
        "DIAG live start category=%s interval=%sms scope=%s",
        data_category,
        interval_ms,
        stream_scope,
    )
    return {
        "success": True,
        "message": "Live data streaming started",
        "interval_ms": interval_ms,
    }


def stop_live_data_stream(runtime: WorkerRuntime, *, backend: Any) -> dict[str, object]:
    """Stop the shared live-data collector and return the public payload."""
    active_scope = runtime.active_live_data_scope

    if runtime.diag_collector:
        runtime.diag_collector.stop()
        runtime.diag_collector = None
    runtime.active_live_data_scope = None

    current_page = backend.detect_current_page()
    if current_page == GDS2Page.DATA_DISPLAY.value:
        backend.go_back()

    logger.info("DIAG live stopped scope=%s", active_scope or "-")
    return {"success": True, "message": "Live data stopped"}


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
    }


def read_diagnostic_dtcs(
    *,
    backend: Any,
    module_name: str,
    data_category: str,
) -> dict[str, Any]:
    """Read DTCs for the direct diagnostics API."""
    current_page = backend.detect_current_page()
    state = backend.get_state()

    if module_name and not state.current_module:
        backend.select_module(module_name)
        current_page = backend.detect_current_page()

    if data_category and current_page != GDS2Page.DATA_DISPLAY.value:
        backend.select_data_category(data_category)

    dtcs = backend.read_dtcs()
    page_context = backend.detect_current_page()
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

    current_page = backend.detect_current_page()
    if current_page != GDS2Page.DATA_DISPLAY.value:
        logger.debug("AI-DIAG navigating to data_display from %s", current_page)
        backend.select_data_category(data_category)
    else:
        logger.debug("AI-DIAG request already on data_display")

    page_guard = make_data_display_guard(backend, data_category, mode="ai_collect")
    return engine.start_session(vehicle_context, collection_guard=page_guard)


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
