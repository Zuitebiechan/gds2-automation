from __future__ import annotations

import json
from pathlib import Path

from backends.gds2.explorer_harness import (
    GDS2ExplorerHarness,
    extract_navigation_path,
    load_latest_snapshot,
)


class _FakeNavigator:
    def __init__(self) -> None:
        self.state = "modal_error"
        self._loading_target: str | None = None
        self.clicked_buttons: list[str] = []
        self.selected_items: list[str] = []
        self.selected_device: str | None = None

    def get_page_id(self) -> dict[str, object]:
        if self.state == "loading" and self._loading_target is not None:
            target = self._loading_target
            self._loading_target = None
            self.state = target
            return {"page_id": "loading", "confidence": "high"}
        page_id = "vehicle_selection" if self.state == "vehicle_ready" else self.state
        return {"page_id": page_id, "confidence": "high"}

    def get_buttons(self) -> list[dict[str, object]]:
        mapping = {
            "modal_error": [{"text": "OK", "enabled": True}],
            "main_menu": [{"text": "Diagnostics", "enabled": True}],
            "vehicle_selection": [
                {"text": "Select Device", "enabled": True},
                {"text": "Back", "enabled": True},
            ],
            "vehicle_ready": [
                {"text": "Select Device", "enabled": True},
                {"text": "Enter", "enabled": True},
                {"text": "Back", "enabled": True},
            ],
            "diagnostics_menu": [
                {"text": "Home", "enabled": True},
                {"text": "Enter", "enabled": True},
            ],
            "module_list": [{"text": "Back", "enabled": True}],
            "module_submenu": [{"text": "Back", "enabled": True}],
            "data_list": [{"text": "Back", "enabled": True}],
            "data_display": [
                {"text": "Create Report", "enabled": True},
                {"text": "Back", "enabled": True},
            ],
        }
        return mapping.get(self.state, [])

    def get_list_items(self, list_index: int = 0) -> list[str]:
        assert list_index == 0
        mapping = {
            "diagnostics_menu": [
                "Module Diagnostics",
                "Vehicle Diagnostics",
                "Session Manager",
            ],
            "module_list": [
                "Engine Control Module",
                "Transmission Control Module",
            ],
            "module_submenu": [
                "Data Display",
                "Diagnostic Trouble Codes",
            ],
            "data_list": [
                "Engine Data",
                "Identification Information",
            ],
            "data_display": [
                "Display Name",
                "ECU",
                "Units",
            ],
        }
        return mapping.get(self.state, [])

    def click_button(self, text: str) -> dict[str, object]:
        self.clicked_buttons.append(text)
        if self.state == "modal_error" and text == "OK":
            self.state = "main_menu"
            return {"success": True, "message": "dismissed"}
        if self.state == "main_menu" and text == "Diagnostics":
            self.state = "vehicle_selection"
            return {"success": True, "message": "opened diagnostics"}
        if self.state == "vehicle_selection" and text == "Select Device":
            return {"success": True, "message": "opened device explorer"}
        if self.state == "vehicle_ready" and text == "Enter":
            self.state = "diagnostics_menu"
            return {"success": True, "message": "entered vehicle"}
        return {"success": False, "message": f"unexpected button {text} on {self.state}"}

    def select_list_item_by_text(
        self,
        list_index: int,
        item_text: str,
        double_click: bool = True,
    ) -> dict[str, object]:
        assert list_index == 0
        assert double_click is True
        self.selected_items.append(item_text)
        if self.state == "diagnostics_menu" and item_text == "Module Diagnostics":
            self.state = "module_list"
            return {"success": True}
        if self.state == "module_list" and item_text == "Engine Control Module":
            self.state = "module_submenu"
            return {"success": True}
        if self.state == "module_submenu" and item_text == "Data Display":
            self.state = "data_list"
            return {"success": True}
        if self.state == "data_list" and item_text == "Engine Data":
            self.state = "data_display"
            return {"success": True}
        return {"success": False, "message": f"unexpected item {item_text} on {self.state}"}


