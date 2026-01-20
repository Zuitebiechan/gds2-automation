"""
GDS2 DTC Information Page

Represents the Vehicle DTC Information page.
"""

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .base_page import BasePage
from src.core.locators import Loc

if TYPE_CHECKING:
    from src.core.driver import GDS2Driver

logger = logging.getLogger(__name__)


class DTCPage(BasePage):
    """
    GDS2 Vehicle DTC Information page.

    This page displays DTC codes and allows:
    - Viewing DTC list
    - Creating HTML report
    - Clearing DTCs
    """

    @property
    def name(self) -> str:
        return "DTC Information"

    def is_displayed(self) -> bool:
        """Check if DTC Information page is displayed."""
        return self.driver.element_exists(Loc.DTCPage.CLEAR_DTCS_BTN, timeout=0.5)

    def wait_for_data_loaded(self, timeout: float = 120) -> 'DTCPage':
        """
        Wait for DTC data to load.

        Data is loaded when "Clear DTCs" button becomes enabled.

        Args:
            timeout: Maximum wait time

        Returns:
            Self for fluent chaining
        """
        self._logger.info("Waiting for DTC data to load...")
        self.driver.wait_for_element_enabled(
            Loc.DTCPage.CLEAR_DTCS_BTN,
            timeout=timeout,
        )
        return self

    def click_create_report(self) -> 'DTCPage':
        """
        Click Create Report button.

        Returns:
            Self for fluent chaining
        """
        self._logger.info("Clicking Create Report button...")
        self.driver.click_button(Loc.DTCPage.CREATE_REPORT_BTN)
        return self

    def click_clear_dtcs(self) -> 'DTCPage':
        """
        Click Clear DTCs button.

        Returns:
            Self for fluent chaining
        """
        self._logger.info("Clicking Clear DTCs button...")
        self.driver.click_button(Loc.DTCPage.CLEAR_DTCS_BTN)
        return self

    def is_clear_dtcs_enabled(self) -> bool:
        """Check if Clear DTCs button is enabled (data loaded)."""
        try:
            element = self.driver.find_element(Loc.DTCPage.CLEAR_DTCS_BTN, timeout=1)
            return element.is_enabled()
        except Exception:
            return False

    def get_report_dir(self) -> Path:
        """Get the directory where GDS2 saves reports."""
        import os
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        return Path(local_app_data) / "Temp" / "GDS 2"

    def find_latest_report(self, prefix: str = "DTC Display") -> Optional[Path]:
        """
        Find the latest report file.

        Args:
            prefix: Report filename prefix

        Returns:
            Path to latest report or None
        """
        report_dir = self.get_report_dir()
        if not report_dir.exists():
            return None

        reports = list(report_dir.glob(f"{prefix}*.html"))
        if not reports:
            return None

        # Sort by modification time
        reports.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return reports[0]

    def create_report_and_get_path(self, timeout: float = 30) -> Optional[Path]:
        """
        Click Create Report and wait for the file.

        Args:
            timeout: Maximum wait time for file

        Returns:
            Path to report file or None
        """
        # Get existing reports before clicking
        existing = set(self.get_report_dir().glob("DTC Display*.html"))

        # Click Create Report
        self.click_create_report()

        # Wait for new report file
        start = time.time()
        while time.time() - start < timeout:
            current = set(self.get_report_dir().glob("DTC Display*.html"))
            new_reports = current - existing
            if new_reports:
                report_path = list(new_reports)[0]
                logger.info(f"Report created: {report_path}")
                return report_path
            time.sleep(1)

        # Fallback to latest report
        logger.warning("New report not detected, using latest")
        return self.find_latest_report()
