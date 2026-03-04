"""Unit tests for GDS2ActionAdapter — bridges agentic executor to real GDS2."""

from unittest.mock import MagicMock, patch

import pytest

from src.agentic.adapters.gds2_adapter import GDS2ActionAdapter
from src.agentic.capability_registry import CapabilityRegistry
from src.agentic.contracts.action_schema import ActionStep, GDS2Action, RetryPolicy
from src.agentic.contracts.state_schema import UIState
from src.agentic.executor import DeterministicExecutor, ExecutionStatus
from src.agentic.policy_guard import PolicyGuard


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_workflow():
    """Create a mock DataViewerWorkflow with expected method signatures."""
    workflow = MagicMock()
    controller = MagicMock()
    workflow.controller = controller
    return workflow, controller


def _make_wired_executor(workflow, collector_factory=None):
    """Create executor with adapter registered."""
    adapter = GDS2ActionAdapter(workflow, collector_factory=collector_factory)
    executor = DeterministicExecutor(PolicyGuard(CapabilityRegistry()))
    adapter.register_all(executor)
    return executor, adapter


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_register_all_populates_all_handlers():
    workflow, _ = _make_mock_workflow()
    executor, _ = _make_wired_executor(workflow)

    registered = executor.get_registered_actions()
    for action in GDS2Action:
        assert action in registered, f"{action} not registered"


# ---------------------------------------------------------------------------
# START_DIAGNOSTICS
# ---------------------------------------------------------------------------

def test_start_diagnostics_calls_workflow_start():
    workflow, _ = _make_mock_workflow()
    workflow.start.return_value = {"modules": ["ECM", "TCM"]}

    executor, adapter = _make_wired_executor(workflow)
    state = UIState(current_page="main_menu")
    step = ActionStep(action=GDS2Action.START_DIAGNOSTICS)

    result = executor.execute_step(step, state)

    assert result.success
    workflow.start.assert_called_once()
    assert result.metadata.get("modules") == ["ECM", "TCM"]


# ---------------------------------------------------------------------------
# SELECT_DEVICE
# ---------------------------------------------------------------------------

def test_select_device_calls_controller():
    workflow, controller = _make_mock_workflow()
    nav_result = MagicMock()
    nav_result.success = True
    nav_result.page = MagicMock(value="vehicle_selection")
    nav_result.selected = "VCI Proxy (Remote)"
    nav_result.error = None
    controller.select_device.return_value = nav_result

    executor, _ = _make_wired_executor(workflow)
    state = UIState(current_page="device_explorer")
    step = ActionStep(
        action=GDS2Action.SELECT_DEVICE,
        args={"device_name": "VCI Proxy (Remote)"},
    )

    result = executor.execute_step(step, state)

    assert result.success
    controller.select_device.assert_called_once_with("VCI Proxy (Remote)")


def test_select_device_rejected_without_device_name():
    workflow, _ = _make_mock_workflow()
    executor, _ = _make_wired_executor(workflow)
    state = UIState(current_page="device_explorer")
    step = ActionStep(action=GDS2Action.SELECT_DEVICE, args={})

    result = executor.execute_step(step, state)

    assert not result.success
    assert "device_name" in (result.error or "")


# ---------------------------------------------------------------------------
# CONNECT_DEVICE
# ---------------------------------------------------------------------------

def test_connect_device_calls_workflow():
    workflow, _ = _make_mock_workflow()
    workflow.connect_device.return_value = {"modules": ["ECM"]}

    executor, _ = _make_wired_executor(workflow)
    state = UIState(current_page="device_explorer")
    step = ActionStep(
        action=GDS2Action.CONNECT_DEVICE,
        args={"device_name": "VCI Proxy (Remote)"},
    )

    result = executor.execute_step(step, state)

    assert result.success
    workflow.connect_device.assert_called_once_with("VCI Proxy (Remote)")


