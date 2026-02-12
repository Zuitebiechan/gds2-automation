"""
Anomaly Detector - Fast rule-based anomaly detection.

This module detects anomalies WITHOUT using AI:
- Unexpected dialogs (via latest.json)
- Operation timeouts (via elapsed time)
- State mismatches (via page detection)

All detection is fast (<1ms) and deterministic.
"""

import json
import logging
from pathlib import Path
from typing import Optional

from .types import Anomaly, AnomalyType, AnomalySeverity

logger = logging.getLogger(__name__)


class AnomalyDetector:
    """
    Fast rule-based anomaly detector.

    Detects anomalies using:
    - latest.json (Java Agent data)
    - Elapsed time vs expected time
    - Page state comparisons

    Does NOT call LLM - detection is purely rule-based.
    """

    def __init__(self, data_dir: Optional[Path] = None):
        """
        Initialize anomaly detector.

        Args:
            data_dir: Directory containing latest.json (default: ~/gds2-data)
        """
        self.data_dir = data_dir or (Path.home() / "gds2-data")
        self.latest_json_path = self.data_dir / "latest.json"

    def check_unexpected_dialog(self) -> Optional[Anomaly]:
        """
        Check for unexpected modal dialogs via latest.json.

        Uses Java Agent's pageContext.isModalShowing field to detect dialogs.

        Returns:
            Anomaly if dialog detected, None otherwise
        """
        if not self.latest_json_path.exists():
            logger.debug("latest.json not found, cannot check for dialogs")
            return None

        try:
            # Read latest.json (GBK encoding for GDS2)
            data = json.loads(self.latest_json_path.read_text(encoding="gbk"))
            page_context = data.get("pageContext", {})

            # Check if modal dialog is showing
            if page_context.get("isModalShowing"):
                modal_title = page_context.get("modalTitle", "Unknown")
                modal_buttons = page_context.get("modalButtons", [])

                # Determine severity based on dialog title
                severity = self._classify_dialog_severity(modal_title)

                return Anomaly(
                    type=AnomalyType.UNEXPECTED_DIALOG,
                    severity=severity,
                    context={
                        "modal_title": modal_title,
                        "modal_buttons": modal_buttons,
                        "isModalShowing": True,
                        "windows": page_context.get("windows", []),
                    },
                )

        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning(f"Failed to parse latest.json: {e}")
        except Exception as e:
            logger.error(f"Error checking for dialog: {e}", exc_info=True)

        return None

    def check_timeout(
        self,
        operation: str,
        elapsed_time: float,
        expected_time: float,
        severity: AnomalySeverity = AnomalySeverity.MEDIUM,
    ) -> Optional[Anomaly]:
        """
        Check if an operation has timed out.

        A timeout is detected when elapsed_time > expected_time.

        Args:
            operation: Name of the operation (e.g., "wait_for_button")
            elapsed_time: Actual elapsed time in seconds
            expected_time: Expected time in seconds
            severity: Severity level (default: MEDIUM)

        Returns:
            Anomaly if timeout detected, None otherwise
        """
        if elapsed_time <= expected_time:
            return None

        timeout_ratio = elapsed_time / expected_time if expected_time > 0 else 1.0

        return Anomaly(
            type=AnomalyType.TIMEOUT,
            severity=severity,
            context={
                "operation": operation,
                "elapsed_time": elapsed_time,
                "expected_time": expected_time,
                "timeout_ratio": timeout_ratio,
            },
        )

    def check_state_mismatch(
        self,
        expected_page: str,
        actual_page: str,
        severity: AnomalySeverity = AnomalySeverity.MEDIUM,
    ) -> Optional[Anomaly]:
        """
        Check if current page state doesn't match expectation.

        Args:
            expected_page: Expected page (e.g., "DIAGNOSTICS_MENU")
            actual_page: Actual current page (e.g., "MAIN_MENU")
            severity: Severity level (default: MEDIUM)

        Returns:
            Anomaly if mismatch detected, None otherwise
        """
        if expected_page == actual_page:
            return None

        # UNKNOWN page is not a mismatch (just means detection failed)
        if actual_page == "UNKNOWN":
            logger.debug("Page detection returned UNKNOWN, not treating as mismatch")
            return None

        return Anomaly(
            type=AnomalyType.STATE_MISMATCH,
            severity=severity,
            context={
                "expected_page": expected_page,
                "actual_page": actual_page,
            },
        )

    def _classify_dialog_severity(self, dialog_title: str) -> AnomalySeverity:
        """
        Classify dialog severity based on title.

        Args:
            dialog_title: Dialog window title

        Returns:
            AnomalySeverity level
        """
        title_lower = dialog_title.lower()

        # Critical errors
        critical_keywords = ["fatal", "crash", "exception", "corrupt"]
        if any(kw in title_lower for kw in critical_keywords):
            return AnomalySeverity.CRITICAL

        # High severity errors
        error_keywords = ["error", "failed", "failure"]
        if any(kw in title_lower for kw in error_keywords):
            return AnomalySeverity.HIGH

        # Warnings
        warning_keywords = ["warning", "caution"]
        if any(kw in title_lower for kw in warning_keywords):
            return AnomalySeverity.MEDIUM

        # Default: info/unknown dialogs
        return AnomalySeverity.LOW

    def has_dialog(self) -> bool:
        """
        Quick check: is any dialog currently showing?

        Returns:
            True if dialog is showing, False otherwise
        """
        anomaly = self.check_unexpected_dialog()
        return anomaly is not None
