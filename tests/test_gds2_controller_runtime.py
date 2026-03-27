from types import SimpleNamespace
from unittest.mock import MagicMock

from diagnostic_platform.contracts import BackendState

from backends.gds2.backend import GDS2DiagnosticBackend
from backends.gds2.controller_runtime import GDS2ControllerRuntime


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
