from __future__ import annotations

import json

from diagnostic_platform.contracts import BackendCapability
from diagnostic_platform.runtime import navigation_runtime
from diagnostic_platform.runtime.worker_runtime import WorkerRuntime
from server.api import (
    session_ai_handlers,
    session_live_data_handlers,
    session_navigation_handlers,
)
from src.gds2_orchestration.session_orchestrator import (
    Session,
    SessionContext,
    SessionStatus,
)
from tests.helpers.simulated_gds2 import (
    FakeAIEngine,
    FakeAgentCollectorRegistry,
    install_fake_agent_collector,
    make_simulated_backend,
)


def _session(
    session_id: str,
    *,
    capabilities: list[BackendCapability],
) -> Session:
    session = Session(
        session_id=session_id,
        context=SessionContext(
            brand="Chevrolet",
            model="Simulated Malibu",
            vin="SIMVIN1234567890",
        ),
        status=SessionStatus.RUNNING,
    )
    session.backend_name = "gds2"
    session.capabilities = [capability.value for capability in capabilities]
    return session


class _FakeOrchestrator:
    def __init__(self, session: Session) -> None:
        self._session = session
        self.progress: list[tuple[str, str]] = []

    def get_session(self, session_id: str) -> Session:
        if session_id != self._session.session_id:
            raise KeyError(f"Session {session_id} not found")
        return self._session

    def emit_progress(self, session_id: str, message: str) -> None:
        self.progress.append((session_id, message))


def _parse_sse_event(message: str) -> tuple[str, dict[str, object]]:
    lines = message.strip().splitlines()
    event_type = lines[0].removeprefix("event: ")
    payload = json.loads(lines[1].removeprefix("data: "))
    return event_type, payload


def _bind_simulated_dependencies(
    monkeypatch,
    *,
    runtime: WorkerRuntime,
    orchestrator: _FakeOrchestrator,
    backend,
    ai_engine,
) -> None:
    monkeypatch.setattr(session_navigation_handlers, "_runtime", lambda: runtime)
    monkeypatch.setattr(session_navigation_handlers, "get_orchestrator", lambda: orchestrator)

    monkeypatch.setattr(session_ai_handlers, "_runtime", lambda: runtime)
    monkeypatch.setattr(session_ai_handlers, "get_orchestrator", lambda: orchestrator)
    monkeypatch.setattr(session_ai_handlers, "get_backend", lambda: backend)
    monkeypatch.setattr(session_ai_handlers, "get_ai_engine", lambda: ai_engine)

    monkeypatch.setattr(session_live_data_handlers, "_runtime", lambda: runtime)
    monkeypatch.setattr(session_live_data_handlers, "get_orchestrator", lambda: orchestrator)
    monkeypatch.setattr(session_live_data_handlers, "get_backend", lambda: backend)


def _drive_navigation_to_data_display(
    monkeypatch,
    *,
    runtime: WorkerRuntime,
    session: Session,
    harness,
) -> None:
    monkeypatch.setattr(
        navigation_runtime,
        "_create_navigation_controller",
        lambda: harness.controller,
    )

    payload, status = session_navigation_handlers.start_navigation_session_for_business(
        {
            "session_id": session.session_id,
            "goal": "Navigate to Data Display",
        }
    )

    assert status == 200
    assert payload["success"] is True

    event_iterator = session_navigation_handlers.stream_navigation_events(
        session.session_id,
        sse_response=lambda iterator: iterator,
    )

    seen_done = False
    for message in event_iterator:
        if message.startswith(":"):
            continue

        event_type, event_payload = _parse_sse_event(message)
        if event_type == "decision_required":
            selected_item = (
                "ECM"
                if event_payload["page"] == "module_list"
                else "Engine Data"
            )
            submit_payload, submit_status = (
                session_navigation_handlers.submit_navigation_decision_for_business(
                    {
                        "session_id": session.session_id,
                        "decision_id": event_payload["decision_id"],
                        "selected_item": selected_item,
                    }
                )
            )
            assert submit_status == 200
            assert submit_payload["selected_item"] == selected_item
        elif event_type == "done":
            seen_done = True
            assert event_payload["type"] == "done"
            assert event_payload["final_page"] == "data_display"
            assert event_payload["error"] is None

    assert seen_done is True


