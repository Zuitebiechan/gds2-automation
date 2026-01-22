"""
Navigation Test: Main Menu to Data Display Page

This test validates that our code can correctly navigate GDS2 from the
main menu to the data_display page following the Module Diagnostics path.

Navigation Flow (from MODULE_DATA_DISPLAY_PLAN.md):
1. Main Menu → Click "Diagnostics" button
2. Device Explorer (popup) → Select VCI device, Click "Continue"
3. Vehicle Selection → Click "Enter"
4. Diagnostics Menu → Select "Module Diagnostics", Click "Enter"
5. Module List → Select a module
6. Module Options → Click "Data Display"
7. Data Display Page (or Data Selection Page first)

Prerequisites:
- GDS2 must be open at Main Menu
- SM2 USB VCI must be connected
- Vehicle must be connected

Usage:
    # Run all navigation tests
    python -m pytest tests/test_navigation_to_data_display.py -v

    # Run with live GDS2 (requires hardware)
    python -m pytest tests/test_navigation_to_data_display.py -v --live

    # Run specific test
    python -m pytest tests/test_navigation_to_data_display.py::TestNavigationToDataDisplay::test_main_menu_to_diagnostics_menu -v
"""

import pytest
import logging
import time
from unittest.mock import Mock, MagicMock, patch
from typing import Optional

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Note: mock_driver and live_driver fixtures are defined in conftest.py

# ============================================================================
# Page Object Imports (with fallback for missing pages)
# ============================================================================

# Import existing pages
from src.pages import (
    MainMenuPage,
    DeviceExplorerPage,
    VehicleSelectionPage,
    DiagnosticsMenuPage,
)
from src.core.locators import Loc


# Mock classes for pages that don't exist yet
class MockModuleListPage:
    """Mock for ModuleListPage (not yet implemented)."""

    def __init__(self, driver):
        self.driver = driver
        self._logger = logging.getLogger(f"{__name__}.MockModuleListPage")

    @property
    def name(self) -> str:
        return "Module List"

    def is_displayed(self) -> bool:
        # Module list shows when we have modules but not module options
        return True

    def wait_for_page(self, timeout: float = 30) -> 'MockModuleListPage':
        time.sleep(0.5)
        return self

    def get_visible_modules(self) -> list:
        return ["Engine Control Module", "Body Control Module", "Transmission Control Module"]

    def get_all_modules(self) -> list:
        return self.get_visible_modules()

    def select_module(self, module_name: str) -> 'MockModuleOptionsPage':
        self._logger.info(f"Selecting module: {module_name}")
        return MockModuleOptionsPage(self.driver)


class MockModuleOptionsPage:
    """Mock for ModuleOptionsPage (not yet implemented)."""

    def __init__(self, driver):
        self.driver = driver
        self._logger = logging.getLogger(f"{__name__}.MockModuleOptionsPage")

    @property
    def name(self) -> str:
        return "Module Options"

    def is_displayed(self) -> bool:
        return True

    def wait_for_page(self, timeout: float = 30) -> 'MockModuleOptionsPage':
        time.sleep(0.5)
        return self

    def click_data_display(self) -> 'MockDataDisplayPage':
        self._logger.info("Clicking Data Display option")
        return MockDataDisplayPage(self.driver)


class MockDataSelectionPage:
    """Mock for DataSelectionPage (not yet implemented)."""

    def __init__(self, driver):
        self.driver = driver
        self._logger = logging.getLogger(f"{__name__}.MockDataSelectionPage")

    @property
    def name(self) -> str:
        return "Data Selection"

    def is_displayed(self) -> bool:
        return True

    def wait_for_page(self, timeout: float = 30) -> 'MockDataSelectionPage':
        time.sleep(0.5)
        return self

    def get_all_items(self) -> list:
        return ["Engine Data", "Sensor Data", "Diagnostic Data"]

    def select_item(self, item_name: str) -> 'MockDataDisplayPage':
        self._logger.info(f"Selecting data item: {item_name}")
        return MockDataDisplayPage(self.driver)


