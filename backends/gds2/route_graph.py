from __future__ import annotations

import hashlib
import json
from collections import deque
from pathlib import Path
from typing import Any


def load_graph(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _normalize_action(action: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        "kind": str(action.get("kind") or "").strip(),
        "label": str(action.get("label") or "").strip(),
    }
    if "enabled" in action:
        normalized["enabled"] = bool(action.get("enabled"))
    if "list_index" in action:
        normalized["list_index"] = int(action.get("list_index") or 0)
    return normalized


def _normalize_actions_for_page(page_id: str, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if page_id != "main_menu":
        return actions

    allowed_main_menu_buttons = {
        "Close Application",
        "Diagnostics",
        "Home",
        "Language",
        "Manage Diagnostic Packages",
        "Preferences",
        "Release Notes",
        "Review Stored Data",
        "Update",
    }
    return [
        action
        for action in actions
        if action.get("kind") != "button" or action.get("label") in allowed_main_menu_buttons
    ]


def _semantic_identity_payload(node: dict[str, Any]) -> dict[str, Any]:
    page_id = str(node.get("page_id") or "").strip()
    actions = [
        _normalize_action(action)
        for action in node.get("observed_actions") or []
        if str(action.get("label") or "").strip()
    ]
    actions = _normalize_actions_for_page(page_id, actions)
    actions.sort(key=lambda item: (item["kind"], item["label"], item.get("list_index", -1)))
    list_items = sorted(str(item) for item in node.get("list_items") or [] if str(item).strip())
    navigation_path = [str(item).strip() for item in node.get("navigation_path") or [] if str(item).strip()]
    return {
        "page_id": page_id,
        "observed_actions": actions,
        "list_items": list_items,
        "navigation_path": navigation_path,
    }


def _semantic_node_id(node: dict[str, Any]) -> str:
    payload = _semantic_identity_payload(node)
    return hashlib.sha1(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def merge_graphs(graphs: list[dict[str, Any]]) -> dict[str, Any]:
    merged_nodes: dict[str, dict[str, Any]] = {}
    merged_edges: list[dict[str, Any]] = []
    seen_edges: set[str] = set()
    transient_page_ids = {"unknown", "loading"}
    id_mapping: dict[str, str] = {}

    for graph in graphs:
        for node_id, node in (graph.get("nodes") or {}).items():
            page_id = str(node.get("page_id") or "").strip()
            if page_id in transient_page_ids:
                continue
            semantic_id = _semantic_node_id(node)
            id_mapping[str(node_id)] = semantic_id
            if semantic_id not in merged_nodes:
                payload = _semantic_identity_payload(node)
                raw_page_id = str(node.get("raw_page_id") or "").strip() or payload["page_id"]
                merged_nodes[semantic_id] = {
                    "node_id": semantic_id,
                    "page_id": payload["page_id"],
                    "observed_actions": payload["observed_actions"],
                    "list_items": payload["list_items"],
                    "navigation_path": payload["navigation_path"],
                    "raw_page_id": raw_page_id,
                    "raw_page_ids": sorted({raw_page_id}),
                    "window_title": str(node.get("window_title") or "").strip(),
                    "aliases": sorted({str(node.get("node_id") or node_id)}),
                    "first_seen_label": str(node.get("first_seen_label") or "").strip(),
                }
            else:
                merged = merged_nodes[semantic_id]
                aliases = set(merged.get("aliases") or [])
                aliases.add(str(node.get("node_id") or node_id))
                merged["aliases"] = sorted(alias for alias in aliases if alias)
                raw_ids = set(merged.get("raw_page_ids") or [])
                raw_page_id = str(node.get("raw_page_id") or "").strip() or str(node.get("page_id") or "").strip()
                if raw_page_id:
                    raw_ids.add(raw_page_id)
                merged["raw_page_ids"] = sorted(raw_ids)
                if not merged.get("raw_page_id") and raw_page_id:
                    merged["raw_page_id"] = raw_page_id
                if not merged.get("window_title") and str(node.get("window_title") or "").strip():
                    merged["window_title"] = str(node.get("window_title") or "").strip()
                if not merged.get("first_seen_label") and str(node.get("first_seen_label") or "").strip():
                    merged["first_seen_label"] = str(node.get("first_seen_label") or "").strip()

        for edge in graph.get("edges") or []:
            source_page_id = str(edge.get("source_page_id") or "")
            target_page_id = str(edge.get("target_page_id") or "")
            if source_page_id in transient_page_ids or target_page_id in transient_page_ids:
                continue
            remapped_edge = dict(edge)
            remapped_edge["source_id"] = id_mapping.get(str(edge.get("source_id") or ""), str(edge.get("source_id") or ""))
            remapped_edge["target_id"] = id_mapping.get(str(edge.get("target_id") or ""), str(edge.get("target_id") or ""))
            edge_key = json.dumps(
                {
                    "source_id": remapped_edge.get("source_id"),
                    "target_id": remapped_edge.get("target_id"),
                    "action": remapped_edge.get("action") or {},
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            merged_edges.append(remapped_edge)

    return {
        "version": 1,
        "created_at": "merged",
        "nodes": merged_nodes,
        "edges": merged_edges,
    }


def merge_graph_files(paths: list[str | Path]) -> dict[str, Any]:
    return merge_graphs([load_graph(path) for path in paths])


def write_merged_graph(output_path: str | Path, graphs: list[dict[str, Any]]) -> dict[str, Any]:
    merged = merge_graphs(graphs)
    Path(output_path).write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return merged


def find_path(graph: dict[str, Any], start_node_id: str, target_node_id: str) -> list[dict[str, Any]] | None:
    if start_node_id == target_node_id:
        return []

    adjacency: dict[str, list[dict[str, Any]]] = {}
    for edge in graph.get("edges") or []:
        source_id = str(edge.get("source_id") or "")
        if not source_id:
            continue
        adjacency.setdefault(source_id, []).append(edge)

    queue: deque[tuple[str, list[dict[str, Any]]]] = deque([(start_node_id, [])])
    visited = {start_node_id}

    while queue:
        node_id, path = queue.popleft()
        for edge in adjacency.get(node_id, []):
            next_node_id = str(edge.get("target_id") or "")
            if not next_node_id or next_node_id in visited:
                continue
            next_path = [*path, edge]
            if next_node_id == target_node_id:
                return next_path
            visited.add(next_node_id)
            queue.append((next_node_id, next_path))

    return None


def node_has_action(node: dict[str, Any], label: str, *, kind: str | None = None) -> bool:
    for action in node.get("observed_actions") or []:
        if kind is not None and str(action.get("kind") or "") != kind:
            continue
        if str(action.get("label") or "") == label:
            return True
    return False


def match_snapshot_to_node_details(graph: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any] | None:
    snapshot_effective_page = str(snapshot.get("effective_page_id") or "").strip()
    snapshot_raw_page = str(
        snapshot.get("raw_page_id")
        or (snapshot.get("page") or {}).get("page_id")
        or ""
    ).strip()
    snapshot_actions = {
        str(action.get("label") or "")
        for action in snapshot.get("observed_actions") or []
        if str(action.get("label") or "")
    }
    snapshot_items = {
        str(item)
        for item in snapshot.get("list_items") or []
        if str(item).strip()
    }
    snapshot_navigation_path = [
        str(item).strip()
        for item in snapshot.get("navigation_path") or []
        if str(item).strip()
    ]

    best_match: dict[str, Any] | None = None
    best_score = -1
    for node_id, node in (graph.get("nodes") or {}).items():
        node_page = str(node.get("page_id") or "").strip()
        node_raw_page = str(node.get("raw_page_id") or "").strip()
        node_raw_pages = {
            str(item).strip()
            for item in node.get("raw_page_ids") or []
            if str(item).strip()
        }
        page_matches = False
        page_score = 0
        if snapshot_effective_page and node_page == snapshot_effective_page:
            page_matches = True
            page_score = 100
        elif snapshot_raw_page and (
            node_page == snapshot_raw_page
            or node_raw_page == snapshot_raw_page
            or snapshot_raw_page in node_raw_pages
        ):
            page_matches = True
            page_score = 80
        if not page_matches:
            continue

        node_actions = {
            str(action.get("label") or "")
            for action in node.get("observed_actions") or []
            if str(action.get("label") or "")
        }
        node_items = {
            str(item)
            for item in node.get("list_items") or []
            if str(item).strip()
        }
        node_navigation_path = [
            str(item).strip()
            for item in node.get("navigation_path") or []
            if str(item).strip()
        ]

        action_overlap = len(snapshot_actions & node_actions)
        item_overlap = len(snapshot_items & node_items)
        prefix_overlap = 0
        for left, right in zip(snapshot_navigation_path, node_navigation_path):
            if left != right:
                break
            prefix_overlap += 1
        score = page_score + action_overlap * 10 + item_overlap + prefix_overlap * 20
        if score > best_score:
            best_score = score
            best_match = {
                "node_id": str(node_id),
                "score": score,
                "page_score": page_score,
                "action_overlap": action_overlap,
                "item_overlap": item_overlap,
                "path_overlap": prefix_overlap,
            }

    if best_score <= 0:
        return None
    return best_match


def match_snapshot_to_node(graph: dict[str, Any], snapshot: dict[str, Any]) -> str | None:
    details = match_snapshot_to_node_details(graph, snapshot)
    if details is None:
        return None
    return str(details["node_id"])


def plan_to_action(
    graph: dict[str, Any],
    start_node_id: str,
    label: str,
    *,
    kind: str | None = None,
) -> list[dict[str, Any]] | None:
    nodes = graph.get("nodes") or {}
    candidate_node_ids = [
        node_id
        for node_id, node in nodes.items()
        if node_has_action(node, label, kind=kind)
    ]
    best_path: list[dict[str, Any]] | None = None
    for node_id in candidate_node_ids:
        path = find_path(graph, start_node_id, node_id)
        if path is None:
            continue
        if best_path is None or len(path) < len(best_path):
            best_path = path
    return best_path


def plan_to_page(
    graph: dict[str, Any],
    start_node_id: str,
    page_id: str,
) -> list[dict[str, Any]] | None:
    nodes = graph.get("nodes") or {}
    candidate_node_ids = [
        node_id
        for node_id, node in nodes.items()
        if str(node.get("page_id") or "") == page_id
    ]
    best_path: list[dict[str, Any]] | None = None
    for node_id in candidate_node_ids:
        path = find_path(graph, start_node_id, node_id)
        if path is None:
            continue
        if best_path is None or len(path) < len(best_path):
            best_path = path
    return best_path
