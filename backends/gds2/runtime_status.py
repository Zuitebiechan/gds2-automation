from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any


@dataclass
class GDS2NavigationRuntimeStatusRecorder:
    default_device_name: str
    loading_timeout_sec: float
    max_loading_restarts: int
    runtime_source: str = "registry_runtime"
    _status: dict[str, Any] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._status = {
            "runtime_source": self.runtime_source,
            "status": "idle",
            "default_device_name": self.default_device_name,
            "loading_timeout_sec": self.loading_timeout_sec,
            "max_loading_restarts": self.max_loading_restarts,
        }

    def update_defaults(
        self,
        *,
        default_device_name: str,
        loading_timeout_sec: float,
        max_loading_restarts: int,
    ) -> None:
        self.default_device_name = default_device_name
        self.loading_timeout_sec = loading_timeout_sec
        self.max_loading_restarts = max_loading_restarts

    def get_status(self) -> dict[str, Any]:
        return copy.deepcopy(self._status)

    def update(self, **updates: Any) -> None:
        self._status.update(copy.deepcopy(updates))
        self._status["runtime_source"] = self.runtime_source
        self._status["default_device_name"] = self.default_device_name
        self._status["loading_timeout_sec"] = self.loading_timeout_sec
        self._status["max_loading_restarts"] = self.max_loading_restarts

    @staticmethod
    def recovery_action_counts(recovery_actions: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for action in recovery_actions:
            key = f"{str(action.get('kind') or '').strip()}:{str(action.get('label') or '').strip()}"
            counts[key] = counts.get(key, 0) + 1
        return counts

    def build_route_status(
        self,
        *,
        entry: dict[str, Any],
        route_result: dict[str, Any],
        terminal_reason: str,
        merged_recovery_actions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        recovery_actions = list(merged_recovery_actions or route_result.get("recovery_actions") or [])
        return {
            "route_target_page_key": str(entry.get("page_key") or ""),
            "route_target_category": str(entry.get("category") or ""),
            "canonical_path": list(entry.get("canonical_path") or []),
            "matched_start_node_id": route_result.get("matched_start_node_id"),
            "final_page_id": route_result.get("final_page"),
            "planned_path": copy.deepcopy(route_result.get("planned_path") or []),
            "executed_actions": copy.deepcopy(route_result.get("executed_actions") or []),
            "recovery_actions": copy.deepcopy(recovery_actions),
            "recovery_action_counts": self.recovery_action_counts(recovery_actions),
            "matched_state_trace": copy.deepcopy(route_result.get("state_trace") or []),
            "match_diagnostics": copy.deepcopy(route_result.get("match_diagnostics") or []),
            "terminal_reason": terminal_reason,
        }