def test_simulated_navigation_session_handlers_reach_data_display(monkeypatch) -> None:
    runtime = WorkerRuntime()
    session = _session(
        "session-simulated",
        capabilities=[BackendCapability.NAVIGATION],
    )
    orchestrator = _FakeOrchestrator(session)
    harness = make_simulated_backend()

    _bind_simulated_dependencies(
        monkeypatch,
        runtime=runtime,
        orchestrator=orchestrator,
        backend=harness.backend,
        ai_engine=FakeAIEngine(),
    )

    _drive_navigation_to_data_display(
        monkeypatch,
        runtime=runtime,
        session=session,
        harness=harness,
    )

    assert session.selected_module == "ECM"
    assert session.selected_data_category == "Engine Data"
    assert harness.model.page.value == "data_display"
    assert runtime.get_navigation_session_id(session.session_id) is None


def test_simulated_session_handlers_run_ai_and_clear_dtcs_after_navigation(monkeypatch) -> None:
    runtime = WorkerRuntime()
    session = _session(
        "session-simulated",
        capabilities=[
            BackendCapability.NAVIGATION,
            BackendCapability.AI_DATA_COLLECTION,
            BackendCapability.CLEAR_DTCS,
        ],
    )
    orchestrator = _FakeOrchestrator(session)
    harness = make_simulated_backend()
    ai_engine = FakeAIEngine()

    install_fake_agent_collector(monkeypatch)
    _bind_simulated_dependencies(
        monkeypatch,
        runtime=runtime,
        orchestrator=orchestrator,
        backend=harness.backend,
        ai_engine=ai_engine,
    )

    _drive_navigation_to_data_display(
        monkeypatch,
        runtime=runtime,
        session=session,
        harness=harness,
    )

    payload, status = session_ai_handlers.start_ai_diagnose(
        {"session_id": session.session_id}
    )

    assert status == 200
    assert payload["success"] is True
    assert payload["ai_session_id"] == "ai-sim-1"
    assert runtime.get_ai_session_id(session.session_id) == "ai-sim-1"

    assert len(ai_engine.started_sessions) == 1
    started = ai_engine.started_sessions[0]
    assert started["vehicle_context"] == {
        "vin": "SIMVIN1234567890",
        "module": "ECM",
        "data_category": "Engine Data",
        "brand": "Chevrolet",
        "model": "Simulated Malibu",
    }
    assert [point.parameter for point in started["diagnostic_payload"].live_data] == [
        "RPM",
        "Coolant Temp",
    ]
    assert [dtc.code for dtc in started["diagnostic_payload"].dtcs] == ["P0001"]

    ai_events = session_ai_handlers.stream_ai_diagnose_events(
        session.session_id,
        sse_response=lambda iterator: list(iterator),
    )

    assert ai_events == [
        'event: connected\ndata: {"session_id": "session-simulated"}\n\n',
        'event: progress\ndata: {"phase": "analyzing", "message": "Simulated AI analysis running"}\n\n',
        'event: done\ndata: {"session_id": "ai-sim-1"}\n\n',
    ]
    assert runtime.get_ai_session_id(session.session_id) is None

    clear_payload, clear_status = session_live_data_handlers.clear_session_dtcs(
        {"session_id": session.session_id}
    )

    assert clear_status == 200
    assert clear_payload == {
        "success": True,
        "session_id": "session-simulated",
        "result": {
            "success": True,
            "cleared_count": 2,
            "message": "Clear DTCs completed",
            "page_context": "data_display",
        },
    }
    assert harness.model.dtc_count == 0
    assert ("session-simulated", "AI diagnosis started: ECM / Engine Data") in orchestrator.progress
    assert ("session-simulated", "Clear DTCs completed (2 codes)") in orchestrator.progress