# ---------------------------------------------------------------------------
# SELECT_MODULE
# ---------------------------------------------------------------------------

def test_select_module_calls_workflow():
    workflow, _ = _make_mock_workflow()
    workflow.select_module.return_value = {"data_categories": ["Engine", "Trans"]}

    executor, _ = _make_wired_executor(workflow)
    state = UIState(current_page="module_list")
    step = ActionStep(
        action=GDS2Action.SELECT_MODULE,
        args={"module_name": "ECM"},
    )

    result = executor.execute_step(step, state)

    assert result.success
    workflow.select_module.assert_called_once_with("ECM")
    assert result.metadata.get("data_categories") == ["Engine", "Trans"]


def test_select_module_rejected_without_module_name():
    workflow, _ = _make_mock_workflow()
    executor, _ = _make_wired_executor(workflow)
    state = UIState(current_page="module_list")
    step = ActionStep(action=GDS2Action.SELECT_MODULE, args={})

    result = executor.execute_step(step, state)

    assert not result.success
    assert "module_name" in (result.error or "")


def test_select_module_rejected_wrong_page():
    workflow, _ = _make_mock_workflow()
    executor, _ = _make_wired_executor(workflow)
    state = UIState(current_page="main_menu")
    step = ActionStep(
        action=GDS2Action.SELECT_MODULE,
        args={"module_name": "ECM"},
    )

    result = executor.execute_step(step, state)

    assert not result.success
    assert "not allowed" in (result.error or "")


# ---------------------------------------------------------------------------
# SELECT_DATA_CATEGORY
# ---------------------------------------------------------------------------

def test_select_data_category_calls_workflow():
    workflow, _ = _make_mock_workflow()
    workflow.select_data_category.return_value = {"status": "at_data_display"}

    executor, _ = _make_wired_executor(workflow)
    state = UIState(current_page="data_list")
    step = ActionStep(
        action=GDS2Action.SELECT_DATA_CATEGORY,
        args={"category_name": "Engine Data"},
    )

    result = executor.execute_step(step, state)

    assert result.success
    workflow.select_data_category.assert_called_once_with("Engine Data")


def test_select_data_category_rejected_without_arg():
    workflow, _ = _make_mock_workflow()
    executor, _ = _make_wired_executor(workflow)
    state = UIState(current_page="data_list")
    step = ActionStep(action=GDS2Action.SELECT_DATA_CATEGORY, args={})

    result = executor.execute_step(step, state)

    assert not result.success
    assert "category_name" in (result.error or "")


# ---------------------------------------------------------------------------
# SELECT_SUB_CATEGORY
# ---------------------------------------------------------------------------

def test_select_sub_category_calls_controller():
    workflow, controller = _make_mock_workflow()
    nav_result = MagicMock()
    nav_result.success = True
    nav_result.page = MagicMock(value="data_display")
    nav_result.selected = "PID Data"
    nav_result.error = None
    controller.select_sub_category.return_value = nav_result

    executor, _ = _make_wired_executor(workflow)
    state = UIState(current_page="sub_data_list")
    step = ActionStep(
        action=GDS2Action.SELECT_SUB_CATEGORY,
        args={"sub_category_name": "PID Data"},
    )

    result = executor.execute_step(step, state)

    assert result.success
    controller.select_sub_category.assert_called_once_with("PID Data")


# ---------------------------------------------------------------------------
# READ_DTCS
# ---------------------------------------------------------------------------

def test_read_dtcs_calls_workflow():
    workflow, _ = _make_mock_workflow()
    workflow.read_all_dtcs.return_value = {"dtcs": [{"code": "P0171"}]}

    executor, _ = _make_wired_executor(workflow)
    state = UIState(current_page="data_display")
    step = ActionStep(action=GDS2Action.READ_DTCS)

    result = executor.execute_step(step, state)

    assert result.success
    workflow.read_all_dtcs.assert_called_once()
    assert result.metadata.get("dtcs") == [{"code": "P0171"}]


