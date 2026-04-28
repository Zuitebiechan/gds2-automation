from __future__ import annotations

import copy
import json
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from backends.gds2.explorer_harness import GDS2ExplorerHarness
from backends.gds2.navigation_registry import (
    DEFAULT_REGISTRY_PATH,
    connect_registry,
    list_entries,
    list_page_states,
    list_recovery_policies,
    lookup_recovery_policy,
    lookup_entry,
    rebuild_registry_database,
)
from backends.gds2.runtime_status import GDS2NavigationRuntimeStatusRecorder
from backends.gds2.route_graph import load_graph, merge_graph_files, node_has_action
from backends.gds2.route_navigator import GDS2RouteNavigator
from backends.gds2.vehicle_dtc_status import is_vehicle_dtc_information_label
from diagnostic_platform.session_observability import emit_gds2_ui_event
from diagnostic_platform.runtime.errors import OperationCancelledError
from diagnostic_platform.safe_utils import (
    display_text as _display_text,
    status_value as _status_value,
    strip_optional_text as _strip_optional_text,
)
from src.navigation.action_matcher import ActionMatchError, find_action_match
from src.native.device_explorer import DeviceExplorerController
from src.navigation.controller import NavigationController
from src.streaming.agent_navigator import AgentNavigator

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GRAPH_PATH = ROOT / "reports" / "gds2_route_maps" / "merged_graph.json"
DEFAULT_REPORT_ROOT = ROOT / "reports" / "navigation_registry_probe"
GDS2_AGENT_DIR = Path("C:/tools/gds2-agent")
GDS2_AGENT_LAUNCHER = GDS2_AGENT_DIR / "launch-gds2-with-agent.bat"
DEFAULT_VCI_DEVICE_NAME = "VCI Proxy (Remote)"


@dataclass
class LoadingWatchdog:
    timeout_sec: float
    max_restarts: int
    loading_since: float | None = None
    restart_count: int = 0

    def observe(self, page_id: str, *, now: float | None = None) -> str:
        now = time.time() if now is None else now
        if page_id != "loading":
            self.loading_since = None
            return "ready"
        if self.loading_since is None:
            self.loading_since = now
            return "wait"
        if now - self.loading_since < self.timeout_sec:
            return "wait"
        if self.restart_count >= self.max_restarts:
            return "failed"
        self.restart_count += 1
        self.loading_since = None
        return "restart"


def load_or_rebuild_graph(path: str | Path = DEFAULT_GRAPH_PATH) -> dict[str, Any]:
    graph_path = Path(path)
    if graph_path.exists():
        return load_graph(graph_path)

    route_root = ROOT / "reports" / "gds2_route_maps"
    graph_paths = sorted(item for item in route_root.glob("*/graph.json") if item.is_file())
    if not graph_paths:
        raise RuntimeError(f"No graph found at {graph_path} and no route-map graph files exist")
    graph = merge_graph_files(graph_paths)
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    graph_path.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
    return graph


