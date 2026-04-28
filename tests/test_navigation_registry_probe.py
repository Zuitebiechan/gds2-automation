from __future__ import annotations

import inspect
import shutil
import uuid
from pathlib import Path

import pytest

from backends.gds2.registry_navigation_runtime import (
    action_available,
    clear_dtcs_state_matches,
    data_display_recovery_targets,
    decide_vehicle_selection_action,
    executed_actions_available,
    first_pending_route_step,
    LoadingWatchdog,
    handle_j2534_disconnect,
    match_page_states,
    normalize_default_vci_name,
    policy_for_state,
    recover_to_registry_common_ancestor,
    run_probe,
    select_next_route_step,
    should_defer_recovery_for_route_action,
    should_try_pending_list_action,
    startup_target_list_action,
)
from backends.gds2.registry_navigation_runtime import RegistryNavigationRuntime
from backends.gds2.registry_navigation_runtime import (
    GDS2ClearDTCFlow,
    GDS2RecoveryCoordinator,
    GDS2RegistryRouteExecutor,
)
from backends.gds2.registry_navigation_runtime import evaluate_success_criteria
from src.navigation.action_matcher import find_action_match
from diagnostic_platform.runtime.navigation_runtime import NavSession


def test_vehicle_selection_status_connected_prefers_enter() -> None:
    action = decide_vehicle_selection_action(
        {"vehicle_selection_status": "connected_session"},
        {"Enter", "Disconnect", "Back"},
    )

    assert action == "enter"


def test_registry_runtime_collaborators_use_ports_boundary() -> None:
    for collaborator in (
        GDS2RecoveryCoordinator,
        GDS2ClearDTCFlow,
        GDS2RegistryRouteExecutor,
    ):
        source = inspect.getsource(collaborator)
        assert "self._runtime" not in source
        assert "runtime._" not in source
        assert "self._ports" in source


def test_vehicle_selection_status_connecting_waits() -> None:
    action = decide_vehicle_selection_action(
        {"vehicle_selection_status": "connecting"},
        {"Back"},
    )

    assert action == "wait"


def test_vehicle_selection_status_disconnected_selects_device() -> None:
    action = decide_vehicle_selection_action(
        {"vehicle_selection_status": "disconnected"},
        {"Select Device", "Back"},
    )

    assert action == "select_device"


def test_page_state_matcher_uses_seed_signals_for_vehicle_selection() -> None:
    states = [
        {
            "state_key": "vehicle_selection.connected_session",
            "page_id": "vehicle_selection",
            "signals": {
                "text_contains": "Vehicle data is loaded from session",
                "buttons_enabled": ["Enter"],
            },
        }
    ]

    assert match_page_states(
        snapshot={"effective_page_id": "vehicle_selection", "observed_actions": []},
        page_info={
            "vehicle_selection_message": "Vehicle data is loaded from session. Press ENTER to continue.",
            "button_enabled": {"Enter": True},
        },
        page_states=states,
    ) == ["vehicle_selection.connected_session"]


def test_page_state_matcher_enforces_native_window_title_signal() -> None:
    states = [
        {
            "state_key": "device_explorer.visible",
            "page_id": "device_explorer",
            "signals": {"native_window_title_contains": "Device Explorer"},
        }
    ]

    assert match_page_states(
        snapshot={"effective_page_id": "device_explorer", "observed_actions": []},
        page_info={"window_title": "Device Explorer"},
        page_states=states,
    ) == ["device_explorer.visible"]
    assert match_page_states(
        snapshot={"effective_page_id": "device_explorer", "observed_actions": []},
        page_info={"window_title": "GDS 2"},
        page_states=states,
    ) == []


def test_page_state_matcher_enforces_prior_context_signal() -> None:
    states = [
        {
            "state_key": "j2534_disconnect.visible",
            "page_id": "j2534_disconnect",
            "signals": {"prior_context_any": ["data_display", "data_list"]},
        }
    ]

    assert match_page_states(
        snapshot={"effective_page_id": "j2534_disconnect", "prior_context": "data_display", "observed_actions": []},
        page_info={},
        page_states=states,
    ) == ["j2534_disconnect.visible"]
    assert match_page_states(
        snapshot={"effective_page_id": "j2534_disconnect", "prior_context": "main_menu", "observed_actions": []},
        page_info={},
        page_states=states,
    ) == []


def test_policy_for_state_selects_policy_by_applies_to() -> None:
    policy = policy_for_state(
        [
            {
                "policy_key": "vehicle_selection.connected_enter",
                "applies_to": ["vehicle_selection.connected_session"],
                "action_key": "click_enter",
            }
        ],
        "vehicle_selection.connected_session",
    )

    assert policy["action_key"] == "click_enter"


def test_j2534_disconnect_policy_clicks_ok_when_available() -> None:
    class _Controller:
        def __init__(self) -> None:
            self.clicked = []

        def get_available_buttons(self):
            return {"OK": True}

        def click_button(self, label):
            self.clicked.append(label)
            return type("R", (), {"success": True})()

        def go_back(self):
            raise AssertionError("Back should not be used when OK is available")

    recovery_actions = []
    controller = _Controller()

    assert handle_j2534_disconnect(
        controller=controller,
        recovery_actions=recovery_actions,
        policy={"params": {"soft_retry_attempts": 1, "ok_timeout_sec": 0}},
    ) is True
    assert controller.clicked == ["OK"]
    assert recovery_actions[0]["reason"] == "j2534_disconnect soft recovery"


def test_j2534_disconnect_policy_uses_legacy_data_display_recovery() -> None:
    class _Result:
        success = True
        page = type("P", (), {"value": "data_display"})()
        context = {"recovery_method": "backtrack"}

    class _Controller:
        current_data_category = None
        current_sub_category = None

        def __init__(self) -> None:
            self.context_updates = []
            self.calls = []

        def set_context(self, **kwargs):
            self.context_updates.append(dict(kwargs))

        def recover_data_display_connection(self, **kwargs):
            self.calls.append(dict(kwargs))
            return _Result()

    recovery_actions = []
    controller = _Controller()

    assert handle_j2534_disconnect(
        controller=controller,
        recovery_actions=recovery_actions,
        policy={
            "params": {
                "soft_retry_attempts": 3,
                "ok_timeout_sec": 2.0,
                "backtrack_attempts": 2,
                "retry_delays": [0.0, 1.5, 3.0],
            }
        },
        recovery_data_category="Engine Data",
        recovery_sub_category="Fuel System",
        use_legacy_data_display_recovery=True,
    ) is True
    assert controller.context_updates == [{"data_category": "Engine Data", "sub_category": "Fuel System"}]
    assert controller.calls == [
        {
            "data_category": "Engine Data",
            "soft_retry_attempts": 3,
            "ok_timeout": 2.0,
            "allow_backtrack": True,
            "backtrack_attempts": 2,
            "retry_delays": [0.0, 1.5, 3.0],
        }
    ]
    assert recovery_actions == [
        {
            "kind": "button",
            "label": "Back",
            "reason": "j2534_disconnect fallback backtrack",
            "success": True,
        }
    ]


def test_data_display_recovery_targets_use_canonical_path_before_context() -> None:
    class _Controller:
        current_data_category = "Stale Data"
        current_sub_category = "Stale Sub"

    data_category, sub_category = data_display_recovery_targets(
        {
            "page_kind": "data_display",
            "canonical_path": [
                "Module Diagnostics",
                "Engine Control Module",
                "Data Display",
                "Fuel Trim Data",
            ],
        },
        controller=_Controller(),
    )

    assert (data_category, sub_category) == ("Fuel Trim Data", None)


def test_vehicle_selection_status_falls_back_to_buttons_when_status_missing() -> None:
    assert decide_vehicle_selection_action({}, {"Enter", "Back"}) == "enter"
    assert decide_vehicle_selection_action({}, {"Select Device", "Back"}) == "select_device"
    assert decide_vehicle_selection_action({}, {"Disconnect", "Back"}) == "wait"


def test_normalize_default_vci_name_prefers_proxy_remote() -> None:
    assert normalize_default_vci_name("") == "VCI Proxy (Remote)"
    assert normalize_default_vci_name(None) == "VCI Proxy (Remote)"
    assert normalize_default_vci_name("default") == "VCI Proxy (Remote)"
    assert normalize_default_vci_name("SM2 USB") == "SM2 USB"