def test_read_dtcs_rejected_on_wrong_page():
    workflow, _ = _make_mock_workflow()
    executor, _ = _make_wired_executor(workflow)
    state = UIState(current_page="module_list")
    step = ActionStep(action=GDS2Action.READ_DTCS)

    result = executor.execute_step(step, state)

    assert not result.success
    assert "not allowed" in (result.error or "")


# ---------------------------------------------------------------------------
# LIVE STREAM
# ---------------------------------------------------------------------------

def test_start_live_stream_with_factory():
    workflow, _ = _make_mock_workflow()
    collector = MagicMock()
    factory = MagicMock(return_value=collector)

    executor, _ = _make_wired_executor(workflow, collector_factory=factory)
    state = UIState(current_page="data_display")
    step = ActionStep(
        action=GDS2Action.START_LIVE_STREAM,
        args={"interval_ms": 500},
    )

    result = executor.execute_step(step, state)

    assert result.success
    factory.assert_called_once()
    collector.start.assert_called_once()
    assert result.metadata.get("streaming") is True


def test_start_live_stream_without_factory():
    workflow, _ = _make_mock_workflow()
    executor, _ = _make_wired_executor(workflow, collector_factory=None)
    state = UIState(current_page="data_display")
    step = ActionStep(
        action=GDS2Action.START_LIVE_STREAM,
        args={"interval_ms": 500},
    )

    result = executor.execute_step(step, state)

    assert not result.success
    assert "collector_factory" in (result.error or "")


def test_stop_live_stream():
    workflow, _ = _make_mock_workflow()
    collector = MagicMock()
    factory = MagicMock(return_value=collector)

    executor, adapter = _make_wired_executor(workflow, collector_factory=factory)

    # Start first to set _active_collector
    state = UIState(current_page="data_display")
    start_step = ActionStep(
        action=GDS2Action.START_LIVE_STREAM,
        args={"interval_ms": 1000},
    )
    executor.execute_step(start_step, state)

    # Now stop
    stop_step = ActionStep(action=GDS2Action.STOP_LIVE_STREAM)
    result = executor.execute_step(stop_step, state)

    assert result.success
    collector.stop.assert_called_once()
    workflow.stop_monitoring.assert_called_once()


# ---------------------------------------------------------------------------
# GO_HOME / GO_BACK
# ---------------------------------------------------------------------------

def test_go_home_calls_controller():
    workflow, controller = _make_mock_workflow()
    nav_result = MagicMock()
    nav_result.success = True
    nav_result.page = MagicMock(value="main_menu")
    nav_result.error = None
    controller.go_home.return_value = nav_result

    executor, _ = _make_wired_executor(workflow)
    state = UIState(current_page="data_display")
    step = ActionStep(action=GDS2Action.GO_HOME)

    result = executor.execute_step(step, state)

    assert result.success
    controller.go_home.assert_called_once()


def test_go_back_calls_controller():
    workflow, controller = _make_mock_workflow()
    nav_result = MagicMock()
    nav_result.success = True
    nav_result.page = MagicMock(value="data_list")
    nav_result.error = None
    controller.go_back.return_value = nav_result

    executor, _ = _make_wired_executor(workflow)
    state = UIState(current_page="data_display")
    step = ActionStep(action=GDS2Action.GO_BACK)

    result = executor.execute_step(step, state)

    assert result.success
    controller.go_back.assert_called_once()


# ---------------------------------------------------------------------------
# ABORT_SESSION
# ---------------------------------------------------------------------------

def test_abort_session_cleans_up():
    workflow, controller = _make_mock_workflow()
    nav_result = MagicMock()
    nav_result.success = True
    nav_result.page = MagicMock(value="main_menu")
    controller.go_home.return_value = nav_result

    executor, _ = _make_wired_executor(workflow)
    state = UIState(current_page="main_menu")
    step = ActionStep(action=GDS2Action.ABORT_SESSION)

    result = executor.execute_step(step, state)

    assert result.success
    assert result.metadata.get("aborted") is True
    workflow.stop_monitoring.assert_called_once()
    controller.go_home.assert_called_once()


