import pytest
import types

from src.navigation import GDS2Page
from src.navigation import GDS2Page, NavigationResult
import src.streaming as streaming_module
import src.streaming.agent_data_collector as agent_data_collector_module
from src.workflows.data_viewer import DataViewerWorkflow


class _FakeMapping:
    def __init__(self) -> None:
        self.updated: list[tuple[str, dict[str, int]]] = []
        self.data_updates: list[tuple[str, str, dict[str, int | dict[str, int]]]] = []
        self.sub_updates: list[tuple[str, str, str, dict[str, int]]] = []

    def update_module_list(self, vehicle_id: str, modules: dict[str, int]) -> None:
        self.updated.append((vehicle_id, dict(modules)))

    def update_data_categories(
        self,
        vehicle_id: str,
        module_name: str,
        categories: dict[str, int | dict[str, int]],
    ) -> None:
        self.data_updates.append((vehicle_id, module_name, dict(categories)))

    def update_sub_categories(
        self,
        vehicle_id: str,
        module_name: str,
        data_category: str,
        sub_categories: dict[str, int],
    ) -> None:
        self.sub_updates.append((vehicle_id, module_name, data_category, dict(sub_categories)))


class _FakeNav:
    def __init__(self, controller) -> None:
        self._controller = controller
        self.selected: list[tuple[int, int, bool]] = []

    def select_list_item(self, list_index: int, item_index: int, double_click: bool = True) -> dict:
        self.selected.append((list_index, item_index, double_click))
        self._controller._page = GDS2Page.MODULE_LIST
        return {"success": True, "message": ""}


class _FakeController:
    def __init__(self) -> None:
        self._page = GDS2Page.DIAGNOSTICS_MENU
        self._current_page = GDS2Page.UNKNOWN
        self.nav = _FakeNav(self)
        self.context: dict[str, str] = {}

    def detect_current_page(self):
        return self._page

    def wait_for_list(self, list_index: int = 0, max_attempts: int = 15):
        if self._page == GDS2Page.DIAGNOSTICS_MENU:
            return ["Module Diagnostics", "Other"]
        return ["ECM", "TCM"]

    def wait_for_page_transition(self, page, timeout: int = 30):
        self._page = GDS2Page.MODULE_LIST
        return self._page

    def set_context(self, **kwargs) -> None:
        self.context.update(kwargs)


def test_connect_device_returns_modules_after_diagnostics_menu_transition() -> None:
    workflow = DataViewerWorkflow.__new__(DataViewerWorkflow)
    workflow.controller = _FakeController()
    workflow._mapping = _FakeMapping()
    workflow._vehicle_id = "current_vehicle"
    workflow._vin = None
    workflow._device = None
    workflow._module = None
    workflow._data_category = None
    workflow.stop_monitoring = lambda: None
    workflow._ensure_device_selected = lambda device_name, status: None
    workflow._extract_vin = lambda: "VIN123"

    result = workflow.connect_device("VCI Proxy (Remote)")

    assert result == {
        "modules": ["ECM", "TCM"],
        "vin": "VIN123",
        "device": "VCI Proxy (Remote)",
    }
    assert workflow.controller.nav.selected == [(0, 0, True)]


def test_connect_device_preserves_existing_device_name_for_connected_session() -> None:
    class _ConnectedController:
        def __init__(self) -> None:
            self._page = GDS2Page.MODULE_LIST
            self._current_page = GDS2Page.UNKNOWN
            self.nav = object()
            self.context: dict[str, str] = {}

        def detect_current_page(self):
            return self._page

        def wait_for_list(self, list_index: int = 0, max_attempts: int = 15):
            return ["ECM", "TCM"]

        def set_context(self, **kwargs) -> None:
            self.context.update(kwargs)

    workflow = DataViewerWorkflow.__new__(DataViewerWorkflow)
    workflow.controller = _ConnectedController()
    workflow._mapping = _FakeMapping()
    workflow._vehicle_id = "current_vehicle"
    workflow._vin = None
    workflow._device = "VCI Proxy (Remote)"
    workflow._module = None
    workflow._data_category = None
    workflow.stop_monitoring = lambda: None
    workflow._extract_vin = lambda: None

    result = workflow.connect_device("default")

    assert result == {
        "modules": ["ECM", "TCM"],
        "vin": None,
        "device": "VCI Proxy (Remote)",
    }
    assert workflow.controller.context["device"] == "VCI Proxy (Remote)"


