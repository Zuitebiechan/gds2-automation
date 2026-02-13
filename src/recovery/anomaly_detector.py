"""
Anomaly Detector - Fast rule-based anomaly detection.

This module detects anomalies WITHOUT using AI:
- Unexpected dialogs (via latest.json + Win32 API fallback)
- Operation timeouts (via elapsed time)
- State mismatches (via page detection)

All detection is fast (<1ms) and deterministic.
"""

import ctypes
from ctypes import wintypes
import json
import logging
from pathlib import Path
from typing import Optional, List, Tuple

from .types import Anomaly, AnomalyType, AnomalySeverity

logger = logging.getLogger(__name__)

# Win32 API for native dialog detection
try:
    user32 = ctypes.windll.user32
    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    EnumChildProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _WIN32_AVAILABLE = True
except Exception:
    _WIN32_AVAILABLE = False


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
        Check for unexpected modal dialogs.

        Detection order:
        1. latest.json (Java Agent pageContext.isModalShowing)
        2. Win32 API fallback (for native/Swing dialogs invisible to Java Agent)

        Returns:
            Anomaly if dialog detected, None otherwise
        """
        # Method 1: Check latest.json (Java Agent)
        anomaly = self._check_dialog_from_json()
        if anomaly:
            return anomaly

        # Method 2: Win32 API fallback
        return self._check_dialog_from_win32()

    def _check_dialog_from_json(self) -> Optional[Anomaly]:
        """Check for dialogs via Java Agent's latest.json."""
        if not self.latest_json_path.exists():
            logger.debug("latest.json not found, cannot check for dialogs")
            return None

        try:
            data = json.loads(self.latest_json_path.read_text(encoding="gbk"))
            page_context = data.get("pageContext", {})

            if page_context.get("isModalShowing"):
                modal_title = page_context.get("modalTitle", "Unknown")
                modal_buttons = page_context.get("modalButtons", [])
                modal_message = page_context.get("modalMessage", "")
                severity = self._classify_dialog_severity(modal_title)

                return Anomaly(
                    type=AnomalyType.UNEXPECTED_DIALOG,
                    severity=severity,
                    context={
                        "modal_title": modal_title,
                        "modal_buttons": modal_buttons,
                        "modal_message": modal_message,
                        "isModalShowing": True,
                        "source": "java_agent",
                    },
                )

        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning(f"Failed to parse latest.json: {e}")
        except Exception as e:
            logger.error(f"Error checking for dialog: {e}", exc_info=True)

        return None

    def _check_dialog_from_win32(self) -> Optional[Anomaly]:
        """
        Check for GDS2 error dialogs using Win32 API.

        Detects native/Swing dialogs that the Java Agent cannot see.
        Strategy: find multiple visible windows titled "GDS 2" —
        the extra one (with an OK button child) is an error dialog.
        """
        if not _WIN32_AVAILABLE:
            return None

        try:
            gds2_windows = self._find_gds2_windows()

            if len(gds2_windows) < 2:
                return None

            # Multiple "GDS 2" windows — check which one is a dialog
            for hwnd, title in gds2_windows:
                buttons = self._get_child_button_texts(hwnd)
                # A dialog window has simple buttons like OK, Cancel, Yes, No
                dialog_buttons = {"OK", "Cancel", "Yes", "No", "Retry", "Abort", "Close"}
                if buttons and any(b in dialog_buttons for b in buttons):
                    logger.info(
                        f"Win32 detected GDS2 dialog: HWND={hwnd}, "
                        f"title='{title}', buttons={buttons}"
                    )

                    severity = self._classify_dialog_severity(title)

                    return Anomaly(
                        type=AnomalyType.UNEXPECTED_DIALOG,
                        severity=severity,
                        context={
                            "modal_title": title,
                            "modal_buttons": list(buttons),
                            "isModalShowing": True,
                            "source": "win32",
                            "hwnd": hwnd,
                        },
                    )

        except Exception as e:
            logger.debug(f"Win32 dialog check failed: {e}")

        return None

    def _find_gds2_windows(self) -> List[Tuple[int, str]]:
        """Find all visible windows with 'GDS 2' in the title."""
        results = []

        def callback(hwnd, lparam):
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd) + 1
                buf = ctypes.create_unicode_buffer(length)
                user32.GetWindowTextW(hwnd, buf, length)
                if buf.value == "GDS 2":
                    results.append((hwnd, buf.value))
            return True

        user32.EnumWindows(EnumWindowsProc(callback), 0)
        return results

    def _get_child_button_texts(self, hwnd) -> List[str]:
        """Get text of all Button child controls of a window."""
        buttons = []

        def callback(child_hwnd, lparam):
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(child_hwnd, cls, 256)
            if cls.value == "Button":
                length = user32.GetWindowTextLengthW(child_hwnd) + 1
                txt = ctypes.create_unicode_buffer(length)
                user32.GetWindowTextW(child_hwnd, txt, length)
                if txt.value:
                    buttons.append(txt.value)
            return True

        user32.EnumChildWindows(hwnd, EnumChildProc(callback), 0)
        return buttons

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
