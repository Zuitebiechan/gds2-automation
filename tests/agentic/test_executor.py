"""Unit tests for deterministic executor."""

from src.agentic.capability_registry import CapabilityRegistry
from src.agentic.contracts.action_schema import ActionStep, GDS2Action, RetryPolicy
from src.agentic.contracts.state_schema import UIState
from src.agentic.executor import DeterministicExecutor, ExecutionStatus
from src.agentic.policy_guard import PolicyGuard


def test_execute_step_success():
    executor = DeterministicExecutor(PolicyGuard(CapabilityRegistry()))

    def handler(step: ActionStep, state: UIState):
        return {"success": True, "trace": "ok"}

    executor.register_handler(GDS2Action.START_DIAGNOSTICS, handler)

    state = UIState(current_page="main_menu")
    step = ActionStep(action=GDS2Action.START_DIAGNOSTICS)
    result = executor.execute_step(step, state)

    assert result.status == ExecutionStatus.SUCCESS
    assert result.success is True
    assert result.attempts == 1
    assert result.metadata["trace"] == "ok"


def test_execute_step_policy_rejected():
    executor = DeterministicExecutor(PolicyGuard(CapabilityRegistry()))
    state = UIState(current_page="main_menu")
    step = ActionStep(action=GDS2Action.READ_DTCS)

    result = executor.execute_step(step, state)

    assert result.status == ExecutionStatus.FAILED
    assert result.success is False
    assert result.attempts == 0
    assert "not allowed" in (result.error or "")


def test_execute_step_missing_handler():
    executor = DeterministicExecutor(PolicyGuard(CapabilityRegistry()))
    state = UIState(current_page="main_menu")
    step = ActionStep(action=GDS2Action.START_DIAGNOSTICS)

    result = executor.execute_step(step, state)

    assert result.status == ExecutionStatus.FAILED
    assert result.attempts == 0
    assert "No handler registered" in (result.error or "")


def test_execute_step_retries_then_success():
    executor = DeterministicExecutor(PolicyGuard(CapabilityRegistry()))
    calls = {"n": 0}

    def flaky_handler(step: ActionStep, state: UIState):
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("first attempt failed")
        return {"success": True}

    executor.register_handler(GDS2Action.START_DIAGNOSTICS, flaky_handler)

    state = UIState(current_page="main_menu")
    step = ActionStep(
        action=GDS2Action.START_DIAGNOSTICS,
        retry_policy=RetryPolicy(max_attempts=2, backoff_sec=0.0),
    )

    result = executor.execute_step(step, state)

    assert result.success is True
    assert result.attempts == 2
    assert calls["n"] == 2


def test_execute_plan_stops_on_failure():
    executor = DeterministicExecutor(PolicyGuard(CapabilityRegistry()))

    def ok_handler(step: ActionStep, state: UIState):
        return {"success": True}

    executor.register_handler(GDS2Action.START_DIAGNOSTICS, ok_handler)

    state = UIState(current_page="main_menu")
    steps = [
        ActionStep(action=GDS2Action.START_DIAGNOSTICS),
        ActionStep(action=GDS2Action.READ_DTCS),
    ]

    results = executor.execute_plan(steps, state)

    assert len(results) == 2
    assert results[0].success is True
    assert results[1].success is False
