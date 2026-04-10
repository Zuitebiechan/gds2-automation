"""GDS2 package exports."""

from __future__ import annotations

from typing import Any

__all__ = [
    "BranchDecision",
    "BranchDecisionRequiredError",
    "CapabilityRegistry",
    "ConstrainedPlanner",
    "DecisionDomain",
    "GDS2ActionAdapter",
    "GDS2DiagnosticBackend",
    "PolicyGuard",
    "RankedOption",
    "UIState",
]


def __getattr__(name: str) -> Any:
    if name in {"CapabilityRegistry", "PolicyGuard", "UIState"}:
        from .action_runtime import CapabilityRegistry, PolicyGuard, UIState

        return {
            "CapabilityRegistry": CapabilityRegistry,
            "PolicyGuard": PolicyGuard,
            "UIState": UIState,
        }[name]

    if name == "GDS2ActionAdapter":
        from .action_adapter import GDS2ActionAdapter

        return GDS2ActionAdapter

    if name == "GDS2DiagnosticBackend":
        from .backend import GDS2DiagnosticBackend

        return GDS2DiagnosticBackend

    if name in {
        "BranchDecision",
        "BranchDecisionRequiredError",
        "ConstrainedPlanner",
        "DecisionDomain",
        "RankedOption",
    }:
        from .planner import (
            BranchDecision,
            BranchDecisionRequiredError,
            ConstrainedPlanner,
            DecisionDomain,
            RankedOption,
        )

        return {
            "BranchDecision": BranchDecision,
            "BranchDecisionRequiredError": BranchDecisionRequiredError,
            "ConstrainedPlanner": ConstrainedPlanner,
            "DecisionDomain": DecisionDomain,
            "RankedOption": RankedOption,
        }[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
