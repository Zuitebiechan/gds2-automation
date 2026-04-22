from __future__ import annotations

from backends.gds2.route_graph import (
    find_path,
    match_snapshot_to_node,
    match_snapshot_to_node_details,
    merge_graphs,
    plan_to_action,
    plan_to_page,
)


def _graph(*, nodes: dict, edges: list[dict]) -> dict:
    return {
        "version": 1,
        "created_at": "2026-04-17T00:00:00",
        "nodes": nodes,
        "edges": edges,
    }


def test_merge_graphs_deduplicates_nodes_and_edges() -> None:
    graph_a = _graph(
        nodes={
            "main": {
                "node_id": "main",
                "page_id": "main_menu",
                "observed_actions": [{"kind": "button", "label": "Diagnostics"}],
            },
            "menu": {
                "node_id": "menu",
                "page_id": "diagnostics_menu",
                "observed_actions": [{"kind": "list_item", "label": "Module Diagnostics"}],
            },
        },
        edges=[
            {
                "source_id": "main",
                "target_id": "menu",
                "action": {"kind": "button", "label": "Diagnostics"},
            }
        ],
    )
    graph_b = _graph(
        nodes={
            "menu": {
                "node_id": "menu",
                "page_id": "diagnostics_menu",
                "observed_actions": [{"kind": "list_item", "label": "Module Diagnostics"}],
            },
            "modules": {
                "node_id": "modules",
                "page_id": "module_list",
                "observed_actions": [{"kind": "list_item", "label": "Engine Control Module"}],
            },
        },
        edges=[
            {
                "source_id": "main",
                "target_id": "menu",
                "action": {"kind": "button", "label": "Diagnostics"},
            },
            {
                "source_id": "menu",
                "target_id": "modules",
                "action": {"kind": "list_item", "label": "Module Diagnostics"},
            },
        ],
    )

    merged = merge_graphs([graph_a, graph_b])

    assert len(merged["nodes"]) == 3
    assert len(merged["edges"]) == 2


def test_merge_graphs_filters_transient_unknown_and_loading_edges() -> None:
    graph = _graph(
        nodes={
            "start": {"node_id": "start", "page_id": "main_menu", "observed_actions": []},
            "transient": {"node_id": "transient", "page_id": "unknown", "observed_actions": []},
            "settled": {"node_id": "settled", "page_id": "diagnostics_menu", "observed_actions": []},
        },
        edges=[
            {
                "source_id": "start",
                "target_id": "transient",
                "source_page_id": "main_menu",
                "target_page_id": "unknown",
                "action": {"kind": "button", "label": "Diagnostics"},
            },
            {
                "source_id": "start",
                "target_id": "settled",
                "source_page_id": "main_menu",
                "target_page_id": "diagnostics_menu",
                "action": {"kind": "button", "label": "Diagnostics"},
            },
        ],
    )

    merged = merge_graphs([graph])

    assert len(merged["edges"]) == 1
    target_id = merged["edges"][0]["target_id"]
    assert merged["nodes"][target_id]["page_id"] == "diagnostics_menu"


def test_merge_graphs_semantically_deduplicates_equivalent_nodes() -> None:
    graph = _graph(
        nodes={
            "old": {
                "node_id": "old",
                "page_id": "data_list",
                "observed_actions": [
                    {"kind": "button", "label": "Back"},
                    {"kind": "button", "label": "Enter"},
                    {"kind": "list_item", "label": "Fuel Alcohol Content Event Data", "list_index": 0},
                ],
                "list_items": ["Fuel Alcohol Content Event Data"],
            },
            "new": {
                "node_id": "new",
                "page_id": "data_list",
                "raw_page_id": "data_list",
                "observed_actions": [
                    {"kind": "button", "label": "Back"},
                    {"kind": "button", "label": "Enter"},
                    {"kind": "list_item", "label": "Fuel Alcohol Content Event Data", "list_index": 0},
                ],
                "list_items": ["Fuel Alcohol Content Event Data"],
            },
        },
        edges=[],
    )

    merged = merge_graphs([graph])

    assert len(merged["nodes"]) == 1
    node = next(iter(merged["nodes"].values()))
    assert sorted(node["aliases"]) == ["new", "old"]


