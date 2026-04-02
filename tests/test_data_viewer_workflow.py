from src.navigation import GDS2Page
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