def test_connect_device_raises_when_module_diagnostics_entry_is_missing() -> None:
    class _DiagnosticsMenuController:
        def __init__(self) -> None:
            self._page = GDS2Page.DIAGNOSTICS_MENU
            self._current_page = GDS2Page.UNKNOWN
            self.nav = object()
            self.context: dict[str, str] = {}

        def detect_current_page(self):
            return self._page

        def wait_for_list(self, list_index: int = 0, max_attempts: int = 15):
            return ["Other Diagnostics", "Settings"]

        def set_context(self, **kwargs) -> None:
            self.context.update(kwargs)

    workflow = DataViewerWorkflow.__new__(DataViewerWorkflow)
    workflow.controller = _DiagnosticsMenuController()
    workflow._mapping = _FakeMapping()
    workflow._vehicle_id = "current_vehicle"
    workflow._vin = None
    workflow._device = None
    workflow._module = None
    workflow._data_category = None
    workflow.stop_monitoring = lambda: None
    workflow._extract_vin = lambda: None

    with pytest.raises(RuntimeError, match="Module Diagnostics"):
        workflow.connect_device("default")


def test_navigate_to_module_list_from_raises_when_module_diagnostics_entry_is_missing() -> None:
    class _DiagnosticsMenuController:
        def __init__(self) -> None:
            self._page = GDS2Page.DIAGNOSTICS_MENU
            self._current_page = GDS2Page.UNKNOWN
            self.nav = object()
            self.context: dict[str, str] = {}

        def wait_for_list(self, list_index: int = 0, max_attempts: int = 15):
            return ["Other Diagnostics", "Settings"]

        def set_context(self, **kwargs) -> None:
            self.context.update(kwargs)

    workflow = DataViewerWorkflow.__new__(DataViewerWorkflow)
    workflow.controller = _DiagnosticsMenuController()
    workflow._mapping = _FakeMapping()
    workflow._vehicle_id = "current_vehicle"
    workflow._vin = None
    workflow._device = None
    workflow._module = None
    workflow._data_category = None
    workflow._extract_vin = lambda: None

    with pytest.raises(RuntimeError, match="Module Diagnostics"):
        workflow._navigate_to_module_list_from(GDS2Page.DIAGNOSTICS_MENU, lambda _msg: None)


def test_connect_device_reaches_real_module_list_after_diagnostics_menu_lands_too_deep() -> None:
    class _DiagnosticsMenuController:
        def __init__(self) -> None:
            self._page = GDS2Page.DIAGNOSTICS_MENU
            self._current_page = GDS2Page.UNKNOWN
            self.nav = _FakeNav(self)
            self.context: dict[str, str] = {}

        def detect_current_page(self):
            return self._page

        def wait_for_list(self, list_index: int = 0, max_attempts: int = 15):
            if self._page == GDS2Page.DIAGNOSTICS_MENU:
                return ["Module Diagnostics", "Other"]
            if self._page == GDS2Page.DATA_LIST:
                return ["Engine Data", "Transmission Data"]
            return ["ECM", "TCM"]

        def wait_for_page_transition(self, page, timeout: int = 30):
            self._page = GDS2Page.DATA_LIST
            return self._page

        def set_context(self, **kwargs) -> None:
            self.context.update(kwargs)

    workflow = DataViewerWorkflow.__new__(DataViewerWorkflow)
    workflow.controller = _DiagnosticsMenuController()
    workflow._mapping = _FakeMapping()
    workflow._vehicle_id = "current_vehicle"
    workflow._vin = None
    workflow._device = None
    workflow._module = None
    workflow._data_category = None
    workflow.stop_monitoring = lambda: None
    workflow._extract_vin = lambda: None

    navigated = {"called": 0}

    def _fake_navigate_to_module_list(_status) -> None:
        navigated["called"] += 1
        workflow.controller._page = GDS2Page.MODULE_LIST

    workflow._navigate_to_module_list = _fake_navigate_to_module_list

    result = workflow.connect_device("default")

    assert result["modules"] == ["ECM", "TCM"]
    assert navigated["called"] == 1


