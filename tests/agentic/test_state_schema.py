"""Unit tests for agentic state contracts."""

import pytest

from src.agentic.contracts.state_schema import UIState


def test_state_creation_defaults():
    state = UIState(current_page="main_menu")
    assert state.current_page == "main_menu"
    assert state.visible_buttons == []
    assert state.list_items == []


def test_state_validation_empty_page():
    with pytest.raises(ValueError, match="current_page"):
        UIState(current_page="")


def test_state_helper_methods():
    state = UIState(
        current_page="vehicle_selection",
        visible_buttons=["Enter", "Disconnect"],
        list_items=["Vehicle A", "Vehicle B"],
    )
    assert state.has_button("Enter")
    assert state.has_list_item("Vehicle A")


def test_state_to_from_dict_roundtrip():
    src = UIState(
        current_page="data_display",
        visible_buttons=["Back", "Home", "Create Report"],
        list_items=[],
        context={"module": "[K20] Engine"},
        recent_actions=["select_module", "select_data_category"],
    )
    restored = UIState.from_dict(src.to_dict())

    assert restored.current_page == "data_display"
    assert restored.visible_buttons[2] == "Create Report"
    assert restored.context["module"] == "[K20] Engine"
    assert len(restored.recent_actions) == 2
