"""
Unit tests for recovery data types.

Tests all data classes and enums used in the recovery system.
"""

import pytest
import time
from src.recovery.types import (
    Anomaly,
    AnomalyType,
    AnomalySeverity,
    RecoveryAction,
    RecoveryDecision,
    RecoveryResult,
    OperationContext,
)


class TestAnomalyType:
    """Test AnomalyType enum."""

    def test_all_types_defined(self):
        """Test that all expected anomaly types are defined."""
        assert hasattr(AnomalyType, "UNEXPECTED_DIALOG")
        assert hasattr(AnomalyType, "TIMEOUT")
        assert hasattr(AnomalyType, "STATE_MISMATCH")
        assert hasattr(AnomalyType, "UNKNOWN")


class TestAnomalySeverity:
    """Test AnomalySeverity enum."""

    def test_all_severities_defined(self):
        """Test that all severity levels are defined."""
        assert hasattr(AnomalySeverity, "LOW")
        assert hasattr(AnomalySeverity, "MEDIUM")
        assert hasattr(AnomalySeverity, "HIGH")
        assert hasattr(AnomalySeverity, "CRITICAL")


class TestRecoveryAction:
    """Test RecoveryAction enum."""

    def test_all_actions_defined(self):
        """Test that all recovery actions are defined."""
        assert hasattr(RecoveryAction, "CLICK_BUTTON")
        assert hasattr(RecoveryAction, "WAIT_LONGER")
        assert hasattr(RecoveryAction, "GO_BACK")
        assert hasattr(RecoveryAction, "RETRY_FROM_START")
        assert hasattr(RecoveryAction, "DISMISS_AND_NAVIGATE")
        assert hasattr(RecoveryAction, "ABORT")


class TestAnomaly:
    """Test Anomaly dataclass."""

    def test_create_minimal_anomaly(self):
        """Test creating anomaly with minimal required fields."""
        anomaly = Anomaly(type=AnomalyType.TIMEOUT)

        assert anomaly.type == AnomalyType.TIMEOUT
        assert anomaly.severity == AnomalySeverity.MEDIUM  # Default
        assert anomaly.context == {}
        assert isinstance(anomaly.timestamp, float)

    def test_create_full_anomaly(self):
        """Test creating anomaly with all fields."""
        context = {"operation": "connect_device", "elapsed": 45.0}
        timestamp = time.time()

        anomaly = Anomaly(
            type=AnomalyType.TIMEOUT,
            severity=AnomalySeverity.HIGH,
            context=context,
            timestamp=timestamp,
        )

        assert anomaly.type == AnomalyType.TIMEOUT
        assert anomaly.severity == AnomalySeverity.HIGH
        assert anomaly.context == context
        assert anomaly.timestamp == timestamp

    def test_anomaly_str(self):
        """Test string representation of anomaly."""
        anomaly = Anomaly(
            type=AnomalyType.UNEXPECTED_DIALOG,
            severity=AnomalySeverity.CRITICAL,
            timestamp=1234567890.0,
        )

        str_repr = str(anomaly)
        assert "UNEXPECTED_DIALOG" in str_repr
        assert "CRITICAL" in str_repr
        assert "1234567890" in str_repr


class TestRecoveryDecision:
    """Test RecoveryDecision dataclass."""

    def test_create_minimal_decision(self):
        """Test creating decision with minimal fields."""
        decision = RecoveryDecision(
            action=RecoveryAction.CLICK_BUTTON,
            confidence=0.9,
            reasoning="Click OK to dismiss error dialog",
        )

        assert decision.action == RecoveryAction.CLICK_BUTTON
        assert decision.confidence == 0.9
        assert decision.reasoning == "Click OK to dismiss error dialog"
        assert decision.parameters == {}
        assert decision.estimated_time == 30.0  # Default

    def test_create_full_decision(self):
        """Test creating decision with all fields."""
        decision = RecoveryDecision(
            action=RecoveryAction.WAIT_LONGER,
            confidence=0.85,
            reasoning="VCI connection takes longer",
            parameters={"wait_seconds": 60},
            estimated_time=60.0,
        )

        assert decision.action == RecoveryAction.WAIT_LONGER
        assert decision.parameters == {"wait_seconds": 60}
        assert decision.estimated_time == 60.0

    def test_confidence_validation_valid(self):
        """Test that valid confidence values are accepted."""
        # Test boundary values
        RecoveryDecision(action=RecoveryAction.ABORT, confidence=0.0, reasoning="test")
        RecoveryDecision(action=RecoveryAction.ABORT, confidence=1.0, reasoning="test")
        RecoveryDecision(action=RecoveryAction.ABORT, confidence=0.5, reasoning="test")

    def test_confidence_validation_invalid_high(self):
        """Test that confidence > 1.0 raises error."""
        with pytest.raises(ValueError, match="Confidence must be between 0.0 and 1.0"):
            RecoveryDecision(
                action=RecoveryAction.ABORT,
                confidence=1.5,
                reasoning="test",
            )

    def test_confidence_validation_invalid_low(self):
        """Test that confidence < 0.0 raises error."""
        with pytest.raises(ValueError, match="Confidence must be between 0.0 and 1.0"):
            RecoveryDecision(
                action=RecoveryAction.ABORT,
                confidence=-0.1,
                reasoning="test",
            )

    def test_decision_str(self):
        """Test string representation of decision."""
        decision = RecoveryDecision(
            action=RecoveryAction.CLICK_BUTTON,
            confidence=0.95,
            reasoning="Clear error dialog",
        )

        str_repr = str(decision)
        assert "CLICK_BUTTON" in str_repr
        assert "0.95" in str_repr
        assert "Clear error dialog" in str_repr