def test_connect_device_raises_when_module_diagnostics_does_not_leave_menu() -> None:
    class _StuckNav:
        def select_list_item(
            self,
            list_index: int,
            item_index: int,
            double_click: bool = True,
        ) -> dict:
            return {"success": True, "message": ""}

    class _DiagnosticsMenuController:
        def __init__(self) -> None:
            self._page = GDS2Page.DIAGNOSTICS_MENU
            self._current_page = GDS2Page.UNKNOWN
            self.nav = _StuckNav()
            self.context: dict[str, str] = {}

        def detect_current_page(self):
            return self._page

        def wait_for_list(self, list_index: int = 0, max_attempts: int = 15):
            if self._page == GDS2Page.DIAGNOSTICS_MENU:
                return ["Module Diagnostics", "Other"]
            return ["ECM", "TCM"]

        def wait_for_page_transition(self, page, timeout: int = 30):
            raise TimeoutError("stuck on diagnostics menu")

        def set_context(self, **kwargs) -> None:
            self.context.update(kwargs)

    workflow = DataViewerWorkflow.__new__(DataViewerWorkflow)
    workflow.controller = _DiagnosticsMenuController()
    workflow._mapping = _FakeMapping()
    workflow._vehicle_id = "current_vehicle"
    workflow._vin = None
    workflow._device = None
    workflow._module = None
    workflow._data_category = None
    workflow.stop_monitoring = lambda: None
    workflow._extract_vin = lambda: None

    with pytest.raises(RuntimeError, match="did not leave diagnostics menu"):
        workflow.connect_device("default")


def test_select_data_category_raises_when_sub_category_list_is_empty() -> None:
    class _SubListController:
        def __init__(self) -> None:
            self.current_module = "ECM"

        def detect_current_page(self):
            return GDS2Page.DATA_LIST

        def wait_for_list(self):
            return ["Engine Data", "Transmission Data"]

        def select_data_category(self, category_name: str):
            return NavigationResult(
                success=True,
                page=GDS2Page.SUB_DATA_LIST,
                choices=[],
                context={"module": "ECM", "data_category": category_name},
            )

    workflow = DataViewerWorkflow.__new__(DataViewerWorkflow)
    workflow.controller = _SubListController()
    workflow._mapping = _FakeMapping()
    workflow._vehicle_id = "VIN123"
    workflow._vin = "VIN123"
    workflow._device = "VCI Proxy (Remote)"
    workflow._module = "ECM"
    workflow._data_category = None
    workflow.stop_monitoring = lambda: None
    workflow._resolve_branch_choice = lambda **kwargs: "Engine Data"

    with pytest.raises(RuntimeError, match="Sub-categories detected but list is empty"):
        workflow.select_data_category("Engine Data")


def test_select_data_category_caches_sub_categories_before_selecting_sub_item() -> None:
    class _SubListController:
        def __init__(self) -> None:
            self.current_module = "ECM"
            self.selected_sub_categories: list[str] = []

        def detect_current_page(self):
            return GDS2Page.DATA_LIST

        def wait_for_list(self):
            return ["Engine Data", "Transmission Data"]

        def select_data_category(self, category_name: str):
            return NavigationResult(
                success=True,
                page=GDS2Page.SUB_DATA_LIST,
                choices=["Fuel System", "Injector Balance"],
                context={"module": "ECM", "data_category": category_name},
            )

        def select_sub_category(self, sub_category_name: str):
            self.selected_sub_categories.append(sub_category_name)
            return NavigationResult(
                success=True,
                page=GDS2Page.DATA_DISPLAY,
                selected=sub_category_name,
                context={"sub_category": sub_category_name},
            )

    workflow = DataViewerWorkflow.__new__(DataViewerWorkflow)
    workflow.controller = _SubListController()
    workflow._mapping = _FakeMapping()
    workflow._vehicle_id = "VIN123"
    workflow._vin = "VIN123"
    workflow._device = "VCI Proxy (Remote)"
    workflow._module = "ECM"
    workflow._data_category = None
    workflow.stop_monitoring = lambda: None
    workflow._resolve_branch_choice = (
        lambda **kwargs: "Engine Data"
        if kwargs["choices"] == ["Engine Data", "Transmission Data"]
        else "Fuel System"
    )

    result = workflow.select_data_category("Engine Data")

    assert result == {
        "monitoring": True,
        "sub_categories": ["Fuel System", "Injector Balance"],
    }
    assert workflow.controller.selected_sub_categories == ["Fuel System"]
    assert workflow.mapping.sub_updates == [
        (
            "VIN123",
            "ECM",
            "Engine Data",
            {"Fuel System": 0, "Injector Balance": 1},
        )
    ]


