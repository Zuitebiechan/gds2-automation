"""
GDS2 Vehicle Selection Page

Represents the Vehicle Selection page after device selection.
"""

import logging
import time
from typing import TYPE_CHECKING

from .base_page import BasePage
from src.core.locators import Loc

if TYPE_CHECKING:
    from src.core.driver import GDS2Driver
    from .diagnostics_menu_page import DiagnosticsMenuPage

logger = logging.getLogger(__name__)


class VehicleSelectionPage(BasePage):
    """
    GDS2 Vehicle Selection page.

    This page appears after selecting a VCI device and shows:
    - Vehicle information (if connected)
    - "Vehicle data is loaded" banner
    - "Enter" button to proceed
    """

    @property
    def name(self) -> str:
        return "Vehicle Selection"

    def is_displayed(self) -> bool:
        """
        Check if Vehicle Selection page is displayed.

        Vehicle Selection has Enter button but NOT Module Diagnostics.
        """
        # Must have Enter button
        if not self.driver.element_exists(Loc.VehicleSelection.ENTER_BTN, timeout=0.5):
            return False

        # Must NOT have Module Diagnostics (that would be Diagnostics Menu)
        if self.driver.element_exists(Loc.DiagnosticsMenu.MODULE_DIAGNOSTICS, timeout=0.3):
            return False

        return True

    def click_enter(self) -> 'DiagnosticsMenuPage':
        """
        Click Enter to proceed to diagnostics.

        Returns:
            DiagnosticsMenuPage instance
        """
        from .diagnostics_menu_page import DiagnosticsMenuPage

        self._logger.info("Clicking Enter button...")
        self.driver.click_button(Loc.VehicleSelection.ENTER_BTN)

        # Dismiss any warning dialog
        time.sleep(0.5)
        self._dismiss_warning_dialog()

        # Wait for UI to stabilize
        self.driver.wait_for_ui_stable(timeout=10)

        return DiagnosticsMenuPage(self.driver).wait_for_page(timeout=30)

    def _dismiss_warning_dialog(self) -> bool:
        """Dismiss any warning dialog that appears."""
        self._logger.debug("Checking for warning dialog...")

        # Check main window for OK button
        if self.driver.element_exists(Loc.VehicleSelection.OK_BTN, timeout=0.5):
            self._logger.info("Dismissing warning dialog...")
            self.driver.click_button(Loc.VehicleSelection.OK_BTN)
            return True

        # Check popup windows
        try:
            from pywinauto import Desktop
            desktop = Desktop(backend="uia")
            for win in desktop.windows():
                try:
                    title = win.window_text()
                    if "Warning" in title or "GDS" in title:
                        ok_btn = win.child_window(
                            title=Loc.Common.OK_BTN.title,
                            control_type=Loc.Common.OK_BTN.control_type
                        )
                        if ok_btn.exists(timeout=0.3):
                            ok_btn.click_input()
                            self._logger.info(f"Dismissed warning: {title}")
                            return True
                except Exception:
                    continue
        except Exception:
            pass

        self._logger.debug("No warning dialog found")
        return True
