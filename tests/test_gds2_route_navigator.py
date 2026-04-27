from __future__ import annotations

from dataclasses import dataclass

from backends.gds2.route_navigator import GDS2RouteNavigator
from src.navigation import ControllerSnapshot, NavigationController, find_list_item_match


@dataclass
class _Result:
    success: bool
    error: str | None = None


class _FakeController:
    def __init__(self, start_page: str) -> None:
        self.page = start_page
        self.executed: list[tuple[str, str]] = []
        self.enter_clicks = 0
        self._pages = {
            "main_menu": {
                "buttons": ["Diagnostics", "Home"],
                "lists": [],
            },
            "vehicle_selection": {
                "buttons": ["Back", "Disconnect", "Enter", "Read VIN"],
                "lists": [],
            },
            "diagnostics_menu": {
                "buttons": ["Enter", "Home", "Back"],
                "lists": ["Module Diagnostics", "Vehicle Diagnostics", "Session Manager"],
            },
            "module_list": {
                "buttons": ["Back", "Home", "Vehicle Menu", "Enter"],
                "lists": ["Engine Control Module"],
            },
            "module_submenu": {
                "buttons": ["Back", "Home", "Vehicle Menu", "Enter"],
                "lists": ["Data Display", "Diagnostic Trouble Codes (DTC)"],
            },
            "data_list": {
                "buttons": ["Back", "Enter"],
                "lists": ["Fuel Injector Data", "Engine Data"],
            },
            "data_display": {
                "buttons": ["Add Bookmark", "Back", "Create Report"],
                "lists": [],
            },
            "vehicle_diagnostics_menu": {
                "buttons": ["Back", "Enter", "Home", "Vehicle Menu"],
                "lists": ["Vehicle DTC Information", "Vehicle DTC and ID Information"],
            },
            "unknown_branch": {
                "buttons": ["Back"],
                "lists": [],
            },
        }

    def get_snapshot(self) -> dict:
        page = self._pages[self.page]
        page_id = "data_list" if self.page == "vehicle_diagnostics_menu" else self.page
        return {
            "page": page_id,
            "buttons": list(page["buttons"]),
            "lists": list(page["lists"]),
            "context": {},
        }

    def get_navigation_path(self) -> list[str]:
        page = self._pages.get(self.page, {})
        return list(page.get("navigation_path") or [])

    def go_back(self):
        self.executed.append(("button", "Back"))
        mapping = {
            "vehicle_diagnostics_menu": "diagnostics_menu",
            "module_list": "diagnostics_menu",
            "module_submenu": "module_list",
            "data_list": "module_submenu",
            "data_display": "data_list",
            "unknown_branch": "diagnostics_menu",
        }
        self.page = mapping.get(self.page, self.page)
        return _Result(True)

    def go_home(self):
        self.executed.append(("button", "Home"))
        self.page = "main_menu"
        return _Result(True)

    def click_button(self, label: str):
        self.executed.append(("button", label))
        if self.page == "main_menu" and label == "Diagnostics":
            self.page = "vehicle_selection"
            return _Result(True)
        if label == "Back":
            return self.go_back()
        if label == "Home":
            return self.go_home()
        return _Result(False, error=f"unexpected button {label} on {self.page}")

    def click_navigation_path_item(self, label: str):
        return _Result(False, error=f"unexpected navigation path item {label} on {self.page}")

    def click_enter(self):
        self.executed.append(("button", "Enter"))
        self.enter_clicks += 1
        if self.page == "vehicle_selection":
            self.page = "diagnostics_menu"
            return _Result(True)
        return _Result(False, error=f"unexpected enter on {self.page}")

    def select_list_item(self, label: str):
        self.executed.append(("list_item", label))
        if self.page == "diagnostics_menu" and label == "Module Diagnostics":
            self.page = "module_list"
            return _Result(True)
        if self.page == "module_list" and label == "Engine Control Module":
            self.page = "module_submenu"
            return _Result(True)
        if self.page == "module_submenu" and label == "Data Display":
            self.page = "data_list"
            return _Result(True)
        if self.page == "data_list" and label in {"Fuel Injector Data", "Engine Data"}:
            self.page = "data_display"
            return _Result(True)
        return _Result(False, error=f"unexpected list item {label} on {self.page}")


