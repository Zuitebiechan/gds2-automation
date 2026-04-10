"""GDS2-specific deterministic action runtime semantics."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from diagnostic_platform.action_schema import ActionStep, GDS2Action, RiskLevel


@dataclass
class UIState:
    current_page: str
    visible_buttons: list[str] = field(default_factory=list)
    list_items: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    recent_actions: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.current_page:
            raise ValueError("current_page cannot be empty")

    def has_button(self, name: str) -> bool:
        return name in self.visible_buttons

    def has_list_item(self, name: str) -> bool:
        return name in self.list_items

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_page": self.current_page,
            "visible_buttons": self.visible_buttons,
            "list_items": self.list_items,
            "context": self.context,
            "recent_actions": self.recent_actions,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UIState":
        return cls(**data)


class CapabilityRegistry:
    def __init__(self) -> None:
        self._page_capabilities: dict[str, set[GDS2Action]] = {
            "main_menu": {
                GDS2Action.START_DIAGNOSTICS,
                GDS2Action.SELECT_DEVICE,
                GDS2Action.ABORT_SESSION,
            },
            "device_explorer": {
                GDS2Action.SELECT_DEVICE,
                GDS2Action.CONNECT_DEVICE,
                GDS2Action.ABORT_SESSION,
            },
            "vehicle_selection": {
                GDS2Action.CONNECT_DEVICE,
                GDS2Action.GO_BACK,
                GDS2Action.ABORT_SESSION,
            },
            "module_list": {
                GDS2Action.SELECT_MODULE,
                GDS2Action.GO_HOME,
                GDS2Action.GO_BACK,
                GDS2Action.ABORT_SESSION,
            },
            "module_submenu": {
                GDS2Action.SELECT_MODULE,
                GDS2Action.GO_BACK,
                GDS2Action.ABORT_SESSION,
            },
            "data_list": {
                GDS2Action.SELECT_DATA_CATEGORY,
                GDS2Action.GO_HOME,
                GDS2Action.GO_BACK,
                GDS2Action.ABORT_SESSION,
            },
            "sub_data_list": {
                GDS2Action.SELECT_SUB_CATEGORY,
                GDS2Action.GO_BACK,
                GDS2Action.ABORT_SESSION,
            },
            "data_display": {
                GDS2Action.READ_DTCS,
                GDS2Action.START_LIVE_STREAM,
                GDS2Action.STOP_LIVE_STREAM,
                GDS2Action.GO_HOME,
                GDS2Action.GO_BACK,
                GDS2Action.ABORT_SESSION,
            },
        }

    def all_actions(self) -> list[GDS2Action]:
        return list(GDS2Action)

    def is_action_allowed(self, page: str, action: GDS2Action) -> bool:
        allowed = self._page_capabilities.get(page, set())
        return action in allowed

    def get_allowed_actions(self, page: str) -> list[str]:
        allowed = self._page_capabilities.get(page, set())
        return [action.value for action in allowed]

    def register_page_capability(self, page: str, action: GDS2Action) -> None:
        if page not in self._page_capabilities:
            self._page_capabilities[page] = set()
        self._page_capabilities[page].add(action)


class PolicyGuard:
    def __init__(self, registry: CapabilityRegistry | None = None):
        self.registry = registry or CapabilityRegistry()

    def validate_action(self, step: ActionStep, state: UIState) -> tuple[bool, str | None]:
        if not self.registry.is_action_allowed(state.current_page, step.action):
            return False, f"Action {step.action.value} is not allowed on page {state.current_page}"

        if step.action == GDS2Action.SELECT_MODULE:
            if "module_name" not in step.args:
                return False, "Action select_module requires 'module_name' argument"

        if step.action == GDS2Action.SELECT_DATA_CATEGORY:
            if "category_name" not in step.args:
                return False, "Action select_data_category requires 'category_name' argument"

        if step.action == GDS2Action.SELECT_SUB_CATEGORY:
            if "sub_category_name" not in step.args:
                return False, "Action select_sub_category requires 'sub_category_name' argument"

        if step.action in (GDS2Action.SELECT_DEVICE, GDS2Action.CONNECT_DEVICE):
            if "device_name" not in step.args:
                return False, f"Action {step.action.value} requires 'device_name' argument"

        if step.risk_level == RiskLevel.HIGH:
            if not step.metadata.get("human_approved"):
                return False, "High risk action requires 'human_approved' metadata"

        if step.action == GDS2Action.START_LIVE_STREAM:
            interval = step.args.get("interval_ms", 0)
            if interval <= 0:
                return False, "Action start_live_stream requires positive 'interval_ms'"

        return True, None


__all__ = [
    "CapabilityRegistry",
    "PolicyGuard",
    "UIState",
]
