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
    "GDS2RouteNavigator",
    "find_path",
    "load_graph",
    "match_snapshot_to_node",
    "merge_graph_files",
    "merge_graphs",
    "connect_registry",
    "list_page_states",
    "list_recovery_policies",
    "lookup_page_state",
    "lookup_recovery_policy",
    "plan_to_action",
    "plan_to_page",
    "PolicyGuard",
    "RankedOption",
    "rebuild_registry_database",
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

    if name == "GDS2RouteNavigator":
        from .route_navigator import GDS2RouteNavigator

        return GDS2RouteNavigator

    if name in {
        "connect_registry",
        "list_page_states",
        "list_recovery_policies",
        "lookup_page_state",
        "lookup_recovery_policy",
        "rebuild_registry_database",
    }:
        from .navigation_registry import (
            connect_registry,
            list_page_states,
            list_recovery_policies,
            lookup_page_state,
            lookup_recovery_policy,
            rebuild_registry_database,
        )

        return {
            "connect_registry": connect_registry,
            "list_page_states": list_page_states,
            "list_recovery_policies": list_recovery_policies,
            "lookup_page_state": lookup_page_state,
            "lookup_recovery_policy": lookup_recovery_policy,
            "rebuild_registry_database": rebuild_registry_database,
        }[name]

    if name in {
        "find_path",
        "load_graph",
        "match_snapshot_to_node",
        "merge_graph_files",
        "merge_graphs",
        "plan_to_action",
        "plan_to_page",
    }:
        from .route_graph import (
            find_path,
            load_graph,
            match_snapshot_to_node,
            merge_graph_files,
            merge_graphs,
            plan_to_action,
            plan_to_page,
        )

        return {
            "find_path": find_path,
            "load_graph": load_graph,
            "match_snapshot_to_node": match_snapshot_to_node,
            "merge_graph_files": merge_graph_files,
            "merge_graphs": merge_graphs,
            "plan_to_action": plan_to_action,
            "plan_to_page": plan_to_page,
        }[name]

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
