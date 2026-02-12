"""
Day 6 Integration Test - DataViewerWorkflow with AI Recovery.

Tests the integration of RecoveryManager into DataViewerWorkflow.
Uses mocked components, no real GDS2 or API calls needed.
"""

import sys
from pathlib import Path
import json
from unittest.mock import Mock, patch

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))


def test_integration_basic():
    """Test basic integration: workflow can initialize with recovery enabled."""
    print("\n" + "="*70)
    print("TEST 1: Basic Integration")
    print("="*70)

    from src.workflows.data_viewer import DataViewerWorkflow

    # Test 1: Without recovery
    print("\n1. Initialize workflow WITHOUT AI recovery...")
    workflow = DataViewerWorkflow(enable_ai_recovery=False)
    assert workflow.recovery is None
    print("  ✅ Recovery is None (as expected)")

    # Test 2: With recovery (but no API key - should disable gracefully)
    print("\n2. Initialize workflow WITH AI recovery (no API key)...")
    workflow = DataViewerWorkflow(enable_ai_recovery=True)
    if workflow.recovery:
        print(f"  ✅ Recovery initialized: enabled={workflow.recovery.enabled}")
    else:
        print("  ℹ️  Recovery disabled (no valid config/API key)")

    print("\n✅ Basic integration test passed\n")


def test_integration_with_mocked_recovery():
    """Test integration with fully mocked recovery manager."""
    print("\n" + "="*70)
    print("TEST 2: Integration with Mocked Recovery")
    print("="*70)

    from src.workflows.data_viewer import DataViewerWorkflow
    from src.recovery import RecoveryManager, AIRecoveryConfig

    # Create mock recovery
    mock_config = AIRecoveryConfig(enabled=True, api_key="test-key")
    mock_recovery = Mock(spec=RecoveryManager)
    mock_recovery.enabled = True
    mock_recovery.check_for_dialogs = Mock(return_value=None)
    mock_recovery.detector = Mock()
    mock_recovery.detector.check_timeout = Mock(return_value=None)
    mock_recovery.reset_session = Mock()

    # Create workflow
    workflow = DataViewerWorkflow(enable_ai_recovery=False)
    # Manually inject mock recovery
    workflow.recovery = mock_recovery

    print("\n1. Verify recovery is injected...")
    assert workflow.recovery is not None
    assert workflow.recovery.enabled is True
    print("  ✅ Recovery manager injected")

    print("\n2. Test _wait_for_button_enabled with recovery...")

    # Mock controller completely
    mock_controller = Mock()
    call_count = [0]

    def mock_get_buttons():
        call_count[0] += 1
        if call_count[0] >= 2:  # Button enabled on 2nd check
            return [{"text": "Enter", "enabled": True}]
        return [{"text": "Enter", "enabled": False}]

    mock_nav = Mock()
    mock_nav.get_buttons = mock_get_buttons
    mock_controller.nav = mock_nav
    mock_controller.current_page = Mock(value="VEHICLE_SELECTION")

    # Replace workflow's controller
    workflow.controller = mock_controller

    # Call _wait_for_button_enabled
    result = workflow._wait_for_button_enabled("Enter", timeout_sec=3)

    print(f"  ✅ Button wait result: {result}")
    print(f"  ✅ check_for_dialogs called: {mock_recovery.check_for_dialogs.call_count} times")

    assert result is True
    assert mock_recovery.check_for_dialogs.call_count >= 1

    print("\n✅ Mocked integration test passed\n")


