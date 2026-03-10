"""
Tests for NavigationController.

Tests page detection and navigation state management using mocked Agent.
"""

import pytest
from unittest.mock import MagicMock, patch

from src.navigation import GDS2Page, NavigationController, NavigationResult


class MockAgentNavigator:
    """Mock AgentNavigator for testing."""

    def __init__(self):
        self._buttons = []
        self._list_items = []
        self._click_results = {}
        self._page_id_result = {}  # Empty = fallback to heuristic

    def set_buttons(self, buttons: list):
        """Set mock buttons (list of dicts with 'text' key)."""
        self._buttons = [{"text": b} if isinstance(b, str) else b for b in buttons]

    def set_list_items(self, items: list):
        """Set mock list items."""
        self._list_items = items

    def set_click_result(self, button_text: str, success: bool = True, message: str = ""):
        """Set result for clicking a button."""
        self._click_results[button_text] = {"success": success, "message": message}

    def check_agent(self) -> bool:
        return True

    def get_buttons(self) -> list:
        return self._buttons

    def get_list_items(self, list_index: int = 0) -> list:
        return self._list_items

    def click_button(self, text: str) -> dict:
        if text in self._click_results:
            return self._click_results[text]
        return {"success": True, "message": ""}

    def select_list_item(self, list_index: int, item_index: int, double_click: bool = True) -> dict:
        return {"success": True, "message": ""}

    def get_page_id(self) -> dict:
        """Return mock page identification result."""
        return self._page_id_result

    def set_page_id_result(self, page_id: str = '', confidence: str = 'high',
                           evidence: str = ''):
        """Set mock get_page_id result."""
        if page_id:
            self._page_id_result = {
                'page_id': page_id,
                'confidence': confidence,
                'evidence': evidence,
                'window_title': '',
                'buttons': [],
                'list_item_count': 0,
                'has_modal': False,
            }
        else:
            self._page_id_result = {}


@pytest.fixture
def mock_nav():
    """Create mock navigator."""
    return MockAgentNavigator()


@pytest.fixture
def controller(mock_nav):
    """Create NavigationController with mock navigator."""
    return NavigationController(nav=mock_nav)


class TestPageDetection:
    """Tests for page detection logic."""

    def test_detect_main_menu(self, controller, mock_nav):
        """Test detection of Main Menu page."""
        mock_nav.set_buttons(["Diagnostics", "Update", "Settings", "Info"])
        mock_nav.set_list_items([])

        page = controller.detect_current_page()

        assert page == GDS2Page.MAIN_MENU

    def test_detect_data_display(self, controller, mock_nav):
        """Test detection of Data Display page (has Create Report button)."""
        mock_nav.set_buttons(["Back", "Home", "Refresh", "Create Report"])
        mock_nav.set_list_items([])

        page = controller.detect_current_page()

        assert page == GDS2Page.DATA_DISPLAY

    def test_detect_module_submenu(self, controller, mock_nav):
        """Test detection of Module Submenu (list contains Data Display)."""
        mock_nav.set_buttons(["Back", "Home"])
        mock_nav.set_list_items([
            "Module Information",
            "Diagnostic Trouble Codes (DTCs)",
            "Data Display",
            "Special Functions"
        ])

        page = controller.detect_current_page()

        assert page == GDS2Page.MODULE_SUBMENU

    def test_detect_diagnostics_menu(self, controller, mock_nav):
        """Test detection of Diagnostics Menu (list contains Module Diagnostics)."""
        mock_nav.set_buttons(["Back", "Home"])
        mock_nav.set_list_items([
            "Module Diagnostics",
            "Vehicle ID",
            "Vehicle DTC Information"
        ])

        page = controller.detect_current_page()

        assert page == GDS2Page.DIAGNOSTICS_MENU

    def test_detect_module_list(self, controller, mock_nav):
        """Test detection of Module List (items contain brackets like [K20])."""
        mock_nav.set_buttons(["Back", "Home"])
        mock_nav.set_list_items([
            "[K20] Engine Control Module",
            "[T42] Body Control Module",
            "[N37] Instrument Cluster"
        ])

        page = controller.detect_current_page()

        assert page == GDS2Page.MODULE_LIST

    def test_detect_vehicle_selection(self, controller, mock_nav):
        """Test detection of Vehicle Selection page (has Enter, no Back)."""
        mock_nav.set_buttons(["Enter", "Cancel"])
        mock_nav.set_list_items([])

        page = controller.detect_current_page()

        assert page == GDS2Page.VEHICLE_SELECTION

    def test_detect_data_list(self, controller, mock_nav):
        """Test detection of Data List (generic list with Back button)."""
        mock_nav.set_buttons(["Back", "Home"])
        mock_nav.set_list_items([
            "Engine Data",
            "Fuel System Data",
            "Misfire Data",
            "Ignition Data"
        ])

        page = controller.detect_current_page()

        assert page == GDS2Page.DATA_LIST

    def test_detect_unknown(self, controller, mock_nav):
        """Test detection returns UNKNOWN when no patterns match."""
        mock_nav.set_buttons([])
        mock_nav.set_list_items([])

        page = controller.detect_current_page()

        assert page == GDS2Page.UNKNOWN


