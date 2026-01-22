"""
Pytest Configuration for GDS2 RPA Tests

This file contains shared fixtures and configuration for all tests.
"""

import pytest
import logging
import sys
from pathlib import Path
from unittest.mock import Mock

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

# Configure logging for tests
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def pytest_addoption(parser):
    """Add custom command-line options."""
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="Run live tests against real GDS2 application (requires hardware)"
    )
    parser.addoption(
        "--vci-device",
        action="store",
        default="SM2 USB",
        help="VCI device name for live tests (default: SM2 USB)"
    )


def pytest_configure(config):
    """Configure pytest markers."""
    config.addinivalue_line(
        "markers", "live: mark test as requiring live GDS2 connection"
    )
    config.addinivalue_line(
        "markers", "slow: mark test as slow (navigation tests)"
    )


def pytest_collection_modifyitems(config, items):
    """Modify test collection based on options."""
    if not config.getoption("--live"):
        skip_live = pytest.mark.skip(reason="Need --live option to run")
        for item in items:
            if "live" in item.keywords:
                item.add_marker(skip_live)


# ============================================================================
# Shared Fixtures
# ============================================================================

@pytest.fixture
def mock_driver():
    """
    Create a mock GDS2Driver for unit testing.

    This fixture provides a mock driver that simulates GDS2 behavior
    without requiring actual GDS2 hardware.
    """
    driver = Mock()
    driver.is_connected = True
    driver.default_timeout = 30

    # Mock all common methods
    driver.element_exists = Mock(return_value=True)
    driver.click_button = Mock(return_value=True)
    driver.click_list_item = Mock(return_value=True)
    driver.wait_for_element = Mock(return_value=True)
    driver.wait_for_element_enabled = Mock(return_value=True)
    driver.wait_for_ui_stable = Mock(return_value=True)
    driver.find_element = Mock(return_value=Mock())
    driver.get_window = Mock(return_value=Mock())
    driver.send_keys = Mock()

    return driver


@pytest.fixture
def live_driver(request):
    """
    Create a real GDS2Driver for integration testing.

    This fixture only activates when --live flag is passed.
    Requires GDS2 to be running at Main Menu.
    """
    if not request.config.getoption("--live"):
        pytest.skip("Live test requires --live flag")

    from src.core.driver import GDS2Driver

    driver = GDS2Driver()
    try:
        driver.connect()
    except Exception as e:
        pytest.skip(f"Could not connect to GDS2: {e}")

    yield driver

    driver.disconnect()


@pytest.fixture
def vci_device(request):
    """Get VCI device name from command line or use default."""
    return request.config.getoption("--vci-device")


@pytest.fixture
def main_menu_page(mock_driver):
    """Create a MainMenuPage with mock driver."""
    from src.pages import MainMenuPage
    return MainMenuPage(mock_driver)


@pytest.fixture
def diagnostics_menu_page(mock_driver):
    """Create a DiagnosticsMenuPage with mock driver."""
    from src.pages import DiagnosticsMenuPage
    return DiagnosticsMenuPage(mock_driver)


@pytest.fixture
def vehicle_selection_page(mock_driver):
    """Create a VehicleSelectionPage with mock driver."""
    from src.pages import VehicleSelectionPage
    return VehicleSelectionPage(mock_driver)


# ============================================================================
# Test Utilities
# ============================================================================

class NavigationTestHelper:
    """Helper class for navigation tests."""

    @staticmethod
    def setup_mock_for_page(mock_driver, page_name: str):
        """Configure mock driver to simulate a specific page."""
        from src.core.locators import Loc

        page_configs = {
            "main_menu": {
                "exists": [Loc.MainMenu.DIAGNOSTICS_BTN],
                "not_exists": []
            },
            "device_explorer": {
                "exists": [Loc.DeviceExplorer.CONTINUE_BTN],
                "not_exists": []
            },
            "vehicle_selection": {
                "exists": [Loc.VehicleSelection.ENTER_BTN],
                "not_exists": [Loc.DiagnosticsMenu.MODULE_DIAGNOSTICS]
            },
            "diagnostics_menu": {
                "exists": [Loc.DiagnosticsMenu.MODULE_DIAGNOSTICS],
                "not_exists": []
            },
            "data_display": {
                "exists": [Loc.DataDisplay.CREATE_REPORT_BTN],
                "not_exists": []
            },
        }

        config = page_configs.get(page_name, {"exists": [], "not_exists": []})

        def mock_element_exists(locator, **kwargs):
            if locator in config["exists"]:
                return True
            if locator in config["not_exists"]:
                return False
            return True  # Default to True for other elements

        mock_driver.element_exists = Mock(side_effect=mock_element_exists)


@pytest.fixture
def navigation_helper():
    """Provide navigation test helper."""
    return NavigationTestHelper()
