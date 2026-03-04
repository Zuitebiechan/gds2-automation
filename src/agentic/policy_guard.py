from typing import Optional, Tuple
from .contracts.action_schema import ActionStep, GDS2Action, RiskLevel
from .contracts.state_schema import UIState
from .capability_registry import CapabilityRegistry


class PolicyGuard:
    def __init__(self, registry: Optional[CapabilityRegistry] = None):
        self.registry = registry or CapabilityRegistry()

    def validate_action(self, step: ActionStep, state: UIState) -> Tuple[bool, Optional[str]]:
        # 1. Check if action is allowed for the current page
        if not self.registry.is_action_allowed(state.current_page, step.action):
            return False, f"Action {step.action.value} is not allowed on page {state.current_page}"

        # 2. Specific validation for SELECT_MODULE
        if step.action == GDS2Action.SELECT_MODULE:
            if "module_name" not in step.args:
                return False, "Action select_module requires 'module_name' argument"

        # 3. High risk actions require human approval
        if step.risk_level == RiskLevel.HIGH:
            if not step.metadata.get("human_approved"):
                return False, "High risk action requires 'human_approved' metadata"

        # 4. Live stream interval validation
        if step.action == GDS2Action.START_LIVE_STREAM:
            interval = step.args.get("interval_ms", 0)
            if interval <= 0:
                return False, "Action start_live_stream requires positive 'interval_ms'"

        return True, None