def test_simulated_session_handlers_read_dtcs_after_navigation(monkeypatch) -> None:
    runtime = WorkerRuntime()
    session = _session(
        "session-simulated",
        capabilities=[
            BackendCapability.NAVIGATION,
            BackendCapability.READ_DTCS,
        ],
    )
    orchestrator = _FakeOrchestrator(session)
    harness = make_simulated_backend()

    _bind_simulated_dependencies(
        monkeypatch,
        runtime=runtime,
        orchestrator=orchestrator,
        backend=harness.backend,
        ai_engine=FakeAIEngine(),
    )

    _drive_navigation_to_data_display(
        monkeypatch,
        runtime=runtime,
        session=session,
        harness=harness,
    )

    payload, status = session_live_data_handlers.read_session_dtcs(
        {"session_id": session.session_id}
    )

    assert status == 200
    assert payload["success"] is True
    assert payload["session_id"] == "session-simulated"
    assert payload["result"]["dtc_count"] == 2
    assert payload["result"]["page_context"] == "data_display"
    assert [dtc["code"] for dtc in payload["result"]["dtcs"]] == ["P0001", "P0002"]
    assert ("session-simulated", "Read DTCs completed (2 codes)") in orchestrator.progress


def test_simulated_session_handlers_stream_live_data_without_real_vci(monkeypatch) -> None:
    runtime = WorkerRuntime()
    session = _session(
        "session-simulated",
        capabilities=[
            BackendCapability.NAVIGATION,
            BackendCapability.LIVE_DATA,
        ],
    )
    orchestrator = _FakeOrchestrator(session)
    harness = make_simulated_backend()
    collector_registry = FakeAgentCollectorRegistry()

    install_fake_agent_collector(monkeypatch, registry=collector_registry)
    _bind_simulated_dependencies(
        monkeypatch,
        runtime=runtime,
        orchestrator=orchestrator,
        backend=harness.backend,
        ai_engine=FakeAIEngine(),
    )

    _drive_navigation_to_data_display(
        monkeypatch,
        runtime=runtime,
        session=session,
        harness=harness,
    )

    start_payload, start_status = session_live_data_handlers.start_live_data_session(
        {
            "session_id": session.session_id,
            "interval_ms": 250,
        }
    )

    assert start_status == 200
    assert start_payload == {
        "success": True,
        "session_id": "session-simulated",
        "message": "Live data streaming started",
        "interval_ms": 250,
    }
    assert runtime.is_live_data_active(session.session_id) is True

    stream = session_live_data_handlers.stream_live_data_events(
        session.session_id,
        sse_response=lambda iterator: iterator,
    )

    assert next(stream) == (
        'event: connected\ndata: {"session_id": "session-simulated", "message": "Connected to stream"}\n\n'
    )

    collector = collector_registry.instances[-1]
    collector.emit_snapshot()
    snapshot_message = next(stream)
    event_type, event_payload = _parse_sse_event(snapshot_message)
    assert event_type == "snapshot"
    assert event_payload["type"] == "snapshot"
    assert event_payload["page_context"] == {"page": "data_display"}
    assert event_payload["param_count"] == 2
    assert event_payload["dtc_count"] == 1

    stop_payload, stop_status = session_live_data_handlers.stop_live_data_session(
        {"session_id": session.session_id}
    )

    assert stop_status == 200
    assert stop_payload == {
        "success": True,
        "session_id": "session-simulated",
        "message": "Live data stopped",
    }
    assert runtime.is_live_data_active(session.session_id) is False
    assert harness.model.page.value == "data_list"
    assert ("session-simulated", "Live data started: Engine Data") in orchestrator.progress
    assert ("session-simulated", "Live data stopped") in orchestrator.progress
    stream.close()


