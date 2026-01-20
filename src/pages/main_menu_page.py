"""
GDS2 Main Menu Page

Represents the main menu screen of GDS2.
"""

import logging
from typing import TYPE_CHECKING

from .base_page import BasePage
from src.core.locators import Loc

if TYPE_CHECKING:
    from src.core.driver import GDS2Driver
    from .device_explorer_page import DeviceExplorerPage

logger = logging.getLogger(__name__)


class MainMenuPage(BasePage):
    """
    GDS2 Main Menu page.

    This is the starting point for most workflows.
    Contains buttons for Diagnostics, Programming, etc.
    """

    @property
    def name(self) -> str:
        return "Main Menu"

    def is_displayed(self) -> bool:
        """Check if Main Menu is displayed by looking for Diagnostics button."""
        return self.driver.element_exists(Loc.MainMenu.DIAGNOSTICS_BTN)

    def click_diagnostics(self) -> 'DeviceExplorerPage':
        """
        Click Diagnostics button.

        Returns:
            DeviceExplorerPage instance
        """
        from .device_explorer_page import DeviceExplorerPage

        self._logger.info("Clicking Diagnostics button...")
        self.driver.click_button(Loc.MainMenu.DIAGNOSTICS_BTN)

        return DeviceExplorerPage(self.driver).wait_for_page(timeout=10)

    def has_home_button(self) -> bool:
        """Check if Home button is present (indicates we're not at main menu)."""
        return self.driver.element_exists(Loc.MainMenu.HOME_BTN, timeout=0.5)

    def click_home(self) -> 'MainMenuPage':
        """
        Click Home button to return to main menu.

        Returns:
            MainMenuPage instance (self)
        """
        self._logger.info("Clicking Home button...")
        self.driver.click_button(Loc.MainMenu.HOME_BTN)
        self.wait_for_page()
        return self

    def click_back(self) -> 'MainMenuPage':
        """
        Click Back button.

        Returns:
            Self (may or may not be at main menu)
        """
        if self.driver.element_exists(Loc.MainMenu.BACK_BTN, timeout=0.5):
            self.driver.click_button(Loc.MainMenu.BACK_BTN)
        return self
