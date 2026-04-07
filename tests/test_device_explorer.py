from __future__ import annotations

from src.native import device_explorer
from src.native.device_explorer import DeviceExplorerController, handle_device_explorer


def test_get_available_devices_returns_empty_when_dialog_is_missing(monkeypatch) -> None:
    controller = DeviceExplorerController()
    monkeypatch.setattr(controller, "find_dialog", lambda timeout_sec=3.0: False)

    assert controller.get_available_devices() == []


def test_get_available_devices_returns_names_after_finding_dialog(monkeypatch) -> None:
    controller = DeviceExplorerController()
    controller._dialog_hwnd = None
    monkeypatch.setattr(controller, "find_dialog", lambda timeout_sec=3.0: True)
    monkeypatch.setattr(controller, "get_device_names", lambda: ["SM2 USB", "VCI Proxy"])

    assert controller.get_available_devices() == ["SM2 USB", "VCI Proxy"]


def test_select_device_rejects_missing_listview_and_out_of_range(monkeypatch) -> None:
    controller = DeviceExplorerController()
    assert controller.select_device(0) is False

    controller._listview_hwnd = 101
    monkeypatch.setattr(controller, "get_device_count", lambda: 1)

    assert controller.select_device(2) is False


def test_select_device_ensures_visible_and_clicks_target_item(monkeypatch) -> None:
    controller = DeviceExplorerController()
    controller._listview_hwnd = 101
    calls: list[tuple[str, int, int, int, int] | tuple[str, int] | tuple[str, int, int]] = []

    monkeypatch.setattr(controller, "get_device_count", lambda: 3)
    monkeypatch.setattr(
        device_explorer.user32,
        "SendMessageW",
        lambda hwnd, msg, wparam, lparam: calls.append(("send", hwnd, msg, wparam, lparam)),
    )
    monkeypatch.setattr(
        device_explorer.user32,
        "SetFocus",
        lambda hwnd: calls.append(("focus", hwnd)),
    )
    monkeypatch.setattr(
        controller,
        "_click_listview_item",
        lambda index: calls.append(("click_item", index)),
    )

    assert controller.select_device(1) is True
    assert calls == [
        ("send", 101, device_explorer.LVM_ENSUREVISIBLE, 1, 0),
        ("focus", 101),
        ("click_item", 1),
    ]


def test_select_device_by_name_prefers_exact_then_partial_match(monkeypatch) -> None:
    controller = DeviceExplorerController()
    selected: list[int] = []
    monkeypatch.setattr(controller, "select_device", lambda index: selected.append(index) or True)

    monkeypatch.setattr(controller, "get_device_names", lambda: ["SM2 USB", "Remote VCI"])
    assert controller.select_device_by_name("sm2 usb") is True

    monkeypatch.setattr(controller, "get_device_names", lambda: ["Tech2", "Remote VCI"])
    assert controller.select_device_by_name("remote") is True

    monkeypatch.setattr(controller, "get_device_names", lambda: ["Tech2"])
    assert controller.select_device_by_name("missing") is False

    assert selected == [0, 1]


def test_click_continue_and_cancel_refind_buttons_when_needed(monkeypatch) -> None:
    controller = DeviceExplorerController()
    clicked: list[int] = []

    def _populate_controls() -> None:
        controller._continue_btn_hwnd = 201
        controller._cancel_btn_hwnd = 202

    monkeypatch.setattr(controller, "_find_child_controls", _populate_controls)
    monkeypatch.setattr(controller, "_click_button", lambda hwnd: clicked.append(hwnd))

    assert controller.click_continue() is True
    assert controller.click_cancel() is True
    assert clicked == [201, 202]


def test_handle_device_explorer_returns_expected_success_and_failure_paths(monkeypatch) -> None:
    class _FakeController:
        should_find = False
        should_select = True
        should_continue = True

        def find_dialog(self, timeout_sec: float) -> bool:
            return self.should_find

        def select_device_by_name(self, device_name: str) -> bool:
            return self.should_select

        def click_continue(self) -> bool:
            return self.should_continue

    monkeypatch.setattr("src.native.device_explorer.DeviceExplorerController", _FakeController)
    monkeypatch.setattr("src.native.device_explorer.time.sleep", lambda seconds: None)

    _FakeController.should_find = False
    assert handle_device_explorer("SM2 USB", timeout=1.0) is True

    _FakeController.should_find = True
    _FakeController.should_select = False
    assert handle_device_explorer("SM2 USB", timeout=1.0) is False

    _FakeController.should_select = True
    _FakeController.should_continue = False
    assert handle_device_explorer("SM2 USB", timeout=1.0) is False

    _FakeController.should_continue = True
    assert handle_device_explorer("SM2 USB", timeout=1.0) is True