def test_success_criteria_supports_all_any_and_path_prefix() -> None:
    snapshot = {
        "effective_page_id": "data_display",
        "navigation_path": ["Module Diagnostics", "Engine Control Module", "Diagnostic Trouble Codes (DTC)"],
        "observed_actions": [
            {"kind": "button", "label": "Clear DTCs"},
            {"kind": "button", "label": "Refresh"},
            {"kind": "button", "label": "Create Report"},
        ],
    }

    success, details = evaluate_success_criteria(
        snapshot,
        {
            "page_id_any": ["data_display"],
            "all_of": [
                {"kind": "button", "label": "Clear DTCs"},
                {"kind": "button", "label": "Refresh"},
            ],
            "any_of": [
                {"kind": "button", "label": "Create Report"},
                {"kind": "button", "label": "Details"},
            ],
            "navigation_path_contains_prefix": [
                "Module Diagnostics",
                "Engine Control Module",
                "Diagnostic Trouble Codes (DTC)",
                "DTC Display",
            ],
        },
    )

    assert success is True
    assert details["missing_all_of"] == []
    assert details["matched_any_of"] == [{"kind": "button", "label": "Create Report"}]


def test_success_criteria_accepts_full_path_when_contains_prefix_is_shorter() -> None:
    snapshot = {
        "effective_page_id": "data_display",
        "navigation_path": ["Module Diagnostics", "Engine Control Module", "Data Display", "Engine Data"],
        "observed_actions": [
            {"kind": "button", "label": "Back"},
            {"kind": "button", "label": "Create Report"},
            {"kind": "button", "label": "Add Bookmark"},
        ],
    }

    success, details = evaluate_success_criteria(
        snapshot,
        {
            "page_id_any": ["data_display"],
            "all_of": [
                {"kind": "button", "label": "Create Report"},
                {"kind": "button", "label": "Back"},
            ],
            "any_of": [
                {"kind": "button", "label": "Clear DTCs"},
                {"kind": "button", "label": "Add Bookmark"},
            ],
            "navigation_path_contains_prefix": [
                "Module Diagnostics",
                "Engine Control Module",
            ],
        },
    )

    assert success is True
    assert details["path_ok"] is True


def test_success_criteria_supports_variants() -> None:
    snapshot = {
        "effective_page_id": "clear_dtcs_selection",
        "observed_actions": [
            {"kind": "button", "label": "Add All"},
            {"kind": "button", "label": "Cancel"},
        ],
    }

    success, details = evaluate_success_criteria(
        snapshot,
        {
            "page_id_any": ["clear_dtcs_selection", "clear_dtcs_confirmation"],
            "variants": [
                {
                    "name": "selection",
                    "all_of": [
                        {"kind": "button", "label": "Add All"},
                        {"kind": "button", "label": "Cancel"},
                    ],
                },
                {
                    "name": "confirmation",
                    "all_of": [
                        {"kind": "button", "label": "OK"},
                        {"kind": "button", "label": "Cancel"},
                    ],
                },
            ],
        },
    )

    assert success is True
    assert any(item["name"] == "selection" and item["missing_all_of"] == [] for item in details["variant_results"])


def test_action_available_uses_unique_contains_without_ambiguous_clickability() -> None:
    unique_snapshot = {
        "observed_actions": [
            {"kind": "list_item", "label": "[K20] Engine Control Module", "list_index": 0},
        ]
    }
    ambiguous_snapshot = {
        "observed_actions": [
            {"kind": "list_item", "label": "Fuel Trim Enable", "list_index": 0},
            {"kind": "list_item", "label": "Fuel Trim Disable", "list_index": 0},
        ]
    }

    assert action_available(
        unique_snapshot,
        {"kind": "list_item", "label": "Engine Control Module"},
    ) is True
    assert action_available(
        ambiguous_snapshot,
        {"kind": "list_item", "label": "Fuel Trim"},
    ) is False


def test_first_pending_route_step_advances_through_executed_prefix() -> None:
    route_steps = [
        {"kind": "button", "label": "Clear DTCs"},
        {"kind": "button", "label": "Add All"},
        {"kind": "button", "label": "OK"},
    ]

    assert first_pending_route_step(route_steps, []) == {"kind": "button", "label": "Clear DTCs"}
    assert first_pending_route_step(
        route_steps,
        [{"kind": "button", "label": "Clear DTCs"}],
    ) == {"kind": "button", "label": "Add All"}
    assert first_pending_route_step(
        route_steps,
        [
            {"kind": "button", "label": "Clear DTCs"},
            {"kind": "button", "label": "Add All"},
            {"kind": "button", "label": "OK"},
        ],
    ) is None


def test_select_next_route_step_skips_navigation_path_prefix_and_uses_available_action() -> None:
    route_steps = [
        {"kind": "list_item", "label": "Module Diagnostics"},
        {"kind": "list_item", "label": "Engine Control Module"},
        {"kind": "list_item", "label": "Diagnostic Trouble Codes (DTC)"},
        {"kind": "list_item", "label": "DTC Display"},
        {"kind": "button", "label": "Clear DTCs"},
        {"kind": "button", "label": "Add All"},
        {"kind": "button", "label": "OK"},
    ]
    snapshot = {
        "navigation_path": [
            "Module Diagnostics",
            "Engine Control Module",
            "Diagnostic Trouble Codes (DTC)",
        ],
        "observed_actions": [
            {"kind": "button", "label": "Add All"},
            {"kind": "button", "label": "Cancel"},
        ],
    }

    assert select_next_route_step(route_steps, [], snapshot) == {"kind": "button", "label": "Add All"}


def test_select_next_route_step_skips_previously_executed_middle_action() -> None:
    route_steps = [
        {"kind": "list_item", "label": "Module Diagnostics"},
        {"kind": "list_item", "label": "Engine Control Module"},
        {"kind": "list_item", "label": "Diagnostic Trouble Codes (DTC)"},
        {"kind": "list_item", "label": "DTC Display"},
        {"kind": "button", "label": "Clear DTCs"},
        {"kind": "button", "label": "Add All"},
        {"kind": "button", "label": "OK"},
    ]
    snapshot = {
        "navigation_path": [
            "Module Diagnostics",
            "Engine Control Module",
            "Diagnostic Trouble Codes (DTC)",
        ],
        "observed_actions": [
            {"kind": "button", "label": "OK"},
            {"kind": "button", "label": "Cancel"},
        ],
    }

    assert select_next_route_step(
        route_steps,
        [{"kind": "button", "label": "Add All"}],
        snapshot,
    ) == {"kind": "button", "label": "OK"}


def test_select_next_route_step_handles_duplicate_button_labels_in_sequence() -> None:
    route_steps = [
        {"kind": "button", "label": "Clear DTCs"},
        {"kind": "button", "label": "Add All"},
        {"kind": "button", "label": "OK"},
        {"kind": "button", "label": "OK"},
    ]
    snapshot = {
        "observed_actions": [
            {"kind": "button", "label": "OK"},
            {"kind": "button", "label": "Cancel"},
        ],
    }

    assert select_next_route_step(
        route_steps,
        [
            {"kind": "button", "label": "Clear DTCs"},
            {"kind": "button", "label": "Add All"},
            {"kind": "button", "label": "OK"},
        ],
        snapshot,
    ) == {"kind": "button", "label": "OK"}


def test_select_next_route_step_prefers_required_step_when_optional_is_unavailable() -> None:
    route_steps = [
        {"kind": "device", "label": "VCI Proxy (Remote)", "optional": True},
        {"kind": "list_item", "label": "Module Diagnostics"},
    ]
    snapshot = {
        "observed_actions": [
            {"kind": "button", "label": "Back"},
            {"kind": "button", "label": "Home"},
        ],
    }

    assert select_next_route_step(route_steps, [], snapshot) == {
        "kind": "list_item",
        "label": "Module Diagnostics",
    }


def test_registry_route_executor_tries_graph_action_before_home_recovery() -> None:
    class _Controller:
        def __init__(self) -> None:
            self.home_calls = 0

        def go_home(self):  # pragma: no cover - should not be called
            self.home_calls += 1
            raise AssertionError("route executor should not recover Home from diagnostics_menu")

        def go_back(self):  # pragma: no cover - should not be called
            raise AssertionError("route executor should not recover Back from diagnostics_menu")

    class _RouteNavigator:
        graph = {
            "nodes": {
                "diagnostics": {
                    "page_id": "diagnostics_menu",
                    "observed_actions": [
                        {"kind": "list_item", "label": "Module Diagnostics"},
                    ],
                },
            }
        }

        def __init__(self) -> None:
            self.page = "diagnostics_menu"
            self.executed: list[dict[str, str]] = []

        def capture_settled_snapshot(self) -> dict[str, object]:
            if self.page == "module_list":
                return {
                    "effective_page_id": "module_list",
                    "observed_actions": [
                        {"kind": "list_item", "label": "Engine Control Module"},
                    ],
                    "list_items": ["Engine Control Module"],
                    "navigation_path": ["Module Diagnostics"],
                }
            return {
                "effective_page_id": "diagnostics_menu",
                "observed_actions": [
                    {"kind": "button", "label": "Back"},
                    {"kind": "button", "label": "Home"},
                    {"kind": "button", "label": "Enter"},
                ],
                "list_items": [],
                "navigation_path": [],
            }

        def resolve_bridge_action(self, snapshot):
            return None

        def snapshot_action_match(self, snapshot, label, *, kind=None):
            return find_action_match(snapshot.get("observed_actions") or [], label, kind=kind)

        def execute_action(self, action: dict[str, str]) -> None:
            self.executed.append({"kind": str(action["kind"]), "label": str(action["label"])})
            if action == {"kind": "list_item", "label": "Module Diagnostics"}:
                self.page = "module_list"
                return
            raise AssertionError(f"unexpected action: {action}")

        def match_snapshot(self, snapshot):
            return None

    route_navigator = _RouteNavigator()
    runtime = RegistryNavigationRuntime(
        controller=_Controller(),
        route_navigator=route_navigator,
        entries=[],
    )
    entry = {
        "page_key": "diagnostics.module_diagnostics",
        "canonical_path": ["Module Diagnostics"],
        "route_steps": [
            {"kind": "device", "label": "VCI Proxy (Remote)", "optional": True},
            {"kind": "list_item", "label": "Module Diagnostics"},
        ],
        "success_criteria": {
            "page_id_any": ["module_list"],
            "all_of": [{"kind": "list_item", "label": "Engine Control Module"}],
        },
    }

    result = runtime.execute_registry_route(
        entry=entry,
        max_iterations=3,
        max_backtracks=1,
    )

    assert route_navigator.executed == [{"kind": "list_item", "label": "Module Diagnostics"}]
    assert result["final_page"] == "module_list"


