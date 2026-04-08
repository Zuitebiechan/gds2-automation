from __future__ import annotations

import queue
import threading

from diagnostic_platform.runtime import navigation_runtime
from diagnostic_platform.runtime.diagnostics_runtime import (
    build_diagnostics_start_payload,
)
from src.navigation import GDS2Page
from tests.helpers.simulated_gds2 import (
    FakeAgentCollectorRegistry,
    install_fake_agent_collector,
    make_simulated_backend,
)


def test_simulated_navigation_runtime_reaches_data_display_without_real_gds2():
    harness = make_simulated_backend()
    session = navigation_runtime.NavSession(
        session_id="nav-simulated",
        goal="Navigate to Data Display",
    )
    result_holder: dict[str, object] = {}

    def _runner() -> None:
        result_holder["result"] = navigation_runtime._run_navigation_session(
            session,
            controller=harness.controller,
        )

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()

    while thread.is_alive():
        try:
            event = session.event_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        if event.get("type") != "decision_required":
            continue
        if event.get("page") == "module_list":
            session.decision_queue.put({"selected_item": "ECM"})
        elif event.get("page") == "data_list":
            session.decision_queue.put({"selected_item": "Engine Data"})

    thread.join(timeout=2.0)

    assert thread.is_alive() is False
    assert result_holder["result"] == {
        "goal": "Navigate to Data Display",
        "current_page": "data_display",
        "navigation_history": [
            {"page": "main_menu", "action": "click_diagnostics", "to_page": "vehicle_selection", "success": True},
            {"page": "vehicle_selection", "action": "click_enter", "to_page": "diagnostics_menu", "success": True},
            {"page": "diagnostics_menu", "action": "select_module_diagnostics", "to_page": "module_list", "success": True},
            {"page": "module_list", "action": "select_module", "selected_item": "ECM", "to_page": "module_submenu", "success": True},
            {"page": "module_submenu", "action": "select_data_display", "selected_item": "Data Display", "to_page": "data_list", "success": True},
            {"page": "data_list", "action": "select_data_category", "selected_item": "Engine Data", "to_page": "data_display", "success": True},
        ],
        "selections": {"module": "ECM", "data_category": "Engine Data"},
        "error": None,
    }
    assert harness.model.page == GDS2Page.DATA_DISPLAY


def test_simulated_backend_runs_start_ai_and_clear_dtcs_without_real_vci(monkeypatch):
    harness = make_simulated_backend()
    install_fake_agent_collector(monkeypatch)

    start_payload = build_diagnostics_start_payload(backend=harness.backend)
    harness.backend.select_module("ECM")
    harness.backend.select_data_category("Engine Data")

    diagnostic_payload = harness.backend.collect_ai_payload(
        vehicle_context={
            "brand": "chevrolet",
            "model": "Simulated Malibu",
            "vin": harness.model.vin,
        },
        data_category="Engine Data",
        collection_seconds=0,
    )
    clear_result = harness.backend.clear_dtcs()

    assert start_payload == {
        "success": True,
        "modules": ["ECM", "TCM"],
        "vin": harness.model.vin,
        "device": harness.model.device,
        "backend_name": "gds2",
        "capabilities": [
            "core_session",
            "read_dtcs",
            "live_data",
            "ai_data_collection",
            "navigation",
            "generic_actions",
            "clear_dtcs",
        ],
    }
    assert diagnostic_payload.source_backend == "gds2"
    assert diagnostic_payload.vehicle_context.vin == harness.model.vin
    assert [point.parameter for point in diagnostic_payload.live_data] == [
        "RPM",
        "Coolant Temp",
    ]
    assert [dtc.code for dtc in diagnostic_payload.dtcs] == ["P0001"]
    assert clear_result.success is True
    assert clear_result.cleared_count == 2
    assert harness.model.dtc_count == 0