class _ReplanningController(_FakeController):
    def __init__(self, start_page: str) -> None:
        super().__init__(start_page)
        self._first_back = True

    def go_back(self):
        self.executed.append(("button", "Back"))
        if self.page == "vehicle_diagnostics_menu" and self._first_back:
            self._first_back = False
            self.page = "unknown_branch"
            return _Result(True)
        return super().go_back()


class _LoadingVehicleSelectionController(_FakeController):
    def __init__(self) -> None:
        super().__init__("main_menu")

    def get_snapshot(self) -> dict:
        return {
            "page": "loading",
            "buttons": ["Back", "Disconnect", "Enter", "Read VIN"],
            "lists": [],
            "context": {},
        }


class _LeafFunctionController(_FakeController):
    def __init__(self) -> None:
        super().__init__("data_list")
        self.page = "leaf_function"
        self._pages["leaf_function"] = {
            "buttons": ["Add Bookmark", "Back", "Reset"],
            "lists": ["authored", "Display Name", "ECU", "Units", "Custom Order"],
        }

    def get_snapshot(self) -> dict:
        page = self._pages[self.page]
        page_id = "data_list" if self.page == "leaf_function" else ("data_list" if self.page == "vehicle_diagnostics_menu" else self.page)
        return {
            "page": page_id,
            "buttons": list(page["buttons"]),
            "lists": list(page["lists"]),
            "context": {},
        }

    def go_back(self):
        self.executed.append(("button", "Back"))
        if self.page == "leaf_function":
            self.page = "data_list"
            return _Result(True)
        return super().go_back()


class _WeakMatchListController(_FakeController):
    def __init__(self) -> None:
        super().__init__("data_list")
        self.page = "weak_match_list"
        self._pages["weak_match_list"] = {
            "buttons": ["Back", "Enter", "Home", "Vehicle Menu"],
            "lists": ["Unseen Item A", "Unseen Item B"],
        }

    def get_snapshot(self) -> dict:
        page = self._pages[self.page]
        page_id = "data_list" if self.page == "weak_match_list" else self.page
        return {
            "page": page_id,
            "buttons": list(page["buttons"]),
            "lists": list(page["lists"]),
            "context": {},
        }

    def go_back(self):
        self.executed.append(("button", "Back"))
        if self.page == "weak_match_list":
            self.page = "module_submenu"
            return _Result(True)
        return super().go_back()


class _UniqueContainsController(_FakeController):
    def __init__(self) -> None:
        super().__init__("data_list")
        self.page = "contains_match_list"
        self._pages["contains_match_list"] = {
            "buttons": ["Back", "Enter"],
            "lists": ["[K20] Engine Control Module"],
        }

    def get_snapshot(self) -> dict:
        page = self._pages[self.page]
        page_id = "data_list" if self.page == "contains_match_list" else self.page
        return {
            "page": page_id,
            "buttons": list(page["buttons"]),
            "lists": list(page["lists"]),
            "context": {},
        }

    def select_list_item(self, label: str):
        self.executed.append(("list_item", label))
        if self.page == "contains_match_list" and label == "Engine Control Module":
            self.page = "data_display"
            return _Result(True)
        return super().select_list_item(label)