def test_should_try_pending_list_action_allows_graph_action_on_empty_menu_snapshot() -> None:
    snapshot = {
        "effective_page_id": "diagnostics_menu",
        "observed_actions": [
            {"kind": "button", "label": "Back"},
            {"kind": "button", "label": "Home"},
            {"kind": "button", "label": "Enter"},
        ],
        "list_items": [],
    }

    assert should_try_pending_list_action(
        graph={
            "nodes": {
                "diagnostics": {
                    "page_id": "diagnostics_menu",
                    "observed_actions": [
                        {"kind": "list_item", "label": "Module Diagnostics"},
                    ],
                },
            },
        },
        snapshot=snapshot,
        pending_route_step={"kind": "list_item", "label": "Module Diagnostics"},
        target_path=["Module Diagnostics"],
    ) is True


def test_should_try_pending_list_action_does_not_force_invisible_data_target() -> None:
    snapshot = {
        "effective_page_id": "module_submenu",
        "observed_actions": [
            {"kind": "button", "label": "Back"},
            {"kind": "list_item", "label": "Data Display"},
        ],
        "list_items": ["Data Display"],
    }

    assert should_try_pending_list_action(
        graph={},
        snapshot=snapshot,
        pending_route_step={"kind": "list_item", "label": "Engine Data"},
        target_path=["Module Diagnostics", "Engine Control Module", "Data Display", "Engine Data"],
    ) is False


def test_startup_target_list_action_uses_target_when_optional_step_is_pending() -> None:
    snapshot = {
        "effective_page_id": "diagnostics_menu",
        "observed_actions": [
            {"kind": "button", "label": "Back"},
            {"kind": "button", "label": "Home"},
            {"kind": "button", "label": "Enter"},
        ],
        "list_items": [],
        "navigation_path": ["Vehicle Diagnostics"],
    }

    assert startup_target_list_action(
        snapshot=snapshot,
        pending_route_step={"kind": "device", "label": "VCI Proxy (Remote)", "optional": True},
        target_action={"kind": "list_item", "label": "Module Diagnostics"},
        target_path=["Module Diagnostics"],
    ) == {"kind": "list_item", "label": "Module Diagnostics"}


def test_should_defer_recovery_for_visible_pending_route_action() -> None:
    snapshot = {
        "effective_page_id": "data_list",
        "observed_actions": [
            {"kind": "button", "label": "Back"},
            {"kind": "list_item", "label": "Engine Data"},
        ],
        "list_items": ["Engine Data"],
        "navigation_path": ["Module Diagnostics", "Engine Control Module", "Diagnostic Trouble Codes (DTC)"],
    }

    assert should_defer_recovery_for_route_action(
        snapshot=snapshot,
        pending_route_step={"kind": "list_item", "label": "Engine Data"},
        target_action={"kind": "list_item", "label": "Engine Data"},
        target_path=["Module Diagnostics", "Engine Control Module", "Data Display", "Engine Data"],
    ) is True


def test_should_defer_recovery_keeps_vehicle_selection_policy_in_charge() -> None:
    snapshot = {
        "effective_page_id": "vehicle_selection",
        "observed_actions": [
            {"kind": "button", "label": "Back"},
            {"kind": "button", "label": "Enter"},
        ],
        "list_items": [],
        "navigation_path": [],
    }

    assert should_defer_recovery_for_route_action(
        snapshot=snapshot,
        pending_route_step={"kind": "button", "label": "Enter"},
        target_action={},
        target_path=["Module Diagnostics"],
    ) is False


def test_registry_route_executor_executes_visible_data_target_before_recovery(monkeypatch) -> None:
    monkeypatch.setattr("backends.gds2.registry_navigation_runtime.time.sleep", lambda _seconds: None)

    class _Controller:
        def click_navigation_path_item(self, label: str):  # pragma: no cover - must not be called
            raise AssertionError(f"must not breadcrumb-recover before visible target: {label}")

        def go_back(self):  # pragma: no cover - must not be called
            raise AssertionError("must not Back-recover before visible target")

        def go_home(self):  # pragma: no cover - must not be called
            raise AssertionError("must not Home-recover before visible target")

    class _RouteNavigator:
        graph: dict[str, object] = {}

        def __init__(self) -> None:
            self.page = "data_list"
            self.executed: list[dict[str, str]] = []

        def capture_settled_snapshot(self):
            if self.page == "data_display":
                return {
                    "effective_page_id": "data_display",
                    "observed_actions": [
                        {"kind": "button", "label": "Create Report"},
                        {"kind": "button", "label": "Back"},
                    ],
                    "list_items": [],
                    "navigation_path": ["Module Diagnostics", "Engine Control Module", "Data Display", "Engine Data"],
                }
            return {
                "effective_page_id": "data_list",
                "observed_actions": [
                    {"kind": "button", "label": "Back"},
                    {"kind": "list_item", "label": "Engine Data"},
                ],
                "list_items": ["Engine Data"],
                "navigation_path": ["Module Diagnostics", "Engine Control Module", "Diagnostic Trouble Codes (DTC)"],
            }

        def resolve_bridge_action(self, snapshot):
            return None

        def snapshot_action_match(self, snapshot, label, *, kind=None):
            return find_action_match(snapshot.get("observed_actions") or [], label, kind=kind)

        def execute_action(self, action: dict[str, str]) -> None:
            self.executed.append({"kind": str(action["kind"]), "label": str(action["label"])})
            if action != {"kind": "list_item", "label": "Engine Data"}:
                raise AssertionError(f"unexpected action: {action}")
            self.page = "data_display"

        def match_snapshot(self, snapshot):
            return None

    route_navigator = _RouteNavigator()
    runtime = RegistryNavigationRuntime(
        controller=_Controller(),
        route_navigator=route_navigator,
        entries=[],
    )

    result = runtime.execute_registry_route(
        entry={
            "page_key": "data.engine_data",
            "canonical_path": ["Module Diagnostics", "Engine Control Module", "Data Display", "Engine Data"],
            "route_steps": [
                {"kind": "list_item", "label": "Module Diagnostics"},
                {"kind": "list_item", "label": "Engine Control Module"},
                {"kind": "list_item", "label": "Data Display"},
                {"kind": "list_item", "label": "Engine Data"},
            ],
            "target_action": {"kind": "list_item", "label": "Engine Data"},
            "success_criteria": {
                "page_id_any": ["data_display"],
                "all_of": [
                    {"kind": "button", "label": "Create Report"},
                    {"kind": "button", "label": "Back"},
                ],
            },
        },
        max_iterations=4,
        max_backtracks=1,
    )

    assert route_navigator.executed == [{"kind": "list_item", "label": "Engine Data"}]
    assert result["final_page"] == "data_display"


