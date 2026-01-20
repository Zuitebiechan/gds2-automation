"""
GDS2 Device Explorer Page

Represents the Device Explorer popup window for VCI device selection.
"""

import logging
import time
from typing import TYPE_CHECKING

from .base_page import BasePage
from src.core.locators import Loc

if TYPE_CHECKING:
    from src.core.driver import GDS2Driver
    from .vehicle_selection_page import VehicleSelectionPage

logger = logging.getLogger(__name__)


class DeviceExplorerPage(BasePage):
    """
    GDS2 Device Explorer popup.

    This popup appears after clicking Diagnostics to select VCI device.
    Note: This is a separate popup window, not the main GDS2 window.
    """

    @property
    def name(self) -> str:
        return "Device Explorer"

    def is_displayed(self) -> bool:
        """Check if Device Explorer popup is displayed."""
        try:
            from pywinauto import Desktop
            desktop = Desktop(backend="uia")
            for win in desktop.windows():
                try:
                    if "Device Explorer" in win.window_text():
                        return True
                except Exception:
                    pass
        except Exception:
            pass
        return False

    def _get_popup_window(self):
        """Get the Device Explorer popup window."""
        from pywinauto import Desktop, Application
        desktop = Desktop(backend="uia")

        # First, try to find by title
        for win in desktop.windows():
            try:
                title = win.window_text()
                if "Device Explorer" in title or "Select" in title:
                    handle = win.handle
                    app = Application(backend="uia").connect(handle=handle)
                    return app.window(handle=handle)
            except Exception:
                pass

        # Try finding by Continue button
        for win in desktop.windows():
            try:
                handle = win.handle
                app = Application(backend="uia").connect(handle=handle)
                window = app.window(handle=handle)
                continue_btn = window.child_window(
                    title=Loc.DeviceExplorer.CONTINUE_BTN.title,
                    control_type=Loc.DeviceExplorer.CONTINUE_BTN.control_type
                )
                if continue_btn.exists(timeout=0.3):
                    return window
            except Exception:
                pass

        return None

    def select_device(self, device_name: str) -> 'VehicleSelectionPage':
        """
        Select VCI device and continue.

        Args:
            device_name: Device name (e.g., "SM2 USB")

        Returns:
            VehicleSelectionPage instance
        """
        from .vehicle_selection_page import VehicleSelectionPage

        self._logger.info(f"Selecting VCI device: {device_name}")

        popup = self._get_popup_window()
        if not popup:
            self._logger.warning("Device Explorer popup not found, continuing...")
            return VehicleSelectionPage(self.driver).wait_for_page()

        # Find and click the device
        try:
            for control_type in ["DataItem", "ListItem", "Text"]:
                try:
                    items = list(popup.descendants(control_type=control_type))
                    for item in items:
                        try:
                            text = item.window_text()
                            if device_name in text:
                                item.click_input()
                                self._logger.info(f"Selected device: {text}")
                                time.sleep(0.5)
                                break
                        except Exception:
                            continue
                except Exception:
                    continue

            # Click Continue button
            continue_btn = popup.child_window(
                title=Loc.DeviceExplorer.CONTINUE_BTN.title,
                control_type=Loc.DeviceExplorer.CONTINUE_BTN.control_type
            )
            if continue_btn.exists(timeout=2):
                continue_btn.click_input()
                self._logger.info("Clicked Continue button")

        except Exception as e:
            self._logger.error(f"Error selecting device: {e}")

        return VehicleSelectionPage(self.driver).wait_for_page()
