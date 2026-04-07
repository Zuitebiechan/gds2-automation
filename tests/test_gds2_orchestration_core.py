from __future__ import annotations

import types

import pytest

from src.gds2_orchestration.contracts.action_schema import (
    ActionStep,
    GDS2Action,
    RiskLevel,
    RetryPolicy,
)
from src.gds2_orchestration.contracts.state_schema import UIState
from src.gds2_orchestration.executor import (
    DeterministicExecutor,
    ExecutionStatus,
)
from src.gds2_orchestration.planner import (
    BranchDecision,
    BranchDecisionRequiredError,
    ConstrainedPlanner,
    DecisionDomain,
    RankedOption,
)
from src.gds2_orchestration.policy_guard import PolicyGuard


def test_ranked_option_and_branch_decision_validate_bounds() -> None:
    with pytest.raises(ValueError, match="option cannot be empty"):
        RankedOption(option="", score=0.5)

    with pytest.raises(ValueError, match="score must be in \\[0.0, 1.0\\]"):
        RankedOption(option="ECM", score=1.1)

    with pytest.raises(ValueError, match="selected_option must be None"):
        BranchDecision(
            domain=DecisionDomain.MODULE,
            target="ECM",
            selected_option="ECM",
            requires_human=True,
            confidence=0.5,
        )

    with pytest.raises(ValueError, match="selected_option is required"):
        BranchDecision(
            domain=DecisionDomain.MODULE,
            target="ECM",
            selected_option=None,
            requires_human=False,
            confidence=0.5,
        )


def test_constrained_planner_selects_exact_module_code_match() -> None:
    planner = ConstrainedPlanner()

    decision = planner.decide_module(
        "[K20] Engine Control Module",
        [
            "[K71] Transmission Control Module",
            "[K20] Engine Control Module",
            "[P12] Brake Control Module",
        ],
    )

    assert decision.requires_human is False
    assert decision.selected_option == "[K20] Engine Control Module"
    assert decision.confidence == 1.0
    assert decision.ranked_options[0].reasons[0] == "exact_normalized_match"


def test_constrained_planner_requires_human_for_ambiguous_top_candidates() -> None:
    planner = ConstrainedPlanner(min_confidence=0.58, ambiguity_gap=0.09)

    decision = planner.decide_data_category(
        "Engine Data",
        ["Engine Data 1", "Engine Data 2", "Transmission Data"],
    )

    assert decision.requires_human is True
    assert decision.selected_option is None
    assert "Ambiguous top candidates" in decision.reason


def test_constrained_planner_requires_human_for_low_confidence_match() -> None:
    planner = ConstrainedPlanner()

    decision = planner.decide_sub_category(
        "ABS",
        ["Radio", "Heated Seats"],
    )

    assert decision.requires_human is True
    assert decision.confidence == 0.0
    assert decision.reason == "Low confidence (0.00)"


def test_branch_decision_required_error_preserves_payload() -> None:
    decision = BranchDecision(
        domain=DecisionDomain.MODULE,
        target="ECM",
        selected_option=None,
        requires_human=True,
        confidence=0.42,
        reason="ambiguous",
    )
    error = BranchDecisionRequiredError(decision, ["ECM-A", "ECM-B"])

    assert error.to_dict() == {
        "decision": decision.to_dict(),
        "choices": ["ECM-A", "ECM-B"],
    }


def test_policy_guard_rejects_invalid_actions_and_arguments() -> None:
    guard = PolicyGuard()

    valid, reason = guard.validate_action(
        ActionStep(action=GDS2Action.SELECT_MODULE),
        UIState(current_page="vehicle_selection"),
    )
    assert (valid, reason) == (
        False,
        "Action select_module is not allowed on page vehicle_selection",
    )

    valid, reason = guard.validate_action(
        ActionStep(action=GDS2Action.SELECT_MODULE),
        UIState(current_page="module_list"),
    )
    assert (valid, reason) == (
        False,
        "Action select_module requires 'module_name' argument",
    )

    valid, reason = guard.validate_action(
        ActionStep(action=GDS2Action.START_LIVE_STREAM, args={"interval_ms": 0}),
        UIState(current_page="data_display"),
    )
    assert (valid, reason) == (
        False,
        "Action start_live_stream requires positive 'interval_ms'",
    )

    valid, reason = guard.validate_action(
        ActionStep(
            action=GDS2Action.ABORT_SESSION,
            risk_level=RiskLevel.HIGH,
        ),
        UIState(current_page="module_list"),
    )
    assert (valid, reason) == (
        False,
        "High risk action requires 'human_approved' metadata",
    )


def test_policy_guard_accepts_valid_action() -> None:
    guard = PolicyGuard()

    valid, reason = guard.validate_action(
        ActionStep(
            action=GDS2Action.SELECT_DATA_CATEGORY,
            args={"category_name": "Engine Data"},
        ),
        UIState(current_page="data_list"),
    )

    assert (valid, reason) == (True, None)


def test_executor_retries_until_handler_succeeds(monkeypatch) -> None:
    executor = DeterministicExecutor()
    attempts = {"count": 0}
    monkeypatch.setattr("src.gds2_orchestration.executor.time.sleep", lambda seconds: None)

    def _handler(step: ActionStep, state: UIState):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("transient")
        return {"selected_module": "ECM"}

    executor.register_handler(GDS2Action.SELECT_MODULE, _handler)

    result = executor.execute_step(
        ActionStep(
            action=GDS2Action.SELECT_MODULE,
            args={"module_name": "ECM"},
            retry_policy=RetryPolicy(max_attempts=2, backoff_sec=0.1),
        ),
        UIState(current_page="module_list"),
    )

    assert result.status == ExecutionStatus.SUCCESS
    assert result.attempts == 2
    assert result.metadata == {"selected_module": "ECM"}


def test_executor_returns_failure_for_handler_success_false_payload() -> None:
    executor = DeterministicExecutor()
    executor.register_handler(
        GDS2Action.SELECT_DATA_CATEGORY,
        lambda step, state: {"success": False, "error": "selection failed"},
    )

    result = executor.execute_step(
        ActionStep(
            action=GDS2Action.SELECT_DATA_CATEGORY,
            args={"category_name": "Engine Data"},
        ),
        UIState(current_page="data_list"),
    )

    assert result.status == ExecutionStatus.FAILED
    assert result.error == "selection failed"
    assert result.attempts == 1


def test_execute_plan_stops_after_first_failure() -> None:
    executor = DeterministicExecutor()
    calls: list[str] = []

    executor.register_handler(
        GDS2Action.SELECT_MODULE,
        lambda step, state: {"selected_module": step.args["module_name"]},
    )

    def _failing_handler(step: ActionStep, state: UIState):
        calls.append("failed")
        raise RuntimeError("boom")

    executor.register_handler(GDS2Action.GO_HOME, _failing_handler)
    executor.register_handler(
        GDS2Action.SELECT_DATA_CATEGORY,
        lambda step, state: calls.append("should-not-run"),
    )

    results = executor.execute_plan(
        [
            ActionStep(action=GDS2Action.SELECT_MODULE, args={"module_name": "ECM"}),
            ActionStep(action=GDS2Action.GO_HOME),
            ActionStep(
                action=GDS2Action.SELECT_DATA_CATEGORY,
                args={"category_name": "Engine Data"},
            ),
        ],
        UIState(current_page="module_list"),
    )

    assert [result.status for result in results] == [
        ExecutionStatus.SUCCESS,
        ExecutionStatus.FAILED,
    ]
    assert calls == ["failed"]
