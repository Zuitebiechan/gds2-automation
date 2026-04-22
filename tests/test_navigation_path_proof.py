from __future__ import annotations

import json
from pathlib import Path

from scripts.prove_navigation_path import (
    load_manifest,
    preflight_seek_min_distance,
    preflight_hide_success_buttons,
    preflight_hide_target,
    snapshot_has_target_action,
    success_buttons_present,
    validate_result,
)


def test_load_manifest_reads_target_and_success_buttons(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "target": "Fuel Trim Enable",
                "target_kind": "list_item",
                "success_buttons": ["Enabled", "Disabled", "Release Control"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    manifest = load_manifest(manifest_path)

    assert manifest["target"] == "Fuel Trim Enable"
    assert manifest["success_buttons"] == ["Enabled", "Disabled", "Release Control"]


def test_success_buttons_present_requires_all_expected_buttons() -> None:
    result = {
        "final_snapshot": {
            "observed_actions": [
                {"kind": "button", "label": "Enabled"},
                {"kind": "button", "label": "Disabled"},
                {"kind": "button", "label": "Release Control"},
                {"kind": "button", "label": "Back"},
            ]
        }
    }

    assert success_buttons_present(result, ["Enabled", "Disabled", "Release Control"]) is True
    assert success_buttons_present(result, ["Enabled", "Disabled", "Missing"]) is False


def test_validate_result_rejects_when_target_buttons_already_visible_at_start() -> None:
    route_result = {
        "executed_actions": [{"kind": "list_item", "label": "Fuel Trim Enable"}],
        "final_snapshot": {
            "observed_actions": [
                {"kind": "button", "label": "Enabled"},
                {"kind": "button", "label": "Disabled"},
                {"kind": "button", "label": "Release Control"},
            ]
        },
    }
    start_snapshot = {
        "observed_actions": [
            {"kind": "button", "label": "Enabled"},
            {"kind": "button", "label": "Disabled"},
            {"kind": "button", "label": "Release Control"},
        ]
    }

    success, reason = validate_result(
        route_result=route_result,
        start_snapshot=start_snapshot,
        expected_buttons=["Enabled", "Disabled", "Release Control"],
        manifest={"require_target_hidden_at_start": True},
    )

    assert success is False
    assert reason == "target buttons were already visible before navigation"


def test_validate_result_rejects_when_executed_actions_below_threshold() -> None:
    route_result = {
        "executed_actions": [{"kind": "list_item", "label": "Fuel Trim Enable"}],
        "final_snapshot": {
            "observed_actions": [
                {"kind": "button", "label": "Enabled"},
                {"kind": "button", "label": "Disabled"},
                {"kind": "button", "label": "Release Control"},
            ]
        },
    }
    start_snapshot = {"observed_actions": []}

    success, reason = validate_result(
        route_result=route_result,
        start_snapshot=start_snapshot,
        expected_buttons=["Enabled", "Disabled", "Release Control"],
        manifest={"require_min_executed_actions": 4},
    )

    assert success is False
    assert "executed action count below minimum threshold" in str(reason)


def test_snapshot_has_target_action_matches_list_item_substring() -> None:
    snapshot = {
        "observed_actions": [
            {"kind": "list_item", "label": "Fuel Trim Enable", "list_index": 0},
        ]
    }

    assert snapshot_has_target_action(snapshot, "Fuel Trim Enable", "list_item") is True
    assert snapshot_has_target_action(snapshot, "Fuel Trim", "list_item") is True


def test_preflight_hide_target_backs_up_until_target_is_hidden() -> None:
    class _Controller:
        def __init__(self) -> None:
            self.page = "target_list"

        def go_back(self):
            self.page = "parent"
            return type("R", (), {"success": True})()

    class _Navigator:
        def __init__(self, controller) -> None:
            self._controller = controller

        def _capture_settled_snapshot(self):
            if self._controller.page == "target_list":
                return {
                    "observed_actions": [{"kind": "list_item", "label": "Fuel Trim Enable"}],
                }
            return {
                "observed_actions": [{"kind": "list_item", "label": "Fuel System"}],
            }

    controller = _Controller()
    navigator = _Navigator(controller)

    snapshot, actions = preflight_hide_target(
        controller=controller,
        navigator=navigator,
        target="Fuel Trim Enable",
        target_kind="list_item",
        max_backtracks=4,
    )

    assert actions == [{"kind": "button", "label": "Back"}]
    assert snapshot["observed_actions"][0]["label"] == "Fuel System"


def test_preflight_hide_success_buttons_backs_up_until_buttons_disappear() -> None:
    class _Controller:
        def __init__(self) -> None:
            self.page = "target"

        def go_back(self):
            self.page = "parent"
            return type("R", (), {"success": True})()

    class _Navigator:
        def __init__(self, controller) -> None:
            self._controller = controller

        def _capture_settled_snapshot(self):
            if self._controller.page == "target":
                return {
                    "observed_actions": [
                        {"kind": "button", "label": "Enabled"},
                        {"kind": "button", "label": "Disabled"},
                        {"kind": "button", "label": "Release Control"},
                    ]
                }
            return {"observed_actions": [{"kind": "button", "label": "Back"}]}

    controller = _Controller()
    navigator = _Navigator(controller)

    snapshot, actions = preflight_hide_success_buttons(
        controller=controller,
        navigator=navigator,
        expected_buttons=["Enabled", "Disabled", "Release Control"],
        max_backtracks=4,
    )

    assert actions == [{"kind": "button", "label": "Back"}]
    labels = [item["label"] for item in snapshot["observed_actions"]]
    assert labels == ["Back"]


def test_preflight_seek_min_distance_backtracks_until_path_is_long_enough() -> None:
    class _Controller:
        def __init__(self) -> None:
            self.page = "near"

        def go_back(self):
            self.page = "far"
            return type("R", (), {"success": True})()

    class _Navigator:
        def __init__(self, controller) -> None:
            self._controller = controller

        def _capture_settled_snapshot(self):
            if self._controller.page == "near":
                return {
                    "effective_page_id": "data_list",
                    "observed_actions": [{"kind": "list_item", "label": "Fuel Trim Enable"}],
                    "list_items": ["Fuel Trim Enable"],
                }
            return {
                "effective_page_id": "module_submenu",
                "observed_actions": [{"kind": "list_item", "label": "Control Functions"}],
                "list_items": ["Control Functions"],
            }

        def _match_snapshot(self, snapshot):
            if snapshot["effective_page_id"] == "data_list":
                return "near"
            return "far"

        def _snapshot_has_action(self, snapshot, target, kind):
            return any(item.get("label") == target for item in snapshot.get("observed_actions", []))

    controller = _Controller()
    navigator = _Navigator(controller)
    graph = {
        "nodes": {
            "near": {"node_id": "near", "page_id": "data_list", "observed_actions": [{"kind": "list_item", "label": "Fuel Trim Enable"}]},
            "far": {"node_id": "far", "page_id": "module_submenu", "observed_actions": [{"kind": "list_item", "label": "Control Functions"}]},
            "mid": {"node_id": "mid", "page_id": "data_list", "observed_actions": [{"kind": "list_item", "label": "Fuel System"}]},
        },
        "edges": [
            {"source_id": "far", "target_id": "mid", "action": {"kind": "list_item", "label": "Control Functions"}},
            {"source_id": "mid", "target_id": "near", "action": {"kind": "list_item", "label": "Fuel System"}},
        ],
    }

    snapshot, actions = preflight_seek_min_distance(
        controller=controller,
        navigator=navigator,
        graph=graph,
        target="Fuel Trim Enable",
        target_kind="list_item",
        min_actions=3,
        max_backtracks=4,
    )

    assert actions == [{"kind": "button", "label": "Back"}]
    assert snapshot["effective_page_id"] == "module_submenu"
