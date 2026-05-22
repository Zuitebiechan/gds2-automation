from __future__ import annotations

import types

import diagnostic_platform.session_orchestrator as platform_session_orchestrator_module
from diagnostic_platform.observability import read_active_session_snapshot
from diagnostic_platform.proxy_local_live_data import (
    read_proxy_local_live_data_session_state,
)
from diagnostic_platform.runtime.session_lifecycle import start_business_session
from diagnostic_platform.runtime.session_preflight import run_start_diagnostics
from diagnostic_platform.runtime.session_state import (
    bind_ai_session,
    bind_navigation_session,
    clear_business_session,
    set_connection_epoch,
    set_live_data_active,
    set_session_current_page,
    set_session_selection,
)
from diagnostic_platform.runtime.worker_runtime import WorkerRuntime
from diagnostic_platform.session_observability import emit_session_runtime_event
from src.gds2_orchestration.session_orchestrator import SessionContext, SessionOrchestrator


def _start_gds2_session(monkeypatch):
    runtime = WorkerRuntime()
    orchestrator = SessionOrchestrator()
    monkeypatch.setattr(platform_session_orchestrator_module, "route_backend", lambda brand: "gds2")
    payload = start_business_session(
        runtime,
        orchestrator=orchestrator,
        context=SessionContext(brand="Chevrolet", model="Malibu", vin="VIN123"),
    )
    session = orchestrator.get_session(payload["session_id"])
    return runtime, orchestrator, session, payload


def test_session_state_helpers_keep_active_session_snapshot_in_sync(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    runtime, _, session, _ = _start_gds2_session(monkeypatch)

    set_session_selection(session, module="Engine Control Module", data_category="Engine Data")
    set_session_current_page(session, "data_display")
    bind_navigation_session(runtime, session, "nav-1")
    bind_ai_session(runtime, session, "ai-1")
    set_live_data_active(runtime, session, True)
    set_connection_epoch(runtime, session, "epoch-1")

    snapshot = read_active_session_snapshot()
    assert snapshot is not None
    assert snapshot["session_id"] == session.session_id
    assert snapshot["backend_name"] == "gds2"
    assert snapshot["selected_module"] == "Engine Control Module"
    assert snapshot["selected_data_category"] == "Engine Data"
    assert snapshot["current_page"] == "data_display"
    assert snapshot["navigation_session_id"] == "nav-1"
    assert snapshot["ai_session_id"] == "ai-1"
    assert snapshot["live_data_active"] is True
    assert snapshot["connection_epoch"] == "epoch-1"

    clear_business_session(runtime, session.session_id)
    assert read_active_session_snapshot() is None


def test_live_data_runtime_events_update_proxy_local_session_state(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    runtime, _, session, _ = _start_gds2_session(monkeypatch)

    set_connection_epoch(runtime, session, "epoch-1")
    set_session_selection(
        session,
        module="Engine Control Module",
        data_category="Engine Data",
        runtime=runtime,
    )
    set_live_data_active(runtime, session, True)
    emit_session_runtime_event(
        "session.live_data.started",
        runtime=runtime,
        session=session,
        operation_kind="live_data.start",
        reason="live_data_started",
    )

    state = read_proxy_local_live_data_session_state()
    assert state is not None
    assert state["session_id"] == session.session_id
    assert state["connection_epoch"] == "epoch-1"
    assert state["live_data_active"] is True
    assert state["source_event_type"] == "session.live_data.started"

    set_live_data_active(runtime, session, False)
    emit_session_runtime_event(
        "session.live_data.stopped",
        runtime=runtime,
        session=session,
        operation_kind="live_data.stop",
        reason="live_data_stopped",
    )
    state = read_proxy_local_live_data_session_state()
    assert state is not None
    assert state["session_id"] == session.session_id
    assert state["live_data_active"] is False


def test_run_start_diagnostics_updates_snapshot_with_page_and_connection_epoch(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    runtime, orchestrator, session, _ = _start_gds2_session(monkeypatch)

    backend = types.SimpleNamespace()
    backend.preflight = lambda: {
        "network_quality": {
            "grade": "good",
            "status": "healthy",
            "reason": "ok",
            "connection_epoch": "epoch-1",
            "sample_count": 5,
            "fresh": True,
            "connected": True,
        },
        "connection_epoch": "epoch-1",
    }
    backend.start = lambda cancel_checker=None: {
        "modules": ["Engine Control Module"],
        "device": "SM2 USB",
    }
    backend.get_state = lambda: types.SimpleNamespace(
        current_page="module_list",
        extra={"vin": "VIN123", "device": "SM2 USB"},
    )

    payload = run_start_diagnostics(
        runtime,
        orchestrator=orchestrator,
        backend=backend,
        session_id=session.session_id,
    )

    snapshot = read_active_session_snapshot()
    assert payload["success"] is True
    assert snapshot is not None
    assert snapshot["session_id"] == session.session_id
    assert snapshot["connection_epoch"] == "epoch-1"
    assert snapshot["current_page"] == "module_list"
    assert snapshot["operation_kind"] == "start_diagnostics"