def test_select_sub_module_caches_data_categories() -> None:
    class _SubModuleNav:
        def __init__(self) -> None:
            self.selected: list[tuple[int, int, bool]] = []

        def select_list_item(
            self,
            list_index: int,
            item_index: int,
            double_click: bool = True,
        ) -> dict:
            self.selected.append((list_index, item_index, double_click))
            return {"success": True, "message": ""}

    class _SubModuleController:
        def __init__(self) -> None:
            self.nav = _SubModuleNav()
            self._page = GDS2Page.MODULE_SUBMENU
            self._current_page = GDS2Page.UNKNOWN

        def detect_current_page(self):
            return self._page

        def wait_for_list(self, previous_items=None):
            if previous_items is None:
                return ["Data Display", "Module Information"]
            return ["Engine Data", "Transmission Data"]

        def wait_for_page_transition(self, page, timeout: int = 30):
            self._page = GDS2Page.DATA_LIST
            return self._page

        def dismiss_warning_dialog(self):
            return None

    workflow = DataViewerWorkflow.__new__(DataViewerWorkflow)
    workflow.controller = _SubModuleController()
    workflow._mapping = _FakeMapping()
    workflow._vehicle_id = "VIN123"
    workflow._vin = "VIN123"
    workflow._device = "VCI Proxy (Remote)"
    workflow._module = "ECM"
    workflow._data_category = None
    workflow.stop_monitoring = lambda: None
    workflow._resolve_branch_choice = lambda **kwargs: "Data Display"

    result = workflow.select_sub_module("Data Display")

    assert result == {
        "data_categories": ["Engine Data", "Transmission Data"],
        "sub_module": "Data Display",
    }
    assert workflow.mapping.data_updates == [
        (
            "VIN123",
            "ECM",
            {"Engine Data": 0, "Transmission Data": 1},
        )
    ]


def test_select_sub_module_raises_when_selection_does_not_leave_module_submenu() -> None:
    class _SubModuleNav:
        def select_list_item(
            self,
            list_index: int,
            item_index: int,
            double_click: bool = True,
        ) -> dict:
            return {"success": True, "message": ""}

    class _SubModuleController:
        def __init__(self) -> None:
            self.nav = _SubModuleNav()
            self._page = GDS2Page.MODULE_SUBMENU
            self._current_page = GDS2Page.UNKNOWN

        def detect_current_page(self):
            return self._page

        def wait_for_list(self, previous_items=None):
            return ["Data Display", "Module Information"]

        def wait_for_page_transition(self, page, timeout: int = 30):
            raise TimeoutError("stuck on module submenu")

        def dismiss_warning_dialog(self):
            return None

    workflow = DataViewerWorkflow.__new__(DataViewerWorkflow)
    workflow.controller = _SubModuleController()
    workflow._mapping = _FakeMapping()
    workflow._vehicle_id = "VIN123"
    workflow._vin = "VIN123"
    workflow._device = "VCI Proxy (Remote)"
    workflow._module = "ECM"
    workflow._data_category = None
    workflow.stop_monitoring = lambda: None
    workflow._resolve_branch_choice = lambda **kwargs: "Data Display"

    with pytest.raises(RuntimeError, match="did not leave module submenu"):
        workflow.select_sub_module("Data Display")


