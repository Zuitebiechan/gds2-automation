from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest

from backends.gds2.registry_navigation_runtime import (
    clear_dtcs_state_matches,
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
)
from backends.gds2.registry_navigation_runtime import RegistryNavigationRuntime
from backends.gds2.registry_navigation_runtime import evaluate_success_criteria
from diagnostic_platform.runtime.navigation_runtime import NavSession


def test_vehicle_selection_status_connected_prefers_enter() -> None:
    action = decide_vehicle_selection_action(
        {"vehicle_selection_status": "connected_session"},
        {"Enter", "Disconnect", "Back"},
    )

    assert action == "enter"


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


def test_vehicle_selection_status_falls_back_to_buttons_when_status_missing() -> None:
    assert decide_vehicle_selection_action({}, {"Enter", "Back"}) == "enter"
    assert decide_vehicle_selection_action({}, {"Select Device", "Back"}) == "select_device"


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

    monkeypatch.setattr(
        runtime,
        "execute_registry_route",
        lambda **kwargs: {
            "final_page": "data_display",
            "recovery_actions": [
                {
                    "kind": "button",
                    "label": "OK",
                    "reason": "j2534_disconnect soft recovery",
                }
            ],
            "final_snapshot": {},
        },
    )

    result = runtime.recover_data_display(data_category="Engine Data", mode="stream")

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

        def execute_registry_route(self, *, entry, max_iterations, max_backtracks):
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
