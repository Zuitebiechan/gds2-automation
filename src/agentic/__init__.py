"""Lightweight package exports for the agentic runtime."""

from importlib import import_module
from typing import Any

from .adapters import GDS2ActionAdapter
from .capability_registry import CapabilityRegistry
from .contracts import ActionStep, GDS2Action, RetryPolicy, RiskLevel, UIState
from .executor import DeterministicExecutor, ExecutionResult, ExecutionStatus
from .planner import (
    BranchDecision,
    BranchDecisionRequiredError,
    ConstrainedPlanner,
    DecisionDomain,
    RankedOption,
)
from .policy_guard import PolicyGuard
from .session_orchestrator import (
    DecisionGate,
    DecisionOption,
    Session,
    SessionContext,
    SessionEventType,
    SessionOrchestrator,
    SessionStatus,
    sse_event,
)

__all__ = [
    "CapabilityRegistry",
    "PolicyGuard",
    "ActionStep",
    "GDS2Action",
    "RetryPolicy",
    "RiskLevel",
    "UIState",
    "DeterministicExecutor",
    "ExecutionResult",
    "ExecutionStatus",
    "ConstrainedPlanner",
    "DecisionDomain",
    "RankedOption",
    "BranchDecision",
    "BranchDecisionRequiredError",
    "DecisionGate",
    "DecisionOption",
    "Session",
    "SessionContext",
    "SessionEventType",
    "SessionOrchestrator",
    "SessionStatus",
    "sse_event",
    "GDS2ActionAdapter",
    "create_navigation_graph",
    "visualize_graph",
    "make_initial_state",
    "run_local_interactive",
    "NavigationState",
    "create_llm",
    "LLMFactory",
    "ALL_TOOLS",
]

_LAZY_EXPORTS = {
    "create_navigation_graph": (".graph", "create_navigation_graph"),
    "visualize_graph": (".graph", "visualize_graph"),
    "make_initial_state": (".graph", "make_initial_state"),
    "run_local_interactive": (".graph", "run_local_interactive"),
    "NavigationState": (".state", "NavigationState"),
    "create_llm": (".llm_factory", "create_llm"),
    "LLMFactory": (".llm_factory", "LLMFactory"),
    "ALL_TOOLS": (".tools", "ALL_TOOLS"),
}


def __getattr__(name: str) -> Any:
    """Import heavy navigation and LLM symbols only on first access."""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, attr_name = target
    module = import_module(module_name, __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