def _graph() -> dict:
    return {
        "version": 1,
        "created_at": "merged",
        "nodes": {
            "main": {
                "node_id": "main",
                "page_id": "main_menu",
                "observed_actions": [
                    {"kind": "button", "label": "Diagnostics"},
                    {"kind": "button", "label": "Home"},
                ],
                "list_items": [],
                "navigation_path": [],
            },
            "diagnostics": {
                "node_id": "diagnostics",
                "page_id": "diagnostics_menu",
                "observed_actions": [
                    {"kind": "list_item", "label": "Module Diagnostics"},
                    {"kind": "list_item", "label": "Vehicle Diagnostics"},
                    {"kind": "list_item", "label": "Session Manager"},
                ],
                "list_items": ["Module Diagnostics", "Vehicle Diagnostics", "Session Manager"],
                "navigation_path": ["Module Diagnostics"],
            },
            "modules": {
                "node_id": "modules",
                "page_id": "module_list",
                "observed_actions": [{"kind": "list_item", "label": "Engine Control Module"}],
                "list_items": ["Engine Control Module"],
                "navigation_path": ["Module Diagnostics"],
            },
            "submenu": {
                "node_id": "submenu",
                "page_id": "module_submenu",
                "observed_actions": [{"kind": "list_item", "label": "Data Display"}],
                "list_items": ["Data Display", "Diagnostic Trouble Codes (DTC)"],
                "navigation_path": ["Module Diagnostics", "Engine Control Module"],
            },
            "data_list": {
                "node_id": "data_list",
                "page_id": "data_list",
                "observed_actions": [
                    {"kind": "list_item", "label": "Fuel Injector Data"},
                    {"kind": "list_item", "label": "Engine Data"},
                ],
                "list_items": ["Fuel Injector Data", "Engine Data"],
                "navigation_path": ["Module Diagnostics", "Engine Control Module", "Data Display"],
            },
            "vehicle_diagnostics": {
                "node_id": "vehicle_diagnostics",
                "page_id": "data_list",
                "observed_actions": [
                    {"kind": "list_item", "label": "Vehicle DTC Information"},
                    {"kind": "list_item", "label": "Vehicle DTC and ID Information"},
                ],
                "list_items": ["Vehicle DTC Information", "Vehicle DTC and ID Information"],
                "navigation_path": ["Vehicle Diagnostics"],
            },
        },
        "edges": [
            {"source_id": "main", "target_id": "diagnostics", "action": {"kind": "button", "label": "Diagnostics"}},
            {"source_id": "diagnostics", "target_id": "modules", "action": {"kind": "list_item", "label": "Module Diagnostics"}},
            {"source_id": "modules", "target_id": "submenu", "action": {"kind": "list_item", "label": "Engine Control Module"}},
            {"source_id": "submenu", "target_id": "data_list", "action": {"kind": "list_item", "label": "Data Display"}},
            {"source_id": "vehicle_diagnostics", "target_id": "diagnostics", "action": {"kind": "button", "label": "Back"}},
        ],
    }


def test_route_navigator_executes_planned_route_from_known_branch() -> None:
    controller = _FakeController("vehicle_diagnostics_menu")
    navigator = GDS2RouteNavigator(controller=controller, graph=_graph())

    result = navigator.navigate_to_action("Fuel Injector Data", kind="list_item")

    assert result["final_page"] == "data_display"
    assert result["executed_actions"] == [
        {"kind": "button", "label": "Back"},
        {"kind": "list_item", "label": "Module Diagnostics"},
        {"kind": "list_item", "label": "Engine Control Module"},
        {"kind": "list_item", "label": "Data Display"},
        {"kind": "list_item", "label": "Fuel Injector Data"},
    ]


def test_route_navigator_backtracks_to_anchor_when_current_page_is_unknown() -> None:
    controller = _FakeController("unknown_branch")
    navigator = GDS2RouteNavigator(controller=controller, graph=_graph())

    result = navigator.navigate_to_action("Fuel Injector Data", kind="list_item")

    assert result["final_page"] == "data_display"
    assert result["recovery_actions"][0] == {"kind": "button", "label": "Back"}


def test_route_navigator_replans_after_intermediate_unknown_state() -> None:
    controller = _ReplanningController("vehicle_diagnostics_menu")
    navigator = GDS2RouteNavigator(controller=controller, graph=_graph())

    result = navigator.navigate_to_action("Fuel Injector Data", kind="list_item")

    assert result["final_page"] == "data_display"
    assert result["executed_actions"][-1] == {"kind": "list_item", "label": "Fuel Injector Data"}


def test_route_navigator_classifies_loading_vehicle_selection_snapshot() -> None:
    controller = _LoadingVehicleSelectionController()
    navigator = GDS2RouteNavigator(controller=controller, graph=_graph())

    snapshot = navigator._capture_snapshot()

    assert snapshot["raw_page_id"] == "loading"
    assert snapshot["effective_page_id"] == "vehicle_selection"
    assert snapshot["classification_evidence"]["rule"] == "loading_vehicle_markers"


def test_route_navigator_prefers_raw_controller_snapshot_contract() -> None:
    class _RawSnapshotController(_FakeController):
        def __init__(self) -> None:
            super().__init__("data_list")
            self.capture_modes: list[str] = []

        def get_controller_snapshot(self, *, capture_mode: str):
            self.capture_modes.append(capture_mode)
            return ControllerSnapshot(
                raw_page_id="loading",
                buttons=("Back", "Disconnect", "Enter", "Read VIN"),
                list_items=(),
                context={"device": "VCI Proxy (Remote)"},
                capture_source="test",
                capture_mode=capture_mode,
            )

        def get_snapshot(self):  # pragma: no cover - should not be used
            raise AssertionError("route navigator should use get_controller_snapshot first")

    controller = _RawSnapshotController()
    navigator = GDS2RouteNavigator(controller=controller, graph=_graph())

    snapshot = navigator._capture_snapshot()

    assert controller.capture_modes == ["route_navigator_snapshot"]
    assert snapshot["raw_page_id"] == "loading"
    assert snapshot["effective_page_id"] == "vehicle_selection"
    assert snapshot["classification_evidence"]["rule"] == "loading_vehicle_markers"