def test_merge_graphs_keeps_distinct_navigation_paths_separate() -> None:
    graph = _graph(
        nodes={
            "control": {
                "node_id": "control",
                "page_id": "data_list",
                "navigation_path": ["Module Diagnostics", "Engine Control Module", "Control Functions"],
                "observed_actions": [
                    {"kind": "button", "label": "Back"},
                    {"kind": "button", "label": "Enter"},
                    {"kind": "list_item", "label": "Fuel System", "list_index": 0},
                ],
                "list_items": ["Fuel System"],
            },
            "reset": {
                "node_id": "reset",
                "page_id": "data_list",
                "navigation_path": ["Module Diagnostics", "Engine Control Module", "Configuration/Reset Functions"],
                "observed_actions": [
                    {"kind": "button", "label": "Back"},
                    {"kind": "button", "label": "Enter"},
                    {"kind": "list_item", "label": "Fuel System", "list_index": 0},
                ],
                "list_items": ["Fuel System"],
            },
        },
        edges=[],
    )

    merged = merge_graphs([graph])

    assert len(merged["nodes"]) == 2


def test_merge_graphs_excludes_transient_nodes_entirely() -> None:
    graph = _graph(
        nodes={
            "loading_node": {
                "node_id": "loading_node",
                "page_id": "loading",
                "observed_actions": [{"kind": "button", "label": "Back"}],
                "list_items": [],
            },
            "real_node": {
                "node_id": "real_node",
                "page_id": "diagnostics_menu",
                "observed_actions": [{"kind": "list_item", "label": "Module Diagnostics"}],
                "list_items": ["Module Diagnostics"],
            },
        },
        edges=[],
    )

    merged = merge_graphs([graph])

    assert len(merged["nodes"]) == 1
    only_node = next(iter(merged["nodes"].values()))
    assert only_node["page_id"] == "diagnostics_menu"


def test_merge_graphs_normalizes_main_menu_mojibake_variant() -> None:
    graph = _graph(
        nodes={
            "clean": {
                "node_id": "clean",
                "page_id": "main_menu",
                "observed_actions": [
                    {"kind": "button", "label": "Diagnostics"},
                    {"kind": "button", "label": "Update"},
                ],
                "list_items": [],
            },
            "mojibake": {
                "node_id": "mojibake",
                "page_id": "main_menu",
                "observed_actions": [
                    {"kind": "button", "label": "Diagnostics"},
                    {"kind": "button", "label": "Update"},
                    {"kind": "button", "label": "·ñ"},
                    {"kind": "button", "label": "ÊÇ"},
                ],
                "list_items": [],
            },
        },
        edges=[],
    )

    merged = merge_graphs([graph])

    assert len(merged["nodes"]) == 1


def test_find_path_returns_shortest_edge_sequence() -> None:
    graph = _graph(
        nodes={
            "a": {"node_id": "a", "page_id": "main_menu", "observed_actions": []},
            "b": {"node_id": "b", "page_id": "diagnostics_menu", "observed_actions": []},
            "c": {"node_id": "c", "page_id": "module_list", "observed_actions": []},
        },
        edges=[
            {"source_id": "a", "target_id": "b", "action": {"kind": "button", "label": "Diagnostics"}},
            {"source_id": "b", "target_id": "c", "action": {"kind": "list_item", "label": "Module Diagnostics"}},
            {"source_id": "a", "target_id": "c", "action": {"kind": "button", "label": "Shortcut"}},
        ],
    )

    path = find_path(graph, "a", "c")

    assert path is not None
    assert [edge["action"]["label"] for edge in path] == ["Shortcut"]


def test_plan_to_action_reaches_node_that_exposes_requested_action() -> None:
    graph = _graph(
        nodes={
            "main": {
                "node_id": "main",
                "page_id": "main_menu",
                "observed_actions": [{"kind": "button", "label": "Diagnostics"}],
            },
            "modules": {
                "node_id": "modules",
                "page_id": "module_list",
                "observed_actions": [{"kind": "list_item", "label": "Engine Control Module"}],
            },
            "submenu": {
                "node_id": "submenu",
                "page_id": "module_submenu",
                "observed_actions": [{"kind": "list_item", "label": "Data Display"}],
            },
        },
        edges=[
            {"source_id": "main", "target_id": "modules", "action": {"kind": "button", "label": "Diagnostics"}},
            {
                "source_id": "modules",
                "target_id": "submenu",
                "action": {"kind": "list_item", "label": "Engine Control Module"},
            },
        ],
    )

    path = plan_to_action(graph, "main", "Data Display", kind="list_item")

    assert path is not None
    assert [edge["target_id"] for edge in path] == ["modules", "submenu"]


def test_plan_to_page_returns_shortest_route_to_matching_page() -> None:
    graph = _graph(
        nodes={
            "main": {"node_id": "main", "page_id": "main_menu", "observed_actions": []},
            "menu": {"node_id": "menu", "page_id": "diagnostics_menu", "observed_actions": []},
            "data": {
                "node_id": "data",
                "page_id": "data_display",
                "observed_actions": [{"kind": "button", "label": "Create Report"}],
            },
        },
        edges=[
            {"source_id": "main", "target_id": "menu", "action": {"kind": "button", "label": "Diagnostics"}},
            {"source_id": "menu", "target_id": "data", "action": {"kind": "list_item", "label": "Quick Path"}},
        ],
    )

    path = plan_to_page(graph, "main", "data_display")

    assert path is not None
    assert [edge["action"]["label"] for edge in path] == ["Diagnostics", "Quick Path"]