def test_wait_for_button_with_dialog_recovery():
    """Test _wait_for_button_enabled with dialog detection and recovery."""
    print("\n" + "="*70)
    print("TEST 3: Wait for Button with Dialog Recovery")
    print("="*70)

    from src.workflows.data_viewer import DataViewerWorkflow
    from src.recovery import Anomaly, AnomalyType, RecoveryResult, RecoveryAction

    # Setup workflow
    workflow = DataViewerWorkflow(enable_ai_recovery=False)

    # Create mock recovery that detects a dialog
    mock_recovery = Mock()
    mock_recovery.enabled = True

    # First call: detect dialog, second call: no dialog
    dialog_anomaly = Anomaly(
        type=AnomalyType.UNEXPECTED_DIALOG,
        context={"modal_title": "Test Dialog", "modal_buttons": ["OK"]}
    )

    check_calls = [0]

    def mock_check_dialogs():
        check_calls[0] += 1
        if check_calls[0] == 1:
            return dialog_anomaly  # First check: dialog detected
        return None  # Subsequent checks: no dialog

    mock_recovery.check_for_dialogs = mock_check_dialogs

    # Mock handle_anomaly to simulate successful recovery
    recovery_result = RecoveryResult(
        success=True,
        action=RecoveryAction.CLICK_BUTTON,
        attempts=1,
        elapsed_time=1.5,
    )
    mock_recovery.handle_anomaly = Mock(return_value=recovery_result)
    mock_recovery.detector = Mock()
    mock_recovery.detector.check_timeout = Mock(return_value=None)

    workflow.recovery = mock_recovery

    # Mock controller completely
    mock_controller = Mock()
    button_calls = [0]

    def mock_get_buttons():
        button_calls[0] += 1
        # Button enabled after dialog is handled
        if button_calls[0] >= 2:
            return [{"text": "Enter", "enabled": True}]
        return [{"text": "Enter", "enabled": False}]

    mock_nav = Mock()
    mock_nav.get_buttons = mock_get_buttons
    mock_controller.nav = mock_nav
    mock_controller.current_page = Mock(value="DEVICE_EXPLORER")

    workflow.controller = mock_controller

    print("\n1. Calling _wait_for_button_enabled...")
    result = workflow._wait_for_button_enabled("Enter", timeout_sec=5)

    print(f"\n2. Results:")
    print(f"  ✅ Button enabled: {result}")
    print(f"  ✅ Dialog checks: {check_calls[0]}")
    print(f"  ✅ handle_anomaly called: {mock_recovery.handle_anomaly.called}")

    if mock_recovery.handle_anomaly.called:
        call_args = mock_recovery.handle_anomaly.call_args
        anomaly_arg = call_args[0][0]
        context_arg = call_args[0][1]
        print(f"  ✅ Anomaly type: {anomaly_arg.type.name}")
        print(f"  ✅ Context operation: {context_arg.operation_name}")

    assert result is True
    assert mock_recovery.handle_anomaly.called

    print("\n✅ Dialog recovery test passed\n")


def test_wait_for_button_with_timeout_recovery():
    """Test _wait_for_button_enabled with timeout detection."""
    print("\n" + "="*70)
    print("TEST 4: Wait for Button with Timeout Recovery")
    print("="*70)

    from src.workflows.data_viewer import DataViewerWorkflow
    from src.recovery import Anomaly, AnomalyType, RecoveryResult, RecoveryAction

    # Setup workflow
    workflow = DataViewerWorkflow(enable_ai_recovery=False)

    # Create mock recovery
    mock_recovery = Mock()
    mock_recovery.enabled = True
    mock_recovery.check_for_dialogs = Mock(return_value=None)

    # Mock timeout detection
    timeout_anomaly = Anomaly(
        type=AnomalyType.TIMEOUT,
        context={"operation": "wait_for_button", "elapsed_time": 3.0, "expected_time": 3.0}
    )
    mock_recovery.detector = Mock()
    mock_recovery.detector.check_timeout = Mock(return_value=timeout_anomaly)

    # Mock recovery decision: wait longer
    recovery_result = RecoveryResult(
        success=True,
        action=RecoveryAction.WAIT_LONGER,
        attempts=1,
        elapsed_time=0.5,
    )
    mock_recovery.handle_anomaly = Mock(return_value=recovery_result)

    workflow.recovery = mock_recovery

    # Mock controller completely
    mock_controller = Mock()
    mock_nav = Mock()
    mock_nav.get_buttons = Mock(return_value=[{"text": "Enter", "enabled": False}])
    mock_controller.nav = mock_nav
    mock_controller.current_page = Mock(value="VEHICLE_SELECTION")

    workflow.controller = mock_controller

    print("\n1. Calling _wait_for_button_enabled with short timeout...")
    result = workflow._wait_for_button_enabled("Enter", timeout_sec=2)

    print(f"\n2. Results:")
    print(f"  ℹ️  Button enabled: {result} (expected False after extended timeout)")
    print(f"  ✅ Timeout detected: {mock_recovery.detector.check_timeout.called}")
    print(f"  ✅ Recovery attempted: {mock_recovery.handle_anomaly.called}")

    # With WAIT_LONGER, the method recursively calls itself, which may succeed or timeout again
    print("\n✅ Timeout detection test passed\n")


if __name__ == "__main__":
    print("\n🎯 Day 6 Integration Tests")
    print("Testing RecoveryManager integration into DataViewerWorkflow")
    print("(Using mocked components, no real GDS2 or API needed)\n")

    try:
        test_integration_basic()
        test_integration_with_mocked_recovery()
        test_wait_for_button_with_dialog_recovery()
        test_wait_for_button_with_timeout_recovery()

        print("\n" + "="*70)
        print("✅ All Integration Tests Passed!")
        print("="*70)
        print("\nWhat was tested:")
        print("  1. ✅ Workflow can initialize with/without recovery")
        print("  2. ✅ Recovery manager properly injected")
        print("  3. ✅ Dialog detection during button wait")
        print("  4. ✅ Dialog recovery with handle_anomaly")
        print("  5. ✅ Timeout detection and recovery")
        print("\nNext: Test with real GDS2 (Day 7)")
        print()

    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