class _FakeDeviceExplorer:
    def __init__(self, navigator: _FakeNavigator) -> None:
        self._navigator = navigator

    def find_dialog(self, timeout_sec: float = 5.0) -> bool:
        return self._navigator.state == "vehicle_selection"

    def get_device_names(self) -> list[str]:
        return ["MDI", "SM2 USB", "VCI Proxy (Remote)"]

    def select_device_by_name(self, name: str) -> bool:
        if name != "SM2 USB":
            return False
        self._navigator.selected_device = name
        return True

    def click_continue(self) -> bool:
        self._navigator.state = "loading"
        self._navigator._loading_target = "vehicle_ready"
        return True


class _RaceyVehicleSelectionNavigator(_FakeNavigator):
    def __init__(self) -> None:
        super().__init__()
        self.state = "vehicle_selection"

    def get_page_id(self) -> dict[str, object]:
        return {
            "page_id": "vehicle_selection",
            "confidence": "high",
            "buttons": ["Disconnect", "Select Device", "Back"],
        }

    def get_buttons(self) -> list[dict[str, object]]:
        return [
            {"text": "Disconnect", "enabled": True},
            {"text": "Back", "enabled": True},
        ]


class _LoadingVehicleSelectionNavigator(_FakeNavigator):
    def __init__(self) -> None:
        super().__init__()
        self.state = "vehicle_ready"

    def get_page_id(self) -> dict[str, object]:
        if self.state == "vehicle_ready":
            return {
                "page_id": "loading",
                "confidence": "medium",
                "buttons": ["Disconnect", "Enter", "Back"],
            }
        return super().get_page_id()


class _TransientEnterNavigator(_FakeNavigator):
    def __init__(self) -> None:
        super().__init__()
        self.state = "vehicle_ready"
        self._enter_transition_polls = 0

    def get_page_id(self) -> dict[str, object]:
        if self.state == "transition_after_enter":
            self._enter_transition_polls += 1
            if self._enter_transition_polls < 3:
                return {"page_id": "unknown", "confidence": "low", "buttons": ["Back"]}
            self.state = "diagnostics_menu"
        return super().get_page_id()

    def get_buttons(self) -> list[dict[str, object]]:
        if self.state == "transition_after_enter" and self._enter_transition_polls < 3:
            return [{"text": "Back", "enabled": True}]
        return super().get_buttons()

    def click_button(self, text: str) -> dict[str, object]:
        self.clicked_buttons.append(text)
        if self.state == "vehicle_ready" and text == "Enter":
            self.state = "transition_after_enter"
            self._enter_transition_polls = 0
            return {"success": True, "message": "entering diagnostics"}
        return super().click_button(text)


class _DeepLoadingNavigator(_FakeNavigator):
    def get_page_id(self) -> dict[str, object]:
        return {
            "page_id": "loading",
            "confidence": "high",
            "buttons": ["Back", "Enter", "Home", "Vehicle Menu"],
        }

    def get_buttons(self) -> list[dict[str, object]]:
        return [
            {"text": "Back", "enabled": True},
            {"text": "Enter", "enabled": True},
            {"text": "Home", "enabled": True},
            {"text": "Vehicle Menu", "enabled": True},
        ]


