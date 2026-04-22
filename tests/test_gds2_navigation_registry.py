from __future__ import annotations

import sqlite3
from pathlib import Path

from backends.gds2.navigation_registry import (
    DEFAULT_SEED_PATH,
    connect_registry,
    load_seed,
    list_entries,
    list_page_states,
    list_recovery_policies,
    lookup_page_state,
    lookup_recovery_policy,
    lookup_entry,
    rebuild_registry_database,
)


def test_navigation_registry_builds_sqlite_database_from_seed(tmp_path: Path) -> None:
    db_path = tmp_path / "registry.sqlite"

    output = rebuild_registry_database(output_path=db_path, seed_path=DEFAULT_SEED_PATH)

    assert output == db_path
    assert db_path.exists()
    with sqlite3.connect(db_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM navigation_pages").fetchone()[0]
        state_count = connection.execute("SELECT COUNT(*) FROM page_states").fetchone()[0]
        policy_count = connection.execute("SELECT COUNT(*) FROM recovery_policies").fetchone()[0]
    assert count >= 20
    assert state_count >= 5
    assert policy_count >= 5


def test_navigation_registry_lookup_supports_page_key_and_alias(tmp_path: Path) -> None:
    db_path = tmp_path / "registry.sqlite"
    rebuild_registry_database(output_path=db_path, seed_path=DEFAULT_SEED_PATH)

    with connect_registry(db_path) as connection:
        by_key = lookup_entry(connection, "control.fuel_trim_enable")
        by_alias = lookup_entry(connection, "Fuel Trim Enable")

    assert by_key is not None
    assert by_alias is not None
    assert by_alias["page_key"] == by_key["page_key"]
    assert by_key["canonical_path"] == [
        "Module Diagnostics",
        "Engine Control Module",
        "Control Functions",
        "Fuel System",
        "Fuel Trim Enable",
    ]
    assert by_key["target_action"] == {"kind": "list_item", "label": "Fuel Trim Enable"}


def test_navigation_registry_contains_primary_user_flows(tmp_path: Path) -> None:
    db_path = tmp_path / "registry.sqlite"
    rebuild_registry_database(output_path=db_path, seed_path=DEFAULT_SEED_PATH)

    with connect_registry(db_path) as connection:
        entries = {entry["page_key"]: entry for entry in list_entries(connection)}

    required_keys = {
        "dtc.display",
        "dtc.clear.selection",
        "dtc.clear.execute",
        "data.engine_data",
        "control.menu",
        "control.fuel_system",
        "reset.menu",
        "learn.menu",
    }
    assert required_keys <= set(entries)
    assert entries["data.engine_data"]["canonical_path"] == [
        "Module Diagnostics",
        "Engine Control Module",
        "Data Display",
        "Engine Data",
    ]
    assert [
        {"kind": step["kind"], "label": step["label"]}
        for step in entries["dtc.clear.execute"]["route_steps"][-3:]
    ] == [
        {"kind": "button", "label": "Add All"},
        {"kind": "button", "label": "OK"},
        {"kind": "button", "label": "OK"},
    ]
    assert entries["dtc.clear.execute"]["route_steps"][-3]["transition"] == "clear_dtcs_selection_state"


def test_navigation_registry_seed_entries_have_success_criteria() -> None:
    seed = load_seed(DEFAULT_SEED_PATH)

    missing = [
        entry["page_key"]
        for entry in seed["entries"]
        if not entry.get("success_criteria")
    ]

    assert missing == []


def test_navigation_registry_seed_has_page_states_and_recovery_policies() -> None:
    seed = load_seed(DEFAULT_SEED_PATH)

    state_keys = {state["state_key"] for state in seed["page_states"]}
    policy_keys = {policy["policy_key"] for policy in seed["recovery_policies"]}

    assert "vehicle_selection.connected_session" in state_keys
    assert "vehicle_selection.connecting" in state_keys
    assert "vehicle_selection.disconnected" in state_keys
    assert "loading.deep_page" in state_keys
    assert "loading.restart_after_timeout" in policy_keys
    assert "device_explorer.select_sm2_usb" in policy_keys


def test_navigation_registry_policy_lookup_roundtrip(tmp_path: Path) -> None:
    db_path = tmp_path / "registry.sqlite"
    rebuild_registry_database(output_path=db_path, seed_path=DEFAULT_SEED_PATH)

    with connect_registry(db_path) as connection:
        loading = lookup_recovery_policy(connection, "loading.restart_after_timeout")
        connected = lookup_page_state(connection, "vehicle_selection.connected_session")
        states = list_page_states(connection, page_id="vehicle_selection")
        policies = list_recovery_policies(connection)

    assert loading is not None
    assert loading["action_key"] == "restart_gds2"
    assert loading["params"]["timeout_sec"] == 20.0
    assert loading["params"]["max_restarts"] == 1
    assert connected is not None
    assert connected["action_hint"] == "click_enter"
    assert len(states) >= 3
    assert any(policy["policy_key"] == "device_explorer.select_sm2_usb" for policy in policies)


def test_navigation_registry_ecm_scoped_routes_are_absolute() -> None:
    seed = load_seed(DEFAULT_SEED_PATH)
    relative = []
    for entry in seed["entries"]:
        canonical_path = entry.get("canonical_path") or []
        route_steps = [
            step.get("label")
            for step in entry.get("route_steps") or []
            if step.get("kind") == "list_item"
        ]
        canonical_list_prefix = [
            label
            for label in canonical_path
            if label not in {"Clear DTCs", "Add All", "OK"}
        ]
        if canonical_path[:2] == ["Module Diagnostics", "Engine Control Module"] and route_steps:
            if route_steps != canonical_list_prefix:
                relative.append(entry["page_key"])

    assert relative == []