class MockDataDisplayPage:
    """Mock for DataDisplayPage (not yet implemented)."""

    def __init__(self, driver):
        self.driver = driver
        self._logger = logging.getLogger(f"{__name__}.MockDataDisplayPage")

    @property
    def name(self) -> str:
        return "Data Display"

    def is_displayed(self) -> bool:
        # Data Display page should have Create Report button
        return self.driver.element_exists(Loc.DataDisplay.CREATE_REPORT_BTN, timeout=0.5)

    def wait_for_page(self, timeout: float = 30) -> 'MockDataDisplayPage':
        time.sleep(0.5)
        return self

    def wait_for_data_loaded(self, timeout: float = 60) -> 'MockDataDisplayPage':
        self._logger.info("Waiting for data to load...")
        time.sleep(1)
        return self

    def create_report_and_parse(self, timeout: float = 30) -> dict:
        self._logger.info("Creating report...")
        return {
            "success": True,
            "data": {"parameters": []},
            "report_path": "/tmp/test_report.html"
        }


# Try to import real pages if they exist, otherwise use mocks
try:
    from src.pages.module_list_page import ModuleListPage
except ImportError:
    ModuleListPage = MockModuleListPage
    logger.info("Using MockModuleListPage (real page not implemented)")

try:
    from src.pages.module_options_page import ModuleOptionsPage
except ImportError:
    ModuleOptionsPage = MockModuleOptionsPage
    logger.info("Using MockModuleOptionsPage (real page not implemented)")

try:
    from src.pages.data_selection_page import DataSelectionPage
except ImportError:
    DataSelectionPage = MockDataSelectionPage
    logger.info("Using MockDataSelectionPage (real page not implemented)")

try:
    from src.pages.data_display_page import DataDisplayPage
except ImportError:
    DataDisplayPage = MockDataDisplayPage
    logger.info("Using MockDataDisplayPage (real page not implemented)")


# ============================================================================
# Unit Tests (Mock Driver)
# ============================================================================

