"""
GDS2 Diagnostics Menu Page

Represents the main Diagnostics Menu screen.
"""

import logging
import time
from typing import TYPE_CHECKING

from .base_page import BasePage
from src.core.locators import Loc

if TYPE_CHECKING:
    from src.core.driver import GDS2Driver
    from .vehicle_diagnostics_page import VehicleDiagnosticsPage

logger = logging.getLogger(__name__)


class DiagnosticsMenuPage(BasePage):
    """
    GDS2 Diagnostics Menu page.

    Contains menu items:
    - Module Diagnostics
    - Vehicle Diagnostics
    - System Diagnostics
    - Session Manager
    """

    @property
    def name(self) -> str:
        return "Diagnostics Menu"

    def is_displayed(self) -> bool:
        """Check if Diagnostics Menu is displayed."""
        return self.driver.element_exists(Loc.DiagnosticsMenu.MODULE_DIAGNOSTICS)

    def select_vehicle_diagnostics(self) -> 'VehicleDiagnosticsPage':
        """
        Select Vehicle Diagnostics from menu.

        Returns:
            VehicleDiagnosticsPage instance
        """
        from .vehicle_diagnostics_page import VehicleDiagnosticsPage

        self._logger.info("Selecting Vehicle Diagnostics...")

        # Click Vehicle Diagnostics list item
        self.driver.click_list_item(Loc.DiagnosticsMenu.VEHICLE_DIAGNOSTICS)
        time.sleep(0.5)

        # Click Enter if still at menu (sometimes click enters directly)
        if self.driver.element_exists(Loc.DiagnosticsMenu.MODULE_DIAGNOSTICS, timeout=0.5):
            self.driver.click_button(Loc.DiagnosticsMenu.ENTER_BTN)

        return VehicleDiagnosticsPage(self.driver).wait_for_page(timeout=15)

    def select_module_diagnostics(self) -> 'BasePage':
        """
        Select Module Diagnostics from menu.

        Returns:
            Module Diagnostics page (not yet implemented)
        """
        self._logger.info("Selecting Module Diagnostics...")
        self.driver.click_list_item(Loc.DiagnosticsMenu.MODULE_DIAGNOSTICS)
        time.sleep(0.5)
        self.driver.click_button(Loc.DiagnosticsMenu.ENTER_BTN)
        # Return self for now - ModuleDiagnosticsPage not implemented
        return self
