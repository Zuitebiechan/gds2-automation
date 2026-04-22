from __future__ import annotations

import json
from pathlib import Path

from diagnostic_platform.observability import flush_product_log_writers
from src.navigation.controller import GDS2Page, NavigationController


class _FakeAgentNav:
    def __init__(self) -> None:
        self.page_id = "data_list"
        self.navigation_path = [
            "Module Diagnostics",
            "Engine Control Module",
            "Control Functions",
        ]
        self._pages = {
            "data_list": {
                "buttons": ["Back", "Enter"],
                "lists": ["Fuel System", "Turbocharger"],
            },
            "module_submenu": {
                "buttons": ["Back", "Home", "Vehicle Menu", "Enter"],
                "lists": ["Data Display", "Diagnostic Trouble Codes (DTC)"],
            },
        }

    def get_page_id(self) -> dict:
        return {
            "page_id": self.page_id,
            "confidence": "high",
        }

    def get_buttons(self) -> list[dict]:
        return [{"text": text} for text in self._pages[self.page_id]["buttons"]]

    def get_list_items(self, list_index: int = 0) -> list[str]:
        return list(self._pages[self.page_id]["lists"])

    def get_navigation_path(self) -> list[str]:
        return list(self.navigation_path)

    def click_navigation_path_item(self, text: str) -> dict:
        if text != "Engine Control Module":
            return {"success": False, "message": f"unexpected breadcrumb {text}"}
        self.page_id = "module_submenu"
        self.navigation_path = ["Module Diagnostics", "Engine Control Module"]
        return {"success": True, "message": "clicked"}


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


def test_navigation_controller_reads_navigation_path_from_agent() -> None:
    controller = NavigationController(nav=_FakeAgentNav())

    assert controller.get_navigation_path() == [
        "Module Diagnostics",
        "Engine Control Module",
        "Control Functions",
    ]


def test_navigation_controller_detect_page_emits_page_detected_event(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    controller = NavigationController(nav=_FakeAgentNav())
    controller._current_page = GDS2Page.UNKNOWN

    page = controller.detect_current_page(retries=0)

    assert page == GDS2Page.DATA_LIST
    events = _read_cloud_events(tmp_path)
    detected = [event for event in events if event["event_type"] == "page.detected"]
    assert detected
    assert detected[-1]["page_after"] == "data_list"
    assert detected[-1]["detection_mode"] == "agent"


def test_navigation_controller_click_navigation_path_item_updates_page() -> None:
    controller = NavigationController(nav=_FakeAgentNav())
    controller._sleep = lambda *_args, **_kwargs: None
    controller._current_page = GDS2Page.DATA_LIST

    result = controller.click_navigation_path_item("Engine Control Module")

    assert result.success is True
    assert result.page == GDS2Page.MODULE_SUBMENU
    assert result.selected == "Engine Control Module"
    assert controller.history == [GDS2Page.DATA_LIST]


def test_navigation_controller_click_enter_emits_event(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))

    class _EnterNav:
        def click_button(self, text: str) -> dict[str, object]:
            return {"success": True}

    controller = NavigationController(nav=_EnterNav())
    controller._current_page = GDS2Page.VEHICLE_SELECTION
    transitions = iter([GDS2Page.DIAGNOSTICS_MENU])
    controller.wait_for_page_transition = lambda old_page, timeout: next(transitions)
    controller.dismiss_warning_dialog = lambda: False
    controller.get_list_items = lambda list_index=0: ["Module Diagnostics"]

    result = controller.click_enter()

    assert result.success is True
    events = _read_cloud_events(tmp_path)
    assert "click_enter" in [event["event_type"] for event in events]


def test_navigation_controller_open_device_selector_emits_event(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    import src.native as native_module

    class _DeviceController:
        def find_dialog(self, timeout_sec: float = 5.0):
            return True

        def get_device_names(self):
            return ["SM2 USB"]

    monkeypatch.setattr(native_module, "DeviceExplorerController", lambda: _DeviceController())

    class _Nav:
        def click_button(self, text: str) -> dict[str, object]:
            return {"success": True}

    controller = NavigationController(nav=_Nav())
    controller._current_page = GDS2Page.VEHICLE_SELECTION
    controller.detect_current_page = lambda retries=1, retry_delay=1.5: GDS2Page.VEHICLE_SELECTION
    controller._sleep = lambda *_args, **_kwargs: None

    result = controller.open_device_selector()

    assert result.success is True
    events = _read_cloud_events(tmp_path)
    assert "device_explorer_opened" in [event["event_type"] for event in events]


def test_navigation_controller_select_device_emits_event(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    import src.native as native_module

    class _DeviceController:
        def find_dialog(self, timeout_sec: float = 2.0):
            return True

        def select_device_by_name(self, device_name: str):
            return device_name == "SM2 USB"

        def get_device_names(self):
            return ["SM2 USB"]

        def click_continue(self):
            return True

    monkeypatch.setattr(native_module, "DeviceExplorerController", lambda: _DeviceController())

    controller = NavigationController(nav=_FakeAgentNav())
    controller.wait_for_page_transition = lambda old_page, timeout: GDS2Page.VEHICLE_SELECTION
    controller.get_list_items = lambda list_index=0: []
    controller._sleep = lambda *_args, **_kwargs: None

    result = controller.select_device("SM2 USB")

    assert result.success is True
    events = _read_cloud_events(tmp_path)
    assert "device_selected" in [event["event_type"] for event in events]