def test_simulated_clear_dtcs_conflicts_while_live_data_stream_is_active(monkeypatch) -> None:
    runtime = WorkerRuntime()
    session = _session(
        "session-simulated",
        capabilities=[
            BackendCapability.NAVIGATION,
            BackendCapability.LIVE_DATA,
            BackendCapability.CLEAR_DTCS,
        ],
    )
    orchestrator = _FakeOrchestrator(session)
    harness = make_simulated_backend()
    collector_registry = FakeAgentCollectorRegistry()

    install_fake_agent_collector(monkeypatch, registry=collector_registry)
    _bind_simulated_dependencies(
        monkeypatch,
        runtime=runtime,
        orchestrator=orchestrator,
        backend=harness.backend,
        ai_engine=FakeAIEngine(),
    )

    _drive_navigation_to_data_display(
        monkeypatch,
        runtime=runtime,
        session=session,
        harness=harness,
    )

    start_payload, start_status = session_live_data_handlers.start_live_data_session(
        {"session_id": session.session_id}
    )

    assert start_status == 200
    assert start_payload["success"] is True
    assert runtime.is_live_data_active(session.session_id) is True

    clear_payload, clear_status = session_live_data_handlers.clear_session_dtcs(
        {"session_id": session.session_id}
    )

    assert clear_status == 409
    assert clear_payload == {
        "success": False,
        "error": "Cannot clear DTCs while live data streaming is active",
    }


def test_simulated_clear_dtcs_conflicts_while_ai_session_is_active(monkeypatch) -> None:
    runtime = WorkerRuntime()
    session = _session(
        "session-simulated",
        capabilities=[
            BackendCapability.NAVIGATION,
            BackendCapability.AI_DATA_COLLECTION,
            BackendCapability.CLEAR_DTCS,
        ],
    )
    orchestrator = _FakeOrchestrator(session)
    harness = make_simulated_backend()
    ai_engine = FakeAIEngine()

    install_fake_agent_collector(monkeypatch)
    _bind_simulated_dependencies(
        monkeypatch,
        runtime=runtime,
        orchestrator=orchestrator,
        backend=harness.backend,
        ai_engine=ai_engine,
    )

    _drive_navigation_to_data_display(
        monkeypatch,
        runtime=runtime,
        session=session,
        harness=harness,
    )

    ai_payload, ai_status = session_ai_handlers.start_ai_diagnose(
        {"session_id": session.session_id}
    )

    assert ai_status == 200
    assert ai_payload["success"] is True
    assert runtime.get_ai_session_id(session.session_id) == "ai-sim-1"

    clear_payload, clear_status = session_live_data_handlers.clear_session_dtcs(
        {"session_id": session.session_id}
    )

    assert clear_status == 409
    assert clear_payload == {
        "success": False,
        "error": "Cannot clear DTCs while AI diagnosis is active",
    }


def test_simulated_clear_dtcs_conflicts_while_navigation_is_active(monkeypatch) -> None:
    runtime = WorkerRuntime()
    session = _session(
        "session-simulated",
        capabilities=[
            BackendCapability.NAVIGATION,
            BackendCapability.CLEAR_DTCS,
        ],
    )
    orchestrator = _FakeOrchestrator(session)
    harness = make_simulated_backend()

    _bind_simulated_dependencies(
        monkeypatch,
        runtime=runtime,
        orchestrator=orchestrator,
        backend=harness.backend,
        ai_engine=FakeAIEngine(),
    )
    monkeypatch.setattr(
        navigation_runtime,
        "_create_navigation_controller",
        lambda: harness.controller,
    )

    nav_payload, nav_status = session_navigation_handlers.start_navigation_session_for_business(
        {
            "session_id": session.session_id,
            "goal": "Navigate to Data Display",
        }
    )

    assert nav_status == 200
    assert nav_payload["success"] is True
    assert runtime.get_navigation_session_id(session.session_id) is not None

    clear_payload, clear_status = session_live_data_handlers.clear_session_dtcs(
        {"session_id": session.session_id}
    )

    assert clear_status == 409
    assert clear_payload == {
        "success": False,
        "error": "Cannot clear DTCs while navigation is active",
    }


