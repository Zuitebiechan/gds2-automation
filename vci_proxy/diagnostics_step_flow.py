from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

DiagnosticsBranch = Literal["", "module", "vehicle"]
OutputMode = Literal["dtc", "ai"]


@dataclass(frozen=True)
class DiagnosticsStepFlowState:
    session_active: bool
    branch: DiagnosticsBranch = ""
    current_page: str = ""
    selected_module: str = ""
    selected_data_category: str = ""
    vehicle_dtc_ready: bool = False
    vehicle_dtc_status_message: str = ""
    category_confirmed: bool = False
    stream_active: bool = False
    ai_sse_running: bool = False
    ai_start_pending: bool = False
    auto_ai_start_scheduled: bool = False
    session_live_data_active: bool = False
    session_ai_active: bool = False
    session_navigation_active: bool = False
    output_mode: OutputMode = "dtc"


@dataclass(frozen=True)
class DiagnosticsStepFlowView:
    busy: bool
    branch_choice_enabled: bool
    show_module_selection: bool
    show_data_selection: bool
    show_module_actions: bool
    show_vehicle_actions: bool
    can_select_module: bool
    can_select_data_category: bool
    can_run_ai: bool
    can_read_dtcs: bool
    can_clear_dtcs: bool
    step_title: str
    step_hint: str
    output_mode: OutputMode


def _is_vehicle_dtc_information_category(value: str) -> bool:
    return str(value or "").strip().casefold() in {
        "vehicle dtc information",
        "vehicle dtcs",
        "vehicle_dtc.information",
    }


def build_step_flow_view(state: DiagnosticsStepFlowState) -> DiagnosticsStepFlowView:
    current_page = str(state.current_page or "").strip().lower()
    has_module = bool(str(state.selected_module or "").strip())
    has_category = bool(str(state.selected_data_category or "").strip())
    vehicle_dtc_category = _is_vehicle_dtc_information_category(state.selected_data_category)
    busy = any(
        (
            state.stream_active,
            state.ai_sse_running,
            state.ai_start_pending,
            state.auto_ai_start_scheduled,
            state.session_live_data_active,
            state.session_ai_active,
            state.session_navigation_active,
        )
    )
    branch_choice_enabled = state.session_active and not busy

    show_module_selection = state.branch == "module"
    show_data_selection = state.branch == "module"
    show_module_actions = state.branch == "module"
    show_vehicle_actions = state.branch == "vehicle"

    can_select_module = state.session_active and show_module_selection and has_module and not busy
    can_select_data_category = (
        state.session_active
        and show_data_selection
        and has_module
        and has_category
        and not busy
    )

    module_ready = (
        state.session_active
        and state.branch == "module"
        and has_module
        and has_category
        and state.category_confirmed
        and current_page == "data_display"
        and not busy
    )
    vehicle_ready = (
        state.session_active
        and state.branch == "vehicle"
        and current_page == "data_display"
        and has_category
        and (not vehicle_dtc_category or state.vehicle_dtc_ready)
        and not busy
    )

    can_run_ai = module_ready
    can_read_dtcs = module_ready or vehicle_ready
    can_clear_dtcs = module_ready or vehicle_ready

    if not state.session_active:
        step_title = "Step 1: Start Session"
        step_hint = "Start Session first, then choose Module Diagnostics or Vehicle Diagnostics."
    elif state.branch == "":
        step_title = "Step 1: Choose Diagnostics Mode"
        step_hint = "Choose Module Diagnostics or Vehicle Diagnostics to continue."
    elif state.branch == "module":
        if not has_module:
            step_title = "Step 2: Select Module"
            step_hint = "Choose a module and submit it."
        elif not has_category or not state.category_confirmed or current_page != "data_display":
            step_title = "Step 3: Select Data"
            step_hint = "Choose a data item and submit it."
        else:
            step_title = "Step 4: Run Module Actions"
            step_hint = "Run AI Diagnostics, Read DTCs, or Clear DTCs."
    else:
        step_title = "Step 2: Vehicle DTC Information"
        if current_page != "data_display":
            step_hint = "Navigating to Vehicle DTC Information..."
        elif vehicle_dtc_category and not state.vehicle_dtc_ready:
            step_hint = (
                str(state.vehicle_dtc_status_message or "").strip()
                or "Waiting for Vehicle DTC table to finish loading..."
            )
        else:
            step_hint = "Vehicle DTC Information is ready. Choose Read DTCs or Clear DTCs."

    return DiagnosticsStepFlowView(
        busy=busy,
        branch_choice_enabled=branch_choice_enabled,
        show_module_selection=show_module_selection,
        show_data_selection=show_data_selection,
        show_module_actions=show_module_actions,
        show_vehicle_actions=show_vehicle_actions,
        can_select_module=can_select_module,
        can_select_data_category=can_select_data_category,
        can_run_ai=can_run_ai,
        can_read_dtcs=can_read_dtcs,
        can_clear_dtcs=can_clear_dtcs,
        step_title=step_title,
        step_hint=step_hint,
        output_mode=state.output_mode,
    )


__all__ = [
    "DiagnosticsBranch",
    "DiagnosticsStepFlowState",
    "DiagnosticsStepFlowView",
    "OutputMode",
    "build_step_flow_view",
]