class _LoadingThenSettledListNavigator(_FakeNavigator):
    def __init__(self) -> None:
        super().__init__()
        self.state = "vehicle_diagnostics_list"
        self._transition_polls = 0

    def get_page_id(self) -> dict[str, object]:
        if self.state == "transition":
            self._transition_polls += 1
            if self._transition_polls < 3:
                return {
                    "page_id": "loading",
                    "confidence": "high",
                    "buttons": ["Back", "Enter", "Home", "Vehicle Menu"],
                }
            self.state = "vehicle_dtc_detail"
        if self.state == "vehicle_diagnostics_list":
            return {
                "page_id": "data_list",
                "confidence": "high",
                "buttons": ["Back", "Enter", "Home", "Vehicle Menu"],
            }
        if self.state == "vehicle_dtc_detail":
            return {
                "page_id": "data_display",
                "confidence": "high",
                "buttons": ["Back", "Create Report", "Details", "Refresh"],
            }
        return super().get_page_id()

    def get_buttons(self) -> list[dict[str, object]]:
        if self.state == "vehicle_diagnostics_list":
            return [
                {"text": "Back", "enabled": True},
                {"text": "Enter", "enabled": True},
                {"text": "Home", "enabled": True},
                {"text": "Vehicle Menu", "enabled": True},
            ]
        if self.state == "transition":
            return [
                {"text": "Back", "enabled": True},
                {"text": "Enter", "enabled": True},
                {"text": "Home", "enabled": True},
                {"text": "Vehicle Menu", "enabled": True},
            ]
        if self.state == "vehicle_dtc_detail":
            return [
                {"text": "Back", "enabled": True},
                {"text": "Create Report", "enabled": True},
                {"text": "Details", "enabled": True},
                {"text": "Refresh", "enabled": True},
            ]
        return super().get_buttons()

    def get_list_items(self, list_index: int = 0) -> list[str]:
        assert list_index == 0
        if self.state == "vehicle_diagnostics_list":
            return ["Vehicle DTC and ID Information"]
        return []

    def select_list_item_by_text(
        self,
        list_index: int,
        item_text: str,
        double_click: bool = True,
    ) -> dict[str, object]:
        assert list_index == 0
        assert double_click is True
        if self.state == "vehicle_diagnostics_list" and item_text == "Vehicle DTC and ID Information":
            self.state = "transition"
            self._transition_polls = 0
            return {"success": True}
        return {"success": False, "message": f"unexpected item {item_text} on {self.state}"}


def _write_latest_snapshot(path: Path, payload: dict[str, object], encoding: str) -> None:
    path.write_bytes(json.dumps(payload, ensure_ascii=False).encode(encoding))


def test_load_latest_snapshot_decodes_gb18030_json(tmp_path: Path) -> None:
    latest_path = tmp_path / "latest.json"
    payload = {"pageContext": {"moduleName": "控制模块"}, "tables": []}
    _write_latest_snapshot(latest_path, payload, "gb18030")

    loaded = load_latest_snapshot(latest_path)

    assert loaded == payload


def test_extract_navigation_path_prefers_single_column_table() -> None:
    latest_json = {
        "pageContext": {
            "moduleName": "2017,Buick,Envision,Module Diagnostics,Engine Control Module,Configuration/Reset Functions"
        },
        "tables": [
            {
                "columns": [""],
                "rows": [{"": " Module Diagnostics"}, {"": " Engine Control Module"}, {"": " Configuration/Reset Functions"}],
            }
        ],
    }

    assert extract_navigation_path(latest_json) == [
        "Module Diagnostics",
        "Engine Control Module",
        "Configuration/Reset Functions",
    ]


def test_extract_navigation_path_falls_back_when_table_contains_null_tabledata() -> None:
    latest_json = {
        "pageContext": {
            "moduleName": "2017,Buick,Envision,Module Diagnostics,Engine Control Module,Control Functions,Fuel System,Fuel Trim Enable"
        },
        "tables": [
            {
                "columns": [""],
                "rows": [{"": " null_TableData"}, {"": " null_TableData"}],
            }
        ],
    }

    assert extract_navigation_path(latest_json) == [
        "Module Diagnostics",
        "Engine Control Module",
        "Control Functions",
        "Fuel System",
        "Fuel Trim Enable",
    ]


def test_extract_navigation_path_ignores_vehicle_identity_without_route_markers() -> None:
    latest_json = {
        "pageContext": {
            "moduleName": "2017,Buick,Envision"
        },
        "tables": [],
    }

    assert extract_navigation_path(latest_json) == []


def test_gds2_explorer_harness_reaches_data_display_and_records_artifacts(tmp_path: Path) -> None:
    latest_path = tmp_path / "latest.json"
    _write_latest_snapshot(
        latest_path,
        {"pageContext": {"windowTitle": "GDS 2"}, "tables": []},
        "gb18030",
    )
    navigator = _FakeNavigator()

    harness = GDS2ExplorerHarness(
        navigator=navigator,
        latest_json_path=latest_path,
        artifact_root=tmp_path / "artifacts",
        device_controller_factory=lambda: _FakeDeviceExplorer(navigator),
        sleep_fn=lambda _seconds: None,
    )

    result = harness.run_mainline(
        device_name="SM2 USB",
        module_name="Engine Control Module",
        data_category="Engine Data",
    )

    assert result["final_page"] == "data_display"
    assert navigator.selected_device == "SM2 USB"
    assert navigator.clicked_buttons == ["OK", "Diagnostics", "Select Device", "Enter"]
    assert navigator.selected_items == [
        "Module Diagnostics",
        "Engine Control Module",
        "Data Display",
        "Engine Data",
    ]
    assert len(result["snapshots"]) >= 5
    artifact_dir = Path(result["artifact_dir"])
    assert artifact_dir.exists()
    assert any(path.name.endswith(".json") for path in artifact_dir.iterdir())