def test_simulated_live_data_stream_emits_guard_events_over_sse(monkeypatch) -> None:
    runtime = WorkerRuntime()
    session = _session(
        "session-simulated",
        capabilities=[
            BackendCapability.NAVIGATION,
            BackendCapability.LIVE_DATA,
        ],
    )
    orchestrator = _FakeOrchestrator(session)
    harness = make_simulated_backend()
    collector_registry = FakeAgentCollectorRegistry()

    install_fake_agent_collector(monkeypatch, registry=collector_registry)
    _bind_simulated_dependencies(
        monkeypatch,
        runtime=runtime,
        orchestrator=orchestrator,
        backend=harness.backend,
        ai_engine=FakeAIEngine(),
    )

    _drive_navigation_to_data_display(
        monkeypatch,
        runtime=runtime,
        session=session,
        harness=harness,
    )

    start_payload, start_status = session_live_data_handlers.start_live_data_session(
        {"session_id": session.session_id}
    )

    assert start_status == 200
    assert start_payload["success"] is True

    stream = session_live_data_handlers.stream_live_data_events(
        session.session_id,
        sse_response=lambda iterator: iterator,
    )
    assert next(stream) == (
        'event: connected\ndata: {"session_id": "session-simulated", "message": "Connected to stream"}\n\n'
    )

    collector = collector_registry.instances[-1]
    collector.emit_guard_event(
        {
            "ok": True,
            "mode": "stream",
            "message": "Recovered Data Display after J2534 disconnect.",
        }
    )

    guard_message = next(stream)
    event_type, event_payload = _parse_sse_event(guard_message)
    assert event_type == "guard"
    assert event_payload == {
        "message": "Recovered Data Display after J2534 disconnect.",
    }
    stream.close()


def test_simulated_live_data_stream_emits_error_events_over_sse(monkeypatch) -> None:
    runtime = WorkerRuntime()
    session = _session(
        "session-simulated",
        capabilities=[
            BackendCapability.NAVIGATION,
            BackendCapability.LIVE_DATA,
        ],
    )
    orchestrator = _FakeOrchestrator(session)
    harness = make_simulated_backend()
    collector_registry = FakeAgentCollectorRegistry()

    install_fake_agent_collector(monkeypatch, registry=collector_registry)
    _bind_simulated_dependencies(
        monkeypatch,
        runtime=runtime,
        orchestrator=orchestrator,
        backend=harness.backend,
        ai_engine=FakeAIEngine(),
    )

    _drive_navigation_to_data_display(
        monkeypatch,
        runtime=runtime,
        session=session,
        harness=harness,
    )

    start_payload, start_status = session_live_data_handlers.start_live_data_session(
        {"session_id": session.session_id}
    )

    assert start_status == 200
    assert start_payload["success"] is True

    stream = session_live_data_handlers.stream_live_data_events(
        session.session_id,
        sse_response=lambda iterator: iterator,
    )
    assert next(stream) == (
        'event: connected\ndata: {"session_id": "session-simulated", "message": "Connected to stream"}\n\n'
    )

    collector = collector_registry.instances[-1]
    collector.emit_error("Simulated live data failure")

    error_message = next(stream)
    event_type, event_payload = _parse_sse_event(error_message)
    assert event_type == "error"
    assert event_payload["type"] == "error"
    assert event_payload["message"] == "Simulated live data failure"
    stream.close()