def test_simulated_ai_collection_restarts_window_after_guard_recovery(monkeypatch):
    harness = make_simulated_backend()
    collector_registry = FakeAgentCollectorRegistry()
    install_fake_agent_collector(monkeypatch, registry=collector_registry)
    harness.backend.select_module("ECM")
    harness.backend.select_data_category("Engine Data")

    class _FakeClock:
        def __init__(self) -> None:
            self.now = 0.0
            self.sleep_calls = 0

        def time(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.sleep_calls += 1
            collector = collector_registry.instances[-1]
            if self.sleep_calls == 1:
                collector.emit_snapshot()
            elif self.sleep_calls == 2:
                collector.emit_guard_event(
                    {
                        "ok": True,
                        "mode": "ai_collect",
                        "recovered": True,
                        "recovery_method": "backtrack",
                        "restart_collection": True,
                        "message": "Recovered Data Display after reconnect; restarting AI collection window.",
                    }
                )
            elif self.sleep_calls == 3:
                collector.emit_snapshot()
            self.now += seconds

    fake_clock = _FakeClock()
    monkeypatch.setattr("backends.gds2.backend.time.time", fake_clock.time)
    monkeypatch.setattr("backends.gds2.backend.time.sleep", fake_clock.sleep)

    diagnostic_payload = harness.backend.collect_ai_payload(
        vehicle_context={
            "brand": "chevrolet",
            "model": "Simulated Malibu",
            "vin": harness.model.vin,
        },
        data_category="Engine Data",
        collection_seconds=3,
    )

    assert fake_clock.sleep_calls >= 3
    assert [point.parameter for point in diagnostic_payload.live_data] == [
        "RPM",
        "Coolant Temp",
    ]
    assert len(diagnostic_payload.live_data) == 2
    assert [dtc.code for dtc in diagnostic_payload.dtcs] == ["P0001"]


def test_simulated_ai_collection_fails_when_java_agent_is_unavailable(monkeypatch):
    harness = make_simulated_backend()
    harness.backend.select_module("ECM")
    harness.backend.select_data_category("Engine Data")

    class _UnavailableCollector:
        def __init__(self, **kwargs):
            self._on_snapshot = kwargs["on_snapshot"]
            self._on_error = kwargs["on_error"]
            self.is_running = False

        def check_agent_available(self):
            return {"available": False}

        def start(self):
            self.is_running = True

        def stop(self):
            self.is_running = False

    monkeypatch.setattr("backends.gds2.backend.AgentDataCollector", _UnavailableCollector)

    try:
        harness.backend.collect_ai_payload(
            vehicle_context={
                "brand": "chevrolet",
                "model": "Simulated Malibu",
                "vin": harness.model.vin,
            },
            data_category="Engine Data",
            collection_seconds=0,
        )
    except RuntimeError as exc:
        assert str(exc) == "Java Agent not available. Start GDS2 with the agent."
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("collect_ai_payload should fail when agent is unavailable")


def test_simulated_ai_collection_fails_when_collector_reports_error(monkeypatch):
    harness = make_simulated_backend()
    harness.backend.select_module("ECM")
    harness.backend.select_data_category("Engine Data")

    class _ErroringCollector:
        def __init__(self, **kwargs):
            self._on_snapshot = kwargs["on_snapshot"]
            self._on_error = kwargs["on_error"]
            self.is_running = False

        def check_agent_available(self):
            return {"available": True}

        def start(self):
            self.is_running = True
            self._on_error("Simulated collector failure")

        def stop(self):
            self.is_running = False

    monkeypatch.setattr("backends.gds2.backend.AgentDataCollector", _ErroringCollector)

    try:
        harness.backend.collect_ai_payload(
            vehicle_context={
                "brand": "chevrolet",
                "model": "Simulated Malibu",
                "vin": harness.model.vin,
            },
            data_category="Engine Data",
            collection_seconds=0,
        )
    except RuntimeError as exc:
        assert str(exc) == "Simulated collector failure"
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("collect_ai_payload should fail when collector reports an error")
