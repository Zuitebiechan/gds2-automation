"""Unit tests for policy guard validation."""

from src.agentic.capability_registry import CapabilityRegistry
from src.agentic.contracts.action_schema import ActionStep, GDS2Action, RiskLevel
from src.agentic.contracts.state_schema import UIState
from src.agentic.policy_guard import PolicyGuard


def test_policy_guard_success_for_allowed_action():
    guard = PolicyGuard(CapabilityRegistry())
    state = UIState(current_page="main_menu")
    step = ActionStep(action=GDS2Action.START_DIAGNOSTICS)

    ok, error = guard.validate_action(step, state)
    assert ok is True
    assert error is None


def test_policy_guard_rejects_disallowed_page_action():
    guard = PolicyGuard(CapabilityRegistry())
    state = UIState(current_page="main_menu")
    step = ActionStep(action=GDS2Action.READ_DTCS)

    ok, error = guard.validate_action(step, state)
    assert ok is False
    assert "not allowed" in error


def test_policy_guard_requires_select_module_arg():
    guard = PolicyGuard(CapabilityRegistry())
    state = UIState(current_page="module_list")
    step = ActionStep(action=GDS2Action.SELECT_MODULE, args={})

    ok, error = guard.validate_action(step, state)
    assert ok is False
    assert "module_name" in error


def test_policy_guard_requires_human_approval_for_high_risk():
    guard = PolicyGuard(CapabilityRegistry())
    state = UIState(current_page="main_menu")
    step = ActionStep(
        action=GDS2Action.ABORT_SESSION,
        risk_level=RiskLevel.HIGH,
        metadata={"human_approved": False},
    )

    ok, error = guard.validate_action(step, state)
    assert ok is False
    assert "human_approved" in error


def test_policy_guard_validates_live_stream_interval():
    guard = PolicyGuard(CapabilityRegistry())
    state = UIState(current_page="data_display")
    step = ActionStep(
        action=GDS2Action.START_LIVE_STREAM,
        args={"interval_ms": -100},
    )

    ok, error = guard.validate_action(step, state)
    assert ok is False
    assert "interval_ms" in error
