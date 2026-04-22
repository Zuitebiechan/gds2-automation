from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def _imports_src_gds2_orchestration(path: Path) -> bool:
    return any(module.startswith("src.gds2_orchestration") for module in _imported_modules(path))


def test_platform_session_models_are_reexported_by_legacy_orchestrator() -> None:
    from diagnostic_platform.session_models import (
        DecisionGate,
        DecisionOption,
        Session,
        SessionContext,
        SessionStatus,
    )
    from src.gds2_orchestration.session_orchestrator import (
        DecisionGate as LegacyDecisionGate,
        DecisionOption as LegacyDecisionOption,
        Session as LegacySession,
        SessionContext as LegacySessionContext,
        SessionStatus as LegacySessionStatus,
    )

    assert LegacyDecisionGate is DecisionGate
    assert LegacyDecisionOption is DecisionOption
    assert LegacySession is Session
    assert LegacySessionContext is SessionContext
    assert LegacySessionStatus is SessionStatus


def test_platform_runtime_modules_use_platform_session_models() -> None:
    targets = [
        ROOT / "diagnostic_platform" / "runtime" / "session_actions.py",
        ROOT / "diagnostic_platform" / "runtime" / "session_backends.py",
        ROOT / "diagnostic_platform" / "runtime" / "session_decisions.py",
        ROOT / "diagnostic_platform" / "runtime" / "session_lifecycle.py",
        ROOT / "diagnostic_platform" / "runtime" / "session_preflight.py",
        ROOT / "diagnostic_platform" / "runtime" / "session_streams.py",
        ROOT / "server" / "api" / "session.py",
        ROOT / "server" / "api" / "session_live_data_handlers.py",
    ]

    for path in targets:
        imports = _imported_modules(path)
        assert "diagnostic_platform.session_models" in imports
        assert "src.gds2_orchestration.session_orchestrator" not in imports


def test_runtime_wiring_uses_platform_orchestrator_boundary() -> None:
    targets = [
        ROOT / "diagnostic_platform" / "runtime" / "worker_runtime.py",
        ROOT / "server" / "api" / "session_dependencies.py",
    ]

    for path in targets:
        imports = _imported_modules(path)
        assert "diagnostic_platform.session_orchestrator" in imports
        assert "src.gds2_orchestration.session_orchestrator" not in imports


def test_branch_decision_models_are_reexported_by_legacy_planner() -> None:
    from diagnostic_platform.branch_planning import (
        BranchDecision,
        BranchDecisionRequiredError,
        DecisionDomain,
        RankedOption,
    )
    from src.gds2_orchestration.planner import (
        BranchDecision as LegacyBranchDecision,
        BranchDecisionRequiredError as LegacyBranchDecisionRequiredError,
        DecisionDomain as LegacyDecisionDomain,
        RankedOption as LegacyRankedOption,
    )

    assert LegacyBranchDecision is BranchDecision
    assert LegacyBranchDecisionRequiredError is BranchDecisionRequiredError
    assert LegacyDecisionDomain is DecisionDomain
    assert LegacyRankedOption is RankedOption


def test_platform_and_server_use_platform_branch_decision_boundary() -> None:
    targets = [
        ROOT / "diagnostic_platform" / "runtime" / "session_decisions.py",
        ROOT / "server" / "api" / "session.py",
    ]

    for path in targets:
        imports = _imported_modules(path)
        assert "diagnostic_platform.branch_planning" in imports
        assert "src.gds2_orchestration.planner" not in imports


def test_action_schema_models_are_reexported_by_legacy_contracts() -> None:
    from diagnostic_platform.action_schema import ActionStep, GDS2Action, RetryPolicy, RiskLevel
    from src.gds2_orchestration.contracts.action_schema import (
        ActionStep as LegacyActionStep,
        GDS2Action as LegacyGDS2Action,
        RetryPolicy as LegacyRetryPolicy,
        RiskLevel as LegacyRiskLevel,
    )

    assert LegacyActionStep is ActionStep
    assert LegacyGDS2Action is GDS2Action
    assert LegacyRetryPolicy is RetryPolicy
    assert LegacyRiskLevel is RiskLevel


def test_platform_runtime_uses_platform_action_schema_boundary() -> None:
    targets = [
        ROOT / "diagnostic_platform" / "runtime" / "session_actions.py",
        ROOT / "backends" / "gds2" / "backend.py",
    ]

    for path in targets:
        imports = _imported_modules(path)
        assert "diagnostic_platform.action_schema" in imports
        assert "src.gds2_orchestration.contracts.action_schema" not in imports


