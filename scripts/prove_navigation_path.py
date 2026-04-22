from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backends.gds2.route_graph import load_graph, merge_graph_files, plan_to_action
from backends.gds2.route_navigator import GDS2RouteNavigator
from src.navigation.controller import NavigationController


DEFAULT_MANIFEST = ROOT / "scripts" / "navigation_path_proof_manifest.json"
DEFAULT_DEBUG_OUTPUT_ROOT = "reports/navigation_path_debug"


def load_manifest(path: str | Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    manifest_path = Path(path)
    if not manifest_path.is_absolute():
        manifest_path = ROOT / manifest_path
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def resolve_repo_path(path_value: str | Path) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    return ROOT / path


def load_or_build_graph(graph_path: str | Path) -> dict[str, Any]:
    path = resolve_repo_path(graph_path)
    if path.exists():
        return load_graph(path)

    route_root = ROOT / "reports" / "gds2_route_maps"
    graph_paths = sorted(item for item in route_root.glob("*/graph.json") if item.is_file())
    if not graph_paths:
        raise RuntimeError(f"No graph found at {path} and no route-map graph files exist")

    graph = merge_graph_files(graph_paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
    return graph


def final_buttons(result: dict[str, Any]) -> list[str]:
    snapshot = result.get("final_snapshot") or {}
    return [
        str(action.get("label") or "")
        for action in snapshot.get("observed_actions") or []
        if str(action.get("kind") or "") == "button" and str(action.get("label") or "")
    ]


def success_buttons_present(result: dict[str, Any], expected_buttons: list[str]) -> bool:
    buttons = set(final_buttons(result))
    return all(button in buttons for button in expected_buttons)


def graph_checksum(graph: dict[str, Any]) -> str:
    payload = json.dumps(graph, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha1(payload).hexdigest()


def validate_result(
    *,
    route_result: dict[str, Any],
    start_snapshot: dict[str, Any],
    expected_buttons: list[str],
    manifest: dict[str, Any],
) -> tuple[bool, str | None]:
    if not success_buttons_present(route_result, expected_buttons):
        return False, "final page did not expose all expected buttons"

    if manifest.get("require_target_hidden_at_start"):
        start_buttons = {
            str(action.get("label") or "")
            for action in start_snapshot.get("observed_actions") or []
            if str(action.get("kind") or "") == "button"
        }
        if all(button in start_buttons for button in expected_buttons):
            return False, "target buttons were already visible before navigation"

    min_actions = int(manifest.get("require_min_executed_actions", 0))
    if len(route_result.get("executed_actions") or []) < min_actions:
        return False, f"executed action count below minimum threshold ({min_actions})"

    return True, None


def snapshot_has_target_action(snapshot: dict[str, Any], target: str, target_kind: str) -> bool:
    target = str(target).strip()
    for action in snapshot.get("observed_actions") or []:
        if str(action.get("kind") or "") != target_kind:
            continue
        label = str(action.get("label") or "").strip()
        if label == target:
            return True
        if target_kind == "list_item" and target and label and (target in label or label in target):
            return True
    return False


def preflight_hide_target(
    *,
    controller: NavigationController,
    navigator: GDS2RouteNavigator,
    target: str,
    target_kind: str,
    max_backtracks: int,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    actions: list[dict[str, str]] = []
    snapshot = navigator._capture_settled_snapshot()
    while snapshot_has_target_action(snapshot, target, target_kind) and len(actions) < max_backtracks:
        result = controller.go_back()
        if not getattr(result, "success", False):
            break
        actions.append({"kind": "button", "label": "Back"})
        snapshot = navigator._capture_settled_snapshot()
    return snapshot, actions


def preflight_hide_success_buttons(
    *,
    controller: NavigationController,
    navigator: GDS2RouteNavigator,
    expected_buttons: list[str],
    max_backtracks: int,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    actions: list[dict[str, str]] = []
    snapshot = navigator._capture_settled_snapshot()
    while success_buttons_present({"final_snapshot": snapshot}, expected_buttons) and len(actions) < max_backtracks:
        result = controller.go_back()
        if not getattr(result, "success", False):
            break
        actions.append({"kind": "button", "label": "Back"})
        snapshot = navigator._capture_settled_snapshot()
    return snapshot, actions


def preflight_seek_min_distance(
    *,
    controller: NavigationController,
    navigator: GDS2RouteNavigator,
    graph: dict[str, Any],
    target: str,
    target_kind: str,
    min_actions: int,
    max_backtracks: int,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    actions: list[dict[str, str]] = []
    snapshot = navigator._capture_settled_snapshot()
    while len(actions) < max_backtracks:
        matched = navigator._match_snapshot(snapshot)
        if matched:
            path = plan_to_action(graph, matched, target, kind=target_kind)
            planned_actions = len(path or [])
            if navigator._snapshot_has_action(snapshot, target, kind=target_kind):
                planned_actions += 1
            elif path:
                planned_actions += 1
            if planned_actions >= min_actions:
                return snapshot, actions
        result = controller.go_back()
        if not getattr(result, "success", False):
            break
        actions.append({"kind": "button", "label": "Back"})
        snapshot = navigator._capture_settled_snapshot()
    return snapshot, actions


def run_proof(manifest: dict[str, Any]) -> dict[str, Any]:
    if manifest.get("rebuild_graph_each_run", True):
        route_root = ROOT / "reports" / "gds2_route_maps"
        graph_paths = sorted(item for item in route_root.glob("*/graph.json") if item.is_file())
        if not graph_paths:
            raise RuntimeError("No route-map graphs available to rebuild proof graph")
        graph = merge_graph_files(graph_paths)
        graph_target_path = resolve_repo_path(manifest["graph_path"])
        graph_target_path.parent.mkdir(parents=True, exist_ok=True)
        graph_target_path.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        graph = load_or_build_graph(manifest["graph_path"])
    controller = NavigationController()
    navigator = GDS2RouteNavigator(controller=controller, graph=graph)

    started_at = datetime.now().isoformat(timespec="seconds")
    target = str(manifest["target"])
    target_kind = str(manifest.get("target_kind") or "list_item")
    expected_buttons = [str(button) for button in manifest.get("success_buttons") or []]

    try:
        start_snapshot = navigator._capture_settled_snapshot()
        preflight_actions: list[dict[str, str]] = []
        if manifest.get("preflight_hide_success_buttons"):
            start_snapshot, preflight_actions = preflight_hide_success_buttons(
                controller=controller,
                navigator=navigator,
                expected_buttons=expected_buttons,
                max_backtracks=int(manifest.get("preflight_max_backtracks", 4)),
            )
        if manifest.get("preflight_backtrack_until_target_hidden"):
            start_snapshot, extra_actions = preflight_hide_target(
                controller=controller,
                navigator=navigator,
                target=target,
                target_kind=target_kind,
                max_backtracks=int(manifest.get("preflight_max_backtracks", 4)),
            )
            preflight_actions.extend(extra_actions)
        min_actions = int(manifest.get("require_min_executed_actions", 0))
        if min_actions > 0:
            start_snapshot, extra_actions = preflight_seek_min_distance(
                controller=controller,
                navigator=navigator,
                graph=graph,
                target=target,
                target_kind=target_kind,
                min_actions=min_actions,
                max_backtracks=max(0, int(manifest.get("preflight_max_backtracks", 4)) - len(preflight_actions)),
            )
            preflight_actions.extend(extra_actions)
        result = navigator.navigate_to_action(
            target,
            kind=target_kind,
            max_backtracks=int(manifest.get("max_backtracks", 8)),
            max_iterations=int(manifest.get("max_iterations", 24)),
        )
        success, failure_reason = validate_result(
            route_result=result,
            start_snapshot=start_snapshot,
            expected_buttons=expected_buttons,
            manifest=manifest,
        )
        return {
            "started_at": started_at,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "target": target,
            "target_kind": target_kind,
            "expected_buttons": expected_buttons,
            "graph_node_count": len(graph.get("nodes") or {}),
            "graph_edge_count": len(graph.get("edges") or {}),
            "graph_checksum": graph_checksum(graph),
            "start_snapshot": start_snapshot,
            "preflight_actions": preflight_actions,
            "start_buttons": [
                str(action.get("label") or "")
                for action in start_snapshot.get("observed_actions") or []
                if str(action.get("kind") or "") == "button"
            ],
            "success": success,
            "failure_reason": failure_reason,
            "final_buttons": final_buttons(result),
            "route_result": result,
        }
    except Exception as exc:
        return {
            "started_at": started_at,
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "target": target,
            "target_kind": target_kind,
            "expected_buttons": expected_buttons,
            "success": False,
            "failure_reason": str(exc),
        }


def _build_debug_runtime() -> tuple[NavigationController, GDS2RouteNavigator]:
    graph = load_or_build_graph("reports/gds2_route_maps/merged_graph.json")
    controller = NavigationController()
    navigator = GDS2RouteNavigator(controller=controller, graph=graph)
    return controller, navigator


def _snapshot_signature(snapshot: dict[str, Any]) -> tuple[Any, ...]:
    observed_actions = tuple(
        (
            str(action.get("kind") or ""),
            str(action.get("label") or ""),
        )
        for action in snapshot.get("observed_actions") or []
        if str(action.get("label") or "").strip()
    )
    return (
        str(snapshot.get("raw_page_id") or ""),
        str(snapshot.get("effective_page_id") or ""),
        tuple(str(item).strip() for item in snapshot.get("list_items") or [] if str(item).strip()),
        tuple(str(item).strip() for item in snapshot.get("navigation_path") or [] if str(item).strip()),
        observed_actions,
    )


def run_snapshot_probe() -> dict[str, Any]:
    controller, navigator = _build_debug_runtime()
    snapshot = navigator._capture_settled_snapshot()
    page_info = controller.nav.get_page_id()
    return {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "snapshot",
        "success": True,
        "page_info": page_info,
        "controller_navigation_path": controller.get_navigation_path(),
        "snapshot": snapshot,
    }


def run_breadcrumb_click_probe(item_text: str) -> dict[str, Any]:
    controller, navigator = _build_debug_runtime()
    before_snapshot = navigator._capture_settled_snapshot()
    before_page_info = controller.nav.get_page_id()
    click_result = controller.click_navigation_path_item(item_text).to_dict()
    after_snapshot = navigator._capture_settled_snapshot()
    after_page_info = controller.nav.get_page_id()
    return {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "click-breadcrumb",
        "breadcrumb_item": item_text,
        "success": bool(click_result.get("success")),
        "page_changed": _snapshot_signature(before_snapshot) != _snapshot_signature(after_snapshot),
        "before_page_info": before_page_info,
        "before_snapshot": before_snapshot,
        "click_result": click_result,
        "after_page_info": after_page_info,
        "after_snapshot": after_snapshot,
    }


def run_breadcrumb_debug_probe(inspect_depth: int) -> dict[str, Any]:
    controller, navigator = _build_debug_runtime()
    snapshot = navigator._capture_settled_snapshot()
    debug_result = controller.nav.debug_navigation_path()
    inspect_result = controller.nav.inspect_controls(max_depth=inspect_depth)
    return {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "debug-breadcrumb",
        "success": bool(debug_result.get("success")),
        "snapshot": snapshot,
        "controller_navigation_path": controller.get_navigation_path(),
        "debug_navigation_path": debug_result,
        "inspect": inspect_result,
    }


def run_button_click_probe(button_text: str) -> dict[str, Any]:
    controller, navigator = _build_debug_runtime()
    before_snapshot = navigator._capture_settled_snapshot()
    click_result = controller.click_button(button_text).to_dict()
    after_snapshot = navigator._capture_settled_snapshot()
    return {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "click-button",
        "button_text": button_text,
        "success": bool(click_result.get("success")),
        "page_changed": _snapshot_signature(before_snapshot) != _snapshot_signature(after_snapshot),
        "before_snapshot": before_snapshot,
        "click_result": click_result,
        "after_snapshot": after_snapshot,
    }


def write_report(result: dict[str, Any], output_root: str | Path) -> Path:
    output_dir = resolve_repo_path(output_root) / datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "result.json"
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prove Navigation Path + anchor recovery can route GDS2 to a target page."
    )
    parser.add_argument(
        "--mode",
        choices=("proof", "snapshot", "click-breadcrumb", "debug-breadcrumb", "click-button"),
        default="proof",
        help="Run the standard proof, snapshot current page, click one breadcrumb item, or collect breadcrumb diagnostics.",
    )
    parser.add_argument(
        "--manifest",
        default=str(DEFAULT_MANIFEST),
        help="Path to proof manifest JSON.",
    )
    parser.add_argument("--target", help="Override target label from manifest.")
    parser.add_argument("--target-kind", help="Override target kind from manifest.")
    parser.add_argument(
        "--breadcrumb-item",
        help="Breadcrumb item text used by --mode click-breadcrumb.",
    )
    parser.add_argument(
        "--button-text",
        help="Button text used by --mode click-button.",
    )
    parser.add_argument(
        "--inspect-depth",
        type=int,
        default=120,
        help="Control tree depth used by --mode debug-breadcrumb.",
    )
    parser.add_argument(
        "--output-root",
        help="Override report output root.",
    )
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="Print loaded manifest and exit without touching GDS2.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest = load_manifest(args.manifest)
    if args.target:
        manifest["target"] = args.target
    if args.target_kind:
        manifest["target_kind"] = args.target_kind

    if args.print_only:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0

    if args.mode == "proof":
        result = run_proof(manifest)
        output_root = args.output_root or manifest.get("output_root", "reports/navigation_path_proof")
        exit_code = 0 if result.get("success") else 1
    elif args.mode == "snapshot":
        result = run_snapshot_probe()
        output_root = args.output_root or DEFAULT_DEBUG_OUTPUT_ROOT
        exit_code = 0
    elif args.mode == "click-breadcrumb":
        if not args.breadcrumb_item:
            raise SystemExit("--breadcrumb-item is required when --mode click-breadcrumb is used")
        result = run_breadcrumb_click_probe(args.breadcrumb_item)
        output_root = args.output_root or DEFAULT_DEBUG_OUTPUT_ROOT
        exit_code = 0 if result.get("success") else 1
    elif args.mode == "click-button":
        if not args.button_text:
            raise SystemExit("--button-text is required when --mode click-button is used")
        result = run_button_click_probe(args.button_text)
        output_root = args.output_root or DEFAULT_DEBUG_OUTPUT_ROOT
        exit_code = 0 if result.get("success") else 1
    else:
        result = run_breadcrumb_debug_probe(args.inspect_depth)
        output_root = args.output_root or DEFAULT_DEBUG_OUTPUT_ROOT
        exit_code = 0 if result.get("success") else 1

    output_path = write_report(result, output_root)
    print(json.dumps({"report": str(output_path), **result}, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
