from src.navigation import GDS2Page
from src.navigation import GDS2Page, NavigationResult
from src.workflows.data_viewer import DataViewerWorkflow


class _FakeMapping:
    def __init__(self) -> None:
        self.updated: list[tuple[str, dict[str, int]]] = []

    def update_module_list(self, vehicle_id: str, modules: dict[str, int]) -> None:
        self.updated.append((vehicle_id, dict(modules)))


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