def test_action_runtime_is_reexported_by_legacy_executor_module() -> None:
    from diagnostic_platform.action_runtime import (
        DeterministicExecutor,
        ExecutionResult,
        ExecutionStatus,
    )
    from src.gds2_orchestration.executor import (
        DeterministicExecutor as LegacyDeterministicExecutor,
        ExecutionResult as LegacyExecutionResult,
        ExecutionStatus as LegacyExecutionStatus,
    )

    assert LegacyDeterministicExecutor is DeterministicExecutor
    assert LegacyExecutionResult is ExecutionResult
    assert LegacyExecutionStatus is ExecutionStatus


def test_backend_runtime_uses_platform_action_runtime_boundary() -> None:
    targets = [
        ROOT / "backends" / "gds2" / "backend.py",
        ROOT / "backends" / "gds2" / "action_adapter.py",
    ]

    for path in targets:
        imports = _imported_modules(path)
        assert "diagnostic_platform.action_runtime" in imports


def test_gds2_action_runtime_models_are_reexported_by_legacy_modules() -> None:
    from backends.gds2.action_runtime import CapabilityRegistry, PolicyGuard, UIState
    from src.gds2_orchestration.capability_registry import CapabilityRegistry as LegacyCapabilityRegistry
    from src.gds2_orchestration.contracts.state_schema import UIState as LegacyUIState
    from src.gds2_orchestration.policy_guard import PolicyGuard as LegacyPolicyGuard

    assert LegacyCapabilityRegistry is CapabilityRegistry
    assert LegacyPolicyGuard is PolicyGuard
    assert LegacyUIState is UIState


def test_gds2_backend_uses_backend_action_runtime_boundary() -> None:
    targets = [
        ROOT / "backends" / "gds2" / "backend.py",
        ROOT / "src" / "gds2_orchestration" / "policy_guard.py",
        ROOT / "src" / "gds2_orchestration" / "capability_registry.py",
        ROOT / "src" / "gds2_orchestration" / "contracts" / "state_schema.py",
    ]

    for path in targets:
        imports = _imported_modules(path)
        assert "backends.gds2.action_runtime" in imports


def test_gds2_planner_is_reexported_by_legacy_module() -> None:
    from backends.gds2.planner import ConstrainedPlanner
    from src.gds2_orchestration.planner import ConstrainedPlanner as LegacyConstrainedPlanner

    assert LegacyConstrainedPlanner is ConstrainedPlanner


def test_gds2_action_adapter_is_reexported_by_legacy_module() -> None:
    from backends.gds2.action_adapter import GDS2ActionAdapter
    from src.gds2_orchestration.adapters.gds2_adapter import (
        GDS2ActionAdapter as LegacyGDS2ActionAdapter,
    )

    assert LegacyGDS2ActionAdapter is GDS2ActionAdapter


def test_gds2_backend_and_remaining_workflows_use_backend_ownership_modules() -> None:
    backend_imports = _imported_modules(ROOT / "backends" / "gds2" / "backend.py")
    assert "backends.gds2.action_adapter" in backend_imports
    assert "src.gds2_orchestration" not in backend_imports

    workflow_imports = _imported_modules(ROOT / "src" / "workflows" / "interactive_workflow.py")
    assert "gds2_orchestration.planner" not in workflow_imports


def test_backend_owned_gds2_modules_do_not_import_legacy_orchestrator_paths() -> None:
    targets = [
        ROOT / "backends" / "gds2" / "planner.py",
        ROOT / "backends" / "gds2" / "action_adapter.py",
        ROOT / "backends" / "gds2" / "backend.py",
    ]

    for path in targets:
        imports = _imported_modules(path)
        assert all(not module.startswith("src.gds2_orchestration") for module in imports), str(
            path.relative_to(ROOT)
        )


def test_platform_session_orchestrator_is_reexported_by_legacy_module() -> None:
    from diagnostic_platform.session_orchestrator import (
        SessionEventType,
        SessionOrchestrator,
        route_backend,
        sse_event,
    )
    from src.gds2_orchestration.session_orchestrator import (
        SessionEventType as LegacySessionEventType,
        SessionOrchestrator as LegacySessionOrchestrator,
        route_backend as LegacyRouteBackend,
        sse_event as LegacySseEvent,
    )

    assert LegacySessionOrchestrator is SessionOrchestrator
    assert LegacySessionEventType is SessionEventType
    assert LegacyRouteBackend is route_backend
    assert LegacySseEvent is sse_event


def test_platform_and_server_no_longer_directly_import_src_gds2_orchestration() -> None:
    for base in ("diagnostic_platform", "server"):
        for path in (ROOT / base).rglob("*.py"):
            assert not _imports_src_gds2_orchestration(path), str(path.relative_to(ROOT))