def test_select_sub_category_raises_when_selection_does_not_reach_data_display() -> None:
    class _SubCategoryController:
        def detect_current_page(self):
            return GDS2Page.SUB_DATA_LIST

        def select_sub_category(self, sub_category_name: str):
            return NavigationResult(
                success=True,
                page=GDS2Page.SUB_DATA_LIST,
                selected=sub_category_name,
                context={"sub_category": sub_category_name},
            )

    workflow = DataViewerWorkflow.__new__(DataViewerWorkflow)
    workflow.controller = _SubCategoryController()
    workflow._mapping = _FakeMapping()
    workflow._vehicle_id = "VIN123"
    workflow._vin = "VIN123"
    workflow._device = "VCI Proxy (Remote)"
    workflow._module = "ECM"
    workflow._data_category = "Engine Data"
    workflow.stop_monitoring = lambda: None

    with pytest.raises(RuntimeError, match="did not reach data display"):
        workflow.select_sub_category("Fuel System")


def test_recover_data_display_connection_raises_when_recovery_ends_off_display() -> None:
    class _RecoveryController:
        current_data_category = "Engine Data"

        def recover_data_display_connection(self, data_category: str, allow_backtrack: bool = True):
            return NavigationResult(
                success=True,
                page=GDS2Page.DATA_LIST,
                context={"data_category": data_category, "allow_backtrack": allow_backtrack},
            )

    workflow = DataViewerWorkflow.__new__(DataViewerWorkflow)
    workflow.controller = _RecoveryController()
    workflow._mapping = _FakeMapping()
    workflow._vehicle_id = "VIN123"
    workflow._vin = "VIN123"
    workflow._device = "VCI Proxy (Remote)"
    workflow._module = "ECM"
    workflow._data_category = "Engine Data"

    with pytest.raises(RuntimeError, match="did not restore Data Display"):
        workflow.recover_data_display_connection()


def test_clear_dtcs_uses_visible_clear_button_without_switching_tabs() -> None:
    class _ClearNav:
        def __init__(self) -> None:
            self.phase = "data_display"
            self.clicked: list[str] = []

        def get_buttons(self):
            mapping = {
                "data_display": [
                    {"text": "Clear DTCs", "enabled": True},
                    {"text": "Create Report", "enabled": True},
                    {"text": "Back", "enabled": True},
                ],
                "selection": [
                    {"text": "Add All", "enabled": True},
                    {"text": "Add", "enabled": True},
                    {"text": "Cancel", "enabled": True},
                    {"text": "OK", "enabled": False},
                ],
                "selection_ready": [
                    {"text": "Add All", "enabled": True},
                    {"text": "Cancel", "enabled": True},
                    {"text": "OK", "enabled": True},
                ],
                "confirm": [
                    {"text": "Continue", "enabled": True},
                    {"text": "Yes", "enabled": True},
                    {"text": "No", "enabled": True},
                    {"text": "Cancel", "enabled": True},
                    {"text": "Clear Records", "enabled": True},
                    {"text": "Save and Clear", "enabled": True},
                    {"text": "OK", "enabled": True},
                ],
                "done": [
                    {"text": "Clear DTCs", "enabled": True},
                    {"text": "Create Report", "enabled": True},
                    {"text": "Back", "enabled": True},
                ],
            }
            return mapping[self.phase]

        def click_button(self, text: str) -> dict:
            self.clicked.append(text)
            transitions = {
                ("data_display", "Clear DTCs"): "selection",
                ("selection", "Add All"): "selection_ready",
                ("selection_ready", "OK"): "confirm",
                ("confirm", "OK"): "done",
            }
            next_phase = transitions.get((self.phase, text))
            if next_phase is None:
                return {"success": False, "message": f"Unexpected click {text} on {self.phase}"}
            self.phase = next_phase
            return {"success": True, "message": ""}

        def get_list_items(self, list_index: int = 0):
            if self.phase in {"selection", "selection_ready"} and list_index == 0:
                return ["Engine Control Module"]
            return []

    class _ClearController:
        def __init__(self) -> None:
            self.nav = _ClearNav()
            self._current_page = GDS2Page.DATA_DISPLAY

        def detect_current_page(self):
            if self.nav.phase in {"data_display", "done"}:
                return GDS2Page.DATA_DISPLAY
            return GDS2Page.UNKNOWN

        def click_button(self, button_text: str):
            result = self.nav.click_button(button_text)
            return NavigationResult(
                success=result.get("success", False),
                page=self.detect_current_page(),
                error=result.get("message") if not result.get("success") else None,
                context={},
            )

        def wait_for_list(self, list_index: int = 0, max_attempts: int = 15, previous_items=None):
            return self.nav.get_list_items(list_index)

        def get_visible_buttons(self):
            return [btn["text"] for btn in self.nav.get_buttons() if btn.get("text")]

    workflow = DataViewerWorkflow()
    workflow.controller = _ClearController()
    read_counts = iter([1, 0])

    def _fake_read_all_dtcs(on_status=None):
        return {
            "dtcs": [{"code": "P0001"}] if next_count[0] > 0 else [],
            "dtc_count": next_count[0],
            "page_context": {"page": "data_display"},
        }

    next_count = [1]

    def _sequenced_read_all_dtcs(on_status=None):
        current = next(read_counts)
        next_count[0] = current
        return _fake_read_all_dtcs(on_status=on_status)

    workflow.read_all_dtcs = _sequenced_read_all_dtcs

    result = workflow.clear_dtcs()

    assert result == {
        "success": True,
        "cleared_count": 1,
        "message": "Clear DTCs completed",
        "page_context": "data_display",
    }
    assert workflow.controller.nav.clicked == ["Clear DTCs", "Add All", "OK", "OK"]


