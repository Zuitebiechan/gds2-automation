"""Unit tests for Action DSL contracts."""

import pytest

from src.agentic.contracts.action_schema import ActionStep, GDS2Action, RetryPolicy, RiskLevel


def test_action_step_creation_defaults():
    step = ActionStep(action=GDS2Action.START_DIAGNOSTICS)
    assert step.action == GDS2Action.START_DIAGNOSTICS
    assert step.timeout_sec == 30.0
    assert step.retry_policy.max_attempts == 1
    assert step.risk_level == RiskLevel.NORMAL


def test_action_step_timeout_validation():
    with pytest.raises(ValueError, match="timeout_sec"):
        ActionStep(action=GDS2Action.START_DIAGNOSTICS, timeout_sec=0)


def test_retry_policy_validation():
    with pytest.raises(ValueError, match="max_attempts"):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError, match="backoff_sec"):
        RetryPolicy(backoff_sec=-1)


def test_action_step_to_from_dict_roundtrip():
    src = ActionStep(
        action=GDS2Action.SELECT_MODULE,
        args={"module_name": "[K20] Engine Control Module"},
        preconditions=["page==module_list"],
        postconditions=["page in [module_submenu,data_display]"],
        timeout_sec=20.0,
        retry_policy=RetryPolicy(max_attempts=2, backoff_sec=1.5),
        risk_level=RiskLevel.NORMAL,
        metadata={"trace_id": "abc123"},
    )
    restored = ActionStep.from_dict(src.to_dict())

    assert restored.action == src.action
    assert restored.args == src.args
    assert restored.preconditions == src.preconditions
    assert restored.postconditions == src.postconditions
    assert restored.retry_policy.max_attempts == 2
    assert restored.retry_policy.backoff_sec == 1.5
    assert restored.metadata["trace_id"] == "abc123"