def test_route_navigator_overrides_stale_vehicle_selection_when_list_is_diagnostics_menu() -> None:
    effective_page = GDS2RouteNavigator._classify_effective_page(
        "vehicle_selection",
        ["Back", "Home", "Enter"],
        ["Module Diagnostics", "Vehicle Diagnostics", "Session Manager"],
    )

    assert effective_page == "diagnostics_menu"


def test_route_navigator_infers_button_kind_when_kind_is_omitted() -> None:
    controller = _FakeController("main_menu")
    navigator = GDS2RouteNavigator(controller=controller, graph=_graph())

    result = navigator.navigate_to_action("Diagnostics")

    assert result["executed_actions"] == [{"kind": "button", "label": "Diagnostics"}]
    assert result["final_page"] == "vehicle_selection"


def test_route_navigator_snapshot_includes_navigation_path_from_reader() -> None:
    controller = _FakeController("main_menu")
    navigator = GDS2RouteNavigator(
        controller=controller,
        graph=_graph(),
        latest_json_reader=lambda: {
            "pageContext": {
                "moduleName": "2017,Buick,Envision,Module Diagnostics,Engine Control Module,Control Functions"
            },
            "tables": [
                {
                    "columns": [""],
                    "rows": [
                        {"": " Module Diagnostics"},
                        {"": " Engine Control Module"},
                        {"": " Control Functions"},
                    ],
                }
            ],
        },
    )

    snapshot = navigator._capture_snapshot()

    assert snapshot["navigation_path"] == [
        "Module Diagnostics",
        "Engine Control Module",
        "Control Functions",
    ]


def test_controller_snapshot_preserves_raw_contract_without_gds2_derived_fields() -> None:
    snapshot = ControllerSnapshot.from_legacy_dict(
        {
            "page": "vehicle_selection",
            "buttons": [" Enter ", "Back", ""],
            "lists": [" Module Diagnostics ", ""],
            "context": {"device": "VCI Proxy (Remote)"},
            "navigation_path": ["Module Diagnostics"],
            "effective_page_id": "diagnostics_menu",
            "classification_evidence": {"rule": "derived"},
            "ambiguity_metadata": {"matches": []},
            "derived_action_data": {"actions": []},
        },
        capture_source="test",
        capture_mode="unit",
        captured_at=123.0,
    )

    assert snapshot.raw_page_id == "vehicle_selection"
    assert snapshot.raw_visible_buttons == ("Enter", "Back")
    assert snapshot.raw_list_items == ("Module Diagnostics",)
    assert snapshot.navigation_path == ("Module Diagnostics",)
    assert snapshot.capture_source == "test"
    assert snapshot.capture_mode == "unit"
    assert snapshot.captured_at == 123.0

    legacy = snapshot.to_legacy_dict()
    assert legacy == {
        "page": "vehicle_selection",
        "buttons": ["Enter", "Back"],
        "lists": ["Module Diagnostics"],
        "context": {"device": "VCI Proxy (Remote)"},
        "navigation_path": ["Module Diagnostics"],
    }
    assert "effective_page_id" not in legacy
    assert "classification_evidence" not in legacy
    assert "ambiguity_metadata" not in legacy
    assert "derived_action_data" not in legacy


def test_route_navigator_bridges_from_main_menu_into_diagnostics_tree() -> None:
    controller = _FakeController("main_menu")
    navigator = GDS2RouteNavigator(controller=controller, graph=_graph())

    result = navigator.navigate_to_action("Fuel Injector Data", kind="list_item")

    assert result["final_page"] == "data_display"
    assert result["executed_actions"][0] == {"kind": "button", "label": "Diagnostics"}
    assert result["executed_actions"][1] == {"kind": "button", "label": "Enter"}


def test_route_navigator_uses_specialized_enter_action_when_available() -> None:
    controller = _FakeController("vehicle_selection")
    navigator = GDS2RouteNavigator(controller=controller, graph=_graph())

    result = navigator.navigate_to_action("Fuel Injector Data", kind="list_item")

    assert controller.enter_clicks >= 1
    assert result["executed_actions"][0] == {"kind": "button", "label": "Enter"}