def test_registry_route_executor_waits_for_data_display_controls_before_back(monkeypatch) -> None:
    monkeypatch.setattr("backends.gds2.registry_navigation_runtime.time.sleep", lambda _seconds: None)

    class _Controller:
        def go_back(self):  # pragma: no cover - must not be called
            raise AssertionError("must not leave data_display while waiting for target controls")

        def go_home(self):  # pragma: no cover - must not be called
            raise AssertionError("must not Home-recover from data_display")

    class _RouteNavigator:
        graph: dict[str, object] = {}

        def __init__(self) -> None:
            self.page = "data_list"
            self.executed: list[dict[str, str]] = []
            self.data_display_reads = 0

        def capture_settled_snapshot(self):
            if self.page == "data_display":
                self.data_display_reads += 1
                if self.data_display_reads == 1:
                    return {
                        "effective_page_id": "data_display",
                        "observed_actions": [
                            {"kind": "button", "label": "Back"},
                        ],
                        "list_items": [],
                        "navigation_path": [
                            "Module Diagnostics",
                            "Engine Control Module",
                            "Data Display",
                            "Engine Data",
                        ],
                    }
                return {
                    "effective_page_id": "data_display",
                    "observed_actions": [
                        {"kind": "button", "label": "Back"},
                        {"kind": "button", "label": "Create Report"},
                        {"kind": "button", "label": "Add Bookmark"},
                    ],
                    "list_items": [],
                    "navigation_path": [
                        "Module Diagnostics",
                        "Engine Control Module",
                        "Data Display",
                        "Engine Data",
                    ],
                }
            return {
                "effective_page_id": "data_list",
                "observed_actions": [
                    {"kind": "button", "label": "Back"},
                    {"kind": "list_item", "label": "Engine Data"},
                ],
                "list_items": ["Engine Data"],
                "navigation_path": ["Module Diagnostics", "Engine Control Module", "Data Display"],
            }

        def resolve_bridge_action(self, snapshot):
            return None

        def snapshot_action_match(self, snapshot, label, *, kind=None):
            return find_action_match(snapshot.get("observed_actions") or [], label, kind=kind)

        def execute_action(self, action: dict[str, str]) -> None:
            self.executed.append({"kind": str(action["kind"]), "label": str(action["label"])})
            if action != {"kind": "list_item", "label": "Engine Data"}:
                raise AssertionError(f"unexpected action: {action}")
            self.page = "data_display"

        def match_snapshot(self, snapshot):
            return None

    route_navigator = _RouteNavigator()
    runtime = RegistryNavigationRuntime(
        controller=_Controller(),
        route_navigator=route_navigator,
        entries=[],
    )

    result = runtime.execute_registry_route(
        entry={
            "page_key": "data.engine_data",
            "canonical_path": ["Module Diagnostics", "Engine Control Module", "Data Display", "Engine Data"],
            "route_steps": [
                {"kind": "list_item", "label": "Module Diagnostics"},
                {"kind": "list_item", "label": "Engine Control Module"},
                {"kind": "list_item", "label": "Data Display"},
                {"kind": "list_item", "label": "Engine Data"},
            ],
            "target_action": {"kind": "list_item", "label": "Engine Data"},
            "success_criteria": {
                "page_id_any": ["data_display"],
                "all_of": [
                    {"kind": "button", "label": "Create Report"},
                    {"kind": "button", "label": "Back"},
                ],
                "any_of": [
                    {"kind": "button", "label": "Clear DTCs"},
                    {"kind": "button", "label": "Add Bookmark"},
                ],
                "navigation_path_contains_prefix": ["Module Diagnostics", "Engine Control Module"],
            },
        },
        max_iterations=5,
        max_backtracks=1,
    )

    assert route_navigator.executed == [{"kind": "list_item", "label": "Engine Data"}]
    assert route_navigator.data_display_reads == 2
    assert result["final_page"] == "data_display"


def test_registry_route_executor_accepts_data_display_when_agent_stays_loading(monkeypatch) -> None:
    monkeypatch.setattr("backends.gds2.registry_navigation_runtime.time.sleep", lambda _seconds: None)

    class _Controller:
        def go_back(self):  # pragma: no cover - must not be called
            raise AssertionError("must not leave visual data_display after target action")

        def go_home(self):  # pragma: no cover - must not be called
            raise AssertionError("must not Home-recover after target action")

    class _RouteNavigator:
        graph: dict[str, object] = {}

        def __init__(self) -> None:
            self.page = "data_list"
            self.executed: list[dict[str, str]] = []

        def capture_settled_snapshot(self):
            if self.page == "loading_after_target":
                return {
                    "effective_page_id": "loading",
                    "observed_actions": [
                        {"kind": "button", "label": "Back"},
                        {"kind": "button", "label": "Home"},
                    ],
                    "list_items": [],
                    "navigation_path": [],
                }
            return {
                "effective_page_id": "data_list",
                "observed_actions": [
                    {"kind": "button", "label": "Back"},
                    {"kind": "list_item", "label": "Engine Data"},
                ],
                "list_items": ["Engine Data"],
                "navigation_path": ["Module Diagnostics", "Engine Control Module", "Data Display"],
            }

        def resolve_bridge_action(self, snapshot):
            return None

        def snapshot_action_match(self, snapshot, label, *, kind=None):
            return find_action_match(snapshot.get("observed_actions") or [], label, kind=kind)

        def execute_action(self, action: dict[str, str]) -> None:
            self.executed.append({"kind": str(action["kind"]), "label": str(action["label"])})
            if action != {"kind": "list_item", "label": "Engine Data"}:
                raise AssertionError(f"unexpected action: {action}")
            self.page = "loading_after_target"

        def match_snapshot(self, snapshot):
            return None

    route_navigator = _RouteNavigator()
    runtime = RegistryNavigationRuntime(
        controller=_Controller(),
        route_navigator=route_navigator,
        entries=[],
    )

    result = runtime.execute_registry_route(
        entry={
            "page_key": "data.engine_data",
            "page_kind": "data_display",
            "canonical_path": ["Module Diagnostics", "Engine Control Module", "Data Display", "Engine Data"],
            "route_steps": [
                {"kind": "list_item", "label": "Module Diagnostics"},
                {"kind": "list_item", "label": "Engine Control Module"},
                {"kind": "list_item", "label": "Data Display"},
                {"kind": "list_item", "label": "Engine Data"},
            ],
            "target_action": {"kind": "list_item", "label": "Engine Data"},
            "success_criteria": {
                "page_id_any": ["data_display"],
                "all_of": [
                    {"kind": "button", "label": "Create Report"},
                    {"kind": "button", "label": "Back"},
                ],
                "any_of": [
                    {"kind": "button", "label": "Clear DTCs"},
                    {"kind": "button", "label": "Add Bookmark"},
                ],
            },
        },
        max_iterations=5,
        max_backtracks=1,
    )

    assert route_navigator.executed == [{"kind": "list_item", "label": "Engine Data"}]
    assert result["final_page"] == "data_display"
    assert result["final_snapshot"]["classification_evidence"]["rule"] == "target_action_loading_terminal_page"
    assert result["recovery_actions"][-1]["kind"] == "terminal_loading"


def test_registry_route_executor_uses_startup_target_before_home_recovery(monkeypatch) -> None:
    monkeypatch.setattr("backends.gds2.registry_navigation_runtime.time.sleep", lambda _seconds: None)

    class _Controller:
        def go_home(self):  # pragma: no cover - must not be called
            raise AssertionError("startup route must not Home-recover from diagnostics_menu")

        def go_back(self):  # pragma: no cover - must not be called
            raise AssertionError("startup route must not Back-recover before Module Diagnostics")

    class _RouteNavigator:
        graph: dict[str, object] = {}

        def __init__(self) -> None:
            self.snapshots = [
                {
                    "effective_page_id": "diagnostics_menu",
                    "observed_actions": [
                        {"kind": "button", "label": "Back"},
                        {"kind": "button", "label": "Home"},
                        {"kind": "button", "label": "Enter"},
                    ],
                    "list_items": [],
                    "navigation_path": [],
                },
                {
                    "effective_page_id": "diagnostics_menu",
                    "observed_actions": [
                        {"kind": "button", "label": "Back"},
                        {"kind": "button", "label": "Home"},
                        {"kind": "button", "label": "Enter"},
                    ],
                    "list_items": [],
                    "navigation_path": [],
                },
                {
                    "effective_page_id": "diagnostics_menu",
                    "observed_actions": [
                        {"kind": "button", "label": "Back"},
                        {"kind": "button", "label": "Home"},
                        {"kind": "button", "label": "Enter"},
                    ],
                    "list_items": [],
                    "navigation_path": ["Vehicle Diagnostics"],
                },
                {
                    "effective_page_id": "module_list",
                    "observed_actions": [
                        {"kind": "list_item", "label": "Engine Control Module"},
                    ],
                    "list_items": ["Engine Control Module"],
                    "navigation_path": ["Module Diagnostics"],
                },
                {
                    "effective_page_id": "module_list",
                    "observed_actions": [
                        {"kind": "list_item", "label": "Engine Control Module"},
                    ],
                    "list_items": ["Engine Control Module"],
                    "navigation_path": ["Module Diagnostics"],
                },
            ]
            self.executed: list[dict[str, str]] = []

        def capture_settled_snapshot(self):
            if len(self.snapshots) > 1:
                return self.snapshots.pop(0)
            return self.snapshots[0]

        def resolve_bridge_action(self, snapshot):
            return None

        def snapshot_action_match(self, snapshot, label, *, kind=None):
            return find_action_match(snapshot.get("observed_actions") or [], label, kind=kind)

        def execute_action(self, action: dict[str, str]) -> None:
            self.executed.append({"kind": str(action["kind"]), "label": str(action["label"])})
            if action != {"kind": "list_item", "label": "Module Diagnostics"}:
                raise AssertionError(f"unexpected action: {action}")

        def match_snapshot(self, snapshot):
            return None

    route_navigator = _RouteNavigator()
    runtime = RegistryNavigationRuntime(
        controller=_Controller(),
        route_navigator=route_navigator,
        entries=[],
    )

    result = runtime.execute_registry_route(
        entry={
            "page_key": "diagnostics.module_diagnostics",
            "canonical_path": ["Module Diagnostics"],
            "route_steps": [
                {"kind": "device", "label": "VCI Proxy (Remote)", "optional": True},
            ],
            "target_action": {"kind": "list_item", "label": "Module Diagnostics"},
            "success_criteria": {
                "page_id_any": ["module_list"],
                "all_of": [{"kind": "list_item", "label": "Engine Control Module"}],
            },
        },
        max_iterations=4,
        max_backtracks=1,
    )

    assert route_navigator.executed == [{"kind": "list_item", "label": "Module Diagnostics"}]
    assert result["final_page"] == "module_list"


