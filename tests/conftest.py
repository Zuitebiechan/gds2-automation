"""
Pytest Configuration for GDS2 RPA Tests

This file contains shared fixtures and configuration for all tests.
Note: Fixtures for archived PyAutoGUI-based code have been removed.
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
def vci_device(request):
    """Get VCI device name from command line or use default."""
    return request.config.getoption("--vci-device")


@pytest.fixture
def mock_agent_navigator():
    """Create a mock AgentNavigator for unit testing."""
    nav = Mock()
    nav.check_agent = Mock(return_value=True)
    nav.click_button = Mock(return_value={'success': True})
    nav.get_buttons = Mock(return_value=[{'text': 'Diagnostics', 'enabled': True}])
    nav.get_list_items = Mock(return_value=['Item 1', 'Item 2'])
    nav.select_list_item = Mock(return_value={'success': True})
    return nav


@pytest.fixture
def live_agent_navigator(request):
    """
    Create a real AgentNavigator for integration testing.

    This fixture only activates when --live flag is passed.
    Requires GDS2 with Agent to be running.
    """
    if not request.config.getoption("--live"):
        pytest.skip("Live test requires --live flag")

    from src.streaming import AgentNavigator

    nav = AgentNavigator()
    if not nav.check_agent():
        pytest.skip("Agent not available - start GDS2 with agent")

    yield nav