def test_simulated_live_data_terminal_error_clears_session_state_and_allows_clear_dtcs(monkeypatch) -> None:
    runtime = WorkerRuntime()
    session = _session(
        "session-simulated",
        capabilities=[
            BackendCapability.NAVIGATION,
            BackendCapability.LIVE_DATA,
            BackendCapability.CLEAR_DTCS,
        ],
    )
    orchestrator = _FakeOrchestrator(session)
    harness = make_simulated_backend()
    collector_registry = FakeAgentCollectorRegistry()

    install_fake_agent_collector(monkeypatch, registry=collector_registry)
    _bind_simulated_dependencies(
        monkeypatch,
        runtime=runtime,
        orchestrator=orchestrator,
        backend=harness.backend,
        ai_engine=FakeAIEngine(),
    )

    _drive_navigation_to_data_display(
        monkeypatch,
        runtime=runtime,
        session=session,
        harness=harness,
    )

    start_payload, start_status = session_live_data_handlers.start_live_data_session(
        {"session_id": session.session_id}
    )

    assert start_status == 200
    assert start_payload["success"] is True
    assert runtime.is_live_data_active(session.session_id) is True

    stream = session_live_data_handlers.stream_live_data_events(
        session.session_id,
        sse_response=lambda iterator: iterator,
    )
    assert next(stream) == (
        'event: connected\ndata: {"session_id": "session-simulated", "message": "Connected to stream"}\n\n'
    )

    collector = collector_registry.instances[-1]
    collector.stop()
    collector.emit_error("Simulated live data failure")

    error_message = next(stream)
    event_type, event_payload = _parse_sse_event(error_message)
    assert event_type == "error"
    assert event_payload["type"] == "error"
    assert event_payload["message"] == "Simulated live data failure"
    assert runtime.is_live_data_active(session.session_id) is False

    inactive_stream_payload, inactive_stream_status = (
        session_live_data_handlers.stream_live_data_events(
            session.session_id,
            sse_response=lambda iterator: iterator,
        )
    )
    assert inactive_stream_status == 404
    assert inactive_stream_payload == {
        "success": False,
        "error": "No active live data stream for session-simulated",
    }

    clear_payload, clear_status = session_live_data_handlers.clear_session_dtcs(
        {"session_id": session.session_id}
    )

    assert clear_status == 200
    assert clear_payload == {
        "success": True,
        "session_id": "session-simulated",
        "result": {
            "success": True,
            "cleared_count": 2,
            "message": "Clear DTCs completed",
            "page_context": "data_display",
        },
    }
    assert harness.model.dtc_count == 0
    stream.close()


def test_simulated_live_data_can_restart_after_stop(monkeypatch) -> None:
    runtime = WorkerRuntime()
    session = _session(
        "session-simulated",
        capabilities=[
            BackendCapability.NAVIGATION,
            BackendCapability.LIVE_DATA,
        ],
    )
    orchestrator = _FakeOrchestrator(session)
    harness = make_simulated_backend()
    collector_registry = FakeAgentCollectorRegistry()

    install_fake_agent_collector(monkeypatch, registry=collector_registry)
    _bind_simulated_dependencies(
        monkeypatch,
        runtime=runtime,
        orchestrator=orchestrator,
        backend=harness.backend,
        ai_engine=FakeAIEngine(),
    )

    _drive_navigation_to_data_display(
        monkeypatch,
        runtime=runtime,
        session=session,
        harness=harness,
    )

    first_start, first_status = session_live_data_handlers.start_live_data_session(
        {"session_id": session.session_id}
    )
    assert first_status == 200
    assert first_start["success"] is True
    assert runtime.is_live_data_active(session.session_id) is True

    stop_payload, stop_status = session_live_data_handlers.stop_live_data_session(
        {"session_id": session.session_id}
    )
    assert stop_status == 200
    assert stop_payload["success"] is True
    assert runtime.is_live_data_active(session.session_id) is False

    inactive_stream_payload, inactive_stream_status = (
        session_live_data_handlers.stream_live_data_events(
            session.session_id,
            sse_response=lambda iterator: iterator,
        )
    )
    assert inactive_stream_status == 404
    assert inactive_stream_payload == {
        "success": False,
        "error": "No active live data stream for session-simulated",
    }

    second_start, second_status = session_live_data_handlers.start_live_data_session(
        {"session_id": session.session_id}
    )

    assert second_status == 200
    assert second_start["success"] is True
    assert runtime.is_live_data_active(session.session_id) is True
    assert len(collector_registry.instances) == 2