def test_registry_route_executor_lets_vehicle_selection_state_handle_disabled_enter(monkeypatch) -> None:
    monkeypatch.setattr("backends.gds2.registry_navigation_runtime.time.sleep", lambda _seconds: None)

    class _Nav:
        def get_page_id(self):
            return {
                "page_id": "vehicle_selection",
                "vehicle_selection_status": "connecting",
                "button_enabled": {"Enter": False, "Back": True},
            }

    class _Controller:
        nav = _Nav()

        def click_enter(self):  # pragma: no cover - should not be called
            raise AssertionError("disabled Enter must be handled by vehicle-selection state")

        def go_home(self):  # pragma: no cover - should not be called
            raise AssertionError("vehicle_selection should not Home-recover")

        def go_back(self):  # pragma: no cover - should not be called
            raise AssertionError("vehicle_selection should not Back-recover")

    class _RouteNavigator:
        graph = {
            "nodes": {
                "diagnostics": {
                    "page_id": "diagnostics_menu",
                    "observed_actions": [
                        {"kind": "list_item", "label": "Module Diagnostics"},
                    ],
                },
            }
        }

        def __init__(self) -> None:
            self.snapshots = [
                {
                    "effective_page_id": "vehicle_selection",
                    "observed_actions": [
                        {"kind": "button", "label": "Back"},
                        {"kind": "button", "label": "Enter"},
                    ],
                    "list_items": [],
                    "navigation_path": [],
                },
                {
                    "effective_page_id": "vehicle_selection",
                    "observed_actions": [
                        {"kind": "button", "label": "Back"},
                        {"kind": "button", "label": "Enter"},
                    ],
                    "list_items": [],
                    "navigation_path": [],
                },
                {
                    "effective_page_id": "diagnostics_menu",
                    "observed_actions": [
                        {"kind": "button", "label": "Back"},
                        {"kind": "button", "label": "Home"},
                    ],
                    "list_items": [],
                    "navigation_path": [],
                },
                {
                    "effective_page_id": "module_list",
                    "observed_actions": [
                        {"kind": "list_item", "label": "Engine Control Module"},
                    ],
                    "list_items": ["Engine Control Module"],
                    "navigation_path": ["Module Diagnostics"],
                },
            ]
            self.executed: list[dict[str, str]] = []

        def capture_settled_snapshot(self):
            if len(self.snapshots) > 1:
                return self.snapshots.pop(0)
            return self.snapshots[0]

        def resolve_bridge_action(self, snapshot):
            if snapshot["effective_page_id"] == "vehicle_selection":
                return {"kind": "button", "label": "Enter"}
            return None

        def snapshot_action_match(self, snapshot, label, *, kind=None):
            return find_action_match(snapshot.get("observed_actions") or [], label, kind=kind)

        def execute_action(self, action: dict[str, str]) -> None:
            self.executed.append({"kind": str(action["kind"]), "label": str(action["label"])})

        def match_snapshot(self, snapshot):
            return None

    route_navigator = _RouteNavigator()
    runtime = RegistryNavigationRuntime(
        controller=_Controller(),
        route_navigator=route_navigator,
        entries=[],
    )

    result = runtime.execute_registry_route(
        entry={
            "page_key": "diagnostics.module_diagnostics",
            "canonical_path": ["Module Diagnostics"],
            "route_steps": [
                {"kind": "button", "label": "Enter", "optional": True},
                {"kind": "list_item", "label": "Module Diagnostics"},
            ],
            "success_criteria": {
                "page_id_any": ["module_list"],
                "all_of": [{"kind": "list_item", "label": "Engine Control Module"}],
            },
        },
        max_iterations=4,
        max_backtracks=1,
    )

    assert route_navigator.executed == [{"kind": "list_item", "label": "Module Diagnostics"}]
    assert result["final_page"] == "module_list"


def test_executed_actions_available_requires_all_markers() -> None:
    executed = [
        {"kind": "button", "label": "Clear DTCs"},
        {"kind": "button", "label": "Add All"},
    ]

    assert executed_actions_available(executed, [{"kind": "button", "label": "Add All"}]) is True
    assert executed_actions_available(
        executed,
        [
            {"kind": "button", "label": "Add All"},
            {"kind": "button", "label": "OK"},
        ],
    ) is False


def test_clear_dtcs_state_matches_selected_module_and_button_states() -> None:
    ok, details = clear_dtcs_state_matches(
        {
            "data": {
                "selectedModules": {"rows": ["Engine Control Module"]},
                "buttons": {
                    "OK": {"enabled": True, "disabled": False},
                    "Remove": {"enabled": True, "disabled": False},
                    "Remove All": {"enabled": True, "disabled": False},
                    "Add All": {"enabled": False, "disabled": True},
                    "Add": {"enabled": False, "disabled": True},
                },
            }
        },
        {
            "expect_selected_modules": ["Engine Control Module"],
            "expect_buttons_enabled": ["OK", "Remove", "Remove All"],
            "expect_buttons_disabled": ["Add All", "Add"],
        },
    )

    assert ok is True
    assert details["missing_modules"] == []


def test_registry_navigation_runtime_clear_dtcs_uses_registry_entries(monkeypatch) -> None:
    runtime = RegistryNavigationRuntime(
        controller=object(),
        route_navigator=type(
            "_Navigator",
            (),
            {
                "graph": {},
                "capture_settled_snapshot": lambda self: {
                    "effective_page_id": "data_display",
                    "observed_actions": [],
                    "navigation_path": [],
                },
            },
        )(),
        entries=[
            {"page_key": "dtc.display", "aliases": ["DTC Display"]},
            {"page_key": "dtc.clear.execute", "aliases": ["clear dtcs"]},
        ],
        read_dtcs_snapshot=(lambda counts=iter([2, 0]): {"dtc_count": next(counts)}),
    )
    executed: list[str] = []

    def fake_execute(*, entry, max_iterations, max_backtracks):
        executed.append(str(entry["page_key"]))
        if entry["page_key"] == "dtc.display":
            return {"final_page": "data_display", "recovery_actions": [], "final_snapshot": {}}
        return {
            "final_page": "data_display",
            "recovery_actions": [{"kind": "navigation_path", "label": "Engine Control Module"}],
            "final_snapshot": {},
        }

    monkeypatch.setattr(runtime, "execute_registry_route", fake_execute)

    result = runtime.clear_dtcs()

    assert executed == ["dtc.display", "dtc.clear.execute"]
    assert result == {
        "success": True,
        "cleared_count": 2,
        "message": "Clear DTCs completed",
        "page_context": "data_display",
        "recovery_actions": [{"kind": "navigation_path", "label": "Engine Control Module"}],
    }
    status = runtime.get_runtime_status()
    assert status["last_operation"] == "clear_dtcs"
    assert status["last_route"]["route_target_page_key"] == "dtc.clear.execute"
    assert status["last_route"]["recovery_action_counts"] == {
        "navigation_path:Engine Control Module": 1
    }


