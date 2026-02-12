"""
Unit tests for AnomalyDetector.

Tests dialog detection, timeout detection, and state mismatch detection.
"""

import json
import pytest
from pathlib import Path

from src.recovery import (
    AnomalyDetector,
    AnomalyType,
    AnomalySeverity,
)


class TestAnomalyDetector:
    """Test AnomalyDetector class."""

    @pytest.fixture
    def temp_data_dir(self, tmp_path):
        """Create temporary data directory."""
        data_dir = tmp_path / "gds2-data"
        data_dir.mkdir()
        return data_dir

    @pytest.fixture
    def detector(self, temp_data_dir):
        """Create detector with temp data dir."""
        return AnomalyDetector(data_dir=temp_data_dir)

    def test_init_default_data_dir(self):
        """Test initialization with default data directory."""
        detector = AnomalyDetector()
        assert detector.data_dir == Path.home() / "gds2-data"
        assert detector.latest_json_path == Path.home() / "gds2-data" / "latest.json"

    def test_init_custom_data_dir(self, temp_data_dir):
        """Test initialization with custom data directory."""
        detector = AnomalyDetector(data_dir=temp_data_dir)
        assert detector.data_dir == temp_data_dir

    def test_check_dialog_file_not_exists(self, detector):
        """Test dialog check when latest.json doesn't exist."""
        anomaly = detector.check_unexpected_dialog()
        assert anomaly is None

    def test_check_dialog_no_modal(self, detector, temp_data_dir):
        """Test dialog check when no modal is showing."""
        # Create latest.json with no modal
        data = {
            "pageContext": {
                "isModalShowing": False,
            }
        }
        latest_path = temp_data_dir / "latest.json"
        latest_path.write_text(json.dumps(data), encoding="gbk")

        anomaly = detector.check_unexpected_dialog()
        assert anomaly is None

    def test_check_dialog_error_dialog(self, detector, temp_data_dir):
        """Test detection of error dialog."""
        data = {
            "pageContext": {
                "isModalShowing": True,
                "modalTitle": "Connection Error",
                "modalButtons": ["OK", "Cancel"],
                "windows": [],
            }
        }
        latest_path = temp_data_dir / "latest.json"
        latest_path.write_text(json.dumps(data), encoding="gbk")

        anomaly = detector.check_unexpected_dialog()

        assert anomaly is not None
        assert anomaly.type == AnomalyType.UNEXPECTED_DIALOG
        assert anomaly.severity == AnomalySeverity.HIGH  # "Error" keyword
        assert anomaly.context["modal_title"] == "Connection Error"
        assert anomaly.context["modal_buttons"] == ["OK", "Cancel"]
        assert anomaly.context["isModalShowing"] is True

    def test_check_dialog_warning_dialog(self, detector, temp_data_dir):
        """Test detection of warning dialog."""
        data = {
            "pageContext": {
                "isModalShowing": True,
                "modalTitle": "Warning: Low Battery",
                "modalButtons": ["Continue", "Cancel"],
            }
        }
        latest_path = temp_data_dir / "latest.json"
        latest_path.write_text(json.dumps(data), encoding="gbk")

        anomaly = detector.check_unexpected_dialog()

        assert anomaly.severity == AnomalySeverity.MEDIUM  # "Warning" keyword

    def test_check_dialog_critical_dialog(self, detector, temp_data_dir):
        """Test detection of critical dialog."""
        data = {
            "pageContext": {
                "isModalShowing": True,
                "modalTitle": "Fatal Exception",
                "modalButtons": ["Close"],
            }
        }
        latest_path = temp_data_dir / "latest.json"
        latest_path.write_text(json.dumps(data), encoding="gbk")

        anomaly = detector.check_unexpected_dialog()

        assert anomaly.severity == AnomalySeverity.CRITICAL  # "Fatal" keyword

    def test_check_dialog_info_dialog(self, detector, temp_data_dir):
        """Test detection of info dialog (low severity)."""
        data = {
            "pageContext": {
                "isModalShowing": True,
                "modalTitle": "Information",
                "modalButtons": ["OK"],
            }
        }
        latest_path = temp_data_dir / "latest.json"
        latest_path.write_text(json.dumps(data), encoding="gbk")

        anomaly = detector.check_unexpected_dialog()

        assert anomaly.severity == AnomalySeverity.LOW  # No keywords

    def test_check_dialog_invalid_json(self, detector, temp_data_dir):
        """Test dialog check with invalid JSON."""
        latest_path = temp_data_dir / "latest.json"
        latest_path.write_text("not valid json", encoding="gbk")

        anomaly = detector.check_unexpected_dialog()
        assert anomaly is None  # Should not crash

    def test_check_timeout_no_timeout(self, detector):
        """Test timeout check when operation is within expected time."""
        anomaly = detector.check_timeout(
            operation="test_op",
            elapsed_time=25.0,
            expected_time=30.0,
        )
        assert anomaly is None

    def test_check_timeout_timeout_detected(self, detector):
        """Test timeout detection."""
        anomaly = detector.check_timeout(
            operation="wait_for_button",
            elapsed_time=45.0,
            expected_time=30.0,
        )

        assert anomaly is not None
        assert anomaly.type == AnomalyType.TIMEOUT
        assert anomaly.severity == AnomalySeverity.MEDIUM
        assert anomaly.context["operation"] == "wait_for_button"
        assert anomaly.context["elapsed_time"] == 45.0
        assert anomaly.context["expected_time"] == 30.0
        assert anomaly.context["timeout_ratio"] == 1.5

    def test_check_timeout_custom_severity(self, detector):
        """Test timeout detection with custom severity."""
        anomaly = detector.check_timeout(
            operation="critical_op",
            elapsed_time=60.0,
            expected_time=30.0,
            severity=AnomalySeverity.HIGH,
        )

        assert anomaly.severity == AnomalySeverity.HIGH

    def test_check_state_mismatch_no_mismatch(self, detector):
        """Test state mismatch when pages match."""
        anomaly = detector.check_state_mismatch(
            expected_page="MAIN_MENU",
            actual_page="MAIN_MENU",
        )
        assert anomaly is None

    def test_check_state_mismatch_unknown_page(self, detector):
        """Test state mismatch when actual page is UNKNOWN."""
        # UNKNOWN is not treated as a mismatch
        anomaly = detector.check_state_mismatch(
            expected_page="DIAGNOSTICS_MENU",
            actual_page="UNKNOWN",
        )
        assert anomaly is None

    def test_check_state_mismatch_detected(self, detector):
        """Test state mismatch detection."""
        anomaly = detector.check_state_mismatch(
            expected_page="DIAGNOSTICS_MENU",
            actual_page="MAIN_MENU",
        )

        assert anomaly is not None
        assert anomaly.type == AnomalyType.STATE_MISMATCH
        assert anomaly.severity == AnomalySeverity.MEDIUM
        assert anomaly.context["expected_page"] == "DIAGNOSTICS_MENU"
        assert anomaly.context["actual_page"] == "MAIN_MENU"

    def test_check_state_mismatch_custom_severity(self, detector):
        """Test state mismatch with custom severity."""
        anomaly = detector.check_state_mismatch(
            expected_page="MODULE_LIST",
            actual_page="DEVICE_EXPLORER",
            severity=AnomalySeverity.LOW,
        )

        assert anomaly.severity == AnomalySeverity.LOW

    def test_has_dialog_true(self, detector, temp_data_dir):
        """Test has_dialog returns True when dialog present."""
        data = {
            "pageContext": {
                "isModalShowing": True,
                "modalTitle": "Test Dialog",
                "modalButtons": ["OK"],
            }
        }
        latest_path = temp_data_dir / "latest.json"
        latest_path.write_text(json.dumps(data), encoding="gbk")

        assert detector.has_dialog() is True

    def test_has_dialog_false(self, detector, temp_data_dir):
        """Test has_dialog returns False when no dialog."""
        data = {
            "pageContext": {
                "isModalShowing": False,
            }
        }
        latest_path = temp_data_dir / "latest.json"
        latest_path.write_text(json.dumps(data), encoding="gbk")

        assert detector.has_dialog() is False

    def test_classify_dialog_severity(self, detector):
        """Test dialog severity classification."""
        # Critical
        assert detector._classify_dialog_severity("Fatal Error") == AnomalySeverity.CRITICAL
        assert detector._classify_dialog_severity("System Crash") == AnomalySeverity.CRITICAL

        # High
        assert detector._classify_dialog_severity("Connection Error") == AnomalySeverity.HIGH
        assert detector._classify_dialog_severity("Operation Failed") == AnomalySeverity.HIGH

        # Medium
        assert detector._classify_dialog_severity("Warning: Low Battery") == AnomalySeverity.MEDIUM

        # Low
        assert detector._classify_dialog_severity("Information") == AnomalySeverity.LOW
        assert detector._classify_dialog_severity("About") == AnomalySeverity.LOW