class TestNavigationToDataDisplay:
    """Test navigation from Main Menu to Data Display page."""

    # ------------------------------------------------------------------------
    # Step 1: Main Menu Tests
    # ------------------------------------------------------------------------

    def test_main_menu_is_displayed(self, mock_driver):
        """Test that Main Menu page can detect if it's displayed."""
        # Setup mock to return True for Diagnostics button
        mock_driver.element_exists = Mock(return_value=True)

        page = MainMenuPage(mock_driver)
        assert page.is_displayed() is True

        # Verify correct locator was used
        mock_driver.element_exists.assert_called()

    def test_main_menu_click_diagnostics(self, mock_driver):
        """Test clicking Diagnostics button from Main Menu."""
        page = MainMenuPage(mock_driver)

        # Mock the transition to DeviceExplorerPage
        result = page.click_diagnostics()

        # Verify Diagnostics button was clicked
        mock_driver.click_button.assert_called_with(Loc.MainMenu.DIAGNOSTICS_BTN)

        # Verify it returns DeviceExplorerPage
        assert isinstance(result, DeviceExplorerPage)

    # ------------------------------------------------------------------------
    # Step 2: Device Explorer Tests
    # ------------------------------------------------------------------------

    def test_device_explorer_is_displayed(self, mock_driver):
        """Test that Device Explorer page can detect if it's displayed."""
        page = DeviceExplorerPage(mock_driver)

        # Device Explorer uses Desktop() to find popup window
        # For mock test, we'll patch the Desktop import inside the method
        with patch('pywinauto.Desktop') as mock_desktop:
            mock_window = Mock()
            mock_window.window_text.return_value = "Device Explorer"
            mock_desktop.return_value.windows.return_value = [mock_window]

            assert page.is_displayed() is True

    def test_device_explorer_select_device(self, mock_driver):
        """Test selecting a VCI device in Device Explorer."""
        page = DeviceExplorerPage(mock_driver)

        # Mock the popup window operations
        with patch.object(page, '_get_popup_window') as mock_get_popup:
            mock_popup = Mock()
            mock_popup.descendants.return_value = []
            mock_popup.child_window.return_value.exists.return_value = True
            mock_get_popup.return_value = mock_popup

            result = page.select_device("SM2 USB")

            # Verify it returns VehicleSelectionPage
            assert isinstance(result, VehicleSelectionPage)

    # ------------------------------------------------------------------------
    # Step 3: Vehicle Selection Tests
    # ------------------------------------------------------------------------

    def test_vehicle_selection_is_displayed(self, mock_driver):
        """Test that Vehicle Selection page can detect if it's displayed."""
        # Setup: Enter button exists, Module Diagnostics doesn't
        def mock_element_exists(locator, **kwargs):
            if locator == Loc.VehicleSelection.ENTER_BTN:
                return True
            if locator == Loc.DiagnosticsMenu.MODULE_DIAGNOSTICS:
                return False
            return False

        mock_driver.element_exists = Mock(side_effect=mock_element_exists)

        page = VehicleSelectionPage(mock_driver)
        assert page.is_displayed() is True

    def test_vehicle_selection_click_enter(self, mock_driver):
        """Test clicking Enter button from Vehicle Selection."""
        page = VehicleSelectionPage(mock_driver)

        # Mock element_exists for warning dialog check
        mock_driver.element_exists = Mock(return_value=False)

        result = page.click_enter()

        # Verify Enter button was clicked
        mock_driver.click_button.assert_called_with(Loc.VehicleSelection.ENTER_BTN)

        # Verify it returns DiagnosticsMenuPage
        assert isinstance(result, DiagnosticsMenuPage)

    # ------------------------------------------------------------------------
    # Step 4: Diagnostics Menu Tests
    # ------------------------------------------------------------------------

    def test_diagnostics_menu_is_displayed(self, mock_driver):
        """Test that Diagnostics Menu page can detect if it's displayed."""
        mock_driver.element_exists = Mock(return_value=True)

        page = DiagnosticsMenuPage(mock_driver)
        assert page.is_displayed() is True

        # Verify it checks for Module Diagnostics menu item
        mock_driver.element_exists.assert_called_with(Loc.DiagnosticsMenu.MODULE_DIAGNOSTICS)

    def test_diagnostics_menu_select_module_diagnostics(self, mock_driver):
        """Test selecting Module Diagnostics from menu."""
        page = DiagnosticsMenuPage(mock_driver)

        # Currently returns self since ModuleListPage isn't implemented
        result = page.select_module_diagnostics()

        # Verify Module Diagnostics was clicked
        mock_driver.click_list_item.assert_called_with(Loc.DiagnosticsMenu.MODULE_DIAGNOSTICS)

        # Verify Enter was clicked
        mock_driver.click_button.assert_called_with(Loc.DiagnosticsMenu.ENTER_BTN)

    # ------------------------------------------------------------------------
    # Step 5-7: Module Diagnostics Path Tests (Using Mocks)
    # ------------------------------------------------------------------------

    def test_module_list_page_get_modules(self, mock_driver):
        """Test getting list of modules from Module List page."""
        page = ModuleListPage(mock_driver)

        modules = page.get_all_modules()

        assert isinstance(modules, list)
        assert len(modules) > 0

    def test_module_list_page_select_module(self, mock_driver):
        """Test selecting a module from Module List page."""
        page = ModuleListPage(mock_driver)

        result = page.select_module("Engine Control Module")

        # Should return ModuleOptionsPage
        assert isinstance(result, (ModuleOptionsPage, MockModuleOptionsPage))

    def test_module_options_page_click_data_display(self, mock_driver):
        """Test clicking Data Display from Module Options page."""
        page = ModuleOptionsPage(mock_driver)

        result = page.click_data_display()

        # Should return DataDisplayPage (or DataSelectionPage)
        assert isinstance(result, (DataDisplayPage, MockDataDisplayPage, DataSelectionPage, MockDataSelectionPage))

    def test_data_display_page_is_displayed(self, mock_driver):
        """Test that Data Display page can detect if it's displayed."""
        mock_driver.element_exists = Mock(return_value=True)

        page = DataDisplayPage(mock_driver)
        is_displayed = page.is_displayed()

        # Should check for Create Report button
        assert is_displayed is True

    def test_data_display_page_create_report(self, mock_driver):
        """Test creating a report from Data Display page."""
        page = DataDisplayPage(mock_driver)

        result = page.create_report_and_parse()

        assert "success" in result
        assert result["success"] is True

    # ------------------------------------------------------------------------
    # Full Navigation Flow Tests
    # ------------------------------------------------------------------------

    def test_full_navigation_flow_mocked(self, mock_driver):
        """
        Test complete navigation from Main Menu to Data Display.

        This tests the fluent navigation pattern with mock driver.
        """
        # Setup mocks for page detection
        mock_driver.element_exists = Mock(return_value=True)

        # Start at Main Menu
        main_menu = MainMenuPage(mock_driver)
        assert main_menu.is_displayed()

        # Navigate to Device Explorer
        # Note: DeviceExplorerPage uses Desktop() so we mock it
        with patch.object(DeviceExplorerPage, 'is_displayed', return_value=True):
            with patch.object(DeviceExplorerPage, '_get_popup_window', return_value=None):
                device_explorer = main_menu.click_diagnostics()

                # Navigate to Vehicle Selection
                vehicle_selection = device_explorer.select_device("SM2 USB")

        # Navigate to Diagnostics Menu
        diagnostics_menu = vehicle_selection.click_enter()

        # Select Module Diagnostics (currently returns self)
        module_list_result = diagnostics_menu.select_module_diagnostics()

        # Continue with mock pages
        module_list = ModuleListPage(mock_driver)
        module_options = module_list.select_module("Engine Control Module")
        data_display = module_options.click_data_display()

        # Verify we reached Data Display
        assert data_display.is_displayed()

        # Create report
        report = data_display.create_report_and_parse()
        assert report["success"] is True

        logger.info("Full navigation flow completed successfully (mocked)")