class TestNavigationActions:
    """Tests for navigation actions."""

    def test_go_back_updates_state(self, controller, mock_nav):
        """Test that go_back updates state correctly."""
        # Start at Data Display
        mock_nav.set_buttons(["Back", "Home", "Create Report"])
        controller.detect_current_page()
        assert controller.current_page == GDS2Page.DATA_DISPLAY

        # After back, should detect new page
        mock_nav.set_buttons(["Back", "Home"])
        mock_nav.set_list_items(["Engine Data", "Fuel Data", "Misfire Data"])
        mock_nav.set_click_result("Back", success=True)

        with patch('time.sleep'):
            result = controller.go_back()

        assert result.success
        assert result.page == GDS2Page.DATA_LIST

    def test_go_back_failure(self, controller, mock_nav):
        """Test go_back when button click fails."""
        mock_nav.set_buttons(["Back", "Home"])
        mock_nav.set_click_result("Back", success=False, message="Button not found")

        result = controller.go_back()

        assert not result.success
        assert "Failed to click Back" in result.error

    def test_go_home(self, controller, mock_nav):
        """Test go_home returns to Main Menu."""
        mock_nav.set_click_result("Home", success=True)
        mock_nav.set_buttons(["Diagnostics", "Update"])

        with patch('time.sleep'):
            result = controller.go_home()

        assert result.success
        assert result.page == GDS2Page.MAIN_MENU

    def test_navigate_to_from_deeper_page(self, controller, mock_nav):
        """Test navigate_to clicks Back multiple times to reach target."""
        # Start at Data Display
        mock_nav.set_buttons(["Back", "Home", "Create Report"])
        mock_nav.set_list_items([])
        controller.detect_current_page()
        assert controller.current_page == GDS2Page.DATA_DISPLAY

        # Mock page transitions: Data Display -> Data List -> Module Submenu -> Module List
        pages = [
            (["Back", "Home"], ["Engine Data", "Fuel Data"]),  # Data List
            (["Back", "Home"], ["Data Display", "DTCs"]),       # Module Submenu
            (["Back", "Home"], ["[K20] Engine Control", "[T42] Body Control"]),  # Module List
        ]
        page_index = [0]

        def mock_click_back(text):
            if text == "Back" and page_index[0] < len(pages):
                btns, items = pages[page_index[0]]
                mock_nav.set_buttons(btns)
                mock_nav.set_list_items(items)
                page_index[0] += 1
            return {"success": True, "message": ""}

        mock_nav.click_button = mock_click_back

        with patch('time.sleep'):
            result = controller.navigate_to(GDS2Page.MODULE_LIST)

        assert result.success
        assert result.page == GDS2Page.MODULE_LIST


class TestHistoryTracking:
    """Tests for navigation history."""

    def test_history_tracking(self, controller, mock_nav):
        """Test that history is tracked during navigation."""
        # Initial state
        mock_nav.set_buttons(["Diagnostics", "Update"])
        controller.detect_current_page()
        assert controller.current_page == GDS2Page.MAIN_MENU
        assert len(controller.history) == 0

        # Simulate selection (would normally push to history)
        controller._history.append(GDS2Page.MAIN_MENU)
        controller._current_page = GDS2Page.DIAGNOSTICS_MENU

        assert len(controller.history) == 1
        assert controller.history[0] == GDS2Page.MAIN_MENU

    def test_history_cleared_on_home(self, controller, mock_nav):
        """Test that history is cleared when going home."""
        # Add some history
        controller._history = [GDS2Page.MAIN_MENU, GDS2Page.DIAGNOSTICS_MENU, GDS2Page.MODULE_LIST]
        controller._current_page = GDS2Page.DATA_DISPLAY

        mock_nav.set_click_result("Home", success=True)
        mock_nav.set_buttons(["Diagnostics", "Update"])

        with patch('time.sleep'):
            result = controller.go_home()

        assert result.success
        assert len(controller.history) == 0