def test_gds2_explorer_harness_capture_records_observed_actions(tmp_path: Path) -> None:
    latest_path = tmp_path / "latest.json"
    _write_latest_snapshot(
        latest_path,
        {"pageContext": {"windowTitle": "GDS 2"}, "tables": []},
        "gb18030",
    )
    navigator = _FakeNavigator()

    harness = GDS2ExplorerHarness(
        navigator=navigator,
        latest_json_path=latest_path,
        artifact_root=tmp_path / "artifacts",
        device_controller_factory=lambda: _FakeDeviceExplorer(navigator),
        sleep_fn=lambda _seconds: None,
    )

    snapshot = harness.capture("start")

    assert snapshot["node_id"]
    assert any(
        action["kind"] == "button" and action["label"] == "OK"
        for action in snapshot["observed_actions"]
    )


def test_gds2_explorer_harness_writes_route_graph_for_mainline(tmp_path: Path) -> None:
    latest_path = tmp_path / "latest.json"
    _write_latest_snapshot(
        latest_path,
        {"pageContext": {"windowTitle": "GDS 2"}, "tables": []},
        "gb18030",
    )
    navigator = _FakeNavigator()

    harness = GDS2ExplorerHarness(
        navigator=navigator,
        latest_json_path=latest_path,
        artifact_root=tmp_path / "artifacts",
        device_controller_factory=lambda: _FakeDeviceExplorer(navigator),
        sleep_fn=lambda _seconds: None,
    )

    result = harness.run_mainline(
        device_name="SM2 USB",
        module_name="Engine Control Module",
        data_category="Engine Data",
    )

    graph_path = Path(result["graph_path"])
    assert graph_path.exists()

    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    assert "nodes" in graph and graph["nodes"]
    assert "edges" in graph and graph["edges"]
    assert any(node["page_id"] == "device_explorer" for node in graph["nodes"].values())
    assert any(
        edge["action"]["kind"] == "device"
        and edge["action"]["label"] == "SM2 USB"
        for edge in graph["edges"]
    )


def test_gds2_explorer_harness_merges_page_snapshot_buttons_when_button_query_drifts(
    tmp_path: Path,
) -> None:
    latest_path = tmp_path / "latest.json"
    _write_latest_snapshot(
        latest_path,
        {"pageContext": {"windowTitle": "GDS 2"}, "tables": []},
        "gb18030",
    )
    navigator = _RaceyVehicleSelectionNavigator()

    harness = GDS2ExplorerHarness(
        navigator=navigator,
        latest_json_path=latest_path,
        artifact_root=tmp_path / "artifacts",
        device_controller_factory=lambda: _FakeDeviceExplorer(navigator),
        sleep_fn=lambda _seconds: None,
    )

    snapshot = harness.capture("vehicle_selection")

    assert any(
        action["kind"] == "button" and action["label"] == "Select Device"
        for action in snapshot["observed_actions"]
    )


def test_gds2_explorer_harness_treats_loading_with_vehicle_buttons_as_vehicle_selection(
    tmp_path: Path,
) -> None:
    latest_path = tmp_path / "latest.json"
    _write_latest_snapshot(
        latest_path,
        {"pageContext": {"windowTitle": "GDS 2"}, "tables": []},
        "gb18030",
    )
    navigator = _LoadingVehicleSelectionNavigator()

    harness = GDS2ExplorerHarness(
        navigator=navigator,
        latest_json_path=latest_path,
        artifact_root=tmp_path / "artifacts",
        device_controller_factory=lambda: _FakeDeviceExplorer(navigator),
        sleep_fn=lambda _seconds: None,
    )

    result = harness.run_mainline(
        device_name="SM2 USB",
        module_name="Engine Control Module",
        data_category="Engine Data",
    )

    assert result["final_page"] == "data_display"