def test_registry_navigation_runtime_clear_dtcs_stays_on_vehicle_dtc_branch(monkeypatch) -> None:
    runtime = RegistryNavigationRuntime(
        controller=object(),
        route_navigator=type(
            "_Navigator",
            (),
            {
                "graph": {},
                "capture_settled_snapshot": lambda self: {
                    "effective_page_id": "data_display",
                    "observed_actions": [
                        {"kind": "button", "label": "Clear DTCs"},
                        {"kind": "button", "label": "Refresh"},
                        {"kind": "button", "label": "Back"},
                    ],
                    "navigation_path": ["Vehicle Diagnostics"],
                },
            },
        )(),
        entries=[
            {"page_key": "vehicle_dtc.information", "aliases": ["Vehicle DTC Information"]},
            {"page_key": "dtc.display", "aliases": ["DTC Display"]},
            {"page_key": "dtc.clear.execute", "aliases": ["clear dtcs"]},
        ],
        read_dtc_count=(lambda counts=iter([30, 0]): next(counts)),
        state_reader=lambda: {"data_category": "Vehicle DTC Information"},
    )
    executed: list[str] = []

    def fake_execute(*, entry, max_iterations, max_backtracks):
        executed.append(str(entry["page_key"]))
        return {
            "final_page": "data_display",
            "recovery_actions": [],
            "final_snapshot": {
                "effective_page_id": "data_display",
                "observed_actions": [
                    {"kind": "button", "label": "Clear DTCs"},
                    {"kind": "button", "label": "Refresh"},
                    {"kind": "button", "label": "Back"},
                ],
                "navigation_path": ["Vehicle Diagnostics"],
            },
        }

    monkeypatch.setattr(runtime, "execute_registry_route", fake_execute)

    result = runtime.clear_dtcs()

    assert executed == ["vehicle_dtc.clear.execute"]
    assert result["success"] is True
    assert result["cleared_count"] == 30
    status = runtime.get_runtime_status()
    assert status["last_route"]["route_target_page_key"] == "vehicle_dtc.clear.execute"


def test_registry_navigation_runtime_clear_dtcs_relands_vehicle_branch_without_module_fallback(monkeypatch) -> None:
    runtime = RegistryNavigationRuntime(
        controller=object(),
        route_navigator=type(
            "_Navigator",
            (),
            {
                "graph": {},
                "capture_settled_snapshot": lambda self: {
                    "effective_page_id": "vehicle_diagnostics_menu",
                    "observed_actions": [
                        {"kind": "list_item", "label": "Vehicle DTC Information"},
                    ],
                    "navigation_path": ["Vehicle Diagnostics"],
                },
            },
        )(),
        entries=[
            {"page_key": "vehicle_dtc.information", "aliases": ["Vehicle DTC Information"]},
            {"page_key": "dtc.display", "aliases": ["DTC Display"]},
            {"page_key": "dtc.clear.execute", "aliases": ["clear dtcs"]},
        ],
        read_dtc_count=(lambda counts=iter([12, 0]): next(counts)),
        state_reader=lambda: {"data_category": "Vehicle DTC Information"},
    )
    executed: list[str] = []

    def fake_execute(*, entry, max_iterations, max_backtracks):
        executed.append(str(entry["page_key"]))
        return {
            "final_page": "data_display",
            "recovery_actions": [],
            "final_snapshot": {
                "effective_page_id": "data_display",
                "observed_actions": [
                    {"kind": "button", "label": "Clear DTCs"},
                    {"kind": "button", "label": "Refresh"},
                    {"kind": "button", "label": "Back"},
                ],
                "navigation_path": ["Vehicle Diagnostics"],
            },
        }

    monkeypatch.setattr(runtime, "execute_registry_route", fake_execute)

    result = runtime.clear_dtcs()

    assert executed == ["vehicle_dtc.information", "vehicle_dtc.clear.execute"]
    assert result["success"] is True
    assert result["cleared_count"] == 12
    status = runtime.get_runtime_status()
    assert status["last_route"]["route_target_page_key"] == "vehicle_dtc.clear.execute"
    assert status["last_route"]["canonical_path"] == [
        "Vehicle Diagnostics",
        "Vehicle DTC Information",
        "Clear DTCs",
        "Add All",
        "OK",
        "OK",
    ]


def test_registry_navigation_runtime_clear_dtcs_executes_in_place_from_current_data_display(monkeypatch) -> None:
    runtime = RegistryNavigationRuntime(
        controller=object(),
        route_navigator=type(
            "_Navigator",
            (),
            {
                "graph": {},
                "capture_settled_snapshot": lambda self: {
                    "effective_page_id": "data_display",
                    "observed_actions": [
                        {"kind": "button", "label": "Clear DTCs"},
                        {"kind": "button", "label": "Refresh"},
                        {"kind": "button", "label": "Back"},
                    ],
                    "navigation_path": [
                        "Module Diagnostics",
                        "Engine Control Module",
                        "Data Display",
                        "Engine Data",
                    ],
                },
            },
        )(),
        entries=[
            {"page_key": "dtc.display", "aliases": ["DTC Display"]},
            {"page_key": "dtc.clear.execute", "aliases": ["clear dtcs"]},
        ],
        read_dtc_count=(lambda counts=iter([2, 0]): next(counts)),
        state_reader=lambda: {"data_category": "Engine Data"},
    )
    executed: list[str] = []
    route_paths: list[list[str]] = []

    def fake_execute(*, entry, max_iterations, max_backtracks):
        executed.append(str(entry["page_key"]))
        route_paths.append(list(entry.get("canonical_path") or []))
        return {
            "final_page": "data_display",
            "recovery_actions": [],
            "final_snapshot": {
                "effective_page_id": "data_display",
                "observed_actions": [
                    {"kind": "button", "label": "Clear DTCs"},
                    {"kind": "button", "label": "Refresh"},
                    {"kind": "button", "label": "Back"},
                ],
                "navigation_path": [
                    "Module Diagnostics",
                    "Engine Control Module",
                    "Data Display",
                    "Engine Data",
                ],
            },
        }

    monkeypatch.setattr(runtime, "execute_registry_route", fake_execute)

    result = runtime.clear_dtcs()

    assert executed == ["data_display.clear.execute"]
    assert route_paths == [
        [
            "Module Diagnostics",
            "Engine Control Module",
            "Data Display",
            "Engine Data",
            "Clear DTCs",
            "Add All",
            "OK",
            "OK",
        ]
    ]
    assert result["success"] is True
    assert result["cleared_count"] == 2
    status = runtime.get_runtime_status()
    assert status["last_route"]["route_target_page_key"] == "data_display.clear.execute"
    assert status["last_route"]["canonical_path"] == route_paths[0]


def test_registry_navigation_runtime_guided_vehicle_root_emits_decision_and_completes(monkeypatch) -> None:
    class _Controller:
        def __init__(self) -> None:
            self.selected_items: list[str] = []

        def detect_current_page(self):
            return "data_list" if not self.selected_items else "data_display"

        def wait_for_list(self):
            return ["Vehicle DTC Information", "Vehicle DTC and ID Information"]

        def select_list_item(self, label):
            self.selected_items.append(label)
            return type("R", (), {"success": True})()

    controller = _Controller()
    runtime = RegistryNavigationRuntime(
        controller=controller,
        route_navigator=type("_Navigator", (), {"graph": {}})(),
        entries=[
            {"page_key": "diagnostics.vehicle_diagnostics", "aliases": ["Vehicle Diagnostics"]},
        ],
    )
    monkeypatch.setattr(
        runtime,
        "execute_registry_route",
        lambda **kwargs: {
            "final_page": "data_list",
            "executed_actions": [{"kind": "list_item", "label": "Vehicle Diagnostics"}],
        },
    )

    session = NavSession(session_id="nav-vehicle", goal="Vehicle Diagnostics")
    session.decision_queue.put({"selected_item": "Vehicle DTC Information"})

    final = runtime._run_navigation_session(session)
    events = []
    while not session.event_queue.empty():
        events.append(session.event_queue.get_nowait())

    assert any(
        event["type"] == "decision_required"
        and event["page"] == "vehicle_diagnostics_menu"
        and event["items"] == ["Vehicle DTC Information", "Vehicle DTC and ID Information"]
        for event in events
    )
    assert events[-1]["type"] == "done"
    assert final["current_page"] == "data_display"
    assert final["selections"] == {"selected_item": "Vehicle DTC Information"}
    assert controller.selected_items == ["Vehicle DTC Information"]


def test_registry_navigation_runtime_vehicle_dtcs_alias_uses_registry_route(monkeypatch) -> None:
    runtime = RegistryNavigationRuntime(
        controller=object(),
        route_navigator=type("_Navigator", (), {"graph": {}})(),
        entries=[
            {"page_key": "vehicle_dtc.information", "aliases": ["Vehicle DTCs"]},
        ],
    )
    executed: list[str] = []

    monkeypatch.setattr(
        runtime,
        "execute_registry_route",
        lambda **kwargs: executed.append(kwargs["entry"]["page_key"]) or {
            "final_page": "data_display",
            "executed_actions": [{"kind": "list_item", "label": "Vehicle DTC Information"}],
        },
    )

    session = NavSession(session_id="nav-vehicle-dtcs", goal="Vehicle DTCs")

    final = runtime._run_navigation_session(session)

    assert executed == ["vehicle_dtc.information"]
    assert final["current_page"] == "data_display"
    assert final["selections"] == {}