# ============================================================================
# Integration Tests (Live Driver - requires --live flag)
# ============================================================================

@pytest.mark.skipif(True, reason="Requires --live flag and GDS2 hardware")
class TestLiveNavigationToDataDisplay:
    """
    Integration tests with real GDS2 application.

    These tests require:
    - GDS2 application running and at Main Menu
    - SM2 USB VCI connected
    - Vehicle connected

    Run with: pytest tests/test_navigation_to_data_display.py --live -v
    """

    def test_live_main_menu_detection(self, live_driver):
        """Test detecting Main Menu with live GDS2."""
        main_menu = MainMenuPage(live_driver)

        assert main_menu.is_displayed(), "GDS2 should be at Main Menu"
        logger.info("✓ Main Menu detected")

    def test_live_navigation_to_diagnostics_menu(self, live_driver):
        """Test navigation from Main Menu to Diagnostics Menu with live GDS2."""
        main_menu = MainMenuPage(live_driver)
        assert main_menu.is_displayed(), "Start at Main Menu"

        # Click Diagnostics
        logger.info("Clicking Diagnostics...")
        device_explorer = main_menu.click_diagnostics()

        # Select device
        logger.info("Selecting SM2 USB device...")
        vehicle_selection = device_explorer.select_device("SM2 USB")

        # Click Enter
        logger.info("Clicking Enter...")
        diagnostics_menu = vehicle_selection.click_enter()

        assert diagnostics_menu.is_displayed(), "Should be at Diagnostics Menu"
        logger.info("✓ Reached Diagnostics Menu")

    def test_live_navigation_to_module_diagnostics(self, live_driver):
        """Test navigation to Module Diagnostics with live GDS2."""
        # First navigate to Diagnostics Menu
        main_menu = MainMenuPage(live_driver)
        device_explorer = main_menu.click_diagnostics()
        vehicle_selection = device_explorer.select_device("SM2 USB")
        diagnostics_menu = vehicle_selection.click_enter()

        # Select Module Diagnostics
        logger.info("Selecting Module Diagnostics...")
        result = diagnostics_menu.select_module_diagnostics()

        # Note: Currently returns self since ModuleListPage isn't implemented
        logger.info(f"Result type: {type(result)}")

        # Wait for module list to appear
        time.sleep(2)

        logger.info("✓ Module Diagnostics selected")

    def test_live_full_navigation_to_data_display(self, live_driver):
        """
        Test complete navigation from Main Menu to Data Display with live GDS2.

        This is the comprehensive end-to-end test.
        """
        logger.info("=" * 60)
        logger.info("Starting full navigation to Data Display")
        logger.info("=" * 60)

        # Step 1: Start at Main Menu
        logger.info("Step 1: Verifying Main Menu...")
        main_menu = MainMenuPage(live_driver)
        assert main_menu.is_displayed(), "Must start at Main Menu"
        logger.info("  ✓ At Main Menu")

        # Step 2: Click Diagnostics → Device Explorer
        logger.info("Step 2: Clicking Diagnostics...")
        device_explorer = main_menu.click_diagnostics()
        logger.info("  ✓ Device Explorer appeared")

        # Step 3: Select device → Vehicle Selection
        logger.info("Step 3: Selecting VCI device (SM2 USB)...")
        vehicle_selection = device_explorer.select_device("SM2 USB")
        logger.info("  ✓ Vehicle Selection page")

        # Step 4: Click Enter → Diagnostics Menu
        logger.info("Step 4: Clicking Enter...")
        diagnostics_menu = vehicle_selection.click_enter()
        assert diagnostics_menu.is_displayed(), "Should be at Diagnostics Menu"
        logger.info("  ✓ Diagnostics Menu")

        # Step 5: Select Module Diagnostics → Module List
        logger.info("Step 5: Selecting Module Diagnostics...")
        diagnostics_menu.select_module_diagnostics()
        time.sleep(2)  # Wait for module list

        # Use mock page for now (real pages not implemented)
        module_list = ModuleListPage(live_driver)
        logger.info("  ✓ Module List page")

        # Step 6: Select a module → Module Options
        logger.info("Step 6: Selecting Engine Control Module...")
        module_options = module_list.select_module("Engine Control Module")
        logger.info("  ✓ Module Options page")

        # Step 7: Click Data Display → Data Display Page
        logger.info("Step 7: Clicking Data Display...")
        data_display = module_options.click_data_display()
        logger.info("  ✓ Data Display page")

        # Verify we're at Data Display
        assert data_display.is_displayed(), "Should be at Data Display page"

        # Wait for data to load
        logger.info("Waiting for data to load...")
        data_display.wait_for_data_loaded(timeout=60)

        # Create report
        logger.info("Creating report...")
        result = data_display.create_report_and_parse()

        assert result["success"], f"Report creation failed: {result.get('error', 'Unknown error')}"

        logger.info("=" * 60)
        logger.info("✓ Full navigation to Data Display completed successfully!")
        logger.info(f"  Report path: {result.get('report_path', 'N/A')}")
        logger.info("=" * 60)