def test_gds2_explorer_harness_persists_effective_vehicle_selection_page_id(
    tmp_path: Path,
) -> None:
    latest_path = tmp_path / "latest.json"
    _write_latest_snapshot(
        latest_path,
        {"pageContext": {"windowTitle": "GDS 2"}, "tables": []},
        "gb18030",
    )
    navigator = _LoadingVehicleSelectionNavigator()

    harness = GDS2ExplorerHarness(
        navigator=navigator,
        latest_json_path=latest_path,
        artifact_root=tmp_path / "artifacts",
        device_controller_factory=lambda: _FakeDeviceExplorer(navigator),
        sleep_fn=lambda _seconds: None,
    )

    snapshot = harness.capture("vehicle_selection")

    node = harness._graph["nodes"][snapshot["node_id"]]
    assert snapshot["raw_page_id"] == "loading"
    assert snapshot["effective_page_id"] == "vehicle_selection"
    assert node["page_id"] == "vehicle_selection"
    assert node["raw_page_id"] == "loading"


def test_gds2_explorer_harness_does_not_treat_deep_loading_as_vehicle_selection(
    tmp_path: Path,
) -> None:
    latest_path = tmp_path / "latest.json"
    _write_latest_snapshot(
        latest_path,
        {"pageContext": {"windowTitle": "GDS 2"}, "tables": []},
        "gb18030",
    )
    navigator = _DeepLoadingNavigator()

    harness = GDS2ExplorerHarness(
        navigator=navigator,
        latest_json_path=latest_path,
        artifact_root=tmp_path / "artifacts",
        device_controller_factory=lambda: _FakeDeviceExplorer(navigator),
        sleep_fn=lambda _seconds: None,
    )

    snapshot = harness.capture("deep_loading")

    assert snapshot["raw_page_id"] == "loading"
    assert snapshot["effective_page_id"] == "loading"


def test_gds2_explorer_harness_waits_past_transient_unknown_after_button_click(
    tmp_path: Path,
) -> None:
    latest_path = tmp_path / "latest.json"
    _write_latest_snapshot(
        latest_path,
        {"pageContext": {"windowTitle": "GDS 2"}, "tables": []},
        "gb18030",
    )
    navigator = _TransientEnterNavigator()

    harness = GDS2ExplorerHarness(
        navigator=navigator,
        latest_json_path=latest_path,
        artifact_root=tmp_path / "artifacts",
        device_controller_factory=lambda: _FakeDeviceExplorer(navigator),
        sleep_fn=lambda _seconds: None,
    )

    source = harness.capture("start")
    target = harness._click_button("Enter", "after_click_enter", source_snapshot=source)

    assert harness._effective_page_id(target) == "diagnostics_menu"
    assert harness._graph["edges"][-1]["target_page_id"] == "diagnostics_menu"


def test_gds2_explorer_harness_records_settled_target_after_loading_transition(
    tmp_path: Path,
) -> None:
    latest_path = tmp_path / "latest.json"
    _write_latest_snapshot(
        latest_path,
        {"pageContext": {"windowTitle": "GDS 2"}, "tables": []},
        "gb18030",
    )
    navigator = _LoadingThenSettledListNavigator()

    harness = GDS2ExplorerHarness(
        navigator=navigator,
        latest_json_path=latest_path,
        artifact_root=tmp_path / "artifacts",
        device_controller_factory=lambda: _FakeDeviceExplorer(navigator),
        sleep_fn=lambda _seconds: None,
    )

    source = harness.capture("vehicle_diagnostics_list")
    target = harness._select_list_item(
        "Vehicle DTC and ID Information",
        "vehicle_dtc_and_id",
        source_snapshot=source,
    )

    assert target["effective_page_id"] == "data_display"
    assert harness._graph["edges"][-1]["target_page_id"] == "data_display"
    assert harness._graph["edges"][-1]["target_raw_page_id"] == "data_display"