def test_route_navigator_backtracks_out_of_leaf_function_page_before_matching() -> None:
    controller = _LeafFunctionController()
    navigator = GDS2RouteNavigator(controller=controller, graph=_graph())

    result = navigator.navigate_to_action("Fuel Injector Data", kind="list_item")

    assert result["final_page"] == "data_display"
    assert result["recovery_actions"][0] == {"kind": "button", "label": "Back"}


def test_route_navigator_rejects_weak_data_list_match_and_backtracks() -> None:
    controller = _WeakMatchListController()
    navigator = GDS2RouteNavigator(controller=controller, graph=_graph())

    result = navigator.navigate_to_action("Fuel Injector Data", kind="list_item")

    assert result["final_page"] == "data_display"
    assert result["recovery_actions"][0] == {"kind": "button", "label": "Back"}


def test_route_navigator_waits_for_list_page_to_populate() -> None:
    snapshot = {
        "effective_page_id": "data_list",
        "observed_actions": [
            {"kind": "button", "label": "Back"},
            {"kind": "button", "label": "Enter"},
        ],
        "list_items": [],
    }

    assert GDS2RouteNavigator._snapshot_needs_settle(snapshot) is True


def test_route_navigator_waits_for_vehicle_selection_to_finish_connecting() -> None:
    snapshot = {
        "effective_page_id": "vehicle_selection",
        "observed_actions": [
            {"kind": "button", "label": "Back"},
            {"kind": "button", "label": "Disconnect"},
        ],
        "list_items": [],
    }

    assert GDS2RouteNavigator._snapshot_needs_settle(snapshot) is True


def test_route_navigator_matches_list_item_by_substring_for_actionability() -> None:
    snapshot = {
        "observed_actions": [
            {"kind": "list_item", "label": "[K20] Engine Control Module", "list_index": 0}
        ]
    }

    assert GDS2RouteNavigator._snapshot_has_action(
        snapshot,
        "Engine Control Module",
        kind="list_item",
    ) is True


def test_shared_list_item_matcher_rejects_ambiguous_contains_match() -> None:
    match = find_list_item_match(
        "Fuel Trim",
        ["Fuel Trim Enable", "Fuel Trim Disable"],
    )

    assert match.matched is False
    assert match.ambiguous is True
    assert match.candidate_labels == ("Fuel Trim Enable", "Fuel Trim Disable")
    assert NavigationController._find_matching_list_item(
        "Fuel Trim",
        ["Fuel Trim Enable", "Fuel Trim Disable"],
    ) == (None, None)


def test_route_navigator_rejects_ambiguous_substring_list_item_match() -> None:
    snapshot = {
        "observed_actions": [
            {"kind": "list_item", "label": "Fuel Trim Enable", "list_index": 0},
            {"kind": "list_item", "label": "Fuel Trim Disable", "list_index": 0},
        ]
    }

    match = GDS2RouteNavigator._action_match(snapshot, "Fuel Trim", kind="list_item")

    assert match.ambiguous is True
    assert GDS2RouteNavigator._snapshot_has_action(
        snapshot,
        "Fuel Trim",
        kind="list_item",
    ) is False


def test_route_navigator_records_diagnostics_for_unique_contains_match() -> None:
    controller = _UniqueContainsController()
    navigator = GDS2RouteNavigator(controller=controller, graph=_graph())

    result = navigator.navigate_to_action("Engine Control Module", kind="list_item")

    assert result["final_page"] == "data_display"
    assert result["match_diagnostics"] == [
        {
            "target_label": "Engine Control Module",
            "action_kind": "list_item",
            "owner": "route_navigator.direct_target",
            "selected_policy": "unique_contains",
            "candidate_labels": ["[K20] Engine Control Module"],
            "resolution": "unique_contains",
        }
    ]


