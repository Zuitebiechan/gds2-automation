"""Lightweight package exports for the deterministic GDS2 orchestration runtime."""

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
]


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