class TestContextManagement:
    """Tests for context management."""

    def test_context_cleared_on_back_to_module_list(self, controller, mock_nav):
        """Test that module context is cleared when going back to Module List."""
        controller.set_context(module="[K20] Engine", data_category="Engine Data")
        assert controller.current_module == "[K20] Engine"
        assert controller.current_data_category == "Engine Data"

        # Mock back to module list
        mock_nav.set_buttons(["Back", "Home"])
        mock_nav.set_list_items(["[K20] Engine", "[T42] Body"])
        mock_nav.set_click_result("Back", success=True)

        with patch('time.sleep'):
            result = controller.go_back()

        assert result.page == GDS2Page.MODULE_LIST
        assert controller.current_module is None
        assert controller.current_data_category is None

    def test_set_and_get_context(self, controller):
        """Test setting and getting context."""
        controller.set_context(
            module="[K20] Engine Control Module",
            data_category="Misfire Data",
            device="SM2 USB"
        )

        ctx = controller.get_context()
        assert ctx["module"] == "[K20] Engine Control Module"
        assert ctx["data_category"] == "Misfire Data"
        assert ctx["device"] == "SM2 USB"

    def test_clear_context_keeps_device(self, controller):
        """Test that clear_context keeps device selection."""
        controller.set_context(
            module="[K20] Engine",
            data_category="Engine Data",
            device="SM2 USB"
        )

        controller.clear_context()

        assert controller.current_module is None
        assert controller.current_data_category is None
        assert controller._context["device"] == "SM2 USB"


class TestNavigationResult:
    """Tests for NavigationResult dataclass."""

    def test_to_dict(self):
        """Test NavigationResult serialization."""
        result = NavigationResult(
            success=True,
            page=GDS2Page.DATA_DISPLAY,
            choices=["Engine Data", "Fuel Data"],
            selected="Engine Data",
            context={"module": "[K20] Engine"}
        )

        d = result.to_dict()

        assert d["success"] is True
        assert d["page"] == "data_display"
        assert d["choices"] == ["Engine Data", "Fuel Data"]
        assert d["selected"] == "Engine Data"
        assert d["context"]["module"] == "[K20] Engine"

    def test_error_result(self):
        """Test NavigationResult with error."""
        result = NavigationResult(
            success=False,
            page=GDS2Page.UNKNOWN,
            error="Agent not connected"
        )

        assert not result.success
        assert result.error == "Agent not connected"
        assert result.choices is None


class TestListSelection:
    """Tests for list item selection."""

    def test_select_list_item_by_text(self, controller, mock_nav):
        """Test selecting a list item by text."""
        mock_nav.set_list_items([
            "Engine Data",
            "Fuel System Data",
            "Misfire Data"
        ])
        mock_nav.set_buttons(["Back", "Home"])

        with patch('time.sleep'):
            result = controller.select_list_item("Misfire Data")

        assert result.success
        assert result.selected == "Misfire Data"

    def test_select_list_item_partial_match(self, controller, mock_nav):
        """Test selecting by partial text match."""
        mock_nav.set_list_items([
            "[K20] Engine Control Module",
            "[T42] Body Control Module"
        ])
        mock_nav.set_buttons(["Back", "Home"])

        with patch('time.sleep'):
            result = controller.select_list_item("Engine Control")

        assert result.success
        assert "[K20] Engine Control Module" in result.selected

    def test_select_list_item_not_found(self, controller, mock_nav):
        """Test error when item not found."""
        mock_nav.set_list_items(["Engine Data", "Fuel Data"])

        result = controller.select_list_item("Nonexistent Item")

        assert not result.success
        assert "not found" in result.error
        assert result.choices == ["Engine Data", "Fuel Data"]


