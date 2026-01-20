"""
GDS2 Vehicle Diagnostics Page

Represents the Vehicle Diagnostics submenu.
"""

import logging
import time
from typing import TYPE_CHECKING

from .base_page import BasePage
from src.core.locators import Loc

if TYPE_CHECKING:
    from src.core.driver import GDS2Driver
    from .dtc_page import DTCPage

logger = logging.getLogger(__name__)


class VehicleDiagnosticsPage(BasePage):
    """
    GDS2 Vehicle Diagnostics submenu.

    Contains items:
    - Supported Modules
    - Vehicle DTC Information
    - Clear Vehicle DTCs
    """

    @property
    def name(self) -> str:
        return "Vehicle Diagnostics"

    def is_displayed(self) -> bool:
        """Check if Vehicle Diagnostics submenu is displayed."""
        return self.driver.element_exists(Loc.VehicleDiagnostics.VEHICLE_DTC_INFO)

    def select_vehicle_dtc_info(self) -> 'DTCPage':
        """
        Select Vehicle DTC Information.

        Returns:
            DTCPage instance
        """
        from .dtc_page import DTCPage

        self._logger.info("Selecting Vehicle DTC Information...")

        # Click the list item
        self.driver.click_list_item(Loc.VehicleDiagnostics.VEHICLE_DTC_INFO)
        time.sleep(0.3)

        # Click Enter button
        if self.driver.element_exists(Loc.VehicleDiagnostics.ENTER_BTN, timeout=3):
            self.driver.click_button(Loc.VehicleDiagnostics.ENTER_BTN)

        return DTCPage(self.driver).wait_for_page(timeout=30)

    def select_clear_vehicle_dtcs(self) -> 'BasePage':
        """
        Select Clear Vehicle DTCs.

        Returns:
            Clear DTCs confirmation page (not yet implemented)
        """
        self._logger.info("Selecting Clear Vehicle DTCs...")

        self.driver.click_list_item(Loc.VehicleDiagnostics.CLEAR_VEHICLE_DTCS)
        time.sleep(0.3)

        if self.driver.element_exists(Loc.VehicleDiagnostics.ENTER_BTN, timeout=3):
            self.driver.click_button(Loc.VehicleDiagnostics.ENTER_BTN)

        # Return self for now - ClearDTCPage not implemented
        return self
