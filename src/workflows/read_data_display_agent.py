"""
Read Data Display Workflow - Full Agent Version

Uses InteractiveWorkflow for step-by-step navigation.
Wraps it into a single execute() call for backward compatibility.

NO PyAutoGUI or OpenCV needed. NO foreground/resolution requirements.

Benefits:
- Java Agent: Direct JavaFX API calls (~1ms), supports invisible list items
- Windows API: Direct Win32 message control for Device Explorer
- Works in background, no screen dependency
- State-aware page detection via NavigationController
"""

import logging
from typing import Dict, Any

from .interactive_workflow import InteractiveWorkflow
from ..navigation import GDS2Page

logger = logging.getLogger(__name__)


class ReadDataDisplayAgentWorkflow:
    """
    Workflow: Navigate to Data Display using Java Agent + Windows API.

    Delegates to InteractiveWorkflow for all navigation.
    Provides backward-compatible execute() method.
    """

    def __init__(self, vehicle_id: str = "current_vehicle"):
        """Initialize workflow."""
        self.vehicle_id = vehicle_id
        self._workflow = InteractiveWorkflow()
        self._workflow._vehicle_id = vehicle_id

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

        Delegates each step to InteractiveWorkflow, which uses
        NavigationController for state-aware navigation.

        Args:
            target_module: Module to select (e.g., "[K20] Engine Control Module")
            data_category: Data category to select (e.g., "Engine Data")
            vci_device: VCI device name for Device Explorer

        Returns:
            Dictionary with success status
        """
        wf = self._workflow
        logger.info(f"=== Starting {self.name} Workflow ===")
        logger.info(f"Target Module: {target_module}")
        logger.info(f"Data Category: {data_category}")

        try:
            # Step 1: Check Agent connection
            result = wf.start()
            if not result.success:
                return self._error_result(result.error)
            logger.info(f"  Current page: {result.page.value}")

            # Step 2: Click Diagnostics
            logger.info("Step 1: Clicking Diagnostics...")
            result = wf.step_diagnostics()
            if not result.success:
                return self._error_result(f"Could not click Diagnostics: {result.error}")

            # Step 3: Handle Device Explorer if it appeared
            if result.page == GDS2Page.DEVICE_EXPLORER:
                logger.info(f"Step 2: Device Explorer appeared, devices: {result.choices}")
                result = wf.step_select_device(vci_device)
                if not result.success:
                    return self._error_result(f"Device selection failed: {result.error}")
            else:
                logger.info("Step 2: No Device Explorer (skipped)")

            # Step 4: If at Diagnostics Menu, select Module Diagnostics
            page = wf.controller.detect_current_page()
            if page == GDS2Page.DIAGNOSTICS_MENU:
                logger.info("Step 3: Selecting Module Diagnostics...")
                result = wf.step_module_diagnostics()
                if not result.success:
                    return self._error_result(f"Could not select Module Diagnostics: {result.error}")
            elif page == GDS2Page.MODULE_LIST:
                logger.info("Step 3: Already at Module List (skipped)")
            else:
                logger.info(f"Step 3: Unexpected page {page.value}, attempting Module Diagnostics...")
                result = wf.step_module_diagnostics()
                if not result.success:
                    return self._error_result(f"Could not select Module Diagnostics: {result.error}")

            # Step 5: Select target module
            logger.info(f"Step 4: Selecting module '{target_module}'...")
            result = wf.step_select_module(target_module)
            if not result.success:
                return self._error_result(f"Could not select module: {result.error}")

            # Step 6: Select Data Display
            logger.info("Step 5: Selecting Data Display...")
            result = wf.step_data_display()
            if not result.success:
                return self._error_result(f"Could not select Data Display: {result.error}")

            # Step 7: Select data category
            logger.info(f"Step 6: Selecting data category '{data_category}'...")
            result = wf.step_select_data_category(data_category)
            if not result.success:
                return self._error_result(f"Could not select data category: {result.error}")

            # Step 8: Handle sub-categories if present
            if result.page == GDS2Page.SUB_DATA_LIST:
                if result.choices:
                    first_sub = result.choices[0]
                    logger.info(f"Step 7: Sub-categories detected, selecting first: '{first_sub}'...")
                    result = wf.step_select_sub_category(first_sub)
                    if not result.success:
                        return self._error_result(f"Could not select sub-category: {result.error}")
                else:
                    return self._error_result("Sub-categories detected but list is empty")

            # Done - now at Data Display page
            logger.info(f"=== {self.name} Workflow Complete ===")
            logger.info("Now at Data Display page. Use AgentDataCollector for data collection.")
            return {
                "success": True,
                "module": target_module,
                "data_category": data_category,
                "sub_category": result.context.get("sub_category"),
                "page": result.page.value,
            }

        except Exception as e:
            logger.exception(f"Workflow failed: {e}")
            return self._error_result(str(e))

    def _error_result(self, message: str) -> Dict[str, Any]:
        """Create error result dictionary."""
        logger.error(f"  [ERROR] {message}")
        return {
            "success": False,
            "error": message,
        }
