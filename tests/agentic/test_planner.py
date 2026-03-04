"""Tests for constrained module/category branch planner."""

import pytest

from src.agentic.planner import ConstrainedPlanner, DecisionDomain


def test_module_exact_match_selected() -> None:
    planner = ConstrainedPlanner()
    choices = [
        "[K20] Engine Control Module",
        "[K9] Transmission Control Module",
    ]

    decision = planner.decide_module("[K20] Engine Control Module", choices)

    assert decision.requires_human is False
    assert decision.selected_option == "[K20] Engine Control Module"
    assert decision.confidence >= 0.58


def test_module_code_match_wins_disambiguation() -> None:
    planner = ConstrainedPlanner()
    choices = [
        "[K20] Engine Control Module",
        "[K21] Engine Control Module",
    ]

    decision = planner.decide_module("K20 engine", choices)

    assert decision.requires_human is False
    assert decision.selected_option == "[K20] Engine Control Module"


def test_data_category_ambiguous_requires_human() -> None:
    planner = ConstrainedPlanner()
    choices = [
        "Engine Data A",
        "Engine Data B",
    ]

    decision = planner.decide_data_category("Engine Data", choices)

    assert decision.requires_human is True
    assert decision.selected_option is None
    assert "Ambiguous" in decision.reason


def test_no_choices_requires_human() -> None:
    planner = ConstrainedPlanner()
    decision = planner.decide_sub_category("Idle", [])

    assert decision.requires_human is True
    assert decision.reason == "No choices available"


def test_branch_decision_to_dict_contains_domain_and_rankings() -> None:
    planner = ConstrainedPlanner()
    decision = planner.decide_data_category(
        "Fuel Pressure",
        ["Fuel Pressure", "Engine Speed", "Battery Voltage"],
    )
    payload = decision.to_dict()

    assert payload["domain"] == DecisionDomain.DATA_CATEGORY.value
    assert isinstance(payload["ranked_options"], list)
    assert payload["ranked_options"]


def test_invalid_planner_threshold_raises() -> None:
    with pytest.raises(ValueError, match="min_confidence"):
        ConstrainedPlanner(min_confidence=1.0)