def test_clear_dtcs_uses_explicit_pages_when_confirmation_buttons_are_partial() -> None:
    class _ClearNav:
        def __init__(self) -> None:
            self.phase = "data_display"
            self.clicked: list[str] = []

        def get_buttons(self):
            mapping = {
                "data_display": [
                    {"text": "Clear DTCs", "enabled": True},
                    {"text": "Create Report", "enabled": True},
                    {"text": "Back", "enabled": True},
                ],
                "selection": [
                    {"text": "Add All", "enabled": True},
                    {"text": "Add", "enabled": True},
                    {"text": "Cancel", "enabled": True},
                    {"text": "Back", "enabled": True},
                ],
                "selection_ready": [
                    {"text": "Remove", "enabled": True},
                    {"text": "Remove All", "enabled": True},
                    {"text": "Cancel", "enabled": True},
                    {"text": "OK", "enabled": True},
                ],
                "confirm": [
                    {"text": "OK", "enabled": True},
                ],
                "done": [
                    {"text": "Clear DTCs", "enabled": True},
                    {"text": "Create Report", "enabled": True},
                    {"text": "Back", "enabled": True},
                ],
            }
            return mapping[self.phase]

        def click_button(self, text: str) -> dict:
            self.clicked.append(text)
            transitions = {
                ("data_display", "Clear DTCs"): "selection",
                ("selection", "Add All"): "selection_ready",
                ("selection_ready", "OK"): "confirm",
                ("confirm", "OK"): "done",
            }
            next_phase = transitions.get((self.phase, text))
            if next_phase is None:
                return {"success": False, "message": f"Unexpected click {text} on {self.phase}"}
            self.phase = next_phase
            return {"success": True, "message": ""}

        def get_list_items(self, list_index: int = 0):
            return []

    class _ClearController:
        def __init__(self) -> None:
            self.nav = _ClearNav()
            self._current_page = GDS2Page.DATA_DISPLAY

        def detect_current_page(self):
            mapping = {
                "data_display": GDS2Page.DATA_DISPLAY,
                "selection": GDS2Page.CLEAR_DTCS_SELECTION,
                "selection_ready": GDS2Page.CLEAR_DTCS_SELECTION,
                "confirm": GDS2Page.CLEAR_DTCS_CONFIRMATION,
                "done": GDS2Page.DATA_DISPLAY,
            }
            return mapping[self.nav.phase]

    workflow = DataViewerWorkflow()
    workflow.controller = _ClearController()
    read_counts = iter([2, 0])

    def _fake_read_all_dtcs(on_status=None):
        current = next(read_counts)
        return {
            "dtcs": [{"code": "P0001"}] * current,
            "dtc_count": current,
            "page_context": {"page": "data_display"},
        }

    workflow.read_all_dtcs = _fake_read_all_dtcs

    result = workflow.clear_dtcs()

    assert result == {
        "success": True,
        "cleared_count": 2,
        "message": "Clear DTCs completed",
        "page_context": "data_display",
    }
    assert workflow.controller.nav.clicked == ["Clear DTCs", "Add All", "OK", "OK"]


