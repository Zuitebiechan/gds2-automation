from __future__ import annotations

from src.navigation import GDS2Page, NavigationResult
from src.workflows import read_data_display_agent
from src.workflows.read_data_display_agent import ReadDataDisplayAgentWorkflow


def _result(
    success: bool,
    page: GDS2Page,
    *,
    choices: list[str] | None = None,
    error: str | None = None,
    context: dict[str, str] | None = None,
) -> NavigationResult:
    return NavigationResult(
        success=success,
        page=page,
        choices=choices,
        error=error,
        context=context or {},
    )


def test_execute_runs_full_workflow_and_selects_first_sub_category(monkeypatch) -> None:
    class _FakeController:
        def __init__(self) -> None:
            self.detected_pages = iter([GDS2Page.DIAGNOSTICS_MENU])

        def detect_current_page(self) -> GDS2Page:
            return next(self.detected_pages)

    class _FakeInteractiveWorkflow:
        def __init__(self) -> None:
            self._vehicle_id = None
            self.controller = _FakeController()
            self.calls: list[tuple[str, object]] = []

        def start(self) -> NavigationResult:
            self.calls.append(("start", None))
            return _result(True, GDS2Page.MAIN_MENU)

        def step_diagnostics(self) -> NavigationResult:
            self.calls.append(("step_diagnostics", None))
            return _result(True, GDS2Page.DEVICE_EXPLORER, choices=["SM2 USB"])

        def step_select_device(self, device_name: str) -> NavigationResult:
            self.calls.append(("step_select_device", device_name))
            return _result(True, GDS2Page.VEHICLE_SELECTION)

        def step_module_diagnostics(self) -> NavigationResult:
            self.calls.append(("step_module_diagnostics", None))
            return _result(True, GDS2Page.MODULE_LIST)

        def step_select_module(self, module_name: str) -> NavigationResult:
            self.calls.append(("step_select_module", module_name))
            return _result(True, GDS2Page.MODULE_SUBMENU)

        def step_data_display(self) -> NavigationResult:
            self.calls.append(("step_data_display", None))
            return _result(True, GDS2Page.DATA_LIST)

        def step_select_data_category(self, data_category: str) -> NavigationResult:
            self.calls.append(("step_select_data_category", data_category))
            return _result(
                True,
                GDS2Page.SUB_DATA_LIST,
                choices=["Fuel Trim", "Injector Balance"],
                context={"module": "ECM"},
            )

        def step_select_sub_category(self, sub_category: str) -> NavigationResult:
            self.calls.append(("step_select_sub_category", sub_category))
            return _result(
                True,
                GDS2Page.DATA_DISPLAY,
                context={"sub_category": sub_category},
            )

    monkeypatch.setattr(read_data_display_agent, "InteractiveWorkflow", _FakeInteractiveWorkflow)

    workflow = ReadDataDisplayAgentWorkflow(vehicle_id="VIN123")
    result = workflow.execute(
        target_module="[K20] Engine Control Module",
        data_category="Engine Data",
        vci_device="SM2 USB",
    )

    assert workflow._workflow._vehicle_id == "VIN123"
    assert workflow.name == "Read Data Display (Agent)"
    assert "Java Agent" in workflow.description
    assert workflow._workflow.calls == [
        ("start", None),
        ("step_diagnostics", None),
        ("step_select_device", "SM2 USB"),
        ("step_module_diagnostics", None),
        ("step_select_module", "[K20] Engine Control Module"),
        ("step_data_display", None),
        ("step_select_data_category", "Engine Data"),
        ("step_select_sub_category", "Fuel Trim"),
    ]
    assert result == {
        "success": True,
        "module": "[K20] Engine Control Module",
        "data_category": "Engine Data",
        "sub_category": "Fuel Trim",
        "page": "data_display",
    }


def test_execute_skips_module_diagnostics_when_already_at_module_list(monkeypatch) -> None:
    class _FakeController:
        def detect_current_page(self) -> GDS2Page:
            return GDS2Page.MODULE_LIST

    class _FakeInteractiveWorkflow:
        def __init__(self) -> None:
            self._vehicle_id = None
            self.controller = _FakeController()
            self.step_module_diagnostics_called = False

        def start(self) -> NavigationResult:
            return _result(True, GDS2Page.MAIN_MENU)

        def step_diagnostics(self) -> NavigationResult:
            return _result(True, GDS2Page.VEHICLE_SELECTION)

        def step_module_diagnostics(self) -> NavigationResult:
            self.step_module_diagnostics_called = True
            return _result(True, GDS2Page.MODULE_LIST)

        def step_select_module(self, _module_name: str) -> NavigationResult:
            return _result(True, GDS2Page.MODULE_SUBMENU)

        def step_data_display(self) -> NavigationResult:
            return _result(True, GDS2Page.DATA_LIST)

        def step_select_data_category(self, _data_category: str) -> NavigationResult:
            return _result(True, GDS2Page.DATA_DISPLAY, context={})

    monkeypatch.setattr(read_data_display_agent, "InteractiveWorkflow", _FakeInteractiveWorkflow)

    workflow = ReadDataDisplayAgentWorkflow()
    result = workflow.execute("ECM", "Engine Data")

    assert workflow._workflow.step_module_diagnostics_called is False
    assert result["success"] is True
    assert result["sub_category"] is None
    assert result["page"] == "data_display"


def test_execute_returns_error_when_start_fails(monkeypatch) -> None:
    class _FakeInteractiveWorkflow:
        def __init__(self) -> None:
            self._vehicle_id = None
            self.controller = object()

        def start(self) -> NavigationResult:
            return _result(False, GDS2Page.UNKNOWN, error="agent offline")

    monkeypatch.setattr(read_data_display_agent, "InteractiveWorkflow", _FakeInteractiveWorkflow)

    workflow = ReadDataDisplayAgentWorkflow()

    assert workflow.execute("ECM", "Engine Data") == {
        "success": False,
        "error": "agent offline",
    }


def test_execute_returns_error_when_sub_category_list_is_empty(monkeypatch) -> None:
    class _FakeController:
        def detect_current_page(self) -> GDS2Page:
            return GDS2Page.DIAGNOSTICS_MENU

    class _FakeInteractiveWorkflow:
        def __init__(self) -> None:
            self._vehicle_id = None
            self.controller = _FakeController()

        def start(self) -> NavigationResult:
            return _result(True, GDS2Page.MAIN_MENU)

        def step_diagnostics(self) -> NavigationResult:
            return _result(True, GDS2Page.VEHICLE_SELECTION)

        def step_module_diagnostics(self) -> NavigationResult:
            return _result(True, GDS2Page.MODULE_LIST)

        def step_select_module(self, _module_name: str) -> NavigationResult:
            return _result(True, GDS2Page.MODULE_SUBMENU)

        def step_data_display(self) -> NavigationResult:
            return _result(True, GDS2Page.DATA_LIST)

        def step_select_data_category(self, _data_category: str) -> NavigationResult:
            return _result(True, GDS2Page.SUB_DATA_LIST, choices=[])

    monkeypatch.setattr(read_data_display_agent, "InteractiveWorkflow", _FakeInteractiveWorkflow)

    workflow = ReadDataDisplayAgentWorkflow()

    assert workflow.execute("ECM", "Engine Data") == {
        "success": False,
        "error": "Sub-categories detected but list is empty",
    }