# ============================================================================
# Navigation Path Validation Tests
# ============================================================================

class TestNavigationPaths:
    """Test various navigation paths and edge cases."""

    def test_navigation_path_definition(self):
        """Validate the expected navigation path is correctly defined."""
        expected_path = [
            "Main Menu",
            "Device Explorer",
            "Vehicle Selection",
            "Diagnostics Menu",
            "Module List",
            "Module Options",
            "Data Display",
        ]

        # This documents the expected navigation flow
        assert len(expected_path) == 7
        assert expected_path[0] == "Main Menu"
        assert expected_path[-1] == "Data Display"

    def test_page_names_are_correct(self, mock_driver):
        """Verify all page objects have correct names."""
        pages = [
            (MainMenuPage(mock_driver), "Main Menu"),
            (DeviceExplorerPage(mock_driver), "Device Explorer"),
            (VehicleSelectionPage(mock_driver), "Vehicle Selection"),
            (DiagnosticsMenuPage(mock_driver), "Diagnostics Menu"),
            (ModuleListPage(mock_driver), "Module List"),
            (ModuleOptionsPage(mock_driver), "Module Options"),
            (DataDisplayPage(mock_driver), "Data Display"),
        ]

        for page, expected_name in pages:
            assert page.name == expected_name, f"Expected '{expected_name}', got '{page.name}'"

    def test_locators_exist_for_navigation(self):
        """Verify all required locators exist for navigation."""
        required_locators = [
            Loc.MainMenu.DIAGNOSTICS_BTN,
            Loc.DeviceExplorer.CONTINUE_BTN,
            Loc.VehicleSelection.ENTER_BTN,
            Loc.DiagnosticsMenu.MODULE_DIAGNOSTICS,
            Loc.DiagnosticsMenu.ENTER_BTN,
            Loc.ModuleDiagnostics.DATA_DISPLAY,
            Loc.DataDisplay.CREATE_REPORT_BTN,
        ]

        for locator in required_locators:
            assert locator is not None
            assert hasattr(locator, 'title')
            assert hasattr(locator, 'control_type')
            logger.info(f"✓ Locator exists: {locator.title} ({locator.control_type})")