class TestAgentPageDetection:
    """Tests for Java Agent-based page detection (get_page_id)."""

    def test_agent_detection_main_menu(self, controller, mock_nav):
        """Test Agent-based detection of Main Menu."""
        mock_nav.set_page_id_result('main_menu', 'high', 'buttons Diagnostics + Update')
        page = controller.detect_current_page()
        assert page == GDS2Page.MAIN_MENU

    def test_agent_detection_data_display(self, controller, mock_nav):
        """Test Agent-based detection of Data Display."""
        mock_nav.set_page_id_result('data_display', 'high', 'Create Report button')
        page = controller.detect_current_page()
        assert page == GDS2Page.DATA_DISPLAY

    def test_agent_detection_module_list(self, controller, mock_nav):
        """Test Agent-based detection of Module List."""
        mock_nav.set_page_id_result('module_list', 'high', 'list items with [...] patterns')
        page = controller.detect_current_page()
        assert page == GDS2Page.MODULE_LIST

    def test_agent_detection_module_submenu(self, controller, mock_nav):
        """Test Agent-based detection of Module Submenu."""
        mock_nav.set_page_id_result('module_submenu', 'high', 'Data Display in list')
        page = controller.detect_current_page()
        assert page == GDS2Page.MODULE_SUBMENU

    def test_agent_detection_diagnostics_menu(self, controller, mock_nav):
        """Test Agent-based detection of Diagnostics Menu."""
        mock_nav.set_page_id_result('diagnostics_menu', 'high', 'Module Diagnostics in list')
        page = controller.detect_current_page()
        assert page == GDS2Page.DIAGNOSTICS_MENU

    def test_agent_detection_vehicle_selection(self, controller, mock_nav):
        """Test Agent-based detection of Vehicle Selection."""
        mock_nav.set_page_id_result('vehicle_selection', 'high', 'Enter + Disconnect')
        page = controller.detect_current_page()
        assert page == GDS2Page.VEHICLE_SELECTION

    def test_agent_detection_data_list(self, controller, mock_nav):
        """Test Agent-based detection of Data List."""
        mock_nav.set_page_id_result('data_list', 'medium', 'list + Back, no markers')
        page = controller.detect_current_page()
        assert page == GDS2Page.DATA_LIST

    def test_agent_detection_unknown_falls_back_to_heuristic(self, controller, mock_nav):
        """Test that UNKNOWN from Agent falls back to heuristic."""
        mock_nav.set_page_id_result('unknown', 'low', 'no matching rule')
        # Set up heuristic data
        mock_nav.set_buttons(["Diagnostics", "Update", "Settings"])
        mock_nav.set_list_items([])
        page = controller.detect_current_page()
        assert page == GDS2Page.MAIN_MENU

    def test_agent_detection_empty_falls_back_to_heuristic(self, controller, mock_nav):
        """Test that empty result from Agent falls back to heuristic."""
        # Empty page_id_result (simulates older Agent without get_page_id)
        mock_nav.set_page_id_result()
        mock_nav.set_buttons(["Back", "Home", "Create Report"])
        mock_nav.set_list_items([])
        page = controller.detect_current_page()
        assert page == GDS2Page.DATA_DISPLAY

    def test_agent_detection_all_page_ids(self, controller, mock_nav):
        """Test all page_id values map to correct GDS2Page enums."""
        page_id_to_enum = {
            'main_menu': GDS2Page.MAIN_MENU,
            'device_explorer': GDS2Page.DEVICE_EXPLORER,
            'vehicle_selection': GDS2Page.VEHICLE_SELECTION,
            'diagnostics_menu': GDS2Page.DIAGNOSTICS_MENU,
            'module_list': GDS2Page.MODULE_LIST,
            'module_submenu': GDS2Page.MODULE_SUBMENU,
            'data_list': GDS2Page.DATA_LIST,
            'sub_data_list': GDS2Page.SUB_DATA_LIST,
            'data_display': GDS2Page.DATA_DISPLAY,
        }
        for page_id, expected_enum in page_id_to_enum.items():
            mock_nav.set_page_id_result(page_id, 'high', f'test {page_id}')
            page = controller.detect_current_page()
            assert page == expected_enum, f"{page_id} should map to {expected_enum}"
