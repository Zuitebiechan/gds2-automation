from types import SimpleNamespace
from unittest.mock import MagicMock

from diagnostic_platform.contracts import BackendCapability, BackendRegistry, BackendState

from backends.gds2.backend import GDS2DiagnosticBackend
from backends.gds2.controller_runtime import GDS2ControllerRuntime
from src.navigation import GDS2Page, NavigationController, NavigationResult
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


def test_gds2_backend_clear_dtcs_delegates_to_workflow():
    runtime = MagicMock()
    runtime.get_workflow.return_value = MagicMock(
        clear_dtcs=MagicMock(
            return_value={
                "success": True,
                "cleared_count": 5,
                "message": "Clear DTCs completed",
                "page_context": "data_display",
            }
        )
    )
    backend = GDS2DiagnosticBackend(runtime=runtime)

    result = backend.clear_dtcs()

    assert BackendCapability.CLEAR_DTCS in backend.capabilities
    assert result.success is True
    assert result.cleared_count == 5
    assert result.message == "Clear DTCs completed"
    runtime.get_workflow.return_value.clear_dtcs.assert_called_once_with()


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


def test_gds2_backend_data_display_guard_recovers_stream_after_disconnect() -> None:
    runtime = MagicMock()
    controller = MagicMock()
    controller.detect_current_page.return_value = GDS2Page.J2534_DISCONNECT
    controller.recover_data_display_connection.return_value = SimpleNamespace(
        success=True,
        page=GDS2Page.DATA_DISPLAY,
        context={"recovery_method": "in_place"},
    )
    workflow = MagicMock()
    workflow.controller = controller
    runtime.get_controller.return_value = controller
    runtime.get_workflow.return_value = workflow
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


def test_gds2_backend_data_display_guard_requests_ai_restart_after_backtrack_recovery() -> None:
    runtime = MagicMock()
    controller = MagicMock()
    controller.detect_current_page.return_value = GDS2Page.J2534_DISCONNECT
    controller.recover_data_display_connection.return_value = SimpleNamespace(
        success=True,
        page=GDS2Page.DATA_DISPLAY,
        context={"recovery_method": "backtrack"},
    )
    workflow = MagicMock()
    workflow.controller = controller
    runtime.get_controller.return_value = controller
    runtime.get_workflow.return_value = workflow
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


def test_gds2_backend_data_display_guard_fails_ai_collection_when_recovery_fails() -> None:
    runtime = MagicMock()
    controller = MagicMock()
    controller.detect_current_page.return_value = GDS2Page.J2534_DISCONNECT
    controller.recover_data_display_connection.return_value = SimpleNamespace(
        success=False,
        page=GDS2Page.J2534_DISCONNECT,
        context={},
    )
    workflow = MagicMock()
    workflow.controller = controller
    runtime.get_controller.return_value = controller
    runtime.get_workflow.return_value = workflow
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