def test_simulated_clear_dtcs_recovers_after_live_data_stop_using_session_selection(monkeypatch) -> None:
    runtime = WorkerRuntime()
    session = _session(
        "session-simulated",
        capabilities=[
            BackendCapability.NAVIGATION,
            BackendCapability.LIVE_DATA,
            BackendCapability.CLEAR_DTCS,
        ],
    )
    orchestrator = _FakeOrchestrator(session)
    harness = make_simulated_backend()
    collector_registry = FakeAgentCollectorRegistry()

    install_fake_agent_collector(monkeypatch, registry=collector_registry)
    _bind_simulated_dependencies(
        monkeypatch,
        runtime=runtime,
        orchestrator=orchestrator,
        backend=harness.backend,
        ai_engine=FakeAIEngine(),
    )

    _drive_navigation_to_data_display(
        monkeypatch,
        runtime=runtime,
        session=session,
        harness=harness,
    )

    start_payload, start_status = session_live_data_handlers.start_live_data_session(
        {"session_id": session.session_id}
    )
    assert start_status == 200
    assert start_payload["success"] is True

    stop_payload, stop_status = session_live_data_handlers.stop_live_data_session(
        {"session_id": session.session_id}
    )
    assert stop_status == 200
    assert stop_payload["success"] is True
    assert harness.model.page.value == "data_list"

    clear_payload, clear_status = session_live_data_handlers.clear_session_dtcs(
        {"session_id": session.session_id}
    )

    assert clear_status == 200
    assert clear_payload == {
        "success": True,
        "session_id": "session-simulated",
        "result": {
            "success": True,
            "cleared_count": 2,
            "message": "Clear DTCs completed",
            "page_context": "data_display",
        },
    }
    assert harness.model.dtc_count == 0


def test_simulated_ai_can_start_again_after_terminal_stream(monkeypatch) -> None:
    runtime = WorkerRuntime()
    session = _session(
        "session-simulated",
        capabilities=[
            BackendCapability.NAVIGATION,
            BackendCapability.AI_DATA_COLLECTION,
        ],
    )
    orchestrator = _FakeOrchestrator(session)
    harness = make_simulated_backend()
    ai_engine = FakeAIEngine()

    install_fake_agent_collector(monkeypatch)
    _bind_simulated_dependencies(
        monkeypatch,
        runtime=runtime,
        orchestrator=orchestrator,
        backend=harness.backend,
        ai_engine=ai_engine,
    )

    _drive_navigation_to_data_display(
        monkeypatch,
        runtime=runtime,
        session=session,
        harness=harness,
    )

    first_payload, first_status = session_ai_handlers.start_ai_diagnose(
        {"session_id": session.session_id}
    )
    assert first_status == 200
    assert first_payload["ai_session_id"] == "ai-sim-1"

    first_events = session_ai_handlers.stream_ai_diagnose_events(
        session.session_id,
        sse_response=lambda iterator: list(iterator),
    )
    assert first_events[-1] == 'event: done\ndata: {"session_id": "ai-sim-1"}\n\n'
    assert runtime.get_ai_session_id(session.session_id) is None

    second_payload, second_status = session_ai_handlers.start_ai_diagnose(
        {"session_id": session.session_id}
    )

    assert second_status == 200
    assert second_payload["ai_session_id"] == "ai-sim-2"
    assert len(ai_engine.started_sessions) == 2