def test_match_snapshot_to_node_prefers_highest_action_overlap() -> None:
    graph = _graph(
        nodes={
            "regular": {
                "node_id": "regular",
                "page_id": "data_display",
                "observed_actions": [
                    {"kind": "button", "label": "Add Bookmark"},
                    {"kind": "button", "label": "Back"},
                    {"kind": "button", "label": "Create Report"},
                    {"kind": "button", "label": "Clear DTCs"},
                ],
                "list_items": ["Display Name", "ECU"],
            },
            "vehicle_dtc": {
                "node_id": "vehicle_dtc",
                "page_id": "data_display",
                "observed_actions": [
                    {"kind": "button", "label": "Add Bookmark"},
                    {"kind": "button", "label": "Back"},
                    {"kind": "button", "label": "Create Report"},
                    {"kind": "button", "label": "Details"},
                    {"kind": "button", "label": "Refresh"},
                ],
                "list_items": [],
            },
        },
        edges=[],
    )

    snapshot = {
        "page": {"page_id": "data_display"},
        "observed_actions": [
            {"kind": "button", "label": "Add Bookmark"},
            {"kind": "button", "label": "Back"},
            {"kind": "button", "label": "Create Report"},
            {"kind": "button", "label": "Details"},
            {"kind": "button", "label": "Refresh"},
        ],
        "list_items": [],
    }

    assert match_snapshot_to_node(graph, snapshot) == "vehicle_dtc"


def test_match_snapshot_to_node_uses_effective_page_id_when_present() -> None:
    graph = _graph(
        nodes={
            "vehicle": {
                "node_id": "vehicle",
                "page_id": "vehicle_selection",
                "observed_actions": [
                    {"kind": "button", "label": "Back"},
                    {"kind": "button", "label": "Disconnect"},
                    {"kind": "button", "label": "Enter"},
                ],
                "list_items": [],
            }
        },
        edges=[],
    )

    snapshot = {
        "page": {"page_id": "loading"},
        "raw_page_id": "loading",
        "effective_page_id": "vehicle_selection",
        "observed_actions": [
            {"kind": "button", "label": "Back"},
            {"kind": "button", "label": "Disconnect"},
            {"kind": "button", "label": "Enter"},
        ],
        "list_items": [],
    }

    assert match_snapshot_to_node(graph, snapshot) == "vehicle"


def test_match_snapshot_to_node_falls_back_to_node_raw_page_ids() -> None:
    graph = _graph(
        nodes={
            "vehicle": {
                "node_id": "vehicle",
                "page_id": "vehicle_selection",
                "raw_page_id": "vehicle_selection",
                "raw_page_ids": ["vehicle_selection", "loading"],
                "observed_actions": [
                    {"kind": "button", "label": "Back"},
                    {"kind": "button", "label": "Disconnect"},
                    {"kind": "button", "label": "Enter"},
                ],
                "list_items": [],
            }
        },
        edges=[],
    )

    snapshot = {
        "page": {"page_id": "loading"},
        "raw_page_id": "loading",
        "observed_actions": [
            {"kind": "button", "label": "Back"},
            {"kind": "button", "label": "Disconnect"},
            {"kind": "button", "label": "Enter"},
        ],
        "list_items": [],
    }

    assert match_snapshot_to_node(graph, snapshot) == "vehicle"


def test_match_snapshot_to_node_details_reports_overlap_components() -> None:
    graph = _graph(
        nodes={
            "node": {
                "node_id": "node",
                "page_id": "data_list",
                "observed_actions": [
                    {"kind": "button", "label": "Back"},
                    {"kind": "button", "label": "Enter"},
                    {"kind": "list_item", "label": "Fuel Injector Data", "list_index": 0},
                ],
                "list_items": ["Fuel Injector Data"],
            }
        },
        edges=[],
    )

    snapshot = {
        "page": {"page_id": "data_list"},
        "effective_page_id": "data_list",
        "observed_actions": [
            {"kind": "button", "label": "Back"},
            {"kind": "button", "label": "Enter"},
        ],
        "list_items": [],
    }

    details = match_snapshot_to_node_details(graph, snapshot)

    assert details is not None
    assert details["page_score"] == 100
    assert details["action_overlap"] == 2
    assert details["item_overlap"] == 0