def test_registry_navigation_runtime_select_module_falls_back_to_visible_list_item(monkeypatch) -> None:
    runtime = RegistryNavigationRuntime(
        controller=object(),
        route_navigator=type(
            "_Navigator",
            (),
            {
                "graph": {},
                "navigate_to_action": lambda self, label, kind=None, max_backtracks=None, max_iterations=None: {
                    "matched_start_node_id": None,
                    "planned_path": [],
                    "executed_actions": [{"kind": "list_item", "label": label}],
                    "recovery_actions": [],
                    "final_page": "data_list",
                    "final_snapshot": {
                        "effective_page_id": "data_list",
                        "list_items": ["Engine Data", "Body Data"],
                    },
                },
            },
        )(),
        entries=[],
    )

    result = runtime.select_module("[K9] Body Control Module")

    assert result == {
        "selected_module": "[K9] Body Control Module",
        "data_categories": ["Engine Data", "Body Data"],
    }


def test_registry_navigation_runtime_select_module_falls_back_from_data_display(monkeypatch) -> None:
    calls: list[tuple[str, str | None, int | None, int | None]] = []
    runtime = RegistryNavigationRuntime(
        controller=object(),
        route_navigator=type(
            "_Navigator",
            (),
            {
                "graph": {},
                "navigate_to_action": lambda self, label, kind=None, max_backtracks=None, max_iterations=None: (
                    calls.append((label, kind, max_backtracks, max_iterations))
                    or {
                        "matched_start_node_id": "module_list",
                        "planned_path": [{"kind": "button", "label": "Back"}],
                        "executed_actions": [
                            {"kind": "button", "label": "Back"},
                            {"kind": "list_item", "label": label},
                        ],
                        "recovery_actions": [{"kind": "button", "label": "Back"}],
                        "final_page": "data_list",
                        "final_snapshot": {
                            "effective_page_id": "data_list",
                            "list_items": ["Body Data"],
                        },
                    }
                ),
            },
        )(),
        entries=[],
        route_max_backtracks=6,
        route_max_iterations=18,
    )

    result = runtime.select_module("[K9] Body Control Module")

    assert calls == [("[K9] Body Control Module", "list_item", 6, 18)]
    assert result == {
        "selected_module": "[K9] Body Control Module",
        "data_categories": ["Body Data"],
    }


def test_registry_navigation_runtime_recover_data_display_uses_registry_route(monkeypatch) -> None:
    runtime = RegistryNavigationRuntime(
        controller=object(),
        route_navigator=type(
            "_Navigator",
            (),
            {
                "graph": {},
                "capture_settled_snapshot": lambda self: {
                    "effective_page_id": "j2534_disconnect",
                    "observed_actions": [],
                    "navigation_path": [],
                },
            },
        )(),
        entries=[
            {"page_key": "data.engine_data", "aliases": ["Engine Data"]},
        ],
    )

    calls = []

    def _execute_registry_route(**kwargs):
        calls.append(dict(kwargs))
        return {
            "final_page": "data_display",
            "recovery_actions": [
                {
                    "kind": "button",
                    "label": "OK",
                    "reason": "j2534_disconnect soft recovery",
                }
            ],
            "final_snapshot": {},
        }

    monkeypatch.setattr(runtime, "execute_registry_route", _execute_registry_route)

    result = runtime.recover_data_display(data_category="Engine Data", mode="stream")

    assert calls[0]["recovery_data_category"] == "Engine Data"
    assert result == {
        "ok": True,
        "mode": "stream",
        "recovered": True,
        "recovery_method": "in_place",
        "restart_collection": False,
        "message": "Recovered Data Display after J2534 disconnect.",
        "recovery_actions": [
            {
                "kind": "button",
                "label": "OK",
                "reason": "j2534_disconnect soft recovery",
            }
        ],
    }
    status = runtime.get_runtime_status()
    assert status["last_operation"] == "recover_data_display"
    assert status["last_route"]["route_target_page_key"] == "data.engine_data"
    assert status["last_route"]["terminal_reason"] == "recovered_data_display"


def test_registry_navigation_runtime_clear_dtcs_records_failure_status(monkeypatch) -> None:
    runtime = RegistryNavigationRuntime(
        controller=object(),
        route_navigator=type(
            "_Navigator",
            (),
            {
                "graph": {},
                "capture_settled_snapshot": lambda self: {
                    "effective_page_id": "data_display",
                    "observed_actions": [],
                    "navigation_path": [],
                },
            },
        )(),
        entries=[
            {"page_key": "dtc.display", "aliases": ["DTC Display"]},
            {"page_key": "dtc.clear.execute", "aliases": ["clear dtcs"]},
        ],
    )

    monkeypatch.setattr(
        runtime,
        "execute_registry_route",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("route failed")),
    )

    with pytest.raises(RuntimeError, match="route failed"):
        runtime.clear_dtcs()

    status = runtime.get_runtime_status()
    assert status["status"] == "failed"
    assert status["last_operation"] == "clear_dtcs"
    assert status["last_error"] == "route failed"
    assert status["last_route"]["terminal_reason"] == "failed_clear_dtcs"


def test_registry_navigation_runtime_clear_dtcs_vehicle_failure_records_vehicle_route(monkeypatch) -> None:
    runtime = RegistryNavigationRuntime(
        controller=object(),
        route_navigator=type(
            "_Navigator",
            (),
            {
                "graph": {},
                "capture_settled_snapshot": lambda self: {
                    "effective_page_id": "data_display",
                    "observed_actions": [
                        {"kind": "button", "label": "Clear DTCs"},
                        {"kind": "button", "label": "Refresh"},
                        {"kind": "button", "label": "Back"},
                    ],
                    "navigation_path": ["Vehicle Diagnostics"],
                },
            },
        )(),
        entries=[
            {"page_key": "vehicle_dtc.information", "aliases": ["Vehicle DTC Information"]},
            {"page_key": "dtc.display", "aliases": ["DTC Display"]},
            {"page_key": "dtc.clear.execute", "aliases": ["clear dtcs"]},
        ],
        read_dtc_count=(lambda counts=iter([3]): next(counts)),
        state_reader=lambda: {"data_category": "Vehicle DTC Information"},
    )

    monkeypatch.setattr(
        runtime,
        "execute_registry_route",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("route failed")),
    )

    with pytest.raises(RuntimeError, match="route failed"):
        runtime.clear_dtcs()

    status = runtime.get_runtime_status()
    assert status["status"] == "failed"
    assert status["last_route"]["route_target_page_key"] == "vehicle_dtc.clear.execute"
    assert status["last_route"]["canonical_path"] == [
        "Vehicle Diagnostics",
        "Vehicle DTC Information",
        "Clear DTCs",
        "Add All",
        "OK",
        "OK",
    ]


def test_registry_navigation_runtime_recover_data_display_records_failed_restore_status(monkeypatch) -> None:
    runtime = RegistryNavigationRuntime(
        controller=object(),
        route_navigator=type(
            "_Navigator",
            (),
            {
                "graph": {},
                "capture_settled_snapshot": lambda self: {
                    "effective_page_id": "j2534_disconnect",
                    "observed_actions": [],
                    "navigation_path": [],
                },
            },
        )(),
        entries=[
            {
                "page_key": "data.engine_data",
                "aliases": ["Engine Data"],
                "canonical_path": ["Module Diagnostics", "Engine Control Module", "Data Display", "Engine Data"],
            },
        ],
    )

    monkeypatch.setattr(
        runtime,
        "execute_registry_route",
        lambda **kwargs: {
            "final_page": "data_list",
            "recovery_actions": [],
            "final_snapshot": {},
            "state_trace": [],
        },
    )

    result = runtime.recover_data_display(data_category="Engine Data", mode="stream")

    assert result["ok"] is False
    status = runtime.get_runtime_status()
    assert status["status"] == "failed"
    assert status["last_operation"] == "recover_data_display"
    assert "did not restore Data Display" in status["last_error"]
    assert status["last_route"]["terminal_reason"] == "failed_restore_data_display"


def test_registry_route_status_preserves_match_diagnostics() -> None:
    runtime = RegistryNavigationRuntime(
        controller=object(),
        route_navigator=type("_Navigator", (), {"graph": {}})(),
        entries=[],
    )
    diagnostic = {
        "target_label": "Engine Control Module",
        "action_kind": "list_item",
        "owner": "registry_runtime.route_step",
        "selected_policy": "unique_contains",
        "candidate_labels": ["[K20] Engine Control Module"],
        "resolution": "unique_contains",
    }

    status = runtime._build_route_status(
        entry={"page_key": "module.engine", "category": "module", "canonical_path": ["Module Diagnostics"]},
        route_result={
            "matched_start_node_id": "module_list",
            "final_page": "module_submenu",
            "planned_path": [],
            "executed_actions": [],
            "recovery_actions": [],
            "state_trace": [],
            "match_diagnostics": [diagnostic],
        },
        terminal_reason="selected_module",
    )

    assert status["match_diagnostics"] == [diagnostic]


