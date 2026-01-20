"""
Read Vehicle DTC Workflow

Reads all DTCs from the vehicle using fluent Page Object navigation.
"""

import logging
import time
from typing import Dict, Any, TYPE_CHECKING

from .base_workflow import BaseWorkflow
from src.pages import MainMenuPage
from src.utils.report_parser import DTCReportParser

if TYPE_CHECKING:
    from src.core.driver import GDS2Driver

logger = logging.getLogger(__name__)


class ReadVehicleDTCWorkflow(BaseWorkflow):
    """
    Workflow: Read all vehicle DTCs.

    Uses fluent Page Object navigation pattern:
        MainMenu -> DeviceExplorer -> VehicleSelection ->
        DiagnosticsMenu -> VehicleDiagnostics -> DTCPage

    Each page method returns the next page object, enabling:
        dtc_page = (main_menu
            .click_diagnostics()
            .select_device(vci_device)
            .click_enter()
            .select_vehicle_diagnostics()
            .select_vehicle_dtc_info())
    """

    def __init__(self, driver: 'GDS2Driver'):
        super().__init__(driver)
        self.report_parser = DTCReportParser()

    @property
    def name(self) -> str:
        return "Read Vehicle DTC"

    @property
    def description(self) -> str:
        return "Read all DTCs from the vehicle and generate HTML report"

    def execute(self, vci_device: str = "SM2 USB", **kwargs) -> Dict[str, Any]:
        """
        Execute the workflow using fluent navigation.

        Args:
            vci_device: VCI device name (e.g., "SM2 USB")

        Returns:
            Dictionary with:
                - success: bool
                - dtc_list: List of DTC dictionaries
                - vehicle_info: Vehicle information
                - module_status: Module status list
                - report_path: Path to HTML report
                - error: Error message if failed
        """
        logger.info(f"=== Starting {self.name} Workflow ===")
        logger.info(f"VCI Device: {vci_device}")

        try:
            # Step 1: Ensure at Main Menu
            logger.info("[Step 1/3] Ensuring at Main Menu...")
            main_menu = self._ensure_at_main_menu()
            if main_menu is None:
                return self._error_result("Failed to navigate to Main Menu")

            # Step 2: Navigate to DTC page using fluent chain
            logger.info("[Step 2/3] Navigating to DTC page...")
            dtc_page = (main_menu
                .click_diagnostics()
                .select_device(vci_device)
                .click_enter()
                .select_vehicle_diagnostics()
                .select_vehicle_dtc_info())

            # Step 3: Get DTC data
            logger.info("[Step 3/3] Collecting DTC data...")
            result = self._collect_dtc_data(dtc_page)

            logger.info(f"=== {self.name} Workflow Complete ===")
            return result

        except Exception as e:
            logger.error(f"Workflow failed: {e}")
            return self._error_result(str(e))

    def _ensure_at_main_menu(self) -> MainMenuPage:
        """
        Navigate to Main Menu if not already there.

        Returns:
            MainMenuPage instance or None if navigation failed
        """
        main_menu = MainMenuPage(self.driver)

        # Check if already at Main Menu
        if main_menu.is_displayed():
            logger.info("Already at Main Menu")
            return main_menu

        # Try Home button
        if main_menu.has_home_button():
            logger.info("Clicking Home to return to Main Menu...")
            try:
                main_menu.click_home()
                time.sleep(1)
                if main_menu.is_displayed():
                    return main_menu
            except Exception:
                pass

        # Try Back button repeatedly
        for i in range(10):
            if main_menu.is_displayed():
                return main_menu

            try:
                main_menu.click_back()
                time.sleep(0.5)
            except Exception:
                pass

        return main_menu if main_menu.is_displayed() else None

    def _collect_dtc_data(self, dtc_page) -> Dict[str, Any]:
        """
        Wait for DTC data and create report.

        Args:
            dtc_page: DTCPage instance

        Returns:
            Result dictionary with DTC data
        """
        # Wait for data to load (Clear DTCs button becomes enabled)
        dtc_page.wait_for_data_loaded(timeout=120)

        # Create report and get path
        report_path = dtc_page.create_report_and_get_path(timeout=30)
        if not report_path:
            return self._error_result("Failed to create report")

        # Parse report
        try:
            parsed = self.report_parser.parse_dtc_report(str(report_path))

            return {
                "success": True,
                "dtc_list": parsed.get("dtc_list", []),
                "vehicle_info": parsed.get("vehicle_info", {}),
                "module_status": parsed.get("module_status", []),
                "report_path": str(report_path),
                "report_created": True,
            }
        except Exception as e:
            logger.error(f"Failed to parse report: {e}")
            return self._error_result(f"Failed to parse report: {e}")

    def _error_result(self, message: str) -> Dict[str, Any]:
        """Create error result dictionary."""
        return {
            "success": False,
            "error": message,
            "dtc_list": [],
            "vehicle_info": {},
            "module_status": [],
            "report_path": None,
            "report_created": False,
        }