def test_wait_for_data_display_restore_prefers_restored_page_over_stale_detection() -> None:
    class _Nav:
        def get_buttons(self):
            return [
                {"text": "Clear DTCs", "enabled": True},
                {"text": "Create Report", "enabled": True},
                {"text": "Back", "enabled": True},
            ]

    class _Controller:
        def __init__(self) -> None:
            self.nav = _Nav()

        def detect_current_page(self):
            return GDS2Page.CLEAR_DTCS_CONFIRMATION

    workflow = DataViewerWorkflow()
    workflow.controller = _Controller()

    assert workflow._wait_for_data_display_restore(timeout_sec=0.5) == GDS2Page.DATA_DISPLAY


def test_clear_dtcs_re_reads_count_when_flow_returns_directly_to_data_display() -> None:
    class _DirectReturnNav:
        def __init__(self) -> None:
            self.clicked: list[str] = []

        def get_buttons(self):
            return [
                {"text": "Clear DTCs", "enabled": True},
                {"text": "Create Report", "enabled": True},
                {"text": "Back", "enabled": True},
            ]

        def click_button(self, text: str) -> dict:
            self.clicked.append(text)
            return {"success": True, "message": ""}

    class _DirectReturnController:
        def __init__(self) -> None:
            self.nav = _DirectReturnNav()
            self._current_page = GDS2Page.DATA_DISPLAY

        def detect_current_page(self):
            return GDS2Page.DATA_DISPLAY

    workflow = DataViewerWorkflow()
    workflow.controller = _DirectReturnController()
    read_counts = iter([3, 1])

    def _fake_read_all_dtcs(on_status=None):
        current = next(read_counts)
        return {
            "dtcs": [{"code": f"P{index:04d}"} for index in range(current)],
            "dtc_count": current,
            "page_context": {"page": "data_display"},
        }

    workflow.read_all_dtcs = _fake_read_all_dtcs
    workflow._wait_for_clear_dtcs_page = lambda timeout_sec=10.0, allow_data_display=False: GDS2Page.DATA_DISPLAY

    result = workflow.clear_dtcs()

    assert result == {
        "success": True,
        "cleared_count": 2,
        "message": "Clear DTCs completed",
        "page_context": "data_display",
    }
    assert workflow.controller.nav.clicked == ["Clear DTCs"]


def test_read_all_dtcs_rejects_stale_agent_snapshot_page(monkeypatch, tmp_path) -> None:
    class _DisplayController:
        def detect_current_page(self):
            return GDS2Page.DATA_DISPLAY

    class _FakeCollector:
        def __init__(self):
            self.json_path = str(tmp_path / "agent.json")

        def check_agent_available(self):
            return {"available": True}

    agent_json = tmp_path / "agent.json"
    agent_json.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(streaming_module, "AgentDataCollector", _FakeCollector)
    monkeypatch.setattr(
        agent_data_collector_module,
        "_parse_agent_json",
        lambda raw: types.SimpleNamespace(
            dtcs=[],
            page_context={"page": "module_list"},
        ),
    )

    workflow = DataViewerWorkflow()
    workflow.controller = _DisplayController()

    with pytest.raises(RuntimeError, match="stale Agent snapshot"):
        workflow.read_all_dtcs()


def test_read_all_dtcs_surfaces_missing_agent_json_as_runtime_error(monkeypatch, tmp_path) -> None:
    class _DisplayController:
        def detect_current_page(self):
            return GDS2Page.DATA_DISPLAY

    class _FakeCollector:
        def __init__(self):
            self.json_path = str(tmp_path / "missing-agent.json")

        def check_agent_available(self):
            return {"available": True}

    monkeypatch.setattr(streaming_module, "AgentDataCollector", _FakeCollector)

    workflow = DataViewerWorkflow()
    workflow.controller = _DisplayController()

    with pytest.raises(RuntimeError, match="Agent JSON file not found"):
        workflow.read_all_dtcs()