class TestRecoveryResult:
    """Test RecoveryResult dataclass."""

    def test_create_success_result(self):
        """Test creating successful recovery result."""
        result = RecoveryResult(
            success=True,
            action=RecoveryAction.CLICK_BUTTON,
            attempts=1,
            elapsed_time=2.5,
            new_state="DIAGNOSTICS_MENU",
        )

        assert result.success is True
        assert result.action == RecoveryAction.CLICK_BUTTON
        assert result.attempts == 1
        assert result.elapsed_time == 2.5
        assert result.error is None
        assert result.new_state == "DIAGNOSTICS_MENU"

    def test_create_failure_result(self):
        """Test creating failed recovery result."""
        result = RecoveryResult(
            success=False,
            action=RecoveryAction.GO_BACK,
            attempts=3,
            elapsed_time=15.2,
            error="Navigation failed after 3 attempts",
        )

        assert result.success is False
        assert result.attempts == 3
        assert result.error == "Navigation failed after 3 attempts"
        assert result.new_state is None

    def test_result_str_success(self):
        """Test string representation of successful result."""
        result = RecoveryResult(
            success=True,
            action=RecoveryAction.WAIT_LONGER,
            attempts=2,
            elapsed_time=45.3,
        )

        str_repr = str(result)
        assert "SUCCESS" in str_repr
        assert "WAIT_LONGER" in str_repr
        assert "2 attempts" in str_repr
        assert "45.3" in str_repr

    def test_result_str_failure(self):
        """Test string representation of failed result."""
        result = RecoveryResult(
            success=False,
            action=RecoveryAction.RETRY_FROM_START,
            attempts=1,
            elapsed_time=10.0,
        )

        str_repr = str(result)
        assert "FAILED" in str_repr
        assert "RETRY_FROM_START" in str_repr


class TestOperationContext:
    """Test OperationContext dataclass."""

    def test_create_minimal_context(self):
        """Test creating context with minimal fields."""
        context = OperationContext(operation_name="connect_device")

        assert context.operation_name == "connect_device"
        assert context.current_page == "UNKNOWN"
        assert context.visible_buttons == []
        assert context.recent_actions == []
        assert context.elapsed_time == 0.0
        assert context.expected_time == 30.0
        assert context.additional_info == {}

    def test_create_full_context(self):
        """Test creating context with all fields."""
        context = OperationContext(
            operation_name="wait_for_enter",
            current_page="DEVICE_EXPLORER",
            visible_buttons=["Enter", "Disconnect"],
            recent_actions=["open_device_explorer", "select_device"],
            elapsed_time=45.0,
            expected_time=30.0,
            additional_info={"device_name": "SM2 USB"},
        )

        assert context.operation_name == "wait_for_enter"
        assert context.current_page == "DEVICE_EXPLORER"
        assert context.visible_buttons == ["Enter", "Disconnect"]
        assert context.recent_actions == ["open_device_explorer", "select_device"]
        assert context.elapsed_time == 45.0
        assert context.expected_time == 30.0
        assert context.additional_info == {"device_name": "SM2 USB"}

    def test_to_dict(self):
        """Test converting context to dictionary."""
        context = OperationContext(
            operation_name="test_operation",
            current_page="TEST_PAGE",
            visible_buttons=["Button1", "Button2"],
            recent_actions=["action1", "action2", "action3"],
            elapsed_time=10.0,
            expected_time=5.0,
            additional_info={"key1": "value1", "key2": 123},
        )

        result_dict = context.to_dict()

        assert result_dict["operation"] == "test_operation"
        assert result_dict["current_page"] == "TEST_PAGE"
        assert result_dict["visible_buttons"] == ["Button1", "Button2"]
        assert result_dict["recent_actions"] == ["action1", "action2", "action3"]
        assert result_dict["elapsed_time"] == 10.0
        assert result_dict["expected_time"] == 5.0
        assert result_dict["key1"] == "value1"
        assert result_dict["key2"] == 123

    def test_to_dict_limits_recent_actions(self):
        """Test that to_dict only includes last 5 recent actions."""
        context = OperationContext(
            operation_name="test",
            recent_actions=["a1", "a2", "a3", "a4", "a5", "a6", "a7"],
        )

        result_dict = context.to_dict()

        # Should only include last 5 actions
        assert len(result_dict["recent_actions"]) == 5
        assert result_dict["recent_actions"] == ["a3", "a4", "a5", "a6", "a7"]