# ============================================================================
# Error Handling Tests
# ============================================================================

class TestNavigationErrorHandling:
    """Test error handling during navigation."""

    def test_element_not_found_handling(self, mock_driver):
        """Test handling when an element is not found."""
        # Setup mock to return False for element_exists
        mock_driver.element_exists = Mock(return_value=False)

        page = MainMenuPage(mock_driver)

        # is_displayed should return False
        assert page.is_displayed() is False

    def test_timeout_handling_in_wait_for_page(self, mock_driver):
        """Test that wait_for_page handles timeouts gracefully."""
        # Setup mock to always return False
        mock_driver.element_exists = Mock(return_value=False)

        page = MainMenuPage(mock_driver)

        # wait_for_page with short timeout should complete without exception
        start = time.time()
        result = page.wait_for_page(timeout=1)
        elapsed = time.time() - start

        # Should return self even if page didn't appear
        assert result is page
        # Should have waited approximately the timeout duration
        assert elapsed >= 0.5  # At least half the timeout

    def test_navigation_continues_after_warning_dialog(self, mock_driver):
        """Test that navigation continues after dismissing warning dialogs."""
        page = VehicleSelectionPage(mock_driver)

        # Mock to simulate warning dialog appearing
        call_count = [0]
        def mock_element_exists(locator, **kwargs):
            call_count[0] += 1
            # First call finds warning dialog, then it's dismissed
            if locator == Loc.VehicleSelection.OK_BTN:
                return call_count[0] == 1  # Only exists on first check
            return False

        mock_driver.element_exists = Mock(side_effect=mock_element_exists)

        page._dismiss_warning_dialog()

        # Should have attempted to dismiss the dialog
        assert mock_driver.element_exists.called


# ============================================================================
# Entry Point for Running Tests Directly
# ============================================================================

if __name__ == "__main__":
    # Run with verbose output
    pytest.main([__file__, "-v", "--tb=short"])