def test_registry_failure_status_preserves_ambiguous_match_diagnostics() -> None:
    snapshot = {
        "effective_page_id": "data_list",
        "observed_actions": [
            {"kind": "list_item", "label": "Fuel Trim Enable", "list_index": 0},
            {"kind": "list_item", "label": "Fuel Trim Disable", "list_index": 0},
        ],
        "navigation_path": [],
        "list_items": ["Fuel Trim Enable", "Fuel Trim Disable"],
    }

    class _Navigator:
        graph = {}

        def capture_settled_snapshot(self):
            return dict(snapshot)

        def match_snapshot(self, _snapshot):
            return None

        def snapshot_action_match(self, current_snapshot, label, *, kind):
            from src.navigation import find_action_match

            return find_action_match(
                current_snapshot.get("observed_actions") or [],
                label,
                kind=kind,
            )

        def resolve_bridge_action(self, _snapshot):
            return None

    runtime = RegistryNavigationRuntime(
        controller=object(),
        route_navigator=_Navigator(),
        entries=[
            {
                "page_key": "data.fuel_trim",
                "aliases": ["Fuel Trim"],
                "category": "data",
                "canonical_path": [],
                "target_action": {"kind": "list_item", "label": "Fuel Trim"},
                "route_steps": [],
            }
        ],
    )

    with pytest.raises(RuntimeError, match="Ambiguous action match"):
        runtime.select_data_category("Fuel Trim")

    status = runtime.get_runtime_status()
    assert status["last_route"]["terminal_reason"] == "failed_select_data_category"
    assert status["last_route"]["match_diagnostics"] == [
        {
            "target_label": "Fuel Trim",
            "action_kind": "list_item",
            "owner": "registry_runtime.target_action",
            "selected_policy": "ambiguous",
            "candidate_labels": ["Fuel Trim Enable", "Fuel Trim Disable"],
            "resolution": "ambiguous",
        }
    ]


def test_loading_watchdog_waits_restarts_and_fails_after_limit() -> None:
    watchdog = LoadingWatchdog(timeout_sec=5.0, max_restarts=1)

    assert watchdog.observe("loading", now=10.0) == "wait"
    assert watchdog.observe("loading", now=14.0) == "wait"
    assert watchdog.observe("loading", now=16.0) == "restart"
    assert watchdog.observe("loading", now=20.0) == "wait"
    assert watchdog.observe("loading", now=26.0) == "failed"
    assert watchdog.observe("data_list", now=27.0) == "ready"


def test_common_ancestor_recovery_does_not_press_back_on_loading() -> None:
    class _Controller:
        def __init__(self) -> None:
            self.back_calls = 0

        def go_back(self):
            self.back_calls += 1
            return type("R", (), {"success": True})()

    class _Navigator:
        def capture_settled_snapshot(self):
            return {
                "effective_page_id": "loading",
                "navigation_path": ["Vehicle Diagnostics"],
                "observed_actions": [{"kind": "button", "label": "Back"}],
            }

    controller = _Controller()

    actions = recover_to_registry_common_ancestor(
        controller=controller,
        route_navigator=_Navigator(),
        target_path=["Module Diagnostics", "Engine Control Module"],
        max_backtracks=3,
    )

    assert actions == []
    assert controller.back_calls == 0


def _workspace_tmp_dir(name: str) -> Path:
    path = Path(".omx") / "test_tmp" / f"{name}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_run_probe_uses_loading_policy_defaults_from_registry(monkeypatch) -> None:
    from backends.gds2.navigation_registry import DEFAULT_SEED_PATH, rebuild_registry_database

    tmp_path = _workspace_tmp_dir("registry-probe-loading-policy")
    db_path = tmp_path / "registry.sqlite"
    rebuild_registry_database(output_path=db_path, seed_path=DEFAULT_SEED_PATH)

    class _AgentNavigator:
        def __init__(self, timeout_sec=15.0):
            self.timeout_sec = timeout_sec

    class _Controller:
        def __init__(self, nav=None):
            self.nav = nav

    class _RouteNavigator:
        def __init__(self, controller, graph):
            self._controller = controller
            self._graph = graph

        def capture_settled_snapshot(self):
            return {"effective_page_id": "diagnostics_menu", "observed_actions": [], "navigation_path": []}

    captured = {}

    def fake_execute(runtime, **kwargs):
        captured["loading_timeout_sec"] = runtime._loading_timeout_sec
        captured["max_loading_restarts"] = runtime._max_loading_restarts
        return {
            "final_snapshot": {
                "effective_page_id": "data_display",
                "observed_actions": [
                    {"kind": "button", "label": "Create Report"},
                    {"kind": "button", "label": "Back"},
                    {"kind": "button", "label": "Clear DTCs"},
                ],
                "navigation_path": ["Module Diagnostics", "Engine Control Module"],
            },
            "final_page": "data_display",
        }

    monkeypatch.setattr("backends.gds2.registry_navigation_runtime.AgentNavigator", _AgentNavigator)
    monkeypatch.setattr("backends.gds2.registry_navigation_runtime.NavigationController", _Controller)
    monkeypatch.setattr("backends.gds2.registry_navigation_runtime.GDS2RouteNavigator", _RouteNavigator)
    monkeypatch.setattr("backends.gds2.registry_navigation_runtime.load_or_rebuild_graph", lambda _path=None: {})
    monkeypatch.setattr(
        "backends.gds2.registry_navigation_runtime.RegistryNavigationRuntime.execute_registry_route",
        lambda self, **kwargs: fake_execute(self, **kwargs),
    )

    result = run_probe(
        entry_key="Engine Data",
        registry_path=db_path,
        graph_path="unused.json",
        rebuild_registry=False,
        bootstrap=False,
        max_iterations=1,
        max_backtracks=1,
        loading_timeout_sec=None,
        max_loading_restarts=None,
    )

    assert result["success"] is True
    assert captured == {"loading_timeout_sec": 20.0, "max_loading_restarts": 1}
    shutil.rmtree(tmp_path, ignore_errors=True)


def test_script_run_probe_uses_registry_navigation_runtime_class(monkeypatch) -> None:
    from backends.gds2.navigation_registry import DEFAULT_SEED_PATH, rebuild_registry_database

    tmp_path = _workspace_tmp_dir("registry-probe-runtime-class")
    db_path = tmp_path / "registry.sqlite"
    rebuild_registry_database(output_path=db_path, seed_path=DEFAULT_SEED_PATH)

    class _AgentNavigator:
        def __init__(self, timeout_sec=15.0):
            self.timeout_sec = timeout_sec

    class _Controller:
        def __init__(self, nav=None):
            self.nav = nav

    class _RouteNavigator:
        def __init__(self, controller, graph):
            self._controller = controller
            self._graph = graph

        def capture_settled_snapshot(self):
            return {"effective_page_id": "diagnostics_menu", "observed_actions": [], "navigation_path": []}

    used = {"runtime": False}

    class _Runtime(RegistryNavigationRuntime):
        def __init__(self, **kwargs):
            used["runtime"] = True
            super().__init__(**kwargs)

        def capture_runtime_snapshot(self):
            return {"effective_page_id": "diagnostics_menu", "observed_actions": [], "navigation_path": []}

        def execute_registry_route(self, *, entry, max_iterations, max_backtracks, **_kwargs):
            return {
                "final_snapshot": {
                    "effective_page_id": "data_display",
                    "observed_actions": [
                        {"kind": "button", "label": "Create Report"},
                        {"kind": "button", "label": "Back"},
                        {"kind": "button", "label": "Clear DTCs"},
                    ],
                    "navigation_path": ["Module Diagnostics", "Engine Control Module"],
                },
                "final_page": "data_display",
            }

    monkeypatch.setattr("backends.gds2.registry_navigation_runtime.AgentNavigator", _AgentNavigator)
    monkeypatch.setattr("backends.gds2.registry_navigation_runtime.NavigationController", _Controller)
    monkeypatch.setattr("backends.gds2.registry_navigation_runtime.GDS2RouteNavigator", _RouteNavigator)
    monkeypatch.setattr("backends.gds2.registry_navigation_runtime.RegistryNavigationRuntime", _Runtime)
    monkeypatch.setattr("backends.gds2.registry_navigation_runtime.load_or_rebuild_graph", lambda _path=None: {})

    result = run_probe(
        entry_key="Engine Data",
        registry_path=db_path,
        graph_path="unused.json",
        rebuild_registry=False,
        bootstrap=False,
        max_iterations=1,
        max_backtracks=1,
        loading_timeout_sec=None,
        max_loading_restarts=None,
    )

    assert used["runtime"] is True
    assert result["success"] is True
    shutil.rmtree(tmp_path, ignore_errors=True)
