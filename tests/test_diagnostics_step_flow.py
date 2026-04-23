from __future__ import annotations

from vci_proxy.diagnostics_step_flow import (
    DiagnosticsStepFlowState,
    build_step_flow_view,
)


def test_step_flow_requires_session_before_branch_choice() -> None:
    view = build_step_flow_view(DiagnosticsStepFlowState(session_active=False))

    assert view.branch_choice_enabled is False
    assert view.show_module_selection is False
    assert view.show_vehicle_actions is False
    assert view.step_title == "Step 1: Start Session"


def test_step_flow_exposes_branch_choice_when_session_ready() -> None:
    view = build_step_flow_view(DiagnosticsStepFlowState(session_active=True))

    assert view.branch_choice_enabled is True
    assert view.show_module_selection is False
    assert view.show_vehicle_actions is False
    assert view.step_title == "Step 1: Choose Diagnostics Mode"


def test_module_branch_waits_for_module_then_data_then_actions() -> None:
    no_module = build_step_flow_view(
        DiagnosticsStepFlowState(session_active=True, branch="module")
    )
    assert no_module.show_module_selection is True
    assert no_module.can_select_module is False
    assert no_module.step_title == "Step 2: Select Module"

    no_data = build_step_flow_view(
        DiagnosticsStepFlowState(
            session_active=True,
            branch="module",
            selected_module="ECM",
        )
    )
    assert no_data.can_select_module is True
    assert no_data.can_select_data_category is False
    assert no_data.can_run_ai is False
    assert no_data.step_title == "Step 3: Select Data"

    ready = build_step_flow_view(
        DiagnosticsStepFlowState(
            session_active=True,
            branch="module",
            current_page="data_display",
            selected_module="ECM",
            selected_data_category="Engine Data",
            category_confirmed=True,
        )
    )
    assert ready.show_module_actions is True
    assert ready.show_vehicle_actions is False
    assert ready.can_run_ai is True
    assert ready.can_read_dtcs is True
    assert ready.can_clear_dtcs is True
    assert ready.step_title == "Step 4: Run Module Actions"


def test_vehicle_branch_only_exposes_vehicle_actions() -> None:
    waiting = build_step_flow_view(
        DiagnosticsStepFlowState(session_active=True, branch="vehicle")
    )
    assert waiting.show_module_selection is False
    assert waiting.show_vehicle_actions is True
    assert waiting.can_run_ai is False
    assert waiting.can_read_dtcs is False
    assert waiting.step_title == "Step 2: Vehicle DTC Information"

    ready = build_step_flow_view(
        DiagnosticsStepFlowState(
            session_active=True,
            branch="vehicle",
            current_page="data_display",
            selected_data_category="Vehicle DTC Information",
            vehicle_dtc_ready=True,
        )
    )
    assert ready.show_vehicle_actions is True
    assert ready.can_run_ai is False
    assert ready.can_read_dtcs is True
    assert ready.can_clear_dtcs is True


def test_vehicle_branch_waits_for_vehicle_dtc_table_before_actions() -> None:
    view = build_step_flow_view(
        DiagnosticsStepFlowState(
            session_active=True,
            branch="vehicle",
            current_page="data_display",
            selected_data_category="Vehicle DTC Information",
            vehicle_dtc_ready=False,
            vehicle_dtc_status_message="Vehicle DTC Information is still loading.",
        )
    )

    assert view.show_vehicle_actions is True
    assert view.can_read_dtcs is False
    assert view.can_clear_dtcs is False
    assert view.step_hint == "Vehicle DTC Information is still loading."


def test_busy_state_disables_affordances_without_changing_branch() -> None:
    view = build_step_flow_view(
        DiagnosticsStepFlowState(
            session_active=True,
            branch="module",
            selected_module="ECM",
            selected_data_category="Engine Data",
            current_page="data_display",
            category_confirmed=True,
            ai_start_pending=True,
        )
    )

    assert view.busy is True
    assert view.branch_choice_enabled is False
    assert view.can_select_module is False
    assert view.can_select_data_category is False
    assert view.can_run_ai is False
    assert view.can_read_dtcs is False
    assert view.can_clear_dtcs is False