def test_abort_session_handles_cleanup_error():
    workflow, controller = _make_mock_workflow()
    workflow.stop_monitoring.side_effect = RuntimeError("cleanup failed")
    nav_result = MagicMock()
    nav_result.success = True
    nav_result.page = MagicMock(value="main_menu")
    controller.go_home.return_value = nav_result

    executor, _ = _make_wired_executor(workflow)
    state = UIState(current_page="main_menu")
    step = ActionStep(action=GDS2Action.ABORT_SESSION)

    result = executor.execute_step(step, state)

    # Should still succeed despite cleanup error
    assert result.success
    assert result.metadata.get("aborted") is True


# ---------------------------------------------------------------------------
# get_current_ui_state
# ---------------------------------------------------------------------------

def test_get_current_ui_state():
    workflow, controller = _make_mock_workflow()
    page_enum = MagicMock(value="module_list")
    controller.detect_current_page.return_value = page_enum
    controller.get_visible_buttons.return_value = [
        {"text": "Home"}, {"text": "Back"},
    ]
    controller.get_list_items.return_value = ["ECM", "TCM", "BCM"]
    controller.get_context.return_value = {"module": "ECM"}

    adapter = GDS2ActionAdapter(workflow)
    ui_state = adapter.get_current_ui_state()

    assert ui_state.current_page == "module_list"
    assert "Home" in ui_state.visible_buttons
    assert "ECM" in ui_state.list_items
    assert ui_state.context.get("module") == "ECM"


# ---------------------------------------------------------------------------
# Retry through adapter
# ---------------------------------------------------------------------------

def test_retry_works_through_adapter():
    """Verify that executor retry logic works when adapter handler fails first."""
    workflow, _ = _make_mock_workflow()
    call_count = {"n": 0}

    original_start = workflow.start
    def flaky_start(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] < 2:
            raise RuntimeError("GDS2 busy")
        return {"modules": ["ECM"]}
    workflow.start = flaky_start

    executor, _ = _make_wired_executor(workflow)
    state = UIState(current_page="main_menu")
    step = ActionStep(
        action=GDS2Action.START_DIAGNOSTICS,
        retry_policy=RetryPolicy(max_attempts=3, backoff_sec=0.0),
    )

    result = executor.execute_step(step, state)

    assert result.success
    assert result.attempts == 2
    assert call_count["n"] == 2


# ---------------------------------------------------------------------------
# Full plan execution through adapter
# ---------------------------------------------------------------------------

def test_execute_plan_through_adapter():
    """Test a mini-plan: START_DIAGNOSTICS -> SELECT_MODULE."""
    workflow, _ = _make_mock_workflow()
    workflow.start.return_value = {"modules": ["ECM", "TCM"]}
    workflow.select_module.return_value = {"data_categories": ["Engine"]}

    executor, _ = _make_wired_executor(workflow)
    state = UIState(current_page="main_menu")

    steps = [
        ActionStep(action=GDS2Action.START_DIAGNOSTICS),
        ActionStep(
            action=GDS2Action.SELECT_MODULE,
            args={"module_name": "ECM"},
        ),
    ]

    # For plan execution, module_list must be the page for step 2.
    # But execute_plan uses the same state for all steps. In real usage
    # state would be re-read after each step. For this test, we accept
    # that the second step will fail policy guard (wrong page).
    results = executor.execute_plan(steps, state)

    assert len(results) == 2
    assert results[0].success  # START_DIAGNOSTICS on main_menu = allowed
    # Second step (SELECT_MODULE on main_menu) will fail policy guard
    assert not results[1].success
    assert "not allowed" in (results[1].error or "")
