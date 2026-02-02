"""
Read Data Display Workflow - Full Agent Version

Uses Java Agent for GDS2 main window navigation.
Uses Windows API for Device Explorer dialog (Win32 native).

NO PyAutoGUI or OpenCV needed. NO foreground/resolution requirements.

Benefits:
- Java Agent: Direct JavaFX API calls (~1ms), supports invisible list items
- Windows API: Direct Win32 message control for Device Explorer
- Works in background, no screen dependency
"""

import logging
import time
from typing import Dict, Any

from ..streaming import AgentNavigator
from ..native import handle_device_explorer

logger = logging.getLogger(__name__)


class ReadDataDisplayAgentWorkflow:
    """
    Workflow: Navigate to Data Display using Java Agent + Windows API.

    No PyAutoGUI, OpenCV, or screen dependency:
    - Java Agent for GDS2 main window (buttons, lists)
    - Windows API for Device Explorer dialog (Win32 native controls)
    """

    def __init__(self, vehicle_id: str = "current_vehicle"):
        """Initialize workflow."""
        self.vehicle_id = vehicle_id
        self.nav = AgentNavigator(timeout_sec=15.0)
        self._current_module = None

    @property
    def name(self) -> str:
        return "Read Data Display (Agent)"

    @property
    def description(self) -> str:
        return "Navigate to Data Display using Java Agent for fast, reliable navigation"

    def execute(
        self,
        target_module: str,
        data_category: str,
        vci_device: str = "SM2 USB",
        **kwargs,
    ) -> Dict[str, Any]:
        """
        Execute the workflow: Navigate from Main Menu to Data Display page.

        Args:
            target_module: Module to select (e.g., "[K20] Engine Control Module")
            data_category: Data category to select (e.g., "Engine Data")
            vci_device: VCI device name for Device Explorer

        Returns:
            Dictionary with success status
        """
        logger.info(f"=== Starting {self.name} Workflow ===")
        logger.info(f"Target Module: {target_module}")
        logger.info(f"Data Category: {data_category}")

        self._current_module = target_module

        try:
            # Step 1: Check Agent connection
            if not self._check_agent():
                return self._error_result("Java Agent not available. Start GDS2 with agent.")

            # Step 2: Verify at Main Menu and click Diagnostics
            if not self._click_diagnostics():
                return self._error_result("Could not click Diagnostics button")

            # Step 3: Handle Device Explorer popup (Windows API - Win32 native)
            self._handle_device_explorer(vci_device)

            # Step 4: Click Enter for Vehicle Selection (Java Agent)
            if not self._click_enter():
                return self._error_result("Could not click Enter button")

            # Step 5: Handle warning dialog if present
            self._dismiss_warning_dialog()

            # Step 6: Select Module Diagnostics
            if not self._select_module_diagnostics():
                return self._error_result("Could not select Module Diagnostics")

            # Step 7: Select target module
            if not self._select_module(target_module):
                return self._error_result(f"Could not select module: {target_module}")

            # Step 8: Select Data Display
            if not self._select_data_display():
                return self._error_result("Could not select Data Display")

            # Step 9: Handle warning dialog if present
            self._dismiss_warning_dialog()

            # Step 10: Select data category
            if not self._select_data_category(data_category):
                return self._error_result(f"Could not select data category: {data_category}")

            # Done - now at Data Display page
            logger.info(f"=== {self.name} Workflow Complete ===")
            logger.info("Now at Data Display page. Use AgentDataCollector for data collection.")
            return {
                "success": True,
                "module": target_module,
                "data_category": data_category,
            }

        except Exception as e:
            logger.exception(f"Workflow failed: {e}")
            return self._error_result(str(e))

    # =========================================================================
    # Java Agent Navigation Methods
    # =========================================================================

    def _check_agent(self) -> bool:
        """Check if Java Agent is available."""
        logger.info("Step 0: Checking Java Agent connection...")
        if self.nav.check_agent():
            logger.info("  [OK] Java Agent connected")
            return True
        else:
            logger.error("  [ERROR] Java Agent not responding")
            return False

    def _click_diagnostics(self) -> bool:
        """Click Diagnostics button using Java Agent."""
        logger.info("Step 1: Clicking Diagnostics button (Java Agent)...")

        result = self.nav.click_button("Diagnostics")
        if result.get('success'):
            logger.info("  [OK] Clicked Diagnostics")
            time.sleep(3)  # Wait for page load / Device Explorer
            return True
        else:
            logger.error(f"  [ERROR] {result.get('message')}")
            return False

    def _click_enter(self) -> bool:
        """Click Enter button using Java Agent."""
        logger.info("Step 4: Clicking Enter button (Java Agent)...")

        # Wait for Enter button to be available
        for _ in range(10):
            buttons = self.nav.get_buttons()
            button_texts = [b.get('text') for b in buttons]

            if "Enter" in button_texts:
                # Check if enabled (not in disabled list)
                enter_btn = next((b for b in buttons if b.get('text') == 'Enter'), None)
                if enter_btn:
                    result = self.nav.click_button("Enter")
                    if result.get('success'):
                        logger.info("  [OK] Clicked Enter")
                        time.sleep(3)
                        return True

            time.sleep(0.5)

        logger.error("  [ERROR] Enter button not found or not enabled")
        return False

    def _dismiss_warning_dialog(self):
        """Dismiss warning dialog if present (Java Agent)."""
        logger.info("  Checking for warning dialog...")

        for _ in range(3):
            buttons = self.nav.get_buttons()
            button_texts = [b.get('text') for b in buttons]

            if "OK" in button_texts:
                logger.info("  Warning dialog detected, clicking OK...")
                result = self.nav.click_button("OK")
                if result.get('success'):
                    logger.info("  [OK] Dismissed warning dialog")
                    time.sleep(1)
                    return
            time.sleep(0.3)

        logger.info("  [INFO] No warning dialog")

    def _select_module_diagnostics(self) -> bool:
        """Select Module Diagnostics from menu (Java Agent)."""
        logger.info("Step 5: Selecting Module Diagnostics (Java Agent)...")

        # Wait for list to load (may take a few seconds after dialog dismiss)
        items = []
        for attempt in range(15):
            items = self.nav.get_list_items(0)
            if items:
                break
            logger.info(f"  Waiting for menu to load... ({attempt+1})")
            time.sleep(1)

        logger.info(f"  Menu items: {items}")

        for i, item in enumerate(items):
            if "Module Diagnostics" in item:
                result = self.nav.select_list_item(0, i, double_click=True)
                if result.get('success'):
                    logger.info(f"  [OK] Selected Module Diagnostics at index {i}")
                    time.sleep(3)
                    return True

        logger.error("  [ERROR] Module Diagnostics not found in menu")
        return False

    def _select_module(self, target_module: str) -> bool:
        """Select target module from list (Java Agent)."""
        logger.info(f"Step 6: Selecting module '{target_module}' (Java Agent)...")

        items = self.nav.get_list_items(0)
        logger.info(f"  Found {len(items)} modules")

        # Find module by name (partial match)
        for i, item in enumerate(items):
            if target_module in item or item in target_module:
                result = self.nav.select_list_item(0, i, double_click=True)
                if result.get('success'):
                    logger.info(f"  [OK] Selected '{item}' at index {i}")
                    time.sleep(3)
                    return True

        # Try partial match with key parts
        module_key = target_module.split("]")[-1].strip() if "]" in target_module else target_module
        for i, item in enumerate(items):
            if module_key in item:
                result = self.nav.select_list_item(0, i, double_click=True)
                if result.get('success'):
                    logger.info(f"  [OK] Selected '{item}' at index {i}")
                    time.sleep(3)
                    return True

        logger.error(f"  [ERROR] Module '{target_module}' not found")
        return False

    def _select_data_display(self) -> bool:
        """Select Data Display from module submenu (Java Agent)."""
        logger.info("Step 7: Selecting Data Display (Java Agent)...")

        # Wait for submenu to load (may take a few seconds after module selection)
        items = []
        for attempt in range(15):
            items = self.nav.get_list_items(0)
            if items:
                break
            logger.info(f"  Waiting for submenu to load... ({attempt+1})")
            time.sleep(1)

        logger.info(f"  Submenu items: {items}")

        for i, item in enumerate(items):
            if "Data Display" in item:
                result = self.nav.select_list_item(0, i, double_click=True)
                if result.get('success'):
                    logger.info(f"  [OK] Selected Data Display at index {i}")
                    time.sleep(3)
                    return True

        logger.error("  [ERROR] Data Display not found in submenu")
        return False

    def _select_data_category(self, data_category: str) -> bool:
        """Select data category from list (Java Agent)."""
        logger.info(f"Step 8: Selecting data category '{data_category}' (Java Agent)...")

        # Wait for data list to load
        items = []
        for attempt in range(15):
            items = self.nav.get_list_items(0)
            if items:
                break
            logger.info(f"  Waiting for data list to load... ({attempt+1})")
            time.sleep(1)

        logger.info(f"  Found {len(items)} data categories")

        for i, item in enumerate(items):
            if data_category in item or item in data_category:
                result = self.nav.select_list_item(0, i, double_click=True)
                if result.get('success'):
                    logger.info(f"  [OK] Selected '{item}' at index {i}")
                    time.sleep(5)  # Wait for Data Display page to load
                    return True

        logger.error(f"  [ERROR] Data category '{data_category}' not found")
        return False

    # =========================================================================
    # Windows API Method (Device Explorer only)
    # =========================================================================

    def _handle_device_explorer(self, vci_device: str):
        """
        Handle Device Explorer popup using Windows API.

        Device Explorer is a Win32 native dialog (#32770) running in the
        same JVM process. We control it via SendMessage - no screen dependency.
        """
        logger.info("Step 2-3: Handling Device Explorer (Windows API)...")

        if handle_device_explorer(device_name=vci_device, timeout=5.0):
            logger.info("  [OK] Device Explorer handled via Windows API")
            time.sleep(3)
        else:
            logger.info("  [INFO] No Device Explorer or handling failed")

    def _error_result(self, message: str) -> Dict[str, Any]:
        """Create error result dictionary."""
        logger.error(f"  [ERROR] {message}")
        return {
            "success": False,
            "error": message,
        }
