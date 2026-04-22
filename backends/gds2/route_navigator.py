from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from backends.gds2.route_graph import match_snapshot_to_node_details, node_has_action, plan_to_action
from backends.gds2.explorer_harness import extract_navigation_path, load_latest_snapshot


class GDS2RouteNavigator:
    def __init__(
        self,
        *,
        controller: Any,
        graph: dict[str, Any],
        latest_json_reader: Any | None = None,
    ) -> None:
        self._controller = controller
        self._graph = graph
        self._latest_json_reader = latest_json_reader or (
            lambda: load_latest_snapshot(Path.home() / "gds2-data" / "latest.json")
        )

    @property
    def graph(self) -> dict[str, Any]:
        return self._graph

    def capture_snapshot(self) -> dict[str, Any]:
        return self._capture_snapshot()

    def capture_settled_snapshot(
        self,
        timeout_sec: float = 8.0,
        poll_interval: float = 0.5,
    ) -> dict[str, Any]:
        return self._capture_settled_snapshot(timeout_sec=timeout_sec, poll_interval=poll_interval)

    def match_snapshot(self, snapshot: dict[str, Any]) -> str | None:
        return self._match_snapshot(snapshot)

    def snapshot_has_action(self, snapshot: dict[str, Any], label: str, *, kind: str | None) -> bool:
        return self._snapshot_has_action(snapshot, label, kind=kind)

    def execute_action(self, action: dict[str, Any]) -> None:
        self._execute_action(action)

    def resolve_bridge_action(self, snapshot: dict[str, Any]) -> dict[str, str] | None:
        return self._resolve_bridge_action(snapshot)

    def navigate_to_action(
        self,
        label: str,
        *,
        kind: str | None = None,
        max_backtracks: int = 8,
        max_iterations: int = 24,
    ) -> dict[str, Any]:
        recovery_actions: list[dict[str, str]] = []
        executed_actions: list[dict[str, str]] = []
        matched_start_node_id: str | None = None
        last_path: list[dict[str, Any]] | None = None

        for _ in range(max(1, max_iterations)):
            direct_snapshot = self._capture_settled_snapshot()
            if self._snapshot_has_action(direct_snapshot, label, kind=kind):
                resolved_kind = self._resolve_action_kind(direct_snapshot, label, kind=kind)
                self._execute_action({"kind": resolved_kind, "label": label})
                executed_actions.append({"kind": resolved_kind, "label": label})
                final_snapshot = self._capture_settled_snapshot()
                return {
                    "matched_start_node_id": matched_start_node_id,
                    "recovery_actions": recovery_actions,
                    "planned_path": [edge["action"] for edge in (last_path or [])],
                    "executed_actions": executed_actions,
                    "final_page": final_snapshot["effective_page_id"],
                    "final_snapshot": final_snapshot,
                }

            bridge_action = self._resolve_bridge_action(direct_snapshot)
            if bridge_action is not None:
                self._execute_action(bridge_action)
                executed_actions.append({"kind": str(bridge_action["kind"]), "label": str(bridge_action["label"])})
                continue

            if self._should_backtrack_to_anchor(direct_snapshot):
                result = self._controller.go_back()
                if not getattr(result, "success", False):
                    raise RuntimeError("Failed to backtrack from non-anchor page")
                recovery_actions.append({"kind": "button", "label": "Back"})
                continue

            snapshot, matched_node_id = self._recover_to_known_node(
                label,
                kind=kind,
                max_backtracks=max_backtracks,
                recovery_actions=recovery_actions,
            )
            if not matched_node_id:
                raise RuntimeError(f"Could not recover to a known node for target '{label}'")
            if matched_start_node_id is None:
                matched_start_node_id = matched_node_id

            path = plan_to_action(self._graph, matched_node_id, label, kind=kind)
            if path is None or not path:
                raise RuntimeError(f"No route to action '{label}' from node '{matched_node_id}'")

            last_path = path
            action = path[0]["action"]
            self._execute_action(action)
            executed_actions.append({"kind": str(action["kind"]), "label": str(action["label"])})

        raise RuntimeError(f"Route to action '{label}' did not converge within {max_iterations} steps")

    @staticmethod
    def _resolve_bridge_action(snapshot: dict[str, Any]) -> dict[str, str] | None:
        page_id = str(snapshot.get("effective_page_id") or "")
        if page_id == "main_menu" and GDS2RouteNavigator._snapshot_has_action(snapshot, "Diagnostics", kind="button"):
            return {"kind": "button", "label": "Diagnostics"}
        if page_id == "vehicle_selection" and GDS2RouteNavigator._snapshot_has_action(snapshot, "Enter", kind="button"):
            return {"kind": "button", "label": "Enter"}
        return None

    @staticmethod
    def _should_backtrack_to_anchor(snapshot: dict[str, Any]) -> bool:
        page_id = str(snapshot.get("effective_page_id") or "")
        buttons = {
            str(action.get("label") or "")
            for action in snapshot.get("observed_actions") or []
            if str(action.get("kind") or "") == "button"
        }

        if "Back" not in buttons:
            return False

        anchor_like_pages = {"main_menu", "vehicle_selection", "diagnostics_menu", "module_list", "module_submenu"}
        if page_id in anchor_like_pages:
            return False

        if page_id == "data_list":
            if {"Add Bookmark", "Reset", "Learn", "Activate", "On", "Off", "Open", "Close", "Start", "Stop", "Continue", "Show DTC Status"} & buttons:
                return True
            list_items = [str(item) for item in snapshot.get("list_items") or [] if str(item).strip()]
            table_headers = {"authored", "Display Name", "ECU", "Units", "Custom Order"}
            if set(list_items) == table_headers:
                return True
            return False

        return True

    def _recover_to_known_node(
        self,
        label: str,
        *,
        kind: str | None,
        max_backtracks: int,
        recovery_actions: list[dict[str, str]],
    ) -> tuple[dict[str, Any], str | None]:
        snapshot = self._capture_settled_snapshot()
        matched_node_id = self._match_snapshot(snapshot)
        if matched_node_id and self._match_is_actionable(snapshot, node_id=matched_node_id, label=label, kind=kind):
            return snapshot, matched_node_id

        attempted_navigation_jumps: set[tuple[Any, str]] = set()
        snapshot, matched_node_id = self._attempt_navigation_path_jump(
            snapshot,
            label=label,
            kind=kind,
            recovery_actions=recovery_actions,
            attempted_navigation_jumps=attempted_navigation_jumps,
        )
        if matched_node_id and self._match_is_actionable(snapshot, node_id=matched_node_id, label=label, kind=kind):
            return snapshot, matched_node_id

        for _ in range(max(0, max_backtracks)):
            result = self._controller.go_back()
            if not getattr(result, "success", False):
                break
            recovery_actions.append({"kind": "button", "label": "Back"})
            snapshot = self._capture_settled_snapshot()
            matched_node_id = self._match_snapshot(snapshot)
            if matched_node_id and self._match_is_actionable(snapshot, node_id=matched_node_id, label=label, kind=kind):
                return snapshot, matched_node_id
            snapshot, matched_node_id = self._attempt_navigation_path_jump(
                snapshot,
                label=label,
                kind=kind,
                recovery_actions=recovery_actions,
                attempted_navigation_jumps=attempted_navigation_jumps,
            )
            if matched_node_id and self._match_is_actionable(snapshot, node_id=matched_node_id, label=label, kind=kind):
                return snapshot, matched_node_id

        result = self._controller.go_home()
        if getattr(result, "success", False):
            recovery_actions.append({"kind": "button", "label": "Home"})
            snapshot = self._capture_settled_snapshot()
            matched_node_id = self._match_snapshot(snapshot)
            if matched_node_id and self._match_is_actionable(snapshot, node_id=matched_node_id, label=label, kind=kind):
                return snapshot, matched_node_id

        return snapshot, None

    def _match_snapshot(self, snapshot: dict[str, Any]) -> str | None:
        details = match_snapshot_to_node_details(self._graph, snapshot)
        if details is None:
            return None
        return str(details["node_id"])

    def _attempt_navigation_path_jump(
        self,
        snapshot: dict[str, Any],
        *,
        label: str,
        kind: str | None,
        recovery_actions: list[dict[str, str]],
        attempted_navigation_jumps: set[tuple[Any, str]],
    ) -> tuple[dict[str, Any], str | None]:
        matched_node_id = self._match_snapshot(snapshot)
        jump_label = self._select_navigation_path_jump_label(snapshot, label=label, kind=kind)
        if not jump_label or not hasattr(self._controller, "click_navigation_path_item"):
            return snapshot, matched_node_id

        jump_key = (self._snapshot_signature(snapshot), jump_label)
        if jump_key in attempted_navigation_jumps:
            return snapshot, matched_node_id
        attempted_navigation_jumps.add(jump_key)

        try:
            result = self._controller.click_navigation_path_item(jump_label)
        except Exception:
            return snapshot, matched_node_id
        if not getattr(result, "success", False):
            return snapshot, matched_node_id

        jumped_snapshot = self._capture_settled_snapshot()
        if self._snapshot_signature(jumped_snapshot) == self._snapshot_signature(snapshot):
            return jumped_snapshot, self._match_snapshot(jumped_snapshot)

        recovery_actions.append({"kind": "navigation_path", "label": jump_label})
        return jumped_snapshot, self._match_snapshot(jumped_snapshot)

    def _select_navigation_path_jump_label(
        self,
        snapshot: dict[str, Any],
        *,
        label: str,
        kind: str | None,
    ) -> str | None:
        current_path = [
            str(item).strip()
            for item in snapshot.get("navigation_path") or []
            if str(item).strip()
        ]
        if len(current_path) < 2:
            return None

        best_jump_label: str | None = None
        best_prefix_depth = 0
        for target_path in self._candidate_target_navigation_paths(label, kind=kind):
            prefix_depth = self._common_path_prefix_length(current_path, target_path)
            if prefix_depth <= 0 or prefix_depth >= len(current_path):
                continue
            if prefix_depth > best_prefix_depth:
                best_prefix_depth = prefix_depth
                best_jump_label = current_path[prefix_depth - 1]
        return best_jump_label

    def _candidate_target_navigation_paths(self, label: str, *, kind: str | None) -> list[list[str]]:
        paths: list[list[str]] = []
        seen: set[tuple[str, ...]] = set()
        for node in (self._graph.get("nodes") or {}).values():
            if not node_has_action(node, label, kind=kind):
                continue
            path = tuple(
                str(item).strip()
                for item in node.get("navigation_path") or []
                if str(item).strip()
            )
            if not path or path in seen:
                continue
            seen.add(path)
            paths.append(list(path))
        return paths

    @staticmethod
    def _common_path_prefix_length(left: list[str], right: list[str]) -> int:
        prefix_depth = 0
        for left_item, right_item in zip(left, right):
            if left_item != right_item:
                break
            prefix_depth += 1
        return prefix_depth

    @staticmethod
    def _snapshot_signature(snapshot: dict[str, Any]) -> tuple[Any, ...]:
        observed_actions = tuple(
            (
                str(action.get("kind") or ""),
                str(action.get("label") or ""),
            )
            for action in snapshot.get("observed_actions") or []
            if str(action.get("label") or "").strip()
        )
        list_items = tuple(str(item).strip() for item in snapshot.get("list_items") or [] if str(item).strip())
        navigation_path = tuple(
            str(item).strip()
            for item in snapshot.get("navigation_path") or []
            if str(item).strip()
        )
        return (
            str(snapshot.get("raw_page_id") or ""),
            str(snapshot.get("effective_page_id") or ""),
            observed_actions,
            list_items,
            navigation_path,
        )

    def _match_is_actionable(
        self,
        snapshot: dict[str, Any],
        node_id: str,
        *,
        label: str,
        kind: str | None,
    ) -> bool:
        if self._snapshot_has_action(snapshot, label, kind=kind):
            return True
        path = plan_to_action(self._graph, node_id, label, kind=kind)
        if not path:
            return False
        first_action = path[0]["action"]
        return self._snapshot_has_action(
            snapshot,
            str(first_action.get("label") or ""),
            kind=str(first_action.get("kind") or "") or None,
        )

    @staticmethod
    def _snapshot_has_action(snapshot: dict[str, Any], label: str, *, kind: str | None) -> bool:
        target = str(label or "").strip()
        for action in snapshot.get("observed_actions") or []:
            if kind is not None and str(action.get("kind") or "") != kind:
                continue
            current = str(action.get("label") or "").strip()
            if current == target:
                return True
            if kind == "list_item" and target and current and (target in current or current in target):
                return True
        return False

    def _capture_snapshot(self) -> dict[str, Any]:
        raw_snapshot = self._controller.get_snapshot()
        page = str(raw_snapshot.get("page") or "")
        buttons = [str(item) for item in raw_snapshot.get("buttons") or [] if str(item).strip()]
        list_items = [str(item) for item in raw_snapshot.get("lists") or [] if str(item).strip()]
        observed_actions = [
            {"kind": "button", "label": button}
            for button in buttons
        ] + [
            {"kind": "list_item", "label": item, "list_index": 0}
            for item in list_items
        ]
        effective_page = self._classify_effective_page(page, buttons, list_items)
        return {
            "page": {"page_id": page},
            "raw_page_id": page,
            "effective_page_id": effective_page,
            "observed_actions": observed_actions,
            "list_items": list_items,
            "navigation_path": self._read_navigation_path(raw_page=page),
        }

    def _read_navigation_path(self, *, raw_page: str = "") -> list[str]:
        controller_reader = getattr(self._controller, "get_navigation_path", None)
        if callable(controller_reader):
            try:
                controller_path = [
                    str(item).strip()
                    for item in controller_reader() or []
                    if str(item).strip()
                ]
            except Exception:
                controller_path = []
            if controller_path:
                return controller_path
            if raw_page not in {"main_menu", "vehicle_selection", "unknown", "loading"}:
                return []

        latest_json = self._latest_json_reader() or {}
        return extract_navigation_path(latest_json)

    def _capture_settled_snapshot(self, timeout_sec: float = 8.0, poll_interval: float = 0.5) -> dict[str, Any]:
        deadline = time.time() + max(0.0, timeout_sec)
        last_snapshot = self._capture_snapshot()
        while time.time() <= deadline:
            if not self._snapshot_needs_settle(last_snapshot):
                return last_snapshot
            time.sleep(poll_interval)
            last_snapshot = self._capture_snapshot()
        return last_snapshot

    @staticmethod
    def _snapshot_needs_settle(snapshot: dict[str, Any]) -> bool:
        page_id = str(snapshot.get("effective_page_id") or "")
        if page_id in {"loading", "unknown"}:
            return True

        buttons = {
            str(action.get("label") or "")
            for action in snapshot.get("observed_actions") or []
            if str(action.get("kind") or "") == "button"
        }
        list_items = [str(item) for item in snapshot.get("list_items") or [] if str(item).strip()]
        if page_id in {"diagnostics_menu", "module_list", "module_submenu", "data_list"}:
            if {"Enter", "Home", "Vehicle Menu", "Back"} & buttons and not list_items:
                return True
        return False

    @staticmethod
    def _classify_effective_page(page: str, buttons: list[str], list_items: list[str]) -> str:
        page = str(page or "").strip()
        button_texts = set(buttons)
        if page in {"loading", "unknown", "vehicle_selection"} and any("Module Diagnostics" in item for item in list_items):
            return "diagnostics_menu"
        if page in {"loading", "unknown", "vehicle_selection", "diagnostics_menu"} and any("[" in item and "]" in item for item in list_items):
            return "module_list"
        if page in {"loading", "unknown", "vehicle_selection"} and "Back" in button_texts:
            module_submenu_markers = {
                "Data Display",
                "Diagnostic Trouble Codes (DTC)",
                "Control Functions",
                "Configuration/Reset Functions",
            }
            if len(module_submenu_markers & set(list_items)) >= 2:
                return "module_submenu"
            if list_items:
                return "data_list"

        if page != "loading":
            return page

        vehicle_markers = {
            "Select Device",
            "Disconnect",
            "Clear Vehicle Selection",
            "Read VIN",
            "Copy VIN",
        }
        deep_page_markers = {"Home", "Vehicle Menu"}
        if button_texts & vehicle_markers:
            return "vehicle_selection"
        if button_texts & deep_page_markers and not list_items:
            return "loading"
        return page

    @staticmethod
    def _resolve_action_kind(snapshot: dict[str, Any], label: str, *, kind: str | None) -> str:
        if kind is not None:
            return kind
        matching_kinds = {
            str(action.get("kind") or "")
            for action in snapshot.get("observed_actions") or []
            if str(action.get("label") or "") == label
        }
        if len(matching_kinds) == 1:
            return next(iter(matching_kinds))
        if not matching_kinds:
            raise RuntimeError(f"Action '{label}' is not available on the current page")
        raise RuntimeError(f"Action '{label}' is ambiguous on the current page: {sorted(matching_kinds)}")

    def _execute_action(self, action: dict[str, Any]) -> None:
        kind = str(action.get("kind") or "")
        label = str(action.get("label") or "")
        if kind == "button":
            if label == "Enter" and hasattr(self._controller, "click_enter"):
                result = self._controller.click_enter()
            else:
                result = self._controller.click_button(label)
        elif kind == "list_item":
            result = self._controller.select_list_item(label)
        else:
            raise RuntimeError(f"Unsupported route action kind '{kind}'")

        if not getattr(result, "success", False):
            error = getattr(result, "error", None) or f"Failed to execute action {kind}:{label}"
            raise RuntimeError(str(error))
