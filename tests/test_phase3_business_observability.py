from __future__ import annotations

import json
import queue
import types
from pathlib import Path

import diagnostic_platform.session_orchestrator as platform_session_orchestrator_module
from diagnostic_platform.observability import flush_product_log_writers
from diagnostic_platform.runtime.session_actions import (
    apply_navigation_event,
    handle_ai_stream_terminal_event,
    start_ai_diagnosis,
    start_live_data,
    start_navigation,
    stop_live_data,
)
from diagnostic_platform.runtime.session_decisions import submit_session_decision
from diagnostic_platform.runtime.session_lifecycle import abort_business_session, start_business_session
from diagnostic_platform.runtime.session_preflight import run_start_diagnostics
from diagnostic_platform.runtime.worker_runtime import WorkerRuntime
from src.gds2_orchestration.session_orchestrator import SessionContext, SessionOrchestrator


def _read_cloud_events(tmp_path: Path) -> list[dict[str, object]]:
    flush_product_log_writers()
    raw_dir = tmp_path / "RPA_Diagnostic" / "observability" / "cloud" / "raw"
    records: list[dict[str, object]] = []
    for path in sorted(raw_dir.glob("*.jsonl")):
        records.extend(
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    return records


def _start_session(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    runtime = WorkerRuntime()
    orchestrator = SessionOrchestrator()
    monkeypatch.setattr(platform_session_orchestrator_module, "route_backend", lambda brand: "gds2")
    payload = start_business_session(
        runtime,
        orchestrator=orchestrator,
        context=SessionContext(brand="Chevrolet", model="Malibu", vin="VIN123"),
    )
    session = orchestrator.get_session(payload["session_id"])
    return runtime, orchestrator, session


def test_phase3_emits_session_lifecycle_and_network_gate_events(tmp_path: Path, monkeypatch) -> None:
    runtime, orchestrator, session = _start_session(tmp_path, monkeypatch)

    backend = types.SimpleNamespace()
    backend.preflight = lambda: {
        "network_quality": {
            "grade": "block",
            "status": "blocked",
            "reason": "p95 above threshold",
            "connection_epoch": "epoch-1",
            "sample_count": 5,
            "fresh": True,
            "connected": True,
        },
        "connection_epoch": "epoch-1",
    }

    payload = run_start_diagnostics(
        runtime,
        orchestrator=orchestrator,
        backend=backend,
        session_id=session.session_id,
    )
    assert payload["decision_required"] is True

    events = _read_cloud_events(tmp_path)
    event_types = [event["event_type"] for event in events]
    assert "session.lifecycle.started" in event_types
    assert "session.start_diagnostics.started" in event_types
    assert "session.network_gate.preflight" in event_types
    assert "session.network_gate.blocked" in event_types
    assert "session.network_gate.decision_required" in event_types

    abort_business_session(
        runtime,
        orchestrator=orchestrator,
        session_id=session.session_id,
        reason="user-stop",
        get_ai_engine=lambda: types.SimpleNamespace(abort_session=lambda _sid: None),
    )
    events = _read_cloud_events(tmp_path)
    assert "session.lifecycle.aborted" in [event["event_type"] for event in events]


def test_phase3_emits_network_override_invalidation_event(tmp_path: Path, monkeypatch) -> None:
    runtime, orchestrator, session = _start_session(tmp_path, monkeypatch)
    session.network_override = {
        "allowed": True,
        "connection_epoch": "epoch-old",
        "reason": "manual",
    }

    backend = types.SimpleNamespace()
    backend.preflight = lambda: {
        "network_quality": {
            "grade": "good",
            "status": "healthy",
            "reason": "ok",
            "connection_epoch": "epoch-new",
            "sample_count": 5,
            "fresh": True,
            "connected": True,
        },
        "connection_epoch": "epoch-new",
    }
    backend.start = lambda cancel_checker=None: {"modules": ["Engine Control Module"]}
    backend.get_state = lambda: types.SimpleNamespace(
        current_page="module_list",
        extra={"vin": "VIN123"},
    )

    run_start_diagnostics(
        runtime,
        orchestrator=orchestrator,
        backend=backend,
        session_id=session.session_id,
    )

    events = _read_cloud_events(tmp_path)
    assert "session.network_gate.override_invalidated" in [event["event_type"] for event in events]


def test_phase3_emits_navigation_live_data_and_ai_business_events(tmp_path: Path, monkeypatch) -> None:
    runtime, orchestrator, session = _start_session(tmp_path, monkeypatch)
    session.selected_module = "Engine Control Module"
    session.selected_data_category = "Engine Data"
    session.current_page = "data_display"
    session.capabilities = [
        "navigation",
        "live_data",
        "ai_data_collection",
        "core_session",
    ]

    nav_handle = types.SimpleNamespace(
        start_navigation_session=lambda runtime, goal: types.SimpleNamespace(
            session_id="nav-1",
            goal=goal,
            status="running",
            current_page="module_list",
            event_queue=queue.Queue(),
        ),
        get_navigation_session=lambda runtime, session_id: types.SimpleNamespace(
            session_id=session_id,
            goal="Go to Data Display",
            status="running",
            current_page="module_list",
            event_queue=queue.Queue(),
        ),
    )
    runtime.active_backend_bundle = types.SimpleNamespace(navigation_handle=nav_handle)

    start_navigation(
        runtime,
        session,
        goal="Go to Data Display",
        backend=types.SimpleNamespace(get_navigation_runtime=lambda: nav_handle),
        emit_progress=lambda _message: None,
    )
    nav_session = types.SimpleNamespace(
        status="running",
        current_page="data_list",
        pending_decision_id=None,
        pending_items=[],
        error=None,
    )
    apply_navigation_event(
        runtime,
        session,
        nav_session,
        {"type": "done", "page": "data_display", "selections": {"module": "Engine Control Module", "data_category": "Engine Data"}},
    )

    class _LiveDataBackend:
        def start_live_data_session(self, **kwargs):
            return {"stream_scope": kwargs["stream_scope"], "stream_active": True}

        def stop_live_data_session(self):
            return {"stream_active": False}

        def detect_current_page(self):
            return "data_display"

        def get_state(self):
            return types.SimpleNamespace(current_page="data_display", extra={})

    backend = _LiveDataBackend()
    start_live_data(
        runtime,
        session,
        {"data_category": "Engine Data", "module": "Engine Control Module"},
        interval_ms=250,
        backend=backend,
        stream_scope="session:test",
        emit_progress=lambda _message: None,
    )
    session.current_page = "data_list"
    stop_live_data(runtime, session, backend=backend, emit_progress=lambda _message: None)
    assert session.current_page == "data_display"

    engine = types.SimpleNamespace(
        is_active=False,
        start_session_from_payload=lambda vehicle_context, diagnostic_payload: "ai-1",
        retry_with_cached=lambda cached_payload_id, vehicle_context: "ai-2",
    )
    start_ai_diagnosis(
        runtime,
        session,
        vehicle_context={"module": "Engine Control Module"},
        data_category="Engine Data",
        engine=engine,
        diagnostic_payload={"ok": True},
        emit_progress=lambda _message: None,
    )
    handle_ai_stream_terminal_event(runtime, session, 'event: error\ndata: {"message":"boom"}\n\n')

    events = _read_cloud_events(tmp_path)
    event_types = [event["event_type"] for event in events]
    assert "session.navigation.started" in event_types
    assert "session.navigation.completed" in event_types
    assert "session.live_data.started" in event_types
    assert "session.live_data.stopped" in event_types
    assert "session.ai.started" in event_types
    assert "ai.stream.error" in event_types


def test_phase3_emits_network_override_set_on_continue_decision(tmp_path: Path, monkeypatch) -> None:
    runtime, orchestrator, session = _start_session(tmp_path, monkeypatch)

    backend = types.SimpleNamespace()
    backend.preflight = lambda: {
        "network_quality": {
            "grade": "block",
            "status": "blocked",
            "reason": "p95 above threshold",
            "connection_epoch": "epoch-2",
            "sample_count": 5,
            "fresh": True,
            "connected": True,
        },
        "connection_epoch": "epoch-2",
    }
    backend.start = lambda cancel_checker=None: {"modules": ["Engine Control Module"]}
    backend.get_state = lambda: types.SimpleNamespace(
        current_page="module_list",
        extra={"vin": "VIN123"},
    )

    blocked = run_start_diagnostics(
        runtime,
        orchestrator=orchestrator,
        backend=backend,
        session_id=session.session_id,
    )
    decision_id = blocked["decision"]["decision_id"]

    resumed = submit_session_decision(
        runtime,
        orchestrator=orchestrator,
        backend=backend,
        session_id=session.session_id,
        decision_id=decision_id,
        option_id="continue_anyway",
        get_navigation_runtime=None,
        get_backend=lambda: backend,
    )

    assert resumed["success"] is True
    events = _read_cloud_events(tmp_path)
    event_types = [event["event_type"] for event in events]
    assert "session.network_gate.override_set" in event_types
    assert "session.start_diagnostics.completed" in event_types
