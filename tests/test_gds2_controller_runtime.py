import logging
from types import SimpleNamespace
from unittest.mock import MagicMock
import json
from pathlib import Path

import pytest

import backends.gds2.controller_runtime as controller_runtime_module
from diagnostic_platform.observability import flush_product_log_writers
from diagnostic_platform.action_schema import ActionStep, GDS2Action
from diagnostic_platform.contracts import BackendCapability, BackendRegistry, BackendState

from backends.gds2.action_adapter import GDS2ActionAdapter
from backends.gds2.backend import GDS2DiagnosticBackend
from backends.gds2.controller_runtime import GDS2ControllerRuntime
from backends.gds2.registry_navigation_runtime import RegistryNavigationRuntime
from src.navigation import ControllerSnapshot, GDS2Page, NavigationController, NavigationResult
from src.streaming.agent_data_collector import AgentSnapshot, DTCInfo


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


def _write_agent_payload(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _vehicle_dtc_payload(*, waiting_rows: int) -> dict[str, object]:
    rows = [
        {
            "Status": " ",
            "Control Module Name": "Engine Control Module",
            "Control Module Status": "DTCs Stored",
            "DTC Count": "30",
            "DLC Pin": "6,14",
        }
    ]
    for index in range(waiting_rows):
        rows.append(
            {
                "Status": " ",
                "Control Module Name": f"Module {index + 1}",
                "Control Module Status": "Waiting For Data...",
                "DTC Count": "",
                "DLC Pin": "1",
            }
        )
    return {
        "timestamp": 1_776_929_600_000,
        "extractionCount": 1,
        "extractionDurationMs": 1,
        "pageContext": {"page": "data_display"},
        "tables": [
            {
                "tableType": "unknown",
                "columns": [
                    "Status",
                    "Control Module Name",
                    "Control Module Status",
                    "DTC Count",
                    "DLC Pin",
                ],
                "rowCount": len(rows),
                "rows": rows,
            }
        ],
    }


def test_controller_runtime_status_includes_network_quality():
    workflow = _make_workflow()
    runtime = GDS2ControllerRuntime(
        controller=workflow.controller,
        state_reader=workflow.get_state,
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


def test_controller_runtime_preflight_skips_healthy_log(caplog):
    runtime = GDS2ControllerRuntime(
        snapshot_reader=lambda: {
            "connection_epoch": "epoch-1",
            "grade": "good",
            "status": "healthy",
            "reason": "p95 within good",
        },
    )

    with caplog.at_level(logging.WARNING, logger="backends.gds2.controller_runtime"):
        payload = runtime.preflight()

    assert payload["connection_epoch"] == "epoch-1"
    assert "[GDS2_RUNTIME] preflight" not in caplog.text


def test_controller_runtime_preflight_logs_unhealthy_status(caplog):
    runtime = GDS2ControllerRuntime(
        snapshot_reader=lambda: {
            "connection_epoch": "epoch-2",
            "grade": "warn",
            "status": "degraded",
            "reason": "p95 above good threshold",
        },
    )

    with caplog.at_level(logging.WARNING, logger="backends.gds2.controller_runtime"):
        payload = runtime.preflight()

    assert payload["connection_epoch"] == "epoch-2"
    assert "status=degraded" in caplog.text


def test_backend_start_delegates_to_controller_runtime():
    runtime = MagicMock()
    nav_runtime = MagicMock()
    nav_runtime.ensure_started.return_value = {}
    runtime.build_navigation_runtime.return_value = nav_runtime
    backend = GDS2DiagnosticBackend(runtime=runtime)

    backend.start()

    runtime.build_navigation_runtime.assert_called_once_with(source="registry_runtime")
    nav_runtime.ensure_started.assert_called_once_with()


def test_backend_get_modules_uses_cached_start_result_when_page_check_is_unstable():
    runtime = MagicMock()
    nav_runtime = MagicMock()
    nav_runtime.ensure_started.return_value = {
        "modules": ["ECM", "TCM"],
        "vin": "VIN123",
        "device": "VCI Proxy (Remote)",
    }
    runtime.build_navigation_runtime.return_value = nav_runtime
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
    runtime.build_navigation_runtime.return_value = MagicMock(
        get_runtime_status=MagicMock(return_value={"status": "idle", "runtime_source": "registry_runtime"})
    )
    backend = GDS2DiagnosticBackend(runtime=runtime)

    state = backend.get_state()

    assert state.current_page == "module_list"
    assert state.current_module == "ECM"
    assert state.current_data_category == "Engine Data"
    assert state.extra["connection_epoch"] == "epoch-1"
    assert state.extra["navigation_runtime_source"] == "registry_runtime"
    assert state.extra["navigation_runtime_status"] == {
        "status": "idle",
        "runtime_source": "registry_runtime",
    }
    runtime.status.assert_called_once_with()


def test_action_adapter_builds_ui_state_from_raw_controller_snapshot():
    class _Controller:
        def get_controller_snapshot(self, *, capture_mode: str):
            assert capture_mode == "action_adapter_ui_state"
            return ControllerSnapshot(
                raw_page_id="data_list",
                buttons=("Back", "Enter"),
                list_items=("Engine Data",),
                context={"module": "Engine Control Module"},
                capture_source="test",
                capture_mode=capture_mode,
            )

        def detect_current_page(self):  # pragma: no cover - should not be used
            raise AssertionError("adapter should use get_controller_snapshot first")

    adapter = GDS2ActionAdapter(backend=object(), controller=_Controller())

    state = adapter.get_current_ui_state()

    assert state.current_page == "data_list"
    assert state.visible_buttons == ["Back", "Enter"]
    assert state.list_items == ["Engine Data"]
    assert state.context == {"module": "Engine Control Module"}


def test_action_adapter_bridges_legacy_controller_snapshot_methods():
    class _Controller:
        def detect_current_page(self):
            return SimpleNamespace(value="module_list")

        def get_visible_buttons(self):
            return [{"text": "Back"}, "Enter"]

        def get_list_items(self):
            return ["Engine Control Module"]

        def get_context(self):
            return {"module": None}

    adapter = GDS2ActionAdapter(backend=object(), controller=_Controller())

    state = adapter.get_current_ui_state()

    assert state.current_page == "module_list"
    assert state.visible_buttons == ["Back", "Enter"]
    assert state.list_items == ["Engine Control Module"]
    assert state.context == {"module": None}


def test_action_adapter_navigation_handlers_use_internal_command_seam():
    class _Controller:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def go_home(self):
            self.calls.append("go_home")
            return NavigationResult(success=True, page=GDS2Page.MAIN_MENU)

        def go_back(self):
            self.calls.append("go_back")
            return NavigationResult(
                success=True,
                page=GDS2Page.MODULE_LIST,
                error=None,
            )

    controller = _Controller()
    adapter = GDS2ActionAdapter(backend=object(), controller=controller)

    home_result = adapter._handle_go_home(ActionStep(GDS2Action.GO_HOME), object())
    back_result = adapter._handle_go_back(ActionStep(GDS2Action.GO_BACK), object())

    assert controller.calls == ["go_home", "go_back"]
    assert home_result == {"success": True, "page": "main_menu", "error": None}
    assert back_result == {"success": True, "page": "module_list", "error": None}


def test_action_adapter_select_handlers_use_navigation_command_seam():
    class _Controller:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def select_device(self, device_name: str):
            self.calls.append(("select_device", device_name))
            return NavigationResult(
                success=True,
                page=GDS2Page.VEHICLE_SELECTION,
                selected=device_name,
            )

        def select_sub_category(self, sub_category_name: str):
            self.calls.append(("select_sub_category", sub_category_name))
            return NavigationResult(
                success=True,
                page=GDS2Page.DATA_DISPLAY,
                selected=sub_category_name,
            )

    controller = _Controller()
    adapter = GDS2ActionAdapter(backend=object(), controller=controller)

    device_result = adapter._handle_select_device(
        ActionStep(GDS2Action.SELECT_DEVICE, args={"device_name": "VCI Proxy"}),
        object(),
    )
    sub_category_result = adapter._handle_select_sub_category(
        ActionStep(GDS2Action.SELECT_SUB_CATEGORY, args={"sub_category_name": "Fuel Trim"}),
        object(),
    )

    assert controller.calls == [
        ("select_device", "VCI Proxy"),
        ("select_sub_category", "Fuel Trim"),
    ]
    assert device_result == {
        "success": True,
        "page": "vehicle_selection",
        "selected": "VCI Proxy",
        "error": None,
    }
    assert sub_category_result == {
        "success": True,
        "page": "data_display",
        "selected": "Fuel Trim",
        "error": None,
    }


def test_action_adapter_abort_reuses_navigation_command_seam():
    class _Backend:
        def __init__(self) -> None:
            self.stopped = False

        def stop_live_data_session(self) -> None:
            self.stopped = True

    class _Controller:
        def __init__(self) -> None:
            self.go_home_calls = 0

        def go_home(self):
            self.go_home_calls += 1
            return NavigationResult(success=False, page=GDS2Page.MAIN_MENU, error="ignored")

    backend = _Backend()
    controller = _Controller()
    adapter = GDS2ActionAdapter(backend=backend, controller=controller)

    result = adapter._handle_abort_session(ActionStep(GDS2Action.ABORT_SESSION), object())

    assert backend.stopped is True
    assert controller.go_home_calls == 1
    assert result == {"success": True, "page": "main_menu", "aborted": True}


def test_navigation_controller_set_current_page_replaces_private_write() -> None:
    controller = NavigationController(nav=MagicMock())

    page = controller.set_current_page(GDS2Page.DATA_LIST)

    assert page == GDS2Page.DATA_LIST
    assert controller.current_page == GDS2Page.DATA_LIST
    assert controller.history == []


def test_controller_runtime_builds_registry_navigation_runtime(monkeypatch):
    workflow = _make_workflow()
    created = {}

    monkeypatch.setattr(
        controller_runtime_module,
        "load_or_rebuild_graph",
        lambda _path: {"version": 1, "nodes": {}, "edges": []},
    )

    class _Connection:
        def __enter__(self):
            return object()

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(controller_runtime_module, "connect_registry", lambda _path: _Connection())
    monkeypatch.setattr(
        controller_runtime_module,
        "list_entries",
        lambda _connection: [{"page_key": "dtc.clear.execute", "aliases": []}],
    )
    monkeypatch.setattr(controller_runtime_module, "list_page_states", lambda _connection: [])
    monkeypatch.setattr(controller_runtime_module, "list_recovery_policies", lambda _connection: [])
    monkeypatch.setattr(controller_runtime_module, "lookup_recovery_policy", lambda _connection, _key: None)
    monkeypatch.setattr(
        controller_runtime_module,
        "registry_requires_rebuild",
        lambda **_kwargs: False,
    )
    monkeypatch.setattr(
        controller_runtime_module,
        "DEFAULT_REGISTRY_PATH",
        type("_Path", (), {"exists": lambda self: True})(),
    )

    def fake_registry_runtime(**kwargs):
        created.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(controller_runtime_module, "RegistryNavigationRuntime", fake_registry_runtime)

    runtime = GDS2ControllerRuntime(
        controller=workflow.controller,
        state_reader=workflow.get_state,
    )

    nav_runtime = runtime.build_navigation_runtime(source="registry_runtime")

    assert nav_runtime is not None
    assert created["entries"] == [{"page_key": "dtc.clear.execute", "aliases": []}]
    assert callable(created["state_reader"])
    assert created["default_device_name"] == "VCI Proxy (Remote)"
    assert "recovery_coordinator_factory" not in created
    assert "route_executor_factory" not in created
    assert "clear_dtc_flow_factory" not in created
    assert "status_recorder" not in created


def test_controller_runtime_rebuilds_stale_registry_before_loading(monkeypatch):
    workflow = _make_workflow()
    rebuild_calls = []

    monkeypatch.setattr(
        controller_runtime_module,
        "load_or_rebuild_graph",
        lambda _path: {"version": 1, "nodes": {}, "edges": []},
    )

    class _Connection:
        def __enter__(self):
            return object()

        def __exit__(self, exc_type, exc, tb):
            return False

    registry_path = Path("data/test_registry.sqlite")
    monkeypatch.setattr(
        controller_runtime_module,
        "registry_requires_rebuild",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        controller_runtime_module,
        "rebuild_registry_database",
        lambda **kwargs: rebuild_calls.append(kwargs["output_path"]),
    )
    monkeypatch.setattr(controller_runtime_module, "connect_registry", lambda _path: _Connection())
    monkeypatch.setattr(controller_runtime_module, "list_entries", lambda _connection: [])
    monkeypatch.setattr(controller_runtime_module, "list_page_states", lambda _connection: [])
    monkeypatch.setattr(controller_runtime_module, "list_recovery_policies", lambda _connection: [])
    monkeypatch.setattr(controller_runtime_module, "lookup_recovery_policy", lambda _connection, _key: None)
    monkeypatch.setattr(controller_runtime_module, "DEFAULT_REGISTRY_PATH", registry_path)
    monkeypatch.setattr(controller_runtime_module, "RegistryNavigationRuntime", lambda **kwargs: MagicMock())

    runtime = GDS2ControllerRuntime(
        controller=workflow.controller,
        state_reader=workflow.get_state,
    )

    runtime.build_navigation_runtime(source="registry_runtime")

    assert rebuild_calls == [registry_path]


def test_gds2_backend_clear_dtcs_delegates_to_workflow():
    runtime = MagicMock()
    runtime.status.return_value = BackendState(
        current_page="module_list",
        is_connected=True,
        current_module="ECM",
        current_data_category="Engine Data",
        extra={},
    )
    registry_runtime = MagicMock()
    registry_runtime.clear_dtcs.return_value = {
        "success": True,
        "cleared_count": 5,
        "message": "Clear DTCs completed",
        "page_context": "data_display",
    }
    registry_runtime.get_runtime_status.return_value = {
        "status": "idle",
        "runtime_source": "registry_runtime",
    }
    runtime.build_navigation_runtime.return_value = registry_runtime
    backend = GDS2DiagnosticBackend(runtime=runtime)

    result = backend.clear_dtcs()

    assert BackendCapability.CLEAR_DTCS in backend.capabilities
    assert result.success is True
    assert result.cleared_count == 5
    assert result.message == "Clear DTCs completed"
    assert runtime.build_navigation_runtime.call_count >= 1
    registry_runtime.clear_dtcs.assert_called_once_with()


def test_backend_navigation_methods_delegate_via_navigation_runtime_seam():
    runtime = MagicMock()
    controller = MagicMock()
    controller.detect_current_page.return_value = SimpleNamespace(value="module_list")
    runtime.get_controller.return_value = controller
    nav_runtime = MagicMock(
        select_module=MagicMock(return_value={"data_categories": ["Engine Data"]}),
        select_data_category=MagicMock(return_value={"sub_categories": ["Fuel Trim"]}),
        connect_vci=MagicMock(),
    )
    runtime.build_navigation_runtime.return_value = nav_runtime

    backend = GDS2DiagnosticBackend(runtime=runtime)

    backend.connect_vci("SM2 USB")
    backend.select_module("ECM")
    categories = backend.select_data_category("Engine Data")
    page = backend.detect_current_page()
    backend.go_back()

    assert categories == ["Fuel Trim"]
    assert page == "module_list"
    runtime.build_navigation_runtime.assert_any_call(source="registry_runtime")
    nav_runtime.connect_vci.assert_called_once_with("SM2 USB")
    nav_runtime.select_module.assert_called_once_with("ECM")
    nav_runtime.select_data_category.assert_called_once_with("Engine Data")
    controller.detect_current_page.assert_called_once_with()
    controller.go_back.assert_called_once_with()


def test_backend_read_dtcs_uses_controller_runtime_snapshot() -> None:
    runtime = MagicMock()
    controller = MagicMock()
    controller.current_module = "ECM"
    runtime.get_controller.return_value = controller
    runtime.read_all_dtcs.return_value = {
        "dtcs": [
            {
                "code": "P0001",
                "control_module": "Engine",
                "status": "Active",
                "description": "Fuel Volume Regulator",
            }
        ]
    }
    backend = GDS2DiagnosticBackend(runtime=runtime)

    dtcs = backend.read_dtcs()

    assert [dtc.code for dtc in dtcs] == ["P0001"]
    runtime.read_all_dtcs.assert_called_once_with()


def test_controller_runtime_status_exposes_vehicle_dtc_loading_state(tmp_path, monkeypatch) -> None:
    workflow = _make_workflow()
    workflow.controller.detect_current_page.return_value = GDS2Page.DATA_DISPLAY
    latest_json = tmp_path / "gds2-data" / "latest.json"
    _write_agent_payload(latest_json, _vehicle_dtc_payload(waiting_rows=2))
    monkeypatch.setattr(controller_runtime_module.Path, "home", lambda: tmp_path)

    runtime = GDS2ControllerRuntime(
        controller=workflow.controller,
        state_reader=workflow.get_state,
    )

    state = runtime.status()
    status = state.extra["vehicle_dtc_status"]

    assert status["applicable"] is True
    assert status["ready"] is False
    assert status["waiting_for_data_count"] == 2


def test_controller_runtime_read_all_dtcs_rejects_vehicle_dtc_while_loading(tmp_path, monkeypatch) -> None:
    latest_json = tmp_path / "latest.json"
    _write_agent_payload(latest_json, _vehicle_dtc_payload(waiting_rows=1))
    controller = MagicMock()
    controller.detect_current_page.return_value = GDS2Page.DATA_DISPLAY
    controller.get_context.return_value = {}

    class FakeCollector:
        def __init__(self):
            self.json_path = latest_json

        def check_agent_available(self):
            return {"available": True}

    monkeypatch.setattr(controller_runtime_module, "AgentDataCollector", FakeCollector)

    runtime = GDS2ControllerRuntime(controller=controller)

    with pytest.raises(RuntimeError, match="still loading"):
        runtime.read_all_dtcs()


def test_controller_runtime_read_all_dtcs_accepts_ready_vehicle_dtc_snapshot(tmp_path, monkeypatch) -> None:
    latest_json = tmp_path / "latest.json"
    _write_agent_payload(latest_json, _vehicle_dtc_payload(waiting_rows=0))
    controller = MagicMock()
    controller.detect_current_page.return_value = GDS2Page.DATA_DISPLAY
    controller.get_context.return_value = {"data_category": "Vehicle DTC Information"}

    class FakeCollector:
        def __init__(self):
            self.json_path = latest_json

        def check_agent_available(self):
            return {"available": True}

    monkeypatch.setattr(controller_runtime_module, "AgentDataCollector", FakeCollector)

    runtime = GDS2ControllerRuntime(controller=controller)
    result = runtime.read_all_dtcs()

    assert result["dtc_count"] == 30
    assert result["dtc_display_mode"] == "vehicle_summary"
    assert result["dtcs"][0]["control_module"] == "Engine Control Module"
    assert result["dtcs"][0]["code"] == "30"
    assert result["vehicle_dtc_status"]["ready"] is True


def test_gds2_backend_clear_dtcs_waits_for_vehicle_dtc_table_ready() -> None:
    runtime = MagicMock()
    runtime.status.return_value = BackendState(
        current_page="data_display",
        is_connected=True,
        current_module=None,
        current_data_category="Vehicle DTC Information",
        extra={
            "vehicle_dtc_status": {
                "applicable": True,
                "ready": False,
                "message": "Vehicle DTC Information is still loading.",
            }
        },
    )
    registry_runtime = MagicMock(
        get_runtime_status=MagicMock(return_value={"status": "idle", "runtime_source": "registry_runtime"})
    )
    runtime.build_navigation_runtime.return_value = registry_runtime
    backend = GDS2DiagnosticBackend(runtime=runtime)

    with pytest.raises(RuntimeError, match="still loading"):
        backend.clear_dtcs()

    registry_runtime.clear_dtcs.assert_not_called()


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


def test_gds2_backend_data_display_guard_waits_through_loading_page() -> None:
    runtime = MagicMock()
    controller = MagicMock()
    controller.detect_current_page.return_value = GDS2Page.LOADING
    runtime.get_controller.return_value = controller
    runtime.get_workflow.return_value = MagicMock()
    recovery_runtime = MagicMock()
    recovery_runtime._loading_timeout_sec = 20.0
    recovery_runtime._max_loading_restarts = 1
    recovery_runtime.recover_data_display.return_value = {
        "ok": True,
        "mode": "stream",
        "message": "Waiting for GDS2 loading page to finish...",
    }
    runtime.build_navigation_runtime.return_value = recovery_runtime
    backend = GDS2DiagnosticBackend(runtime=runtime)

    guard = backend.build_data_display_guard(
        data_category="Engine Data",
        mode="stream",
        check_interval=0.0,
    )

    assert guard() == {
        "ok": True,
        "mode": "stream",
        "message": "Waiting for GDS2 loading page to finish...",
    }
    runtime.build_navigation_runtime.assert_called_once_with(source="registry_runtime")
    recovery_runtime.recover_data_display.assert_called_once()


def test_gds2_backend_data_display_guard_recovers_stream_after_disconnect() -> None:
    runtime = MagicMock()
    controller = MagicMock()
    controller.detect_current_page.return_value = GDS2Page.J2534_DISCONNECT
    runtime.get_controller.return_value = controller
    runtime.get_workflow.return_value = MagicMock()
    recovery_runtime = MagicMock()
    recovery_runtime._loading_timeout_sec = 20.0
    recovery_runtime._max_loading_restarts = 1
    recovery_runtime.recover_data_display.return_value = {
        "ok": True,
        "mode": "stream",
        "recovered": True,
        "recovery_method": "in_place",
        "restart_collection": False,
        "message": "Recovered Data Display after J2534 disconnect.",
    }
    runtime.build_navigation_runtime.return_value = recovery_runtime
    backend = GDS2DiagnosticBackend(runtime=runtime)

    guard = backend.build_data_display_guard(
        data_category="Engine Data",
        mode="stream",
        check_interval=0.0,
    )

    assert guard() == {
        "ok": True,
        "mode": "stream",
        "recovered": True,
        "recovery_method": "in_place",
        "restart_collection": False,
        "message": "Recovered Data Display after J2534 disconnect.",
    }
    recovery_runtime.recover_data_display.assert_called_once()


def test_gds2_backend_data_display_guard_emits_guard_and_recovery_events(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    runtime = MagicMock()
    controller = MagicMock()
    controller.detect_current_page.return_value = GDS2Page.J2534_DISCONNECT
    runtime.get_controller.return_value = controller
    runtime.get_workflow.return_value = MagicMock()
    recovery_runtime = MagicMock()
    recovery_runtime._loading_timeout_sec = 20.0
    recovery_runtime._max_loading_restarts = 1
    recovery_runtime.recover_data_display.return_value = {
        "ok": True,
        "mode": "stream",
        "recovered": True,
        "recovery_method": "in_place",
        "restart_collection": False,
        "message": "Recovered Data Display after J2534 disconnect.",
    }
    runtime.build_navigation_runtime.return_value = recovery_runtime
    backend = GDS2DiagnosticBackend(runtime=runtime)

    guard = backend.build_data_display_guard(
        data_category="Engine Data",
        mode="stream",
        check_interval=0.0,
    )

    result = guard()

    assert result["ok"] is True
    events = _read_cloud_events(tmp_path)
    event_types = [event["event_type"] for event in events]
    assert "page_guard_triggered" in event_types
    assert "j2534_disconnect_page_seen" in event_types


def test_registry_runtime_recover_data_display_emits_success_event(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    route_navigator = MagicMock()
    route_navigator.capture_settled_snapshot.return_value = {"effective_page_id": "j2534_disconnect"}
    route_navigator.graph = {}
    runtime = RegistryNavigationRuntime(
        controller=MagicMock(),
        route_navigator=route_navigator,
        entries=[{"page_key": "Engine Data", "aliases": []}],
    )
    runtime.execute_registry_route = MagicMock(
        return_value={"final_page": "data_display", "recovery_actions": [{"action": "backtrack"}]}
    )

    result = runtime.recover_data_display(data_category="Engine Data", mode="stream")

    assert result["ok"] is True
    events = _read_cloud_events(tmp_path)
    assert "recovery_attempted" in [event["event_type"] for event in events]
    assert "recovery_succeeded" in [event["event_type"] for event in events]


def test_registry_runtime_recover_data_display_emits_failure_event(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    route_navigator = MagicMock()
    route_navigator.capture_settled_snapshot.return_value = {"effective_page_id": "j2534_disconnect"}
    route_navigator.graph = {}
    runtime = RegistryNavigationRuntime(
        controller=MagicMock(),
        route_navigator=route_navigator,
        entries=[{"page_key": "Engine Data", "aliases": []}],
    )
    runtime.execute_registry_route = MagicMock(side_effect=RuntimeError("route boom"))

    result = runtime.recover_data_display(data_category="Engine Data", mode="stream")

    assert result["ok"] is False
    events = _read_cloud_events(tmp_path)
    assert "recovery_failed" in [event["event_type"] for event in events]


def test_gds2_backend_data_display_guard_requests_ai_restart_after_backtrack_recovery() -> None:
    runtime = MagicMock()
    controller = MagicMock()
    controller.detect_current_page.return_value = GDS2Page.J2534_DISCONNECT
    runtime.get_controller.return_value = controller
    runtime.get_workflow.return_value = MagicMock()
    recovery_runtime = MagicMock()
    recovery_runtime._loading_timeout_sec = 20.0
    recovery_runtime._max_loading_restarts = 1
    recovery_runtime.recover_data_display.return_value = {
        "ok": True,
        "mode": "ai_collect",
        "recovered": True,
        "recovery_method": "backtrack",
        "restart_collection": True,
        "message": "Recovered Data Display after reconnect; restarting AI collection window.",
    }
    runtime.build_navigation_runtime.return_value = recovery_runtime
    backend = GDS2DiagnosticBackend(runtime=runtime)

    guard = backend.build_data_display_guard(
        data_category="Engine Data",
        mode="ai_collect",
        check_interval=0.0,
    )

    assert guard() == {
        "ok": True,
        "mode": "ai_collect",
        "recovered": True,
        "recovery_method": "backtrack",
        "restart_collection": True,
        "message": "Recovered Data Display after reconnect; restarting AI collection window.",
    }
    recovery_runtime.recover_data_display.assert_called_once()


def test_gds2_backend_data_display_guard_fails_ai_collection_when_recovery_fails() -> None:
    runtime = MagicMock()
    controller = MagicMock()
    controller.detect_current_page.return_value = GDS2Page.J2534_DISCONNECT
    runtime.get_controller.return_value = controller
    runtime.get_workflow.return_value = MagicMock()
    recovery_runtime = MagicMock()
    recovery_runtime._loading_timeout_sec = 20.0
    recovery_runtime._max_loading_restarts = 1
    recovery_runtime.recover_data_display.return_value = {
        "ok": False,
        "mode": "ai_collect",
        "error": (
            "Lost communication with J2534 during AI collection and could not "
            "restore Data Display in-place. Please reconnect and restart AI "
            "Diagnostics."
        ),
    }
    runtime.build_navigation_runtime.return_value = recovery_runtime
    backend = GDS2DiagnosticBackend(runtime=runtime)

    guard = backend.build_data_display_guard(
        data_category="Engine Data",
        mode="ai_collect",
        check_interval=0.0,
    )

    assert guard() == {
        "ok": False,
        "mode": "ai_collect",
        "error": (
            "Lost communication with J2534 during AI collection and could not "
            "restore Data Display in-place. Please reconnect and restart AI "
            "Diagnostics."
        ),
    }
    recovery_runtime.recover_data_display.assert_called_once()


def test_navigation_controller_maps_clear_dtcs_agent_page_ids() -> None:
    class _AgentNav:
        def __init__(self, page_id: str) -> None:
            self.page_id = page_id

        def get_page_id(self):
            return {"page_id": self.page_id, "confidence": "high"}

        def get_buttons(self):
            return [{"text": "OK", "enabled": True}]

        def get_list_items(self, list_index: int = 0):
            return []

    selection = NavigationController(nav=_AgentNav("clear_dtcs_selection"))
    confirmation = NavigationController(nav=_AgentNav("clear_dtcs_confirmation"))

    assert selection.detect_current_page(retries=0) == GDS2Page.CLEAR_DTCS_SELECTION
    assert confirmation.detect_current_page(retries=0) == GDS2Page.CLEAR_DTCS_CONFIRMATION


def test_navigation_controller_detects_clear_dtcs_pages_via_heuristic_fallback() -> None:
    class _HeuristicNav:
        def __init__(self, buttons, items) -> None:
            self._buttons = buttons
            self._items = items

        def get_page_id(self):
            return None

        def get_buttons(self):
            return self._buttons

        def get_list_items(self, list_index: int = 0):
            return list(self._items)

    selection = NavigationController(
        nav=_HeuristicNav(
            buttons=[
                {"text": "Add All", "enabled": True},
                {"text": "Add", "enabled": True},
                {"text": "Cancel", "enabled": True},
                {"text": "Back", "enabled": True},
            ],
            items=["Engine Control Module"],
        )
    )
    confirmation = NavigationController(
        nav=_HeuristicNav(
            buttons=[
                {"text": "OK", "enabled": True},
                {"text": "Cancel", "enabled": True},
                {"text": "Clear Records", "enabled": True},
                {"text": "Back", "enabled": True},
            ],
            items=[],
        )
    )

    assert selection.detect_current_page(retries=0) == GDS2Page.CLEAR_DTCS_SELECTION
    assert confirmation.detect_current_page(retries=0) == GDS2Page.CLEAR_DTCS_CONFIRMATION


def test_navigation_controller_detects_clear_dtcs_selection_without_list_items() -> None:
    class _HeuristicNav:
        def get_page_id(self):
            return None

        def get_buttons(self):
            return [
                {"text": "Add All", "enabled": True},
                {"text": "Add", "enabled": True},
                {"text": "Cancel", "enabled": True},
                {"text": "Add Bookmark", "enabled": True},
                {"text": "Back", "enabled": True},
            ]

        def get_list_items(self, list_index: int = 0):
            return []

    controller = NavigationController(nav=_HeuristicNav())

    assert controller.detect_current_page(retries=0) == GDS2Page.CLEAR_DTCS_SELECTION


def test_navigation_controller_treats_ambiguous_back_only_empty_list_as_loading() -> None:
    class _HeuristicNav:
        def get_page_id(self):
            return None

        def get_buttons(self):
            return [
                {"text": "Back", "enabled": True},
            ]

        def get_list_items(self, list_index: int = 0):
            return []

    controller = NavigationController(nav=_HeuristicNav())
    controller._current_page = GDS2Page.MAIN_MENU

    assert controller.detect_current_page(retries=0) == GDS2Page.LOADING


def test_navigation_controller_treats_empty_buttons_and_list_as_loading() -> None:
    class _HeuristicNav:
        def get_page_id(self):
            return None

        def get_buttons(self):
            return []

        def get_list_items(self, list_index: int = 0):
            return []

    controller = NavigationController(nav=_HeuristicNav())

    assert controller.detect_current_page(retries=0) == GDS2Page.LOADING


def test_navigation_controller_treats_back_only_empty_list_as_loading_from_deep_context() -> None:
    class _HeuristicNav:
        def get_page_id(self):
            return None

        def get_buttons(self):
            return [
                {"text": "Back", "enabled": True},
            ]

        def get_list_items(self, list_index: int = 0):
            return []

    controller = NavigationController(nav=_HeuristicNav())
    controller._current_page = GDS2Page.DATA_DISPLAY

    assert controller.detect_current_page(retries=0) == GDS2Page.LOADING


def test_navigation_controller_treats_enter_with_back_and_empty_list_as_loading() -> None:
    class _HeuristicNav:
        def get_page_id(self):
            return None

        def get_buttons(self):
            return [
                {"text": "Enter", "enabled": True},
                {"text": "Back", "enabled": True},
            ]

        def get_list_items(self, list_index: int = 0):
            return []

    controller = NavigationController(nav=_HeuristicNav())

    assert controller.detect_current_page(retries=0) == GDS2Page.LOADING


def test_navigation_controller_detects_vehicle_selection_without_deep_page_buttons() -> None:
    class _HeuristicNav:
        def get_page_id(self):
            return None

        def get_buttons(self):
            return [
                {"text": "Disconnect", "enabled": True},
                {"text": "Select Device", "enabled": True},
            ]

        def get_list_items(self, list_index: int = 0):
            return []

    controller = NavigationController(nav=_HeuristicNav())

    assert controller.detect_current_page(retries=0) == GDS2Page.VEHICLE_SELECTION


def test_navigation_controller_detects_loading_when_buttons_and_items_are_empty() -> None:
    class _HeuristicNav:
        def get_page_id(self):
            return None

        def get_buttons(self):
            return []

        def get_list_items(self, list_index: int = 0):
            return []

    controller = NavigationController(nav=_HeuristicNav())

    assert controller.detect_current_page(retries=0) == GDS2Page.LOADING


def test_navigation_controller_detects_loading_for_enter_with_deep_page_buttons() -> None:
    class _HeuristicNav:
        def get_page_id(self):
            return None

        def get_buttons(self):
            return [
                {"text": "Enter", "enabled": True},
                {"text": "Back", "enabled": True},
            ]

        def get_list_items(self, list_index: int = 0):
            return []

    controller = NavigationController(nav=_HeuristicNav())

    assert controller.detect_current_page(retries=0) == GDS2Page.LOADING


def test_navigation_controller_detects_loading_from_back_only_state_with_deep_context() -> None:
    class _HeuristicNav:
        def get_page_id(self):
            return None

        def get_buttons(self):
            return [{"text": "Back", "enabled": True}]

        def get_list_items(self, list_index: int = 0):
            return []

    controller = NavigationController(nav=_HeuristicNav())
    controller._current_page = GDS2Page.DATA_DISPLAY

    assert controller.detect_current_page(retries=0) == GDS2Page.LOADING


def test_navigation_controller_detects_disconnect_from_back_only_state_when_ok_present() -> None:
    class _HeuristicNav:
        def get_page_id(self):
            return None

        def get_buttons(self):
            return [
                {"text": "Back", "enabled": True},
                {"text": "OK", "enabled": True},
            ]

        def get_list_items(self, list_index: int = 0):
            return []

    controller = NavigationController(nav=_HeuristicNav())
    controller._current_page = GDS2Page.DATA_DISPLAY

    assert controller.detect_current_page(retries=0) == GDS2Page.J2534_DISCONNECT


def test_navigation_controller_detects_loading_from_back_only_state_without_deep_context() -> None:
    class _HeuristicNav:
        def get_page_id(self):
            return None

        def get_buttons(self):
            return [{"text": "Back", "enabled": True}]

        def get_list_items(self, list_index: int = 0):
            return []

    controller = NavigationController(nav=_HeuristicNav())
    controller._current_page = GDS2Page.VEHICLE_SELECTION

    assert controller.detect_current_page(retries=0) == GDS2Page.LOADING


def test_navigation_controller_click_enter_retries_vehicle_selection() -> None:
    class _EnterNav:
        def __init__(self) -> None:
            self.clicks: list[str] = []

        def click_button(self, text: str) -> dict[str, object]:
            self.clicks.append(text)
            return {"success": True}

    nav = _EnterNav()
    controller = NavigationController(nav=nav)
    controller._current_page = GDS2Page.VEHICLE_SELECTION

    transitions = iter([GDS2Page.VEHICLE_SELECTION, GDS2Page.DIAGNOSTICS_MENU])
    dismiss_calls: list[str] = []

    controller.wait_for_page_transition = lambda old_page, timeout: next(transitions)
    controller.dismiss_warning_dialog = lambda: dismiss_calls.append("dismiss") or False
    controller.get_list_items = lambda list_index=0: ["Module Diagnostics"]

    result = controller.click_enter()

    assert nav.clicks == ["Enter", "Enter"]
    assert dismiss_calls == ["dismiss", "dismiss"]
    assert result.success is True
    assert result.page == GDS2Page.DIAGNOSTICS_MENU
    assert result.choices == ["Module Diagnostics"]


def test_navigation_controller_click_enter_returns_to_diagnostics_menu_after_auto_skip() -> None:
    class _EnterNav:
        def __init__(self) -> None:
            self.clicks: list[str] = []

        def click_button(self, text: str) -> dict[str, object]:
            self.clicks.append(text)
            return {"success": True}

    nav = _EnterNav()
    controller = NavigationController(nav=nav)
    controller._current_page = GDS2Page.VEHICLE_SELECTION

    transitions = iter([GDS2Page.MODULE_LIST, GDS2Page.DIAGNOSTICS_MENU])
    controller.wait_for_page_transition = lambda old_page, timeout: next(transitions)
    controller.dismiss_warning_dialog = lambda: False
    controller.get_list_items = lambda list_index=0: ["Module Diagnostics"]

    result = controller.click_enter()

    assert nav.clicks == ["Enter", "Back"]
    assert result.success is True
    assert result.page == GDS2Page.DIAGNOSTICS_MENU
    assert result.choices == ["Module Diagnostics"]


def test_navigation_controller_select_data_category_records_sub_list_context() -> None:
    class _CategoryNav:
        def __init__(self) -> None:
            self.selections: list[tuple[int, int, bool]] = []

        def select_list_item(
            self,
            list_index: int,
            target_index: int,
            *,
            double_click: bool,
        ) -> dict[str, object]:
            self.selections.append((list_index, target_index, double_click))
            return {"success": True}

        def get_list_items(self, list_index: int = 0) -> list[str]:
            return ["Fuel System", "Ignition"]

    nav = _CategoryNav()
    controller = NavigationController(nav=nav)
    controller._current_page = GDS2Page.DATA_LIST
    controller.wait_for_list = lambda list_index=0, max_attempts=15, previous_items=None: [
        "Engine Data",
        "Transmission Data",
    ]
    controller.wait_for_page_transition = lambda old_page, timeout: GDS2Page.UNKNOWN

    result = controller.select_data_category("Engine")

    assert nav.selections == [(0, 0, True)]
    assert result.success is True
    assert result.page == GDS2Page.SUB_DATA_LIST
    assert result.selected == "Engine Data"
    assert result.choices == ["Fuel System", "Ignition"]
    assert controller.current_page == GDS2Page.SUB_DATA_LIST
    assert controller.current_data_category == "Engine Data"
    assert controller.current_sub_category is None
    assert controller.history == [GDS2Page.DATA_LIST]


def test_navigation_controller_select_sub_category_updates_history_and_context() -> None:
    class _SubCategoryNav:
        def __init__(self) -> None:
            self.selections: list[tuple[int, int, bool]] = []

        def select_list_item(
            self,
            list_index: int,
            target_index: int,
            *,
            double_click: bool,
        ) -> dict[str, object]:
            self.selections.append((list_index, target_index, double_click))
            return {"success": True}

    nav = _SubCategoryNav()
    controller = NavigationController(nav=nav)
    controller._current_page = GDS2Page.SUB_DATA_LIST
    controller.set_context(data_category="Engine Data")
    controller.wait_for_list = lambda list_index=0, max_attempts=15, previous_items=None: [
        "Fuel System",
        "Misfire Data",
    ]
    controller.wait_for_page_transition = lambda old_page, timeout: GDS2Page.DATA_DISPLAY

    result = controller.select_sub_category("Fuel")

    assert nav.selections == [(0, 0, True)]
    assert result.success is True
    assert result.page == GDS2Page.DATA_DISPLAY
    assert result.selected == "Fuel System"
    assert controller.current_page == GDS2Page.DATA_DISPLAY
    assert controller.current_data_category == "Engine Data"
    assert controller.current_sub_category == "Fuel System"
    assert controller.history == [GDS2Page.SUB_DATA_LIST]


def test_navigation_controller_recover_data_display_connection_uses_soft_ok() -> None:
    class _RecoveryNav:
        def __init__(self) -> None:
            self.clicks: list[str] = []

        def click_button(self, text: str) -> dict[str, object]:
            self.clicks.append(text)
            return {"success": True}

    nav = _RecoveryNav()
    controller = NavigationController(nav=nav)
    controller.detect_current_page = lambda retries=0: GDS2Page.J2534_DISCONNECT
    controller.wait_for_page_transition = lambda old_page, timeout: GDS2Page.DATA_DISPLAY
    controller.get_available_buttons = lambda: {"OK": True}

    result = controller.recover_data_display_connection(
        data_category="Engine Data",
        soft_retry_attempts=1,
        retry_delays=[0.0],
    )

    assert nav.clicks == ["OK"]
    assert result.success is True
    assert result.page == GDS2Page.DATA_DISPLAY
    assert result.context["recovery_method"] == "soft_ok"


def test_navigation_controller_recover_data_display_connection_backtracks_when_ok_unavailable() -> None:
    controller = NavigationController(nav=MagicMock())
    controller.detect_current_page = lambda retries=0: GDS2Page.J2534_DISCONNECT
    controller.get_available_buttons = lambda: {"OK": False}

    back_calls: list[str] = []
    select_calls: list[str] = []

    def _go_back() -> NavigationResult:
        back_calls.append("back")
        return NavigationResult(success=True, page=GDS2Page.DATA_LIST, context={})

    def _select_data_category(category: str) -> NavigationResult:
        select_calls.append(category)
        return NavigationResult(
            success=True,
            page=GDS2Page.DATA_DISPLAY,
            selected=category,
            context={"data_category": category},
        )

    controller.go_back = _go_back
    controller.select_data_category = _select_data_category

    result = controller.recover_data_display_connection(
        data_category="Engine Data",
        allow_backtrack=True,
        backtrack_attempts=1,
    )

    assert back_calls == ["back"]
    assert select_calls == ["Engine Data"]
    assert result.success is True
    assert result.page == GDS2Page.DATA_DISPLAY
    assert result.context["recovery_method"] == "backtrack"


def test_navigation_controller_recover_data_display_connection_accepts_direct_data_display_after_back() -> None:
    controller = NavigationController(nav=MagicMock())
    controller.detect_current_page = lambda retries=0: GDS2Page.J2534_DISCONNECT
    controller.get_available_buttons = lambda: {"OK": False}

    back_calls: list[str] = []

    def _go_back() -> NavigationResult:
        back_calls.append("back")
        return NavigationResult(success=True, page=GDS2Page.DATA_DISPLAY, context={})

    controller.go_back = _go_back
    controller.select_data_category = MagicMock()

    result = controller.recover_data_display_connection(
        data_category="Engine Data",
        allow_backtrack=True,
        backtrack_attempts=1,
    )

    assert back_calls == ["back"]
    controller.select_data_category.assert_not_called()
    assert result.success is True
    assert result.page == GDS2Page.DATA_DISPLAY
    assert result.context["recovery_method"] == "backtrack"


def test_navigation_controller_recover_data_display_connection_waits_out_loading_after_back() -> None:
    controller = NavigationController(nav=MagicMock())
    controller.detect_current_page = lambda retries=0: GDS2Page.J2534_DISCONNECT
    controller.get_available_buttons = lambda: {"OK": False}

    back_calls: list[str] = []
    select_calls: list[str] = []

    def _go_back() -> NavigationResult:
        back_calls.append("back")
        return NavigationResult(success=True, page=GDS2Page.LOADING, context={})

    def _select_data_category(category: str) -> NavigationResult:
        select_calls.append(category)
        return NavigationResult(
            success=True,
            page=GDS2Page.DATA_DISPLAY,
            selected=category,
            context={"data_category": category},
        )

    controller.go_back = _go_back
    controller.select_data_category = _select_data_category
    controller._wait_for_transition_or_detect = MagicMock(return_value=GDS2Page.DATA_LIST)

    result = controller.recover_data_display_connection(
        data_category="Engine Data",
        allow_backtrack=True,
        backtrack_attempts=1,
    )

    assert back_calls == ["back"]
    controller._wait_for_transition_or_detect.assert_called_once_with(
        GDS2Page.LOADING,
        timeout=10.0,
        timeout_message="Loading page did not settle during reconnect backtrack",
        detect_retries=0,
    )
    assert select_calls == ["Engine Data"]
    assert result.success is True
    assert result.page == GDS2Page.DATA_DISPLAY
    assert result.context["recovery_method"] == "backtrack"
