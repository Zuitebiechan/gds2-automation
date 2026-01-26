"""
Read Data Display Workflow

Uses keyboard navigation (UP/DOWN/ENTER) for module and data list selection.
Uses PyAutoGUI+OpenCV for buttons.

Features:
- On-demand discovery: Module list discovered once, data categories discovered when needed
- Keyboard navigation: DOWN N times + ENTER to select items
- No coordinate-based clicking for lists
"""

import logging
import time
from pathlib import Path
from typing import Dict, Any, Optional

import pyautogui

from .base_workflow import BaseWorkflow
from ..discovery import VehicleMapping, VehicleDiscovery

logger = logging.getLogger(__name__)


class ReadDataDisplayWorkflow(BaseWorkflow):
    """
    Workflow: Navigate to Data Display and create report.

    Uses keyboard navigation for list selection with on-demand discovery.
    """

    def __init__(self, vehicle_id: str = "current_vehicle"):
        """Initialize workflow with vehicle mapping."""
        super().__init__()
        self.vehicle_id = vehicle_id
        self.mapping = VehicleMapping()
        self.discovery = VehicleDiscovery()
        self._current_module = None  # Track current module for data category lookup

    @property
    def name(self) -> str:
        return "Read Data Display"

    @property
    def description(self) -> str:
        return "Navigate to Data Display using keyboard navigation with on-demand discovery"

    def execute(
        self,
        target_module: str,
        data_category: str,
        vci_device: str = "SM2 USB",
        **kwargs,
    ) -> Dict[str, Any]:
        """
        Execute the workflow.

        Args:
            target_module: Module to select (e.g., "[K20] Engine Control Module")
            data_category: Data category to select (e.g., "Ignition Data")
            vci_device: VCI device name

        Returns:
            Dictionary with success status and report path
        """
        logger.info(f"=== Starting {self.name} Workflow ===")
        logger.info(f"Vehicle ID: {self.vehicle_id}")
        logger.info(f"Target Module: {target_module}")
        logger.info(f"Data Category: {data_category}")

        self._current_module = target_module

        try:
            # Step 1: Verify at Main Menu
            if not self._verify_main_menu():
                return self._error_result("Not at Main Menu")

            # Step 2: Click Diagnostics
            if not self._click_diagnostics():
                return self._error_result("Could not click Diagnostics button")

            # Step 3: Handle Device Explorer popup (if appears)
            self._handle_device_explorer(vci_device)

            # Step 4: Click Enter for Vehicle Selection
            if not self._click_enter_vehicle_selection():
                return self._error_result("Could not click Enter for vehicle selection")

            # Step 5: Select Module Diagnostics
            if not self._select_module_diagnostics():
                return self._error_result("Could not select Module Diagnostics")

            # Step 6: Discover modules if needed, then select target module
            if not self._select_module_keyboard(target_module):
                return self._error_result(f"Could not select module: {target_module}")

            # Step 7: Select Data Display
            if not self._select_data_display():
                return self._error_result("Could not select Data Display")

            # Step 8: Discover data categories if needed, then select target
            if not self._select_data_category_keyboard(data_category):
                return self._error_result(f"Could not select data category: {data_category}")

            # Step 9: Create Report
            report_path = self._create_report()
            if not report_path:
                return self._error_result("Could not create report")

            logger.info(f"=== {self.name} Workflow Complete ===")
            return {
                "success": True,
                "report_path": str(report_path) if report_path else None,
            }

        except Exception as e:
            logger.exception(f"Workflow failed: {e}")
            return self._error_result(str(e))

    def _select_module_keyboard(self, target_module: str) -> bool:
        """Select a module using keyboard navigation with on-demand discovery."""
        logger.info(f"Step 6: Selecting '{target_module}' using keyboard navigation...")

        # Check if we have module list, if not, discover it now
        if not self.mapping.has_module_list(self.vehicle_id):
            logger.info("  Module list not found, discovering...")
            self._discover_and_save_modules()

        # Get module index from mapping
        module_index = self.mapping.get_module_index(self.vehicle_id, target_module)

        if module_index is None:
            logger.error(f"  Module '{target_module}' not found in mapping")
            return False

        logger.info(f"  Module index: {module_index}")

        # Press DOWN N times (focus is already on first item)
        if module_index > 0:
            logger.info(f"  Pressing DOWN {module_index} times...")
            for _ in range(module_index):
                pyautogui.press('down')
                time.sleep(0.03)
            time.sleep(0.3)

        # Press ENTER to select
        logger.info("  Pressing ENTER to select...")
        pyautogui.press('enter')
        time.sleep(3)  # Wait for module submenu to load

        logger.info(f"  [OK] Selected {target_module}")
        return True

    def _select_data_category_keyboard(self, data_category: str) -> bool:
        """Select a data category using keyboard navigation with on-demand discovery."""
        logger.info(f"Step 8: Selecting '{data_category}' using keyboard navigation...")

        # Check if we have data categories for this module, if not, discover them now
        if not self.mapping.has_data_categories(self.vehicle_id, self._current_module):
            logger.info(f"  Data categories for '{self._current_module}' not found, discovering...")
            self._discover_and_save_data_categories()

        # Get data category index from mapping
        data_index = self.mapping.get_data_category_index(
            self.vehicle_id, self._current_module, data_category
        )

        if data_index is None:
            logger.error(f"  Data category '{data_category}' not found in mapping")
            return False

        logger.info(f"  Data category index: {data_index}")

        # Press DOWN N times (focus is already on first item)
        if data_index > 0:
            logger.info(f"  Pressing DOWN {data_index} times...")
            for _ in range(data_index):
                pyautogui.press('down')
                time.sleep(0.03)
            time.sleep(0.3)

        # Press ENTER to select
        logger.info("  Pressing ENTER to select...")
        pyautogui.press('enter')
        time.sleep(5)  # Wait for Data Display page to load

        logger.info(f"  [OK] Selected {data_category}")
        return True

    def _discover_and_save_modules(self):
        """Discover modules on current page and save to mapping."""
        logger.info("  Discovering modules...")
        if self.discovery.connect():
            modules = self.discovery.discover_modules()
            logger.info(f"  Found {len(modules)} modules")
            # Convert to index dict
            module_indices = {name: idx for name, idx in modules.items()}
            self.mapping.update_module_list(self.vehicle_id, module_indices)
        else:
            logger.error("  Failed to connect to GDS2 for discovery")

    def _discover_and_save_data_categories(self):
        """Discover data categories on current page and save to mapping."""
        logger.info("  Discovering data categories...")
        if self.discovery.connect():
            data_categories = self.discovery.discover_data_categories()
            logger.info(f"  Found {len(data_categories)} data categories")
            self.mapping.update_data_categories(
                self.vehicle_id, self._current_module, data_categories
            )
        else:
            logger.error("  Failed to connect to GDS2 for discovery")

    # =========================================================================
    # PyAutoGUI + OpenCV methods (inherited behavior)
    # =========================================================================

    def _verify_main_menu(self) -> bool:
        """Verify we're at Main Menu by checking for Diagnostics button."""
        logger.info("Step 1: Verifying Main Menu...")
        coords = self.find_button("diagnostics", confidence=0.8)
        if coords:
            logger.info("  [OK] At Main Menu")
            return True
        else:
            logger.error("  [ERROR] Not at Main Menu - Diagnostics button not found")
            return False

    def _click_diagnostics(self) -> bool:
        """Click Diagnostics button."""
        logger.info("Step 2: Clicking Diagnostics button...")
        if self.click_button("diagnostics", confidence=0.8, timeout=10):
            logger.info("  [OK] Clicked Diagnostics")
            self.wait(3)
            return True
        else:
            logger.error("  [ERROR] Could not find Diagnostics button")
            return False

    def _handle_device_explorer(self, vci_device: str):
        """Handle Device Explorer popup if it appears."""
        logger.info("Step 3: Checking for Device Explorer popup...")
        popup_detected = False
        for _ in range(5):
            coords = self.find_button("continue", confidence=0.9)
            if coords:
                popup_detected = True
                break
            self.wait(0.5)

        if popup_detected:
            logger.info("  [OK] Device Explorer popup detected")

            # Select VCI device using template matching (faster than VLM)
            logger.info(f"  Selecting VCI device '{vci_device}'...")
            device_template = vci_device.lower().replace(" ", "_")  # "SM2 USB" -> "sm2_usb"
            if self.click_device(device_template, confidence=0.85, timeout=5):
                logger.info(f"  [OK] Selected {vci_device}")
            else:
                logger.warning(f"  [WARN] Could not find device template, trying VLM...")
                if self.click_text_vlm(vci_device, apply_offset=False):
                    logger.info(f"  [OK] Selected {vci_device} via VLM")
            self.wait(1)

            logger.info("  Clicking Continue button...")
            if self.click_button("continue", confidence=0.9, timeout=5):
                logger.info("  [OK] Clicked Continue")
            self.wait(3)
        else:
            logger.info("  [INFO] No Device Explorer popup")

    def _click_enter_vehicle_selection(self) -> bool:
        """Click Enter button for vehicle selection."""
        logger.info("Step 4: Clicking Enter button...")
        if self.click_button("enter", confidence=0.85, timeout=10):
            logger.info("  [OK] Clicked Enter")
            self.wait(3)

            # Check for warning dialog and dismiss it
            self._dismiss_warning_dialog()

            self.wait(2)
            return True
        else:
            logger.error("  [ERROR] Could not find Enter button")
            return False

    def _dismiss_warning_dialog(self):
        """Dismiss warning dialog if it appears."""
        logger.info("  Checking for warning dialog...")

        # Check for OK button (warning dialog)
        for _ in range(3):
            coords = self.find_button("ok", confidence=0.85)
            if coords:
                logger.info("  [OK] Warning dialog detected, clicking OK...")
                self.click_button("ok", confidence=0.85, timeout=5)
                self.wait(1)
                logger.info("  [OK] Dismissed warning dialog")
                return
            self.wait(0.5)

        logger.info("  [INFO] No warning dialog")

    def _select_module_diagnostics(self) -> bool:
        """Select Module Diagnostics from the menu."""
        logger.info("Step 5: Selecting 'Module Diagnostics'...")
        if self.click_list_item("module_diagnostics", confidence=0.85, timeout=10):
            logger.info("  [OK] Module Diagnostics selected")
            self.wait(2)
            return True
        else:
            logger.error("  [ERROR] Could not find 'Module Diagnostics'")
            return False

    def _select_data_display(self) -> bool:
        """Select Data Display from module submenu."""
        logger.info("Step 7: Finding 'Data Display'...")
        if self.click_list_item("data_display", confidence=0.90, timeout=10):
            logger.info("  [OK] Clicked on Data Display")
            self.wait(3)
            return True
        else:
            logger.error("  [ERROR] Could not find 'Data Display'")
            return False

    def _create_report(self) -> Optional[Path]:
        """Wait for data and create report."""
        logger.info("Step 9: Clicking Create Report...")
        self.wait(2)

        # Try to find and click Create Report button directly with PyAutoGUI
        if self.click_button("create_report", confidence=0.8, timeout=30):
            logger.info("  [OK] Clicked Create Report!")
            self.wait(2)

            report_dir = Path.home() / "AppData" / "Local" / "Temp" / "GDS 2"
            if report_dir.exists():
                reports = list(report_dir.glob("Data Display_*.html"))
                if reports:
                    latest_report = max(reports, key=lambda p: p.stat().st_mtime)
                    logger.info(f"  Report created: {latest_report}")
                    return latest_report
        else:
            logger.error("  [ERROR] Could not find Create Report button")

        logger.warning("  [WARN] Could not locate report file")
        return None

    def _error_result(self, message: str) -> Dict[str, Any]:
        """Create error result dictionary."""
        logger.error(f"  [ERROR] {message}")
        return {
            "success": False,
            "error": message,
            "report_path": None,
        }