class _BreadcrumbJumpController(_FakeController):
    def __init__(self, *, breadcrumb_changes_page: bool = True) -> None:
        super().__init__("fuel_system")
        self.breadcrumb_changes_page = breadcrumb_changes_page
        self.breadcrumb_clicks: list[str] = []
        self._pages["control_functions"] = {
            "buttons": ["Back", "Home", "Vehicle Menu", "Enter"],
            "lists": ["Fuel System", "Turbocharger", "Reset Learned Values"],
            "navigation_path": ["Module Diagnostics", "Engine Control Module", "Control Functions"],
        }
        self._pages["fuel_system"] = {
            "buttons": ["Back", "Enter"],
            "lists": ["Fuel Trim Enable", "Fuel Rail Pressure"],
            "navigation_path": [
                "Module Diagnostics",
                "Engine Control Module",
                "Control Functions",
                "Fuel System",
            ],
        }

    def get_snapshot(self) -> dict:
        page = self._pages[self.page]
        page_id = "data_list" if self.page in {"control_functions", "fuel_system"} else self.page
        return {
            "page": page_id,
            "buttons": list(page["buttons"]),
            "lists": list(page["lists"]),
            "context": {},
        }

    def go_back(self):
        self.executed.append(("button", "Back"))
        if self.page == "fuel_system":
            self.page = "control_functions"
            return _Result(True)
        if self.page == "control_functions":
            self.page = "module_submenu"
            return _Result(True)
        return super().go_back()

    def click_navigation_path_item(self, label: str):
        self.executed.append(("navigation_path", label))
        self.breadcrumb_clicks.append(label)
        if not self.breadcrumb_changes_page:
            return _Result(True)
        mapping = {
            "Module Diagnostics": "diagnostics_menu",
            "Engine Control Module": "module_submenu",
            "Control Functions": "control_functions",
        }
        target_page = mapping.get(label)
        if target_page is None:
            return _Result(False, error=f"unexpected navigation path item {label} on {self.page}")
        self.page = target_page
        return _Result(True)


def test_route_navigator_prefers_navigation_path_jump_to_common_ancestor() -> None:
    controller = _BreadcrumbJumpController()
    navigator = GDS2RouteNavigator(controller=controller, graph=_graph())

    result = navigator.navigate_to_action("Fuel Injector Data", kind="list_item")

    assert result["final_page"] == "data_display"
    assert result["recovery_actions"] == [{"kind": "navigation_path", "label": "Engine Control Module"}]
    assert controller.breadcrumb_clicks == ["Engine Control Module"]
    assert result["executed_actions"] == [
        {"kind": "list_item", "label": "Data Display"},
        {"kind": "list_item", "label": "Fuel Injector Data"},
    ]


def test_route_navigator_falls_back_when_navigation_path_click_is_noop() -> None:
    controller = _BreadcrumbJumpController(breadcrumb_changes_page=False)
    navigator = GDS2RouteNavigator(controller=controller, graph=_graph())

    result = navigator.navigate_to_action("Fuel Injector Data", kind="list_item")

    assert result["final_page"] == "data_display"
    assert controller.breadcrumb_clicks[:2] == ["Engine Control Module", "Engine Control Module"]
    assert result["recovery_actions"][0] == {"kind": "button", "label": "Back"}
    assert {"kind": "navigation_path", "label": "Engine Control Module"} not in result["recovery_actions"]


def test_route_navigator_prefers_controller_navigation_path_over_latest_json() -> None:
    controller = _BreadcrumbJumpController()
    navigator = GDS2RouteNavigator(
        controller=controller,
        graph=_graph(),
        latest_json_reader=lambda: {
            "pageContext": {
                "moduleName": "2017,Buick,Envision,Vehicle Diagnostics,Wrong Branch"
            }
        },
    )

    snapshot = navigator._capture_snapshot()

    assert snapshot["navigation_path"] == [
        "Module Diagnostics",
        "Engine Control Module",
        "Control Functions",
        "Fuel System",
    ]


def test_route_navigator_public_seams_delegate_to_existing_logic() -> None:
    controller = _FakeController("main_menu")
    graph = _graph()
    navigator = GDS2RouteNavigator(controller=controller, graph=graph)

    snapshot = navigator.capture_snapshot()

    assert navigator.graph is graph
    assert snapshot["effective_page_id"] == "main_menu"
    assert navigator.match_snapshot(snapshot) == "main"
    assert navigator.snapshot_has_action(snapshot, "Diagnostics", kind="button") is True
    assert navigator.resolve_bridge_action(snapshot) == {"kind": "button", "label": "Diagnostics"}
    assert snapshot["classification_evidence"]["rule"] == "raw_page"
    assert snapshot["ambiguity_metadata"] == {}
    assert snapshot["derived_action_data"] == {
        "button_labels": ["Diagnostics", "Home"],
        "list_item_labels": [],
    }
