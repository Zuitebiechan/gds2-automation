from types import SimpleNamespace
from unittest.mock import MagicMock

from diagnostic_platform.contracts import BackendRegistry, BackendState

from backends.gds2.backend import GDS2DiagnosticBackend
from backends.gds2.controller_runtime import GDS2ControllerRuntime
from src.navigation import GDS2Page
from src.streaming.agent_data_collector import AgentSnapshot, DTCInfo


def _make_workflow():
    workflow = MagicMock()
    controller = MagicMock()
    controller.detect_current_page.return_value = SimpleNamespace(value="module_list")
    controller.get_context.return_value = {
        "module": "ECM",
        "data_category": "Engine Data",
        "sub_category": None,
        "device": "VCI Proxy (Remote)",
    }
    workflow.controller = controller
    workflow.get_state.return_value = {
        "vin": "VIN123",
        "device": "VCI Proxy (Remote)",
        "module": "ECM",
        "data_category": "Engine Data",
    }
    return workflow


def test_controller_runtime_status_includes_network_quality():
    workflow = _make_workflow()
    runtime = GDS2ControllerRuntime(
        workflow=workflow,
        snapshot_reader=lambda: {
            "connection_epoch": "epoch-1",
            "connected": True,
            "fresh": True,
            "updated_at": "2026-03-27T00:00:00Z",
            "source": "probe",
            "sample_count": 5,
            "network_ms": {"last": 95.0, "p50": 100.0, "p95": 120.0},
            "grade": "warn",
            "status": "degraded",
            "reason": "p95 above good threshold",
            "probe_failures": 0,
        },
    )

    state = runtime.status()

    assert state.current_page == "module_list"
    assert state.is_connected is True
    assert state.extra["network_quality"]["grade"] == "warn"
    assert state.extra["connection_epoch"] == "epoch-1"


def test_backend_start_delegates_to_controller_runtime():
    runtime = MagicMock()
    backend = GDS2DiagnosticBackend(runtime=runtime)

    backend.start()

    runtime.ensure_ready.assert_called_once_with()


def test_backend_get_modules_uses_cached_start_result_when_page_check_is_unstable():
    runtime = MagicMock()
    runtime.ensure_ready.return_value = {
        "modules": ["ECM", "TCM"],
        "vin": "VIN123",
        "device": "VCI Proxy (Remote)",
    }
    backend = GDS2DiagnosticBackend(runtime=runtime)
    backend._require_page = MagicMock(side_effect=RuntimeError("still at diagnostics_menu"))

    backend.start()
    modules = backend.get_modules()

    assert modules == ["ECM", "TCM"]
    backend._require_page.assert_called_once()


def test_backend_get_state_delegates_to_runtime_status():
    expected = BackendState(
        current_page="module_list",
        is_connected=True,
        current_module="ECM",
        current_data_category="Engine Data",
        extra={"connection_epoch": "epoch-1"},
    )
    runtime = MagicMock()
    runtime.status.return_value = expected
    backend = GDS2DiagnosticBackend(runtime=runtime)

    state = backend.get_state()

    assert state.current_page == "module_list"
    assert state.current_module == "ECM"
    assert state.current_data_category == "Engine Data"
    assert state.extra["connection_epoch"] == "epoch-1"
    runtime.status.assert_called_once_with()


def test_gds2_backend_brand_aliases_route_without_manual_decision():
    runtime = MagicMock()
    backend = GDS2DiagnosticBackend(runtime=runtime)
    registry = BackendRegistry()
    registry.register(backend)

    for brand in ("GM China", "gmchina", "GDS2"):
        resolution = registry.resolve_brand(brand)
        assert resolution.selected_backend_name == "gds2"
        assert resolution.decision_required is False


def test_gds2_backend_start_live_data_session_owns_agent_collector(monkeypatch):
    runtime = MagicMock()
    controller = MagicMock()
    controller.detect_current_page.return_value = GDS2Page.DATA_DISPLAY
    runtime.get_controller.return_value = controller
    runtime.get_workflow.return_value = MagicMock()
    backend = GDS2DiagnosticBackend(runtime=runtime)
    created_collectors = []

    class FakeCollector:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.is_running = False
            self.stop_calls = 0
            created_collectors.append(self)

        def start(self):
            self.is_running = True

        def stop(self):
            self.stop_calls += 1
            self.is_running = False

    monkeypatch.setattr("backends.gds2.backend.AgentDataCollector", FakeCollector)

    payload = backend.start_live_data_session(
        data_category="Engine Data",
        interval_ms=250,
        stream_scope="diagnostics",
    )

    assert payload["success"] is True
    assert payload["message"] == "Live data streaming started"
    assert payload["interval_ms"] == 250
    assert len(created_collectors) == 1
    assert created_collectors[0].kwargs["interval_ms"] == 250
    assert callable(created_collectors[0].kwargs["page_guard"])

    stop_payload = backend.stop_live_data_session()

    assert stop_payload == {"success": True, "message": "Live data stopped"}
    assert created_collectors[0].stop_calls == 1


def test_gds2_backend_collect_ai_payload_returns_platform_payload(monkeypatch):
    runtime = MagicMock()
    controller = MagicMock()
    controller.detect_current_page.return_value = GDS2Page.DATA_DISPLAY
    runtime.get_controller.return_value = controller
    runtime.get_workflow.return_value = MagicMock()
    backend = GDS2DiagnosticBackend(runtime=runtime)

    class FakeCollector:
        def __init__(self, **kwargs):
            self.on_snapshot = kwargs["on_snapshot"]
            self.on_error = kwargs["on_error"]
            self.is_running = False

        def check_agent_available(self):
            return {"available": True}

        def start(self):
            self.is_running = True
            snapshot = AgentSnapshot(
                timestamp=1,
                extraction_count=1,
                extraction_duration_ms=5,
                page_context={"page": "data_display"},
                parameters=[
                    {"name": "RPM", "value": "850", "unit": "rpm", "module": "Engine"},
                    {"name": "Coolant Temp", "value": "90", "unit": "C", "module": "Engine"},
                ],
                dtcs=[
                    DTCInfo(
                        control_module="Engine",
                        dtc_type="Current",
                        code="P0001",
                        symptom_byte="00",
                        description="Fuel Volume Regulator",
                        symptom_description="Open",
                        status="Active",
                    )
                ],
                table_count=1,
            )
            self.on_snapshot(snapshot, [])

        def stop(self):
            self.is_running = False

    monkeypatch.setattr("backends.gds2.backend.AgentDataCollector", FakeCollector)

    payload = backend.collect_ai_payload(
        vehicle_context={"brand": "chevrolet", "model": "Demo", "vin": "VIN123"},
        data_category="Engine Data",
        collection_seconds=0,
    )

    assert payload.source_backend == "gds2"
    assert payload.vehicle_context.brand == "chevrolet"
    assert payload.vehicle_context.vin == "VIN123"
    assert payload.dtcs[0].code == "P0001"
    assert [point.parameter for point in payload.live_data] == ["RPM", "Coolant Temp"]