def restart_gds2_runtime(
    *,
    graph: dict[str, Any],
    recovery_actions: list[dict[str, Any]],
    wait_sec: float = 8.0,
) -> tuple[NavigationController, GDS2RouteNavigator]:
    stop_script = r"""
Get-Process javaw,java -ErrorAction SilentlyContinue |
  Where-Object { $_.Path -like '*\GDS 2\jre6\bin\javaw.exe' -or $_.Path -like '*\GDS 2\jre6\bin\java.exe' } |
  Stop-Process -Force
"""
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", stop_script],
        check=False,
        timeout=20,
    )
    command_file = Path.home() / "gds2-data" / "command.json"
    result_file = Path.home() / "gds2-data" / "result.json"
    command_file.unlink(missing_ok=True)
    result_file.unlink(missing_ok=True)
    subprocess.Popen(
        ["cmd", "/c", str(GDS2_AGENT_LAUNCHER)],
        cwd=str(GDS2_AGENT_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(wait_sec)
    agent_navigator = AgentNavigator(timeout_sec=15.0)
    controller = NavigationController(nav=agent_navigator)
    route_navigator = GDS2RouteNavigator(controller=controller, graph=graph)
    recovery_actions.append(
        {
            "kind": "runtime",
            "label": "restart_gds2",
            "reason": "loading page exceeded watchdog timeout",
            "success": True,
        }
    )
    return controller, route_navigator


def action_available(snapshot: dict[str, Any], expected_action: dict[str, Any]) -> bool:
    expected_kind = str(expected_action.get("kind") or "").strip()
    expected_label = str(expected_action.get("label") or "").strip()
    return find_action_match(
        snapshot.get("observed_actions") or [],
        expected_label,
        kind=expected_kind or None,
    ).matched


def match_diagnostics_from_error(exc: Exception) -> list[dict[str, Any]]:
    return copy.deepcopy(getattr(exc, "match_diagnostics", []) or [])


def navigation_path_matches(snapshot: dict[str, Any], expected_path: list[str], *, mode: str) -> bool:
    actual = [
        str(item).strip()
        for item in snapshot.get("navigation_path") or []
        if str(item).strip()
    ]
    expected = [str(item).strip() for item in expected_path if str(item).strip()]
    if not expected:
        return True
    if mode == "prefix":
        return actual[: len(expected)] == expected
    if mode == "contains_prefix":
        return actual[: len(expected)] == expected or expected[: len(actual)] == actual
    return actual == expected


def evaluate_success_criteria(snapshot: dict[str, Any], criteria: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    page_id = str(snapshot.get("effective_page_id") or "")
    missing_all = [
        expected
        for expected in criteria.get("all_of") or []
        if not action_available(snapshot, expected)
    ]
    any_of = [dict(item) for item in criteria.get("any_of") or []]
    matched_any = [item for item in any_of if action_available(snapshot, item)]
    forbidden = [
        expected
        for expected in criteria.get("forbidden_actions") or []
        if action_available(snapshot, expected)
    ]
    page_candidates = [str(item) for item in criteria.get("page_id_any") or [] if str(item)]
    page_ok = not page_candidates or page_id in page_candidates

    path_ok = True
    path_mode = None
    if criteria.get("navigation_path_prefix"):
        path_mode = "prefix"
        path_ok = navigation_path_matches(snapshot, list(criteria["navigation_path_prefix"]), mode="prefix")
    elif criteria.get("navigation_path_contains_prefix"):
        path_mode = "contains_prefix"
        path_ok = navigation_path_matches(
            snapshot,
            list(criteria["navigation_path_contains_prefix"]),
            mode="contains_prefix",
        )

    variants = [dict(item) for item in criteria.get("variants") or []]
    variant_results = []
    variant_ok = not variants
    for variant in variants:
        ok, detail = evaluate_success_criteria(snapshot, variant)
        detail["name"] = variant.get("name")
        variant_results.append(detail)
        if ok:
            variant_ok = True

    success = (
        page_ok
        and not missing_all
        and (not any_of or bool(matched_any))
        and not forbidden
        and path_ok
        and variant_ok
    )
    return success, {
        "page_ok": page_ok,
        "page_id": page_id,
        "page_id_any": page_candidates,
        "missing_all_of": missing_all,
        "matched_any_of": matched_any,
        "required_any_of": any_of,
        "forbidden_present": forbidden,
        "path_ok": path_ok,
        "path_mode": path_mode,
        "variant_results": variant_results,
    }


def validate_entry_result(entry: dict[str, Any], route_result: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    final_snapshot = route_result.get("final_snapshot") or {}
    criteria = entry.get("success_criteria") or {}
    if criteria:
        return evaluate_success_criteria(final_snapshot, criteria)
    missing = [
        expected
        for expected in entry.get("expected_actions") or []
        if not action_available(final_snapshot, expected)
    ]
    return not missing, {"missing_expected_actions": missing}


def common_prefix_length(left: list[str], right: list[str]) -> int:
    depth = 0
    for left_item, right_item in zip(left, right):
        if left_item != right_item:
            break
        depth += 1
    return depth


def snapshot_signature(snapshot: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(snapshot.get("effective_page_id") or ""),
        tuple(str(item) for item in snapshot.get("navigation_path") or []),
        tuple(str(item) for item in snapshot.get("list_items") or []),
        tuple(
            (str(action.get("kind") or ""), str(action.get("label") or ""))
            for action in snapshot.get("observed_actions") or []
        ),
    )


def expected_actions_available(snapshot: dict[str, Any], expected_actions: list[dict[str, Any]]) -> bool:
    return all(action_available(snapshot, expected_action) for expected_action in expected_actions)


def executed_actions_available(executed_actions: list[dict[str, str]], expected_actions: list[dict[str, Any]]) -> bool:
    executed_keys = {
        (
            str(action.get("kind") or "").strip(),
            str(action.get("label") or "").strip(),
        )
        for action in executed_actions
    }
    return all(
        (
            str(expected.get("kind") or "").strip(),
            str(expected.get("label") or "").strip(),
        )
        in executed_keys
        for expected in expected_actions
    )


def clear_dtcs_state_matches(state: dict[str, Any], step: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    data = state.get("data") or {}
    selected = data.get("selectedModules") or {}
    selected_rows = [
        str(item).strip()
        for item in selected.get("rows") or []
        if str(item).strip()
    ]
    buttons = data.get("buttons") or {}
    expected_modules = [str(item).strip() for item in step.get("expect_selected_modules") or [] if str(item).strip()]
    expected_enabled = [str(item).strip() for item in step.get("expect_buttons_enabled") or [] if str(item).strip()]
    expected_disabled = [str(item).strip() for item in step.get("expect_buttons_disabled") or [] if str(item).strip()]
    missing_modules = [
        module
        for module in expected_modules
        if not any(module == row or module in row or row in module for row in selected_rows)
    ]
    not_enabled = [
        label
        for label in expected_enabled
        if not bool((buttons.get(label) or {}).get("enabled"))
    ]
    not_disabled = [
        label
        for label in expected_disabled
        if not bool((buttons.get(label) or {}).get("disabled"))
    ]
    return not missing_modules and not not_enabled and not not_disabled, {
        "selected_rows": selected_rows,
        "missing_modules": missing_modules,
        "not_enabled": not_enabled,
        "not_disabled": not_disabled,
        "buttons": buttons,
    }


def execute_same_page_state_step(
    *,
    controller: NavigationController,
    step: dict[str, Any],
    recovery_actions: list[dict[str, Any]],
    timeout_sec: float = 8.0,
    poll_interval: float = 0.5,
) -> bool:
    label = str(step.get("label") or "").strip()
    click_result = controller.nav.click_button(label)
    if not click_result.get("success"):
        recovery_actions.append(
            {
                "kind": "button",
                "label": label,
                "reason": "same-page state click failed",
                "success": False,
                "result": click_result,
            }
        )
        return False
    deadline = time.time() + max(0.0, timeout_sec)
    last_details: dict[str, Any] = {}
    while time.time() <= deadline:
        state = controller.nav.get_clear_dtcs_selection_state()
        ok, details = clear_dtcs_state_matches(state, step)
        last_details = details
        if ok:
            recovery_actions.append(
                {
                    "kind": "button",
                    "label": label,
                    "reason": "verified Clear DTCs same-page state change",
                    "success": True,
                    "state": details,
                }
            )
            return True
        time.sleep(poll_interval)
    recovery_actions.append(
        {
            "kind": "button",
            "label": label,
            "reason": "Clear DTCs same-page state change did not verify",
            "success": False,
            "state": last_details,
        }
    )
    return False


def expected_action_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        str(left.get("kind") or "").strip() == str(right.get("kind") or "").strip()
        and str(left.get("label") or "").strip() == str(right.get("label") or "").strip()
    )


def first_pending_route_step(route_steps: list[dict[str, Any]], executed_actions: list[dict[str, str]]) -> dict[str, Any] | None:
    executed_index = 0
    for step in route_steps:
        step_kind = str(step.get("kind") or "").strip()
        step_label = str(step.get("label") or "").strip()
        if not step_kind or not step_label:
            continue
        if executed_index < len(executed_actions) and expected_action_equal(step, executed_actions[executed_index]):
            executed_index += 1
            continue
        return {"kind": step_kind, "label": step_label}
    return None


def select_next_route_step(
    route_steps: list[dict[str, Any]],
    executed_actions: list[dict[str, str]],
    snapshot: dict[str, Any],
) -> dict[str, Any] | None:
    current_path = [
        str(item).strip()
        for item in snapshot.get("navigation_path") or []
        if str(item).strip()
    ]
    prefix_depth = 0
    for step, path_item in zip(route_steps, current_path):
        if str(step.get("label") or "").strip() != path_item:
            break
        prefix_depth += 1

    executed_counts: dict[tuple[str, str], int] = {}
    for action in executed_actions:
        key = (
            str(action.get("kind") or "").strip(),
            str(action.get("label") or "").strip(),
        )
        executed_counts[key] = executed_counts.get(key, 0) + 1
    candidates: list[dict[str, Any]] = []
    for index, step in enumerate(route_steps):
        step_kind = str(step.get("kind") or "").strip()
        step_label = str(step.get("label") or "").strip()
        if not step_kind or not step_label:
            continue
        normalized_step = dict(step)
        normalized_step["kind"] = step_kind
        normalized_step["label"] = step_label
        key = (step_kind, step_label)
        if executed_counts.get(key, 0) > 0:
            executed_counts[key] -= 1
            continue
        if index < prefix_depth:
            continue
        candidates.append(normalized_step)

    for candidate in candidates:
        if action_available(snapshot, candidate):
            return candidate
    if candidates:
        for candidate in candidates:
            if not bool(candidate.get("optional")):
                return candidate
        return candidates[0]
    return None


def snapshot_action_labels(snapshot: dict[str, Any], *, kind: str | None = None) -> set[str]:
    return {
        str(action.get("label") or "")
        for action in snapshot.get("observed_actions") or []
        if (kind is None or str(action.get("kind") or "") == kind)
    }


def graph_page_has_action(graph: dict[str, Any], page_id: str, action: dict[str, Any]) -> bool:
    """Return whether the learned route graph says this page exposes an action."""
    normalized_page = str(page_id or "").strip()
    label = str(action.get("label") or "").strip()
    kind = str(action.get("kind") or "").strip() or None
    if not normalized_page or not label:
        return False
    for node in (graph.get("nodes") or {}).values():
        if str(node.get("page_id") or "").strip() != normalized_page:
            continue
        if node_has_action(node, label, kind=kind):
            return True
    return False


def should_try_pending_list_action(
    *,
    graph: dict[str, Any],
    snapshot: dict[str, Any],
    pending_route_step: dict[str, Any] | None,
    target_path: list[str],
) -> bool:
    """Return whether to try a pending list action despite an incomplete snapshot."""
    if pending_route_step is None:
        return False
    if str(pending_route_step.get("kind") or "") != "list_item":
        return False
    page_id = str(snapshot.get("effective_page_id") or "").strip()
    if page_id not in {"diagnostics_menu", "module_list", "module_submenu", "data_list", "sub_data_list"}:
        return False
    if action_available(snapshot, pending_route_step):
        return False
    if graph_page_has_action(graph, page_id, pending_route_step):
        return True
    return False


def startup_target_list_action(
    *,
    snapshot: dict[str, Any],
    pending_route_step: dict[str, Any] | None,
    target_action: dict[str, Any],
    target_path: list[str],
) -> dict[str, str] | None:
    """Return a diagnostics-start list action that should be attempted before recovery."""
    target_labels = {str(item).strip() for item in target_path if str(item).strip()}
    if "Module Diagnostics" not in target_labels:
        return None

    page_id = str(snapshot.get("effective_page_id") or "").strip()
    buttons = snapshot_action_labels(snapshot, kind="button")
    if page_id == "diagnostics_menu":
        deep_menu_state = True
    elif page_id in {"loading", "unknown"}:
        deep_menu_state = bool({"Back", "Home", "Vehicle Menu", "Enter"} & buttons)
    else:
        deep_menu_state = False
    if not deep_menu_state:
        return None

    candidates = [pending_route_step or {}, target_action or {}]
    for candidate in candidates:
        kind = str(candidate.get("kind") or "").strip()
        label = str(candidate.get("label") or "").strip()
        if kind == "list_item" and label == "Module Diagnostics":
            return {"kind": kind, "label": label}
    return None


def execute_startup_target_list_action(
    *,
    route_navigator: GDS2RouteNavigator,
    snapshot: dict[str, Any],
    action: dict[str, str],
) -> dict[str, Any]:
    before_signature = snapshot_signature(snapshot)
    label = str(action["label"])
    page_id = str(snapshot.get("effective_page_id") or "unknown")
    try:
        route_navigator.execute_action(action)
    except RuntimeError as exc:
        raise RuntimeError(
            f"Registry route expected '{label}' from {page_id}, "
            f"but the list item did not become available: {exc}"
        ) from exc
    after_snapshot = route_navigator.capture_settled_snapshot()
    if snapshot_signature(after_snapshot) == before_signature:
        raise RuntimeError(f"Registry route action '{label}' did not advance from {page_id}")
    return after_snapshot


def should_defer_recovery_for_route_action(
    *,
    snapshot: dict[str, Any],
    pending_route_step: dict[str, Any] | None,
    target_action: dict[str, Any],
    target_path: list[str],
) -> bool:
    if str(snapshot.get("effective_page_id") or "").strip() == "vehicle_selection":
        return False
    if pending_route_step is not None and action_available(snapshot, pending_route_step):
        return True
    if target_action and action_available(snapshot, target_action):
        return True
    return startup_target_list_action(
        snapshot=snapshot,
        pending_route_step=pending_route_step,
        target_action=target_action,
        target_path=target_path,
    ) is not None


def route_action_executed(executed_actions: list[dict[str, str]], action: dict[str, Any]) -> bool:
    kind = str(action.get("kind") or "").strip()
    label = str(action.get("label") or "").strip()
    if not kind or not label:
        return False
    return any(
        str(executed.get("kind") or "").strip() == kind
        and str(executed.get("label") or "").strip() == label
        for executed in executed_actions
    )


def should_wait_for_terminal_success(
    *,
    snapshot: dict[str, Any],
    success_details: dict[str, Any],
    executed_actions: list[dict[str, str]],
    target_action: dict[str, Any],
) -> bool:
    if not route_action_executed(executed_actions, target_action):
        return False
    if not bool(success_details.get("page_ok")):
        return False
    page_id = str(snapshot.get("effective_page_id") or "").strip()
    if page_id not in {"data_display", "sub_data_list", "module_list"}:
        return False
    return bool(
        success_details.get("missing_all_of")
        or (
            success_details.get("required_any_of")
            and not success_details.get("matched_any_of")
        )
        or not bool(success_details.get("path_ok", True))
        or any(
            not bool(detail.get("page_ok"))
            or detail.get("missing_all_of")
            or (
                detail.get("required_any_of")
                and not detail.get("matched_any_of")
            )
            or not bool(detail.get("path_ok", True))
            for detail in success_details.get("variant_results") or []
        )
    )


def button_enabled_map(snapshot: dict[str, Any], page_info: dict[str, Any]) -> dict[str, bool]:
    enabled = {
        str(key): bool(value)
        for key, value in (page_info.get("button_enabled") or {}).items()
    }
    for action in snapshot.get("observed_actions") or []:
        if str(action.get("kind") or "") == "button":
            label = str(action.get("label") or "")
            if label and label not in enabled:
                enabled[label] = True
    return enabled


def match_page_states(
    *,
    snapshot: dict[str, Any],
    page_info: dict[str, Any],
    page_states: list[dict[str, Any]],
    target_path: list[str] | None = None,
) -> list[str]:
    effective_page = str(snapshot.get("effective_page_id") or "")
    raw_page = str(snapshot.get("raw_page_id") or "")
    labels = snapshot_action_labels(snapshot)
    enabled = button_enabled_map(snapshot, page_info)
    message = str(page_info.get("vehicle_selection_message") or "")
    window_title = str(page_info.get("window_title") or snapshot.get("window_title") or "")
    prior_context = str(
        snapshot.get("prior_context")
        or (snapshot.get("context") or {}).get("prior_page")
        or (snapshot.get("context") or {}).get("prior_context")
        or ""
    )
    current_path = [str(item).strip() for item in snapshot.get("navigation_path") or [] if str(item).strip()]
    target = [str(item).strip() for item in target_path or [] if str(item).strip()]
    matches: list[str] = []

    for state in page_states:
        page_id = str(state.get("page_id") or "")
        if page_id not in {"any", effective_page, raw_page}:
            continue
        signals = dict(state.get("signals") or {})
        if signals.get("list_empty") is True and snapshot.get("list_items"):
            continue
        if any(label not in labels for label in signals.get("buttons_present") or []):
            continue
        if any(not enabled.get(label, False) for label in signals.get("buttons_enabled") or []):
            continue
        if any(enabled.get(label, True) for label in signals.get("buttons_disabled_any") or []):
            continue
        if signals.get("text_contains") and str(signals["text_contains"]).lower() not in message.lower():
            continue
        contains_any = [str(item).lower() for item in signals.get("text_contains_any") or []]
        if contains_any and not any(item in message.lower() for item in contains_any):
            continue
        if signals.get("native_window_title_contains"):
            if str(signals["native_window_title_contains"]).lower() not in window_title.lower():
                continue
        prior_any = [str(item) for item in signals.get("prior_context_any") or []]
        if prior_any and prior_context not in prior_any:
            continue
        if signals.get("current_navigation_path_not_prefix_of_target"):
            if not current_path or not target or target[: len(current_path)] == current_path:
                continue
        matches.append(str(state["state_key"]))
    return matches


def policy_for_state(recovery_policies: list[dict[str, Any]], state_key: str) -> dict[str, Any] | None:
    for policy in recovery_policies:
        if state_key in (policy.get("applies_to") or []):
            return policy
    return None


def policy_by_key(recovery_policies: list[dict[str, Any]], policy_key: str) -> dict[str, Any] | None:
    for policy in recovery_policies:
        if str(policy.get("policy_key") or "") == policy_key:
            return policy
    return None


def decide_vehicle_selection_action(page_info: dict[str, Any], labels: set[str]) -> str:
    status = str(page_info.get("vehicle_selection_status") or "").strip()
    if status in {"connected_session", "connected"}:
        return "enter" if "Enter" in labels else "wait"
    if status == "connecting":
        return "wait"
    if status in {"disconnected", "select_device"}:
        return "select_device" if "Select Device" in labels else "wait"
    if "Enter" in labels:
        return "enter"
    if "Select Device" in labels:
        return "select_device"
    if "Disconnect" in labels:
        return "wait"
    return "unknown"


def normalize_default_vci_name(device_name: str | None) -> str:
    normalized = str(device_name or "").strip()
    if not normalized or normalized.lower() == "default":
        return DEFAULT_VCI_DEVICE_NAME
    return normalized


def handle_vehicle_selection(
    *,
    controller: NavigationController,
    route_navigator: GDS2RouteNavigator,
    recovery_actions: list[dict[str, Any]],
    device_name: str = DEFAULT_VCI_DEVICE_NAME,
    page_states: list[dict[str, Any]] | None = None,
    recovery_policies: list[dict[str, Any]] | None = None,
) -> bool:
    snapshot = route_navigator.capture_settled_snapshot()
    if str(snapshot.get("effective_page_id") or "") != "vehicle_selection":
        return False
    labels = snapshot_action_labels(snapshot, kind="button")
    page_info = controller.nav.get_page_id()
    matched_states = match_page_states(
        snapshot=snapshot,
        page_info=page_info,
        page_states=page_states or [],
    )
    selected_policy = None
    for state_key in matched_states:
        selected_policy = policy_for_state(recovery_policies or [], state_key)
        if selected_policy:
            break
    next_action = str((selected_policy or {}).get("action_key") or "")
    if next_action == "select_device_and_continue":
        next_action = "select_device"
    if not next_action:
        enabled = button_enabled_map(snapshot, page_info)
        enabled_labels = {label for label in labels if enabled.get(label, False)}
        next_action = decide_vehicle_selection_action(page_info, enabled_labels)
    params = dict((selected_policy or {}).get("params") or {})
    device_name = normalize_default_vci_name(params.get("device_name") or device_name)

    if next_action == "wait":
        recovery_actions.append(
            {
                "kind": "wait",
                "label": "vehicle_selection",
                "reason": page_info.get("vehicle_selection_status") or "vehicle_selection not ready",
                "success": True,
            }
        )
        time.sleep(float(params.get("poll_interval_sec") or 2.0))
        return True

    if next_action == "enter":
        result = controller.click_enter() if hasattr(controller, "click_enter") else controller.click_button("Enter")
        recovery_actions.append(
            {
                "kind": "button",
                "label": "Enter",
                "reason": "continue from vehicle_selection",
                "success": bool(result.success),
            }
        )
        return bool(result.success)

    if next_action == "select_device":
        click_result = controller.nav.click_button("Select Device")
        recovery_actions.append(
            {
                "kind": "button",
                "label": "Select Device",
                "reason": "vehicle_selection requires VCI selection",
                "success": bool(click_result.get("success")),
            }
        )
        if not click_result.get("success"):
            return False
        time.sleep(float(params.get("pre_dialog_wait_sec") or 1.0))
        device_controller = DeviceExplorerController()
        if not device_controller.find_dialog(timeout_sec=float(params.get("dialog_timeout_sec") or 5.0)):
            recovery_actions.append(
                {
                    "kind": "device",
                    "label": device_name,
                    "reason": "Device Explorer not found",
                    "success": False,
                }
            )
            return False
        selected = device_controller.select_device_by_name(device_name)
        continued = False
        if selected:
            time.sleep(0.3)
            continued = device_controller.click_continue()
        recovery_actions.append(
            {
                "kind": "device",
                "label": device_name,
                "reason": "select VCI from Device Explorer",
                "success": bool(selected and continued),
            }
        )
        time.sleep(float(params.get("post_continue_wait_sec") or 1.5))
        return bool(selected and continued)
    return False


def handle_device_explorer(
    *,
    recovery_actions: list[dict[str, Any]],
    device_name: str = DEFAULT_VCI_DEVICE_NAME,
    policy: dict[str, Any] | None = None,
) -> bool:
    params = dict((policy or {}).get("params") or {})
    device_name = normalize_default_vci_name(params.get("device_name") or device_name)
    device_controller = DeviceExplorerController()
    if not device_controller.find_dialog(timeout_sec=float(params.get("dialog_timeout_sec") or 2.0)):
        return False
    selected = device_controller.select_device_by_name(device_name)
    continued = False
    if selected:
        time.sleep(0.3)
        continued = device_controller.click_continue()
    recovery_actions.append(
        {
            "kind": "device",
            "label": device_name,
            "reason": "initial Device Explorer requires VCI selection before Vehicle Selection banner is populated",
            "success": bool(selected and continued),
        }
    )
    time.sleep(float(params.get("post_continue_wait_sec") or 1.5))
    return bool(selected and continued)


def data_display_recovery_targets(
    entry: dict[str, Any],
    *,
    controller: NavigationController | Any | None = None,
) -> tuple[str | None, str | None]:
    if str(entry.get("page_kind") or "").strip() != "data_display":
        return None, None

    canonical_path = [
        str(item).strip()
        for item in entry.get("canonical_path") or []
        if str(item).strip()
    ]
    data_category: str | None = None
    sub_category: str | None = None
    if "Data Display" in canonical_path:
        data_index = canonical_path.index("Data Display")
        if data_index + 1 < len(canonical_path):
            data_category = canonical_path[data_index + 1]
        if data_index + 2 < len(canonical_path):
            sub_category = canonical_path[data_index + 2]

    if controller is not None:
        current_data_category = str(getattr(controller, "current_data_category", "") or "").strip()
        current_sub_category = str(getattr(controller, "current_sub_category", "") or "").strip()
        if not data_category and current_data_category:
            data_category = current_data_category
        if (
            not sub_category
            and current_sub_category
            and current_data_category
            and current_data_category == data_category
        ):
            sub_category = current_sub_category

    return data_category, sub_category


def _legacy_j2534_recovery_action(result: Any) -> dict[str, Any] | None:
    context = getattr(result, "context", {}) or {}
    recovery_method = str(context.get("recovery_method") or "").strip()
    if recovery_method == "soft_ok":
        return {
            "kind": "button",
            "label": "OK",
            "reason": "j2534_disconnect soft recovery",
            "success": True,
        }
    if recovery_method == "backtrack":
        return {
            "kind": "button",
            "label": "Back",
            "reason": "j2534_disconnect fallback backtrack",
            "success": True,
        }
    if recovery_method == "loading_wait":
        return {
            "kind": "wait",
            "label": "loading",
            "reason": "j2534_disconnect loading wait",
            "success": True,
        }
    return None


def handle_j2534_disconnect(
    *,
    controller: NavigationController,
    recovery_actions: list[dict[str, Any]],
    policy: dict[str, Any] | None = None,
    recovery_data_category: str | None = None,
    recovery_sub_category: str | None = None,
    use_legacy_data_display_recovery: bool = False,
) -> bool:
    params = dict((policy or {}).get("params") or {})
    soft_retry_attempts = int(params.get("soft_retry_attempts") or 1)
    ok_timeout_sec = float(params.get("ok_timeout_sec") or 2.0)
    backtrack_attempts = int(params.get("backtrack_attempts") or 2)
    retry_delays_value = params.get("retry_delays")
    retry_delays = (
        [float(delay) for delay in retry_delays_value]
        if isinstance(retry_delays_value, list)
        else None
    )
    if use_legacy_data_display_recovery and hasattr(controller, "recover_data_display_connection"):
        if hasattr(controller, "set_context"):
            context_updates: dict[str, Any] = {}
            if recovery_data_category:
                context_updates["data_category"] = recovery_data_category
            if recovery_sub_category:
                context_updates["sub_category"] = recovery_sub_category
            if context_updates:
                controller.set_context(**context_updates)
        legacy_result = controller.recover_data_display_connection(
            data_category=recovery_data_category,
            soft_retry_attempts=soft_retry_attempts,
            ok_timeout=ok_timeout_sec,
            allow_backtrack=True,
            backtrack_attempts=backtrack_attempts,
            retry_delays=retry_delays,
        )
        mapped_action = _legacy_j2534_recovery_action(legacy_result)
        if mapped_action is not None:
            recovery_actions.append(mapped_action)
        legacy_page = str(
            getattr(getattr(legacy_result, "page", None), "value", getattr(legacy_result, "page", ""))
            or ""
        ).strip()
        if getattr(legacy_result, "success", False):
            return True
        if legacy_page and legacy_page != "j2534_disconnect":
            recovery_actions.append(
                {
                    "kind": "legacy_recovery",
                    "label": "recover_data_display_connection",
                    "reason": "j2534_disconnect legacy recovery advanced route state",
                    "success": False,
                    "page": legacy_page,
                }
            )
            return True

    for _ in range(max(1, soft_retry_attempts)):
        buttons = controller.get_available_buttons() if hasattr(controller, "get_available_buttons") else {}
        if buttons.get("OK", False):
            result = controller.click_button("OK")
            recovery_actions.append(
                {
                    "kind": "button",
                    "label": "OK",
                    "reason": "j2534_disconnect soft recovery",
                    "success": bool(result.success),
                }
            )
            time.sleep(ok_timeout_sec)
            return bool(result.success)
    result = controller.go_back()
    recovery_actions.append(
        {
            "kind": "button",
            "label": "Back",
            "reason": "j2534_disconnect fallback backtrack",
            "success": bool(result.success),
        }
    )
    return bool(result.success)


def recover_to_registry_common_ancestor(
    *,
    controller: NavigationController,
    route_navigator: GDS2RouteNavigator,
    target_path: list[str],
    max_backtracks: int,
    device_name: str = DEFAULT_VCI_DEVICE_NAME,
    page_states: list[dict[str, Any]] | None = None,
    recovery_policies: list[dict[str, Any]] | None = None,
    recovery_data_category: str | None = None,
    recovery_sub_category: str | None = None,
    use_legacy_data_display_recovery: bool = False,
) -> list[dict[str, Any]]:
    recovery_actions: list[dict[str, Any]] = []
    if not target_path:
        return recovery_actions
    for _ in range(max_backtracks + 1):
        snapshot = route_navigator.capture_settled_snapshot()
        if str(snapshot.get("effective_page_id") or "") == "loading":
            return recovery_actions
        if str(snapshot.get("effective_page_id") or "") == "j2534_disconnect":
            policy = policy_for_state(recovery_policies or [], "j2534_disconnect.visible")
            if handle_j2534_disconnect(
                controller=controller,
                recovery_actions=recovery_actions,
                policy=policy,
                recovery_data_category=recovery_data_category,
                recovery_sub_category=recovery_sub_category,
                use_legacy_data_display_recovery=use_legacy_data_display_recovery,
            ):
                continue
            return recovery_actions
        if str(snapshot.get("effective_page_id") or "") == "device_explorer":
            policy = policy_for_state(recovery_policies or [], "device_explorer.visible")
            if handle_device_explorer(
                recovery_actions=recovery_actions,
                device_name=device_name,
                policy=policy,
            ):
                continue
        current_path = [
            str(item).strip()
            for item in snapshot.get("navigation_path") or []
            if str(item).strip()
        ]
        if not current_path:
            return recovery_actions
        prefix_depth = common_prefix_length(current_path, target_path)
        if prefix_depth == len(current_path):
            return recovery_actions
        if prefix_depth > 0:
            ancestor = current_path[prefix_depth - 1]
            before = snapshot_signature(snapshot)
            click_result = controller.click_navigation_path_item(ancestor)
            after_snapshot = route_navigator.capture_settled_snapshot()
            if click_result.success and snapshot_signature(after_snapshot) != before:
                recovery_actions.append(
                    {
                        "kind": "navigation_path",
                        "label": ancestor,
                        "reason": "current path diverged from registry canonical_path",
                    }
                )
                return recovery_actions
        labels = {
            str(action.get("label") or "")
            for action in snapshot.get("observed_actions") or []
            if str(action.get("kind") or "") == "button"
        }
        if str(snapshot.get("effective_page_id") or "") == "vehicle_selection":
            if handle_vehicle_selection(
                controller=controller,
                route_navigator=route_navigator,
                recovery_actions=recovery_actions,
                device_name=device_name,
                page_states=page_states,
                recovery_policies=recovery_policies,
            ):
                continue
        if "Back" in labels:
            result = controller.go_back()
            recovery_actions.append(
                {
                    "kind": "button",
                    "label": "Back",
                    "reason": "current path diverged from registry canonical_path",
                    "success": bool(result.success),
                }
            )
            if result.success:
                continue
        if "Home" in labels:
            result = controller.go_home()
            recovery_actions.append(
                {
                    "kind": "button",
                    "label": "Home",
                    "reason": "cannot backtrack divergent navigation path",
                    "success": bool(result.success),
                }
            )
        return recovery_actions
    return recovery_actions


class GDS2RegistryRuntimePorts:
    def __init__(self, runtime: Any) -> None:
        self._runtime = runtime

    @property
    def controller(self) -> NavigationController:
        return self._runtime.controller

    @property
    def route_navigator(self) -> GDS2RouteNavigator:
        return self._runtime.route_navigator

    @property
    def route_graph(self) -> dict[str, Any]:
        return self.route_navigator.graph

    @property
    def default_device_name(self) -> str:
        return self._runtime._default_device_name

    @property
    def page_states(self) -> list[dict[str, Any]]:
        return self._runtime._page_states

    @property
    def recovery_policies(self) -> list[dict[str, Any]]:
        return self._runtime._recovery_policies

    @property
    def loading_timeout_sec(self) -> float:
        return self._runtime._loading_timeout_sec

    @property
    def max_loading_restarts(self) -> int:
        return self._runtime._max_loading_restarts

    @property
    def route_max_iterations(self) -> int:
        return self._runtime._route_max_iterations

    @property
    def route_max_backtracks(self) -> int:
        return self._runtime._route_max_backtracks

    def restart_runtime(
        self,
        *,
        graph: dict[str, Any],
        recovery_actions: list[dict[str, Any]],
    ) -> tuple[NavigationController, GDS2RouteNavigator]:
        controller, route_navigator = self._runtime._restart_runtime(
            graph=graph,
            recovery_actions=recovery_actions,
        )
        self._runtime._controller = controller
        self._runtime._route_navigator = route_navigator
        return controller, route_navigator

    def capture_runtime_snapshot(self) -> dict[str, Any]:
        return self._runtime.capture_runtime_snapshot()

    def require_entry(self, page_key: str) -> dict[str, Any]:
        return self._runtime._require_entry(page_key)

    def execute_registry_route(self, **kwargs: Any) -> dict[str, Any]:
        return self._runtime.execute_registry_route(**kwargs)

    def match_page_states(
        self,
        snapshot: dict[str, Any],
        page_info: dict[str, Any],
        *,
        target_path: list[str] | None = None,
    ) -> list[str]:
        return self._runtime.match_page_states(
            snapshot,
            page_info,
            target_path=target_path,
        )

    def select_recovery_policy(self, state_key: str) -> dict[str, Any] | None:
        return self._runtime.select_recovery_policy(state_key)

    def recovery_coordinator(self) -> "GDS2RecoveryCoordinator":
        return self._runtime._recovery_coordinator()

    def recover_data_display_impl(
        self,
        *,
        data_category: str,
        mode: str,
        loading_watchdog: LoadingWatchdog | None = None,
    ) -> dict[str, Any] | None:
        return self._runtime._recover_data_display_impl(
            data_category=data_category,
            mode=mode,
            loading_watchdog=loading_watchdog,
        )

    def is_vehicle_dtc_context(self, snapshot: dict[str, Any]) -> bool:
        return self._runtime._is_vehicle_dtc_context(snapshot)

    def is_data_display_direct_clear_context(self, snapshot: dict[str, Any]) -> bool:
        return self._runtime._is_data_display_direct_clear_context(snapshot)

    def build_vehicle_dtc_clear_entry(self, clear_entry: dict[str, Any]) -> dict[str, Any]:
        return self._runtime._build_vehicle_dtc_clear_entry(clear_entry)

    def build_data_display_clear_entry(
        self,
        clear_entry: dict[str, Any],
        *,
        navigation_path: list[str],
    ) -> dict[str, Any]:
        return self._runtime._build_data_display_clear_entry(
            clear_entry,
            navigation_path=navigation_path,
        )

    def read_dtc_count(self, *, default: int = 0) -> int:
        return self._runtime._read_dtc_count(default=default)

    def read_post_clear_dtc_count(self, *, final_page: str, default: int = 0) -> int:
        return self._runtime._read_post_clear_dtc_count(
            final_page=final_page,
            default=default,
        )

    def set_runtime_status(self, **updates: Any) -> None:
        self._runtime._set_runtime_status(**updates)

    def build_route_status(
        self,
        *,
        entry: dict[str, Any],
        route_result: dict[str, Any],
        terminal_reason: str,
        merged_recovery_actions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return self._runtime._build_route_status(
            entry=entry,
            route_result=route_result,
            terminal_reason=terminal_reason,
            merged_recovery_actions=merged_recovery_actions,
        )


class GDS2RecoveryCoordinator:
    def __init__(self, ports: GDS2RegistryRuntimePorts) -> None:
        self._ports = ports

    def recover_to_common_ancestor(
        self,
        *,
        target_path: list[str],
        max_backtracks: int,
        recovery_data_category: str | None = None,
        recovery_sub_category: str | None = None,
        use_legacy_data_display_recovery: bool = False,
    ) -> list[dict[str, Any]]:
        ports = self._ports
        return recover_to_registry_common_ancestor(
            controller=ports.controller,
            route_navigator=ports.route_navigator,
            target_path=target_path,
            max_backtracks=max_backtracks,
            device_name=ports.default_device_name,
            page_states=ports.page_states,
            recovery_policies=ports.recovery_policies,
            recovery_data_category=recovery_data_category,
            recovery_sub_category=recovery_sub_category,
            use_legacy_data_display_recovery=use_legacy_data_display_recovery,
        )

    def handle_loading(
        self,
        *,
        snapshot: dict[str, Any],
        loading_watchdog: LoadingWatchdog,
        graph: dict[str, Any],
        state_trace: list[dict[str, Any]],
        recovery_actions: list[dict[str, Any]],
    ) -> str:
        loading_action = loading_watchdog.observe(str(snapshot.get("effective_page_id") or ""))
        if loading_action == "ready":
            return "ready"

        trace = {
            "page_id": "loading",
            "matched_state_keys": ["loading.deep_page"],
            "selected_policy_key": "loading.restart_after_timeout",
            "decision": loading_action,
        }
        state_trace.append(trace)

        if loading_action == "wait":
            recovery_actions.append(
                {
                    "kind": "wait",
                    "label": "loading",
                    "reason": "waiting for transient loading page to settle",
                    "success": True,
                }
            )
            time.sleep(2.0)
            return "handled"
        if loading_action == "restart":
            self._ports.restart_runtime(
                graph=graph,
                recovery_actions=recovery_actions,
            )
            return "handled"
        if loading_action == "failed":
            return "failed"
        return "ready"

    def handle_device_explorer(
        self,
        *,
        snapshot: dict[str, Any],
        state_trace: list[dict[str, Any]],
        recovery_actions: list[dict[str, Any]],
    ) -> bool:
        if str(snapshot.get("effective_page_id") or "") != "device_explorer":
            return False
        policy = self._ports.select_recovery_policy("device_explorer.visible")
        state_trace.append(
            {
                "page_id": "device_explorer",
                "matched_state_keys": ["device_explorer.visible"],
                "selected_policy_key": (policy or {}).get("policy_key"),
                "decision": "recover",
            }
        )
        return handle_device_explorer(
            recovery_actions=recovery_actions,
            device_name=self._ports.default_device_name,
            policy=policy,
        )

    def handle_j2534_disconnect(
        self,
        *,
        snapshot: dict[str, Any],
        state_trace: list[dict[str, Any]],
        recovery_actions: list[dict[str, Any]],
        recovery_data_category: str | None,
        recovery_sub_category: str | None,
        use_legacy_data_display_recovery: bool,
    ) -> bool:
        if str(snapshot.get("effective_page_id") or "") != "j2534_disconnect":
            return False
        policy = self._ports.select_recovery_policy("j2534_disconnect.visible")
        state_trace.append(
            {
                "page_id": "j2534_disconnect",
                "matched_state_keys": ["j2534_disconnect.visible"],
                "selected_policy_key": (policy or {}).get("policy_key"),
                "decision": "recover",
            }
        )
        return handle_j2534_disconnect(
            controller=self._ports.controller,
            recovery_actions=recovery_actions,
            policy=policy,
            recovery_data_category=recovery_data_category,
            recovery_sub_category=recovery_sub_category,
            use_legacy_data_display_recovery=use_legacy_data_display_recovery,
        )

    def handle_vehicle_selection(
        self,
        *,
        snapshot: dict[str, Any],
        target_path: list[str],
        state_trace: list[dict[str, Any]],
        recovery_actions: list[dict[str, Any]],
    ) -> bool:
        if str(snapshot.get("effective_page_id") or "") != "vehicle_selection":
            return False
        page_info = self._ports.controller.nav.get_page_id()
        matched_states = self._ports.match_page_states(
            snapshot,
            page_info,
            target_path=target_path,
        )
        selected_policy = None
        for state_key in matched_states:
            selected_policy = self._ports.select_recovery_policy(state_key)
            if selected_policy:
                break
        state_trace.append(
            {
                "page_id": "vehicle_selection",
                "matched_state_keys": matched_states,
                "selected_policy_key": (selected_policy or {}).get("policy_key"),
                "decision": "recover",
            }
        )
        return handle_vehicle_selection(
            controller=self._ports.controller,
            route_navigator=self._ports.route_navigator,
            recovery_actions=recovery_actions,
            device_name=self._ports.default_device_name,
            page_states=self._ports.page_states,
            recovery_policies=self._ports.recovery_policies,
        )

    def recover_data_display(
        self,
        *,
        data_category: str,
        mode: str,
        loading_watchdog: LoadingWatchdog | None = None,
    ) -> dict[str, Any] | None:
        return self._ports.recover_data_display_impl(
            data_category=data_category,
            mode=mode,
            loading_watchdog=loading_watchdog,
        )


class GDS2ClearDTCFlow:
    def __init__(self, ports: GDS2RegistryRuntimePorts) -> None:
        self._ports = ports

    def clear_dtcs(self) -> dict[str, Any]:
        ports = self._ports
        dtc_display_entry = ports.require_entry("dtc.display")
        clear_entry = ports.require_entry("dtc.clear.execute")
        active_clear_entry = clear_entry
        try:
            initial_snapshot = ports.capture_runtime_snapshot()
            vehicle_context = ports.is_vehicle_dtc_context(initial_snapshot)
            active_clear_entry = (
                ports.build_vehicle_dtc_clear_entry(clear_entry)
                if vehicle_context
                else clear_entry
            )
            display_entry = (
                ports.require_entry("vehicle_dtc.information")
                if vehicle_context
                else dtc_display_entry
            )
            if ports.is_data_display_direct_clear_context(initial_snapshot):
                display_result = {
                    "matched_start_node_id": None,
                    "recovery_actions": [],
                    "planned_path": [],
                    "executed_actions": [],
                    "final_page": str(initial_snapshot.get("effective_page_id") or "unknown"),
                    "final_snapshot": dict(initial_snapshot),
                    "state_trace": [],
                }
                if not vehicle_context:
                    active_clear_entry = ports.build_data_display_clear_entry(
                        clear_entry,
                        navigation_path=[
                            str(item).strip()
                            for item in initial_snapshot.get("navigation_path") or []
                            if str(item).strip()
                        ],
                    )
            else:
                display_result = ports.execute_registry_route(
                    entry=display_entry,
                    max_iterations=ports.route_max_iterations,
                    max_backtracks=ports.route_max_backtracks,
                )

            pre_clear_count = ports.read_dtc_count(default=0)
            current_snapshot = ports.capture_runtime_snapshot()
            current_page = str(current_snapshot.get("effective_page_id") or "unknown")
            if pre_clear_count <= 0:
                result = {
                    "success": True,
                    "cleared_count": 0,
                    "message": "No DTCs detected; nothing to clear",
                    "page_context": current_page,
                }
                ports.set_runtime_status(
                    status="ready",
                    last_operation="clear_dtcs",
                    last_result=copy.deepcopy(result),
                    last_route=ports.build_route_status(
                        entry=active_clear_entry,
                        route_result=display_result,
                        terminal_reason="no_dtcs",
                    ),
                )
                return result

            route_result = ports.execute_registry_route(
                entry=active_clear_entry,
                max_iterations=ports.route_max_iterations,
                max_backtracks=ports.route_max_backtracks,
            )
            final_page = str(route_result.get("final_page") or "unknown")
            recovery_actions = [
                *list(display_result.get("recovery_actions") or []),
                *list(route_result.get("recovery_actions") or []),
            ]
            post_clear_count = ports.read_post_clear_dtc_count(
                final_page=final_page,
                default=0,
            )
            cleared_count = max(0, pre_clear_count - post_clear_count)
            result = {
                "success": True,
                "cleared_count": cleared_count,
                "message": "Clear DTCs completed",
                "page_context": final_page,
                "recovery_actions": recovery_actions,
            }
            ports.set_runtime_status(
                status="ready",
                last_operation="clear_dtcs",
                last_result={
                    "success": True,
                    "cleared_count": cleared_count,
                    "message": "Clear DTCs completed",
                    "page_context": final_page,
                },
                last_route=ports.build_route_status(
                    entry=active_clear_entry,
                    route_result=route_result,
                    terminal_reason="clear_dtcs_completed",
                    merged_recovery_actions=recovery_actions,
                ),
            )
            return result
        except Exception as exc:
            ports.set_runtime_status(
                status="failed",
                last_operation="clear_dtcs",
                last_error=str(exc),
                last_route={
                    "route_target_page_key": active_clear_entry.get("page_key"),
                    "route_target_category": active_clear_entry.get("category"),
                    "canonical_path": list(active_clear_entry.get("canonical_path") or []),
                    "match_diagnostics": match_diagnostics_from_error(exc),
                    "terminal_reason": "failed_clear_dtcs",
                },
            )
            raise


class GDS2RegistryRouteExecutor:
    def __init__(self, ports: GDS2RegistryRuntimePorts) -> None:
        self._ports = ports

    def execute(
        self,
        *,
        entry: dict[str, Any],
        max_iterations: int,
        max_backtracks: int,
        cancel_checker: Callable[[], None] | None = None,
        recovery_data_category: str | None = None,
        recovery_sub_category: str | None = None,
    ) -> dict[str, Any]:
        ports = self._ports
        state_trace: list[dict[str, Any]] = []
        target_action = entry.get("target_action") or {}
        target_label = str(target_action.get("label") or "").strip()
        target_kind = str(target_action.get("kind") or "").strip()
        route_steps = [dict(step) for step in entry.get("route_steps") or []]
        success_criteria = dict(entry.get("success_criteria") or {})
        target_path = [str(item).strip() for item in entry.get("canonical_path") or [] if str(item).strip()]
        graph = ports.route_graph
        entry_data_category, entry_sub_category = data_display_recovery_targets(
            entry,
            controller=ports.controller,
        )
        route_recovery_data_category = entry_data_category or recovery_data_category
        route_recovery_sub_category = entry_sub_category or recovery_sub_category
        use_legacy_data_display_recovery = (
            str(entry.get("page_kind") or "").strip() == "data_display"
            and bool(route_recovery_data_category)
        )
        loading_watchdog = LoadingWatchdog(
            timeout_sec=ports.loading_timeout_sec,
            max_restarts=ports.max_loading_restarts,
        )
        match_diagnostics: list[dict[str, Any]] = []
        recovery = ports.recovery_coordinator()
        executed_actions: list[dict[str, str]] = []
        start_snapshot = ports.route_navigator.capture_settled_snapshot()
        initial_pending_route_step = select_next_route_step(route_steps, executed_actions, start_snapshot)

        if should_defer_recovery_for_route_action(
            snapshot=start_snapshot,
            pending_route_step=initial_pending_route_step,
            target_action=target_action,
            target_path=target_path,
        ):
            recovery_actions: list[dict[str, Any]] = []
        else:
            recovery_actions = recovery.recover_to_common_ancestor(
                target_path=target_path,
                max_backtracks=max_backtracks,
                recovery_data_category=route_recovery_data_category,
                recovery_sub_category=route_recovery_sub_category,
                use_legacy_data_display_recovery=use_legacy_data_display_recovery,
            )

        for _ in range(max_iterations):
            if cancel_checker is not None:
                cancel_checker()
            snapshot = ports.route_navigator.capture_settled_snapshot()
            loading_result = recovery.handle_loading(
                snapshot=snapshot,
                loading_watchdog=loading_watchdog,
                graph=graph,
                state_trace=state_trace,
                recovery_actions=recovery_actions,
            )
            if loading_result == "handled":
                continue
            if loading_result == "failed":
                raise RuntimeError(
                    f"Loading page exceeded {ports.loading_timeout_sec}s and max restart count {ports.max_loading_restarts}"
                )

            if recovery.handle_device_explorer(
                snapshot=snapshot,
                state_trace=state_trace,
                recovery_actions=recovery_actions,
            ):
                continue
            if recovery.handle_j2534_disconnect(
                snapshot=snapshot,
                state_trace=state_trace,
                recovery_actions=recovery_actions,
                recovery_data_category=route_recovery_data_category,
                recovery_sub_category=route_recovery_sub_category,
                use_legacy_data_display_recovery=use_legacy_data_display_recovery,
            ):
                continue

            success_requires_executed = [dict(action) for action in success_criteria.get("requires_executed") or []]
            if success_criteria and executed_actions_available(executed_actions, success_requires_executed):
                success, _details = evaluate_success_criteria(snapshot, success_criteria)
                if success:
                    return {
                        "matched_start_node_id": ports.route_navigator.match_snapshot(start_snapshot),
                        "recovery_actions": recovery_actions,
                        "planned_path": route_steps,
                        "executed_actions": executed_actions,
                        "final_page": snapshot["effective_page_id"],
                        "final_snapshot": snapshot,
                        "state_trace": state_trace,
                        "match_diagnostics": match_diagnostics,
                    }
                if should_wait_for_terminal_success(
                    snapshot=snapshot,
                    success_details=_details,
                    executed_actions=executed_actions,
                    target_action=target_action,
                ):
                    recovery_actions.append(
                        {
                            "kind": "wait",
                            "label": str(snapshot.get("effective_page_id") or "target_page"),
                            "reason": "waiting for target page success criteria",
                            "success": True,
                            "state": _details,
                        }
                    )
                    time.sleep(1.0)
                    continue

            button_labels = snapshot_action_labels(snapshot, kind="button")
            pending_route_step = select_next_route_step(route_steps, executed_actions, snapshot)
            if "OK" in button_labels and not (
                pending_route_step
                and pending_route_step.get("kind") == "button"
                and pending_route_step.get("label") == "OK"
            ):
                result = ports.controller.click_button("OK")
                recovery_actions.append(
                    {
                        "kind": "button",
                        "label": "OK",
                        "reason": "dismiss blocking modal",
                        "success": bool(result.success),
                    }
                )
                continue

            if recovery.handle_vehicle_selection(
                snapshot=snapshot,
                target_path=target_path,
                state_trace=state_trace,
                recovery_actions=recovery_actions,
            ):
                continue

            if pending_route_step is None and target_label and target_kind:
                target_match = ports.route_navigator.snapshot_action_match(snapshot, target_label, kind=target_kind)
                if target_match.ambiguous:
                    match_diagnostics.append(
                        target_match.diagnostics(
                            target_label=target_label,
                            action_kind=target_kind,
                            owner="registry_runtime.target_action",
                        )
                    )
                    raise ActionMatchError(
                        f"Ambiguous action match for registry target '{target_label}'",
                        match_diagnostics,
                    )
                if target_match.matched:
                    if target_match.resolution != "exact":
                        match_diagnostics.append(
                            target_match.diagnostics(
                                target_label=target_label,
                                action_kind=target_kind,
                                owner="registry_runtime.target_action",
                            )
                        )
                    ports.route_navigator.execute_action({"kind": target_kind, "label": target_label})
                    executed_actions.append({"kind": target_kind, "label": target_label})
                    continue

            bridge_action = ports.route_navigator.resolve_bridge_action(snapshot)
            if bridge_action is not None:
                ports.route_navigator.execute_action(bridge_action)
                executed_actions.append({"kind": str(bridge_action["kind"]), "label": str(bridge_action["label"])})
                continue

            if (
                should_try_pending_list_action(
                    graph=graph,
                    snapshot=snapshot,
                    pending_route_step=pending_route_step,
                    target_path=target_path,
                )
            ):
                before_signature = snapshot_signature(snapshot)
                ports.route_navigator.execute_action(pending_route_step)
                after_snapshot = ports.route_navigator.capture_settled_snapshot()
                if snapshot_signature(after_snapshot) == before_signature:
                    raise RuntimeError(
                        "Registry route action "
                        f"'{pending_route_step['label']}' did not advance from "
                        f"{snapshot.get('effective_page_id') or 'unknown'}"
                    )
                executed_actions.append(
                    {
                        "kind": str(pending_route_step["kind"]),
                        "label": str(pending_route_step["label"]),
                    }
                )
                continue

            pending_match = None
            if pending_route_step is not None:
                pending_match = ports.route_navigator.snapshot_action_match(
                    snapshot,
                    str(pending_route_step["label"]),
                    kind=str(pending_route_step["kind"]),
                )
                if pending_match.ambiguous:
                    match_diagnostics.append(
                        pending_match.diagnostics(
                            target_label=str(pending_route_step["label"]),
                            action_kind=str(pending_route_step["kind"]),
                            owner="registry_runtime.route_step",
                        )
                    )
                    raise ActionMatchError(
                        f"Ambiguous action match for route step '{pending_route_step['label']}'",
                        match_diagnostics,
                    )

            if pending_route_step is not None and pending_match is not None and pending_match.matched:
                if pending_match.resolution != "exact":
                    match_diagnostics.append(
                        pending_match.diagnostics(
                            target_label=str(pending_route_step["label"]),
                            action_kind=str(pending_route_step["kind"]),
                            owner="registry_runtime.route_step",
                        )
                    )
                if pending_route_step.get("transition") == "clear_dtcs_selection_state":
                    if not execute_same_page_state_step(
                        controller=ports.controller,
                        step=pending_route_step,
                        recovery_actions=recovery_actions,
                    ):
                        raise RuntimeError("Clear DTCs same-page state transition failed")
                    executed_actions.append({"kind": str(pending_route_step["kind"]), "label": str(pending_route_step["label"])})
                    continue
                ports.route_navigator.execute_action(pending_route_step)
                executed_actions.append({"kind": str(pending_route_step["kind"]), "label": str(pending_route_step["label"])})
                continue

            startup_action = startup_target_list_action(
                snapshot=snapshot,
                pending_route_step=pending_route_step,
                target_action=target_action,
                target_path=target_path,
            )
            if startup_action is not None:
                execute_startup_target_list_action(
                    route_navigator=ports.route_navigator,
                    snapshot=snapshot,
                    action=startup_action,
                )
                executed_actions.append(
                    {
                        "kind": str(startup_action["kind"]),
                        "label": str(startup_action["label"]),
                    }
                )
                continue

            recovered = recovery.recover_to_common_ancestor(
                target_path=target_path,
                max_backtracks=max_backtracks,
                recovery_data_category=route_recovery_data_category,
                recovery_sub_category=route_recovery_sub_category,
                use_legacy_data_display_recovery=use_legacy_data_display_recovery,
            )
            if recovered:
                recovery_actions.extend(recovered)
                continue

            if "Back" in button_labels and str(snapshot.get("effective_page_id") or "") != "vehicle_selection":
                result = ports.controller.go_back()
                recovery_actions.append({"kind": "button", "label": "Back", "success": bool(result.success)})
                if result.success:
                    continue

            raise RuntimeError(f"Registry route to '{entry['page_key']}' is stuck on {snapshot['effective_page_id']}")

        raise RuntimeError(f"Registry route to '{entry['page_key']}' did not converge within {max_iterations} steps")


class RegistryNavigationRuntime:
    def __init__(
        self,
        *,
        controller: NavigationController,
        route_navigator: GDS2RouteNavigator,
        entries: list[dict[str, Any]] | None = None,
        page_states: list[dict[str, Any]] | None = None,
        recovery_policies: list[dict[str, Any]] | None = None,
        loading_timeout_sec: float = 30.0,
        max_loading_restarts: int = 1,
        restart_runtime: Callable[..., tuple[NavigationController, GDS2RouteNavigator]] | None = None,
        read_dtcs_snapshot: Callable[[], dict[str, Any]] | None = None,
        read_dtc_count: Callable[[], int] | None = None,
        state_reader: Callable[[], dict[str, Any]] | None = None,
        default_device_name: str = DEFAULT_VCI_DEVICE_NAME,
        route_max_iterations: int = 40,
        route_max_backtracks: int = 12,
    ) -> None:
        self._controller = controller
        self._route_navigator = route_navigator
        self._entries = [dict(entry) for entry in entries or []]
        self._page_states = list(page_states or [])
        self._recovery_policies = list(recovery_policies or [])
        self._loading_timeout_sec = loading_timeout_sec
        self._max_loading_restarts = max_loading_restarts
        self._restart_runtime = restart_runtime or restart_gds2_runtime
        self._read_dtcs_snapshot = read_dtcs_snapshot
        self._read_dtc_count_callback = read_dtc_count
        self._state_reader = state_reader
        self._default_device_name = normalize_default_vci_name(default_device_name)
        self._route_max_iterations = route_max_iterations
        self._route_max_backtracks = route_max_backtracks
        self._status_recorder = GDS2NavigationRuntimeStatusRecorder(
            default_device_name=self._default_device_name,
            loading_timeout_sec=self._loading_timeout_sec,
            max_loading_restarts=self._max_loading_restarts,
        )
        self._ports = GDS2RegistryRuntimePorts(self)
        self._recovery_coordinator_instance = GDS2RecoveryCoordinator(self._ports)
        self._route_executor_instance = GDS2RegistryRouteExecutor(self._ports)
        self._clear_dtc_flow_instance = GDS2ClearDTCFlow(self._ports)
        self._entries_by_alias: dict[str, dict[str, Any]] = {}
        for entry in self._entries:
            page_key = str(entry.get("page_key") or "").strip()
            if page_key:
                self._entries_by_alias[page_key.lower()] = entry
            for alias in entry.get("aliases") or []:
                alias_key = str(alias).strip().lower()
                if alias_key:
                    self._entries_by_alias.setdefault(alias_key, entry)

    @property
    def controller(self) -> NavigationController:
        return self._controller

    @property
    def route_navigator(self) -> GDS2RouteNavigator:
        return self._route_navigator

    def capture_runtime_snapshot(self) -> dict[str, Any]:
        return self._route_navigator.capture_settled_snapshot()

    def get_runtime_status(self) -> dict[str, Any]:
        return self._status_recorder.get_status()

    def _recovery_coordinator(self) -> GDS2RecoveryCoordinator:
        return self._recovery_coordinator_instance

    def _route_executor(self) -> GDS2RegistryRouteExecutor:
        return self._route_executor_instance

    def _clear_dtc_flow(self) -> GDS2ClearDTCFlow:
        return self._clear_dtc_flow_instance

    def match_page_states(self, snapshot: dict[str, Any], page_info: dict[str, Any], target_path: list[str] | None = None) -> list[str]:
        return match_page_states(
            snapshot=snapshot,
            page_info=page_info,
            page_states=self._page_states,
            target_path=target_path,
        )

    def select_recovery_policy(self, state_key: str) -> dict[str, Any] | None:
        return policy_for_state(self._recovery_policies, state_key)

    def ensure_started(
        self,
        *,
        cancel_checker: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        entry = self._require_entry("diagnostics.module_diagnostics")
        try:
            route_result = self.execute_registry_route(
                entry=entry,
                max_iterations=self._route_max_iterations,
                max_backtracks=self._route_max_backtracks,
                cancel_checker=cancel_checker,
            )
            final_snapshot = dict(route_result.get("final_snapshot") or {})
            modules = [
                str(item)
                for item in final_snapshot.get("list_items") or []
                if str(item).strip()
            ]
            state = self._read_runtime_state()
            result = {
                "modules": modules,
                "vin": state.get("vin"),
                "device": state.get("device"),
            }
            self._set_runtime_status(
                status="ready",
                last_operation="ensure_started",
                last_result=copy.deepcopy(result),
                last_route=self._build_route_status(
                    entry=entry,
                    route_result=route_result,
                    terminal_reason="ready",
                ),
            )
            return result
        except Exception as exc:
            self._set_runtime_status(
                status="failed",
                last_operation="ensure_started",
                last_error=str(exc),
                last_route={
                    "route_target_page_key": entry.get("page_key"),
                    "route_target_category": entry.get("category"),
                    "canonical_path": list(entry.get("canonical_path") or []),
                    "match_diagnostics": match_diagnostics_from_error(exc),
                    "terminal_reason": "failed_before_ready",
                },
            )
            raise

    def connect_vci(self, device: str) -> None:
        self._default_device_name = normalize_default_vci_name(device or self._default_device_name)
        self.ensure_started()
        self._set_runtime_status(
            status="ready",
            last_operation="connect_vci",
            default_device_name=self._default_device_name,
        )

    def select_module(self, module: str) -> Any:
        entry = self._lookup_entry(module)
        try:
            if entry is not None:
                route_result = self.execute_registry_route(
                    entry=entry,
                    max_iterations=self._route_max_iterations,
                    max_backtracks=self._route_max_backtracks,
                )
                final_snapshot = dict(route_result.get("final_snapshot") or {})
            else:
                route_result = self._route_navigator.navigate_to_action(
                    str(module),
                    kind="list_item",
                    max_backtracks=self._route_max_backtracks,
                    max_iterations=self._route_max_iterations,
                )
                final_snapshot = dict(route_result.get("final_snapshot") or {})
            final_page = str(route_result.get("final_page") or "")
            if final_page == "module_submenu":
                data_display_item = self._resolve_data_display_item()
                self._route_navigator.execute_action(
                    {"kind": "list_item", "label": data_display_item}
                )
                final_snapshot = self.capture_runtime_snapshot()
                final_page = str(final_snapshot.get("effective_page_id") or final_page)
            if final_page != "data_list":
                raise RuntimeError(
                    f"Registry module selection did not reach data list (got {final_page or 'unknown'})"
                )
            result = {
                "selected_module": str(module),
                "data_categories": [
                    str(item)
                    for item in final_snapshot.get("list_items") or []
                    if str(item).strip()
                ],
            }
            self._set_runtime_status(
                status="ready",
                last_operation="select_module",
                last_result=copy.deepcopy(result),
                last_route=self._build_route_status(
                    entry=entry or {"page_key": str(module), "canonical_path": []},
                    route_result=route_result,
                    terminal_reason="selected_module",
                ),
            )
            return result
        except Exception as exc:
            self._set_runtime_status(
                status="failed",
                last_operation="select_module",
                last_error=str(exc),
                last_route={
                    "route_target_page_key": (entry or {}).get("page_key") or str(module),
                    "route_target_category": (entry or {}).get("category") or "",
                    "canonical_path": list((entry or {}).get("canonical_path") or []),
                    "match_diagnostics": match_diagnostics_from_error(exc),
                    "terminal_reason": "failed_select_module",
                },
            )
            raise

    def select_data_category(self, category: str) -> Any:
        entry = self._lookup_entry(category)
        try:
            if entry is not None:
                route_result = self.execute_registry_route(
                    entry=entry,
                    max_iterations=self._route_max_iterations,
                    max_backtracks=self._route_max_backtracks,
                )
                final_snapshot = dict(route_result.get("final_snapshot") or {})
            else:
                snapshot = self.capture_runtime_snapshot()
                if not self._route_navigator.snapshot_has_action(
                    snapshot,
                    str(category),
                    kind="list_item",
                ):
                    raise RuntimeError(f"Navigation registry entry not found: {category}")
                self._route_navigator.execute_action(
                    {"kind": "list_item", "label": str(category)}
                )
                final_snapshot = self.capture_runtime_snapshot()
                route_result = {
                    "matched_start_node_id": None,
                    "planned_path": [],
                    "executed_actions": [{"kind": "list_item", "label": str(category)}],
                    "recovery_actions": [],
                    "final_page": str(final_snapshot.get("effective_page_id") or ""),
                    "final_snapshot": final_snapshot,
                    "state_trace": [],
                }
            final_page = str(final_snapshot.get("effective_page_id") or "")
            result = {
                "selected_data_category": str(category),
                "sub_categories": [
                    str(item)
                    for item in final_snapshot.get("list_items") or []
                    if str(item).strip()
                ]
                if final_page == "sub_data_list"
                else [],
            }
            self._set_runtime_status(
                status="ready",
                last_operation="select_data_category",
                last_result=copy.deepcopy(result),
                last_route=self._build_route_status(
                    entry=entry or {"page_key": str(category), "canonical_path": []},
                    route_result=route_result,
                    terminal_reason="selected_data_category",
                ),
            )
            return result
        except Exception as exc:
            self._set_runtime_status(
                status="failed",
                last_operation="select_data_category",
                last_error=str(exc),
                last_route={
                    "route_target_page_key": (entry or {}).get("page_key") or str(category),
                    "route_target_category": (entry or {}).get("category") or "",
                    "canonical_path": list((entry or {}).get("canonical_path") or []),
                    "match_diagnostics": match_diagnostics_from_error(exc),
                    "terminal_reason": "failed_select_data_category",
                },
            )
            raise

    def clear_dtcs(self) -> dict[str, Any]:
        return self._clear_dtc_flow().clear_dtcs()

    def detect_current_page(self) -> str:
        try:
            page = self._controller.detect_current_page()
        except TypeError:
            page = self._controller.detect_current_page(retries=0)
        return str(getattr(page, "value", page))

    def go_back(self) -> Any:
        return self._controller.go_back()

    def start_navigation_session(self, runtime: Any, goal: str) -> Any:
        from diagnostic_platform.runtime.navigation_runtime import NavSession

        normalized_goal = _strip_optional_text(goal) or "Navigate to Data Display"
        session_id = uuid.uuid4().hex[:16]
        session = NavSession(session_id=session_id, goal=normalized_goal)
        session.cleanup_callback = runtime.schedule_navigation_session_cleanup

        thread = threading.Thread(
            target=self._run_navigation_session_thread,
            args=(runtime, session),
            daemon=True,
            name=f"gds2-registry-nav-{session_id}",
        )
        session.thread = thread
        runtime.set_navigation_session(session_id, session)
        thread.start()
        self._set_runtime_status(
            status="running",
            last_operation="start_navigation_session",
            active_navigation_session_id=session_id,
            navigation_goal=normalized_goal,
        )
        return session

    def submit_navigation_decision(
        self,
        runtime: Any,
        session_id: str,
        *,
        decision_id: str,
        selected_item: str,
    ) -> dict[str, Any]:
        from diagnostic_platform.runtime.navigation_runtime import NavSessionStatus

        session = runtime.get_navigation_session(session_id)
        if _status_value(session.status) != NavSessionStatus.AWAITING_DECISION.value:
            raise ValueError(
                f"Session is not awaiting a decision (status={_status_value(session.status)})"
            )
        if decision_id and session.pending_decision_id and decision_id != session.pending_decision_id:
            raise ValueError(
                f"Decision ID mismatch: expected '{session.pending_decision_id}', got '{decision_id}'"
            )
        session.status = NavSessionStatus.RUNNING
        session.pending_decision_id = None
        session.pending_items = []
        session.decision_queue.put({"selected_item": selected_item})
        self._set_runtime_status(
            status="running",
            last_operation="submit_navigation_decision",
            active_navigation_session_id=session_id,
            last_navigation_decision={
                "decision_id": decision_id,
                "selected_item": selected_item,
            },
        )
        return {
            "success": True,
            "session_id": session_id,
            "selected_item": selected_item,
        }

    def abort_navigation_session(self, runtime: Any, session_id: str) -> dict[str, Any]:
        from diagnostic_platform.runtime.navigation_runtime import NavSessionStatus

        session = runtime.get_navigation_session(session_id)
        session_status = _status_value(session.status)
        if session_status in (
            NavSessionStatus.COMPLETED.value,
            NavSessionStatus.FAILED.value,
            NavSessionStatus.ABORTED.value,
        ):
            raise ValueError(f"Session already terminated (status={session_status})")
        session.status = NavSessionStatus.ABORTED
        session.error = "Aborted by user"
        session.cancel()
        try:
            session.decision_queue.put_nowait({"selected_item": ""})
        except Exception:
            pass
        try:
            session.event_queue.put_nowait({"type": "error", "error": "Aborted by user"})
        except Exception:
            pass
        if callable(session.cleanup_callback):
            session.cleanup_callback(session_id)
        else:
            runtime.schedule_navigation_session_cleanup(session_id)
        self._set_runtime_status(
            status="aborted",
            last_operation="abort_navigation_session",
            active_navigation_session_id=session_id,
            terminal_reason="aborted_by_user",
        )
        return {
            "success": True,
            "session_id": session_id,
            "status": _status_value(session.status),
        }

    def get_navigation_session(self, runtime: Any, session_id: str) -> Any:
        return runtime.get_navigation_session(session_id)

    def recover_data_display(
        self,
        *,
        data_category: str,
        mode: str,
        loading_watchdog: LoadingWatchdog | None = None,
    ) -> dict[str, Any] | None:
        return self._recovery_coordinator().recover_data_display(
            data_category=data_category,
            mode=mode,
            loading_watchdog=loading_watchdog,
        )

    def _recover_data_display_impl(
        self,
        *,
        data_category: str,
        mode: str,
        loading_watchdog: LoadingWatchdog | None = None,
    ) -> dict[str, Any] | None:
        def _emit_recovery_failed(*, page: str, failure_code: str, reason: str) -> None:
            emit_gds2_ui_event(
                "recovery_failed",
                operation_kind=f"recover_data_display:{mode}",
                page=page or "unknown",
                status="error",
                failure_code=failure_code,
                reason=reason,
                data_category=data_category,
                recovery_mode=mode,
            )

        pre_recovery_actions: list[dict[str, Any]] = []
        snapshot = self.capture_runtime_snapshot()
        current_page = str(snapshot.get("effective_page_id") or "")
        if current_page == "data_display":
            return None
        emit_gds2_ui_event(
            "recovery_attempted",
            operation_kind=f"recover_data_display:{mode}",
            page=current_page or "unknown",
            reason="recover_data_display_attempted",
            data_category=data_category,
            recovery_mode=mode,
        )

        if current_page == "loading" and loading_watchdog is not None:
            loading_action = loading_watchdog.observe("loading")
            if loading_action == "wait":
                return {
                    "ok": True,
                    "mode": mode,
                    "message": "Waiting for GDS2 loading page to finish...",
                }
            if loading_action == "failed":
                result = {
                    "ok": False,
                    "mode": mode,
                    "error": (
                        f"Loading page exceeded {self._loading_timeout_sec}s and max restart count "
                        f"{self._max_loading_restarts}"
                    ),
                }
                self._set_runtime_status(
                    status="failed",
                    last_operation="recover_data_display",
                    last_error=result["error"],
                    last_route={
                        "route_target_page_key": str(data_category),
                        "terminal_reason": "failed_loading_watchdog",
                    },
                )
                _emit_recovery_failed(
                    page=current_page or "loading",
                    failure_code="loading_watchdog_failed",
                    reason=result["error"],
                )
                return result
            if loading_action == "restart":
                self._controller, self._route_navigator = self._restart_runtime(
                    graph=self._route_navigator.graph,
                    recovery_actions=pre_recovery_actions,
                )
                snapshot = self.capture_runtime_snapshot()

        try:
            target_entry = self._require_entry(data_category)
            route_result = self.execute_registry_route(
                entry=target_entry,
                max_iterations=self._route_max_iterations,
                max_backtracks=self._route_max_backtracks,
                recovery_data_category=data_category,
            )
        except Exception as exc:
            result = {
                "ok": False,
                "mode": mode,
                "error": str(exc),
            }
            self._set_runtime_status(
                status="failed",
                last_operation="recover_data_display",
                last_error=str(exc),
                last_route={
                    "route_target_page_key": str(data_category),
                    "match_diagnostics": match_diagnostics_from_error(exc),
                    "terminal_reason": "failed_route_execution",
                },
            )
            _emit_recovery_failed(
                page=current_page or "unknown",
                failure_code="route_execution_failed",
                reason=str(exc),
            )
            return result

        final_page = str(route_result.get("final_page") or "")
        if final_page != "data_display":
            result = {
                "ok": False,
                "mode": mode,
                "error": f"Registry recovery did not restore Data Display (got {final_page or 'unknown'})",
            }
            self._set_runtime_status(
                status="failed",
                last_operation="recover_data_display",
                last_error=result["error"],
                last_route=self._build_route_status(
                    entry=target_entry,
                    route_result=route_result,
                    terminal_reason="failed_restore_data_display",
                ),
            )
            _emit_recovery_failed(
                page=final_page or "unknown",
                failure_code="failed_restore_data_display",
                reason=result["error"],
            )
            return result

        recovery_actions = [
            *pre_recovery_actions,
            *list(route_result.get("recovery_actions") or []),
        ]
        recovery_method = self._derive_recovery_method(recovery_actions)
        restart_collection = mode == "ai_collect" and recovery_method in {
            "backtrack",
            "restart_gds2",
        }
        message = self._recovery_message_for(mode=mode, recovery_method=recovery_method)
        result = {
            "ok": True,
            "mode": mode,
            "recovered": True,
            "recovery_method": recovery_method,
            "restart_collection": restart_collection,
            "message": message,
            "recovery_actions": recovery_actions,
        }
        self._set_runtime_status(
            status="ready",
            last_operation="recover_data_display",
            last_result={
                "ok": True,
                "mode": mode,
                "recovered": True,
                "recovery_method": recovery_method,
                "restart_collection": restart_collection,
                "message": message,
            },
            last_route=self._build_route_status(
                entry=target_entry,
                route_result=route_result,
                terminal_reason="recovered_data_display",
                merged_recovery_actions=recovery_actions,
            ),
        )
        emit_gds2_ui_event(
            "recovery_succeeded",
            operation_kind=f"recover_data_display:{mode}",
            page="data_display",
            reason=message,
            data_category=data_category,
            recovery_mode=mode,
            recovery_method=recovery_method,
            restart_collection=restart_collection,
            recovery_actions=recovery_actions,
        )
        return result

    def _lookup_entry(self, key_or_alias: str) -> dict[str, Any] | None:
        return self._entries_by_alias.get(str(key_or_alias or "").strip().lower())

    def _require_entry(self, key_or_alias: str) -> dict[str, Any]:
        entry = self._lookup_entry(key_or_alias)
        if entry is None:
            raise RuntimeError(f"Navigation registry entry not found: {key_or_alias}")
        return dict(entry)

    def _read_runtime_state(self) -> dict[str, Any]:
        if not callable(self._state_reader):
            return {}
        try:
            return dict(self._state_reader() or {})
        except Exception:
            return {}

    @staticmethod
    def _is_data_display_direct_clear_context(snapshot: dict[str, Any]) -> bool:
        return (
            str(snapshot.get("effective_page_id") or "") == "data_display"
            and "Clear DTCs" in snapshot_action_labels(snapshot, kind="button")
        )

    def _is_vehicle_dtc_context(self, snapshot: dict[str, Any]) -> bool:
        state = self._read_runtime_state()
        if is_vehicle_dtc_information_label(state.get("data_category")):
            return True

        navigation_path = [
            str(item).strip()
            for item in snapshot.get("navigation_path") or []
            if str(item).strip()
        ]
        return bool(navigation_path) and str(navigation_path[0]).strip().casefold() == "vehicle diagnostics"

    @staticmethod
    def _build_data_display_clear_entry(
        clear_entry: dict[str, Any],
        *,
        page_key: str = "data_display.clear.execute",
        title: str = "Execute Clear DTCs From Current Data Display",
        navigation_path: list[str] | None = None,
        success_criteria: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        add_all_step = next(
            (
                dict(step)
                for step in clear_entry.get("route_steps") or []
                if str(step.get("label") or "").strip() == "Add All"
            ),
            {"kind": "button", "label": "Add All"},
        )
        return {
            "page_key": page_key,
            "title": title,
            "category": "dtc",
            "page_kind": "clear_dtcs_execute",
            "canonical_path": [
                *list(navigation_path or ["Data Display"]),
                "Clear DTCs",
                "Add All",
                "OK",
                "OK",
            ],
            "route_steps": [
                {"kind": "button", "label": "Clear DTCs"},
                add_all_step,
                {"kind": "button", "label": "OK"},
                {"kind": "button", "label": "OK"},
            ],
            "target_action": {"kind": "button", "label": "OK"},
            "expected_page_id": "data_display",
            "success_criteria": copy.deepcopy(
                success_criteria
                or {
                    "page_id_any": ["data_display", "data_list"],
                    "requires_executed": [{"kind": "button", "label": "Add All"}],
                }
            ),
            "source": "runtime_synthetic",
        }

    @classmethod
    def _build_vehicle_dtc_clear_entry(cls, clear_entry: dict[str, Any]) -> dict[str, Any]:
        return cls._build_data_display_clear_entry(
            clear_entry,
            page_key="vehicle_dtc.clear.execute",
            title="Execute Clear DTCs From Vehicle DTC Information",
            navigation_path=["Vehicle Diagnostics", "Vehicle DTC Information"],
            success_criteria={
                "page_id_any": ["data_display"],
                "all_of": [
                    {"kind": "button", "label": "Refresh"},
                    {"kind": "button", "label": "Back"},
                ],
                "any_of": [
                    {"kind": "button", "label": "Clear DTCs"},
                    {"kind": "button", "label": "Create Report"},
                    {"kind": "button", "label": "Details"},
                ],
                "navigation_path_contains_prefix": ["Vehicle Diagnostics"],
                "requires_executed": [{"kind": "button", "label": "Add All"}],
            },
        )

    def _resolve_data_display_item(self) -> str:
        items = self._controller.wait_for_list() if hasattr(self._controller, "wait_for_list") else []
        for item in items or []:
            if "data display" in str(item).strip().lower():
                return str(item)
        raise RuntimeError("Data Display option not found")

    def _emit_progress_event(self, session: Any, *, page: str, action: str) -> None:
        session.event_queue.put(
            {
                "type": "progress",
                "node": _display_text(page, default="unknown") or "unknown",
                "page": _display_text(page, default="unknown") or "unknown",
                "action": _display_text(action, default="progress") or "progress",
            }
        )

    def _append_navigation_history(
        self,
        history: list[dict[str, Any]],
        *,
        page: str,
        action: str,
        to_page: str | None = None,
        selected_item: str | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "page": page,
            "action": action,
            "success": True,
        }
        if to_page is not None:
            entry["to_page"] = to_page
        if selected_item is not None:
            entry["selected_item"] = selected_item
        history.append(entry)

    def _await_navigation_decision(self, session: Any, *, page: str, items: list[Any]) -> str:
        normalized_items = [_display_text(item) for item in items]
        normalized_items = [item for item in normalized_items if item]
        if not normalized_items:
            raise RuntimeError(f"No selectable items available on page '{page}'")
        decision_id = uuid.uuid4().hex[:12]
        session.status = "awaiting_decision"
        session.pending_decision_id = decision_id
        session.pending_items = list(normalized_items)
        session.event_queue.put(
            {
                "type": "decision_required",
                "decision_id": decision_id,
                "page": page,
                "items": normalized_items,
            }
        )
        while True:
            session.check_cancelled()
            try:
                payload = session.decision_queue.get(timeout=0.25)
            except Exception:
                time.sleep(0.05)
                continue
            selected_item = _strip_optional_text((payload or {}).get("selected_item"))
            if not selected_item:
                continue
            session.status = "running"
            session.pending_decision_id = None
            session.pending_items = []
            return selected_item

    def _complete_navigation_session(
        self,
        session: Any,
        *,
        current_page: str,
        navigation_history: list[dict[str, Any]],
        selections: dict[str, str],
    ) -> dict[str, Any]:
        final = {
            "goal": session.goal,
            "current_page": current_page,
            "navigation_history": navigation_history,
            "selections": dict(selections),
            "error": None,
        }
        session.event_queue.put(
            {
                "type": "done",
                "final_page": current_page,
                "steps": len(navigation_history),
                "selections": dict(selections),
                "error": None,
            }
        )
        return final

    def _visible_navigation_items(self) -> list[str]:
        items: list[Any] = []
        if hasattr(self._controller, "wait_for_list"):
            try:
                items = list(self._controller.wait_for_list() or [])
            except Exception:
                items = []
        if not items and hasattr(self._controller, "get_list_items"):
            try:
                items = list(self._controller.get_list_items(0) or [])
            except Exception:
                items = []
        return [_display_text(item) for item in items if _display_text(item)]

    def _run_vehicle_diagnostics_guided_session(
        self,
        session: Any,
        *,
        entry: dict[str, Any],
    ) -> dict[str, Any]:
        route_result = self.execute_registry_route(
            entry=entry,
            max_iterations=self._route_max_iterations,
            max_backtracks=self._route_max_backtracks,
            cancel_checker=session.check_cancelled,
        )
        history = list(route_result.get("executed_actions") or [])
        current_page = str(route_result.get("final_page") or self.detect_current_page())
        items = self._visible_navigation_items()
        selected_item = self._await_navigation_decision(
            session,
            page="vehicle_diagnostics_menu",
            items=items,
        )
        self._emit_progress_event(
            session,
            page="vehicle_diagnostics_menu",
            action="select_vehicle_diagnostics_item",
        )
        result = self._controller.select_list_item(selected_item)
        if not getattr(result, "success", False):
            error = getattr(result, "error", None) or f"Failed to select vehicle diagnostics item {selected_item!r}"
            raise RuntimeError(str(error))
        current_page = self.detect_current_page()
        self._append_navigation_history(
            history,
            page="vehicle_diagnostics_menu",
            action="select_vehicle_diagnostics_item",
            to_page=current_page,
            selected_item=selected_item,
        )
        return self._complete_navigation_session(
            session,
            current_page=current_page,
            navigation_history=history,
            selections={"selected_item": selected_item},
        )

    def _run_navigation_session_thread(self, runtime: Any, session: Any) -> None:
        from diagnostic_platform.runtime.navigation_runtime import NavSessionStatus

        try:
            final = self._run_navigation_session(session)
            session.final_state = final
            if session.status not in (NavSessionStatus.ABORTED,):
                session.status = NavSessionStatus.COMPLETED
                self._set_runtime_status(
                    status="completed",
                    last_operation="navigation_session",
                    active_navigation_session_id=session.session_id,
                    terminal_reason="navigation_completed",
                    last_result=copy.deepcopy(final),
                )
        except OperationCancelledError:
            session.status = NavSessionStatus.ABORTED
            session.error = "Aborted by user"
            self._set_runtime_status(
                status="aborted",
                last_operation="navigation_session",
                active_navigation_session_id=session.session_id,
                terminal_reason="navigation_cancelled",
            )
        except Exception as exc:
            session.status = NavSessionStatus.FAILED
            session.error = str(exc)
            self._set_runtime_status(
                status="failed",
                last_operation="navigation_session",
                active_navigation_session_id=session.session_id,
                terminal_reason="navigation_failed",
                last_error=str(exc),
            )
            try:
                session.event_queue.put({"type": "error", "error": str(exc)})
            except Exception:
                pass
        finally:
            if callable(session.cleanup_callback):
                session.cleanup_callback(session.session_id)
            else:
                runtime.schedule_navigation_session_cleanup(session.session_id)

    def _run_navigation_session(self, session: Any) -> dict[str, Any]:
        history: list[dict[str, Any]] = []
        selections: dict[str, str] = {}
        normalized_goal = session.goal.strip().lower()

        if normalized_goal in {
            "vehicle diagnostics",
            "vehicle diagnostics root",
            "diagnostics.vehicle_diagnostics",
        }:
            return self._run_vehicle_diagnostics_guided_session(
                session,
                entry=self._require_entry("diagnostics.vehicle_diagnostics"),
            )

        if normalized_goal not in {
            "navigate to data display",
            "go to data display",
            "data display",
        }:
            route_result = self.execute_registry_route(
                entry=self._require_entry(session.goal),
                max_iterations=self._route_max_iterations,
                max_backtracks=self._route_max_backtracks,
                cancel_checker=session.check_cancelled,
            )
            final_page = str(route_result.get("final_page") or self.detect_current_page())
            history.extend(list(route_result.get("executed_actions") or []))
            return self._complete_navigation_session(
                session,
                current_page=final_page,
                navigation_history=history,
                selections=selections,
            )

        started = self.ensure_started(cancel_checker=session.check_cancelled)
        current_page = self.detect_current_page()
        if current_page != "module_list":
            raise RuntimeError(f"Navigation start did not reach module_list (got {current_page})")

        modules = list(started.get("modules") or [])
        selected_module = self._await_navigation_decision(
            session,
            page="module_list",
            items=modules,
        )
        self._emit_progress_event(session, page="module_list", action="select_module")
        module_result = self.select_module(selected_module)
        current_page = self.detect_current_page()
        self._append_navigation_history(
            history,
            page="module_list",
            action="select_module",
            to_page=current_page,
            selected_item=selected_module,
        )
        selections["module"] = selected_module

        categories = list(module_result.get("data_categories") or [])
        selected_category = self._await_navigation_decision(
            session,
            page="data_list",
            items=categories,
        )
        self._emit_progress_event(session, page="data_list", action="select_data_category")
        self.select_data_category(selected_category)
        current_page = self.detect_current_page()
        self._append_navigation_history(
            history,
            page="data_list",
            action="select_data_category",
            to_page=current_page,
            selected_item=selected_category,
        )
        selections["data_category"] = selected_category

        return self._complete_navigation_session(
            session,
            current_page=current_page,
            navigation_history=history,
            selections=selections,
        )

    def _read_dtc_count(self, *, default: int) -> int:
        if callable(self._read_dtc_count_callback):
            try:
                return int(self._read_dtc_count_callback() or 0)
            except Exception:
                pass
        if not callable(self._read_dtcs_snapshot):
            return default
        try:
            snapshot = self._read_dtcs_snapshot() or {}
            return int(snapshot.get("dtc_count") or 0)
        except Exception:
            return default

    def _read_post_clear_dtc_count(self, *, final_page: str, default: int) -> int:
        if final_page != "data_display":
            return default
        return self._read_dtc_count(default=default)

    def _set_runtime_status(self, **updates: Any) -> None:
        self._status_recorder.update_defaults(
            default_device_name=self._default_device_name,
            loading_timeout_sec=self._loading_timeout_sec,
            max_loading_restarts=self._max_loading_restarts,
        )
        self._status_recorder.update(**updates)

    @staticmethod
    def _recovery_action_counts(recovery_actions: list[dict[str, Any]]) -> dict[str, int]:
        return GDS2NavigationRuntimeStatusRecorder.recovery_action_counts(recovery_actions)

    def _build_route_status(
        self,
        *,
        entry: dict[str, Any],
        route_result: dict[str, Any],
        terminal_reason: str,
        merged_recovery_actions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return self._status_recorder.build_route_status(
            entry=entry,
            route_result=route_result,
            terminal_reason=terminal_reason,
            merged_recovery_actions=merged_recovery_actions,
        )

    @staticmethod
    def _derive_recovery_method(recovery_actions: list[dict[str, Any]]) -> str:
        for action in recovery_actions:
            if str(action.get("kind") or "") == "runtime" and str(action.get("label") or "") == "restart_gds2":
                return "restart_gds2"
        for action in recovery_actions:
            reason = str(action.get("reason") or "")
            if "j2534_disconnect soft recovery" in reason:
                return "in_place"
            if "j2534_disconnect fallback backtrack" in reason:
                return "backtrack"
        if any(str(action.get("kind") or "") == "navigation_path" for action in recovery_actions):
            return "navigation_path"
        if any(str(action.get("kind") or "") == "device" for action in recovery_actions):
            return "device_reconnect"
        if recovery_actions:
            return "route_reentry"
        return "unknown"

    @staticmethod
    def _recovery_message_for(*, mode: str, recovery_method: str) -> str:
        if mode == "ai_collect" and recovery_method in {"backtrack", "restart_gds2"}:
            return "Recovered Data Display after reconnect; restarting AI collection window."
        if recovery_method in {"in_place", "backtrack"}:
            if mode == "ai_collect":
                return "Recovered temporary J2534 disconnect and returned to Data Display."
            return "Recovered Data Display after J2534 disconnect."
        if recovery_method == "device_reconnect":
            return "Recovered Data Display after device re-selection."
        if recovery_method == "navigation_path":
            return "Recovered Data Display after navigation path correction."
        if recovery_method == "route_reentry":
            return "Recovered Data Display after page drift."
        return "Recovered Data Display."

    def execute_registry_route(
        self,
        *,
        entry: dict[str, Any],
        max_iterations: int,
        max_backtracks: int,
        cancel_checker: Callable[[], None] | None = None,
        recovery_data_category: str | None = None,
        recovery_sub_category: str | None = None,
    ) -> dict[str, Any]:
        return self._route_executor().execute(
            entry=entry,
            max_iterations=max_iterations,
            max_backtracks=max_backtracks,
            cancel_checker=cancel_checker,
            recovery_data_category=recovery_data_category,
            recovery_sub_category=recovery_sub_category,
        )


def bootstrap_engine_data_baseline(
    *,
    navigator: AgentNavigator,
    max_attempts: int,
) -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    graph = load_or_rebuild_graph()

    def _snapshot() -> tuple[NavigationController, GDS2RouteNavigator, dict[str, Any]]:
        controller = NavigationController(nav=navigator)
        route_navigator = GDS2RouteNavigator(controller=controller, graph=graph)
        return controller, route_navigator, route_navigator.capture_settled_snapshot()

    for attempt in range(1, max_attempts + 1):
        try:
            harness = GDS2ExplorerHarness(navigator=navigator)
            result = harness.run_mainline(
                device_name=DEFAULT_VCI_DEVICE_NAME,
                module_name="Engine Control Module",
                data_category="Engine Data",
                submenu_item="Data Display",
                max_steps=32,
            )
            attempts.append({"attempt": attempt, "success": True, "result": result.get("final_page")})
            return attempts
        except Exception as exc:
            controller, _route_navigator, snapshot = _snapshot()
            labels = [str(action.get("label") or "") for action in snapshot.get("observed_actions") or []]
            attempt_record: dict[str, Any] = {
                "attempt": attempt,
                "success": False,
                "error": str(exc),
                "page": snapshot.get("effective_page_id"),
                "labels": labels,
                "navigation_path": snapshot.get("navigation_path") or [],
            }
            if "OK" in labels:
                click_result = controller.click_button("OK").to_dict()
                attempt_record["recovery"] = {"kind": "button", "label": "OK", "result": click_result}
                attempts.append(attempt_record)
                continue
            attempts.append(attempt_record)
            raise

    raise RuntimeError(f"Failed to bootstrap Engine Data baseline after {max_attempts} attempts")


def run_probe(
    *,
    entry_key: str,
    registry_path: str | Path,
    graph_path: str | Path,
    rebuild_registry: bool,
    bootstrap: bool,
    max_iterations: int,
    max_backtracks: int,
    loading_timeout_sec: float | None,
    max_loading_restarts: int | None,
) -> dict[str, Any]:
    started_at = datetime.now().isoformat(timespec="seconds")
    registry = Path(registry_path)
    if rebuild_registry or not registry.exists():
        rebuild_registry_database(output_path=registry)

    with connect_registry(registry) as connection:
        entry = lookup_entry(connection, entry_key)
        loading_policy = lookup_recovery_policy(connection, "loading.restart_after_timeout")
        page_states = list_page_states(connection)
        recovery_policies = list_recovery_policies(connection)
    if entry is None:
        raise RuntimeError(f"Navigation registry entry not found: {entry_key}")

    target_action = entry.get("target_action") or {}
    target_label = str(target_action.get("label") or "").strip()
    target_kind = str(target_action.get("kind") or "").strip()
    if not target_label or not target_kind:
        raise RuntimeError(f"Registry entry '{entry['page_key']}' has no target_action")

    if loading_policy is None:
        loading_policy = policy_by_key(recovery_policies, "loading.restart_after_timeout")
    loading_params = (loading_policy or {}).get("params") or {}
    effective_loading_timeout = (
        float(loading_timeout_sec)
        if loading_timeout_sec is not None
        else float(loading_params.get("timeout_sec") or (loading_policy or {}).get("timeout_sec") or 30.0)
    )
    effective_max_loading_restarts = (
        int(max_loading_restarts)
        if max_loading_restarts is not None
        else int(loading_params.get("max_restarts") or (loading_policy or {}).get("max_attempts") or 1)
    )

    agent_navigator = AgentNavigator(timeout_sec=15.0)
    bootstrap_actions: list[dict[str, Any]] = []
    if bootstrap:
        bootstrap_actions = bootstrap_engine_data_baseline(
            navigator=agent_navigator,
            max_attempts=3,
        )

    controller = NavigationController(nav=agent_navigator)
    graph = load_or_rebuild_graph(graph_path)
    route_navigator = GDS2RouteNavigator(controller=controller, graph=graph)
    runtime = RegistryNavigationRuntime(
        controller=controller,
        route_navigator=route_navigator,
        page_states=page_states,
        recovery_policies=recovery_policies,
        loading_timeout_sec=effective_loading_timeout,
        max_loading_restarts=effective_max_loading_restarts,
    )
    start_snapshot = runtime.capture_runtime_snapshot()
    route_result = runtime.execute_registry_route(
        entry=entry,
        max_iterations=max_iterations,
        max_backtracks=max_backtracks,
    )
    success, success_details = validate_entry_result(entry, route_result)
    return {
        "started_at": started_at,
        "completed_at": datetime.now().isoformat(timespec="seconds"),
        "entry_key": entry_key,
        "entry": entry,
        "target_action": target_action,
        "policies": {
            "loading.restart_after_timeout": loading_policy,
            "effective_loading_timeout_sec": effective_loading_timeout,
            "effective_max_loading_restarts": effective_max_loading_restarts,
            "page_state_count": len(page_states),
            "recovery_policy_count": len(recovery_policies),
        },
        "bootstrap_actions": bootstrap_actions,
        "start_snapshot": start_snapshot,
        "success": success,
        "success_details": success_details,
        "missing_expected_actions": success_details.get("missing_expected_actions")
        or success_details.get("missing_all_of")
        or [],
        "route_result": route_result,
    }