def test_get_available_devices_clears_stale_connection_state_before_explorer(monkeypatch) -> None:
    class _VehicleSelectionController:
        def detect_current_page(self):
            return GDS2Page.VEHICLE_SELECTION

        def disconnect_device(self):
            return NavigationResult(success=True, page=GDS2Page.VEHICLE_SELECTION, context={})

        def open_device_selector(self):
            return NavigationResult(success=True, page=GDS2Page.DEVICE_EXPLORER, context={})

    class _FakeExplorer:
        def find_dialog(self, timeout_sec: float = 0.0) -> bool:
            return True

        def get_device_names(self) -> list[str]:
            return ["VCI Proxy (Remote)"]

    workflow = DataViewerWorkflow()
    workflow.controller = _VehicleSelectionController()
    workflow._device = "VCI Proxy (Remote)"
    workflow._module = "ECM"
    workflow._data_category = "Engine Data"
    monkeypatch.setattr(workflow, "_navigate_to_main_menu", lambda status: None)
    monkeypatch.setattr(workflow, "_sleep", lambda *args, **kwargs: None)
    monkeypatch.setattr("src.native.device_explorer.DeviceExplorerController", _FakeExplorer)

    result = workflow.get_available_devices()

    assert result == {"devices": ["VCI Proxy (Remote)"], "at_device_explorer": True}
    assert workflow._device is None
    assert workflow._module is None
    assert workflow._data_category is None


def test_get_available_devices_clears_stale_connection_state_when_diagnostics_opens_explorer(
    monkeypatch,
) -> None:
    class _MainMenuController:
        def detect_current_page(self):
            return GDS2Page.MAIN_MENU

        def start_diagnostics(self):
            return NavigationResult(success=True, page=GDS2Page.DEVICE_EXPLORER, context={})

    workflow = DataViewerWorkflow()
    workflow.controller = _MainMenuController()
    workflow._device = "VCI Proxy (Remote)"
    workflow._module = "ECM"
    workflow._data_category = "Engine Data"
    monkeypatch.setattr(workflow, "_navigate_to_main_menu", lambda status: None)
    monkeypatch.setattr(
        workflow,
        "_get_devices_from_explorer",
        lambda status: {"devices": ["VCI Proxy (Remote)"], "at_device_explorer": True},
    )

    result = workflow.get_available_devices()

    assert result == {"devices": ["VCI Proxy (Remote)"], "at_device_explorer": True}
    assert workflow._device is None
    assert workflow._module is None
    assert workflow._data_category is None


def test_ensure_device_selected_clears_stale_connection_state_when_diagnostics_opens_explorer(
    monkeypatch,
) -> None:
    class _MainMenuController:
        def detect_current_page(self):
            return GDS2Page.MAIN_MENU

        def start_diagnostics(self):
            return NavigationResult(success=True, page=GDS2Page.DEVICE_EXPLORER, context={})

        def set_context(self, **kwargs) -> None:
            self.context = kwargs

    selected_states: list[tuple[str | None, str | None, str | None]] = []

    class _FakeExplorer:
        def find_dialog(self, timeout_sec: float = 5.0):
            return True

    workflow = DataViewerWorkflow()
    workflow.controller = _MainMenuController()
    workflow._device = "VCI Proxy (Remote)"
    workflow._module = "ECM"
    workflow._data_category = "Engine Data"
    monkeypatch.setattr(workflow, "_navigate_to_main_menu", lambda status: None)
    monkeypatch.setattr(workflow, "_sleep", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "src.native.device_explorer.DeviceExplorerController",
        lambda: _FakeExplorer(),
    )

    def _capture_selection(explorer, device_name, status):
        selected_states.append((workflow._device, workflow._module, workflow._data_category))

    monkeypatch.setattr(workflow, "_select_in_explorer", _capture_selection)

    workflow._ensure_device_selected("VCI Proxy (Remote)", lambda msg: None)

    assert selected_states == [(None, None, None)]
    assert workflow._device is None
    assert workflow._module is None
    assert workflow._data_category is None
