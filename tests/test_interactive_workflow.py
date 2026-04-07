from __future__ import annotations

import types

from src.navigation import GDS2Page, NavigationResult
from src.workflows.interactive_workflow import InteractiveWorkflow


class _FakeMapping:
    def __init__(self) -> None:
        self.module_updates: list[tuple[str, dict[str, int]]] = []
        self.sub_category_updates: list[tuple[str, str, str, dict[str, int]]] = []

    def update_module_list(self, vehicle_id: str, modules: dict[str, int]) -> None:
        self.module_updates.append((vehicle_id, dict(modules)))

    def update_sub_categories(
        self,
        vehicle_id: str,
        module_name: str,
        data_category: str,
        sub_categories: dict[str, int],
    ) -> None:
        self.sub_category_updates.append(
            (vehicle_id, module_name, data_category, dict(sub_categories))
        )


def _build_workflow(controller, mapping: _FakeMapping | None = None) -> InteractiveWorkflow:
    workflow = InteractiveWorkflow.__new__(InteractiveWorkflow)
    workflow.controller = controller
    workflow._mapping = mapping or _FakeMapping()
    workflow._vehicle_id = "current_vehicle"
    return workflow


def test_start_returns_error_when_agent_is_unavailable() -> None:
    controller = types.SimpleNamespace(
        check_agent=lambda: False,
        get_context=lambda: {"module": None, "data_category": None},
    )
    workflow = _build_workflow(controller)

    result = workflow.start()

    assert result.success is False
    assert result.page == GDS2Page.UNKNOWN
    assert "Java Agent not available" in (result.error or "")


def test_step_module_diagnostics_discovers_modules_and_updates_cache(monkeypatch) -> None:
    class _FakeNav:
        def __init__(self) -> None:
            self.selected: list[tuple[int, int, bool]] = []

        def select_list_item(self, list_index: int, item_index: int, double_click: bool = True) -> dict:
            self.selected.append((list_index, item_index, double_click))
            return {"success": True}

    class _FakeController:
        def __init__(self) -> None:
            self.nav = _FakeNav()
            self._current_page = GDS2Page.DIAGNOSTICS_MENU
            self._lists = iter(
                [
                    ["Module Diagnostics", "Other"],
                    ["ECM", "TCM"],
                ]
            )

        @property
        def current_page(self) -> GDS2Page:
            return self._current_page

        def wait_for_list(self):
            return next(self._lists)

        def get_context(self) -> dict:
            return {"module": None, "data_category": None}

    monkeypatch.setattr("src.workflows.interactive_workflow.time.sleep", lambda _seconds: None)
    mapping = _FakeMapping()
    workflow = _build_workflow(_FakeController(), mapping)

    result = workflow.step_module_diagnostics()

    assert result.success is True
    assert result.page == GDS2Page.MODULE_LIST
    assert result.choices == ["ECM", "TCM"]
    assert workflow.controller.nav.selected == [(0, 0, True)]
    assert mapping.module_updates == [("current_vehicle", {"ECM": 0, "TCM": 1})]


def test_step_select_data_category_caches_sub_categories() -> None:
    class _FakeController:
        current_module = "ECM"

        def select_data_category(self, _data_category: str) -> NavigationResult:
            return NavigationResult(
                success=True,
                page=GDS2Page.SUB_DATA_LIST,
                choices=["Fuel System", "Injector Balance"],
                context={"module": "ECM"},
            )

    mapping = _FakeMapping()
    workflow = _build_workflow(_FakeController(), mapping)

    result = workflow.step_select_data_category("Engine Data")

    assert result.success is True
    assert mapping.sub_category_updates == [
        (
            "current_vehicle",
            "ECM",
            "Engine Data",
            {"Fuel System": 0, "Injector Balance": 1},
        )
    ]


def test_parse_report_extracts_vehicle_info_and_data_items(tmp_path) -> None:
    report_path = tmp_path / "report.html"
    report_path.write_text(
        """
<table>
<tr><td>Vehicle Identification Number (VIN)</td><td>VIN123</td></tr>
<tr><td>Make</td><td>Chevrolet</td></tr>
<tr><td>Model</td><td>Malibu</td></tr>
<tr><td>Model Year</td><td>2024</td></tr>
<tr><td>Parameter</td><td>Value</td><td>Units</td></tr>
<tr><td>RPM</td><td>850</td><td>rpm</td></tr>
<tr><td>Coolant Temp</td><td>92</td><td>C</td></tr>
</table>
""".strip(),
        encoding="iso-8859-1",
    )
    workflow = _build_workflow(types.SimpleNamespace())

    parsed = workflow.parse_report(str(report_path))

    assert parsed["vehicle_info"] == {
        "vin": "VIN123",
        "make": "Chevrolet",
        "model": "Malibu",
        "year": "2024",
    }
    assert parsed["data_items"] == [
        {"parameter": "RPM", "value": "850", "units": "rpm"},
        {"parameter": "Coolant Temp", "value": "92", "units": "C"},
    ]
