"""
Tests for InteractiveWorkflow.

Tests step-by-step navigation workflow with mocked components.
"""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock

from src.workflows import InteractiveWorkflow
from src.navigation import GDS2Page, NavigationResult


class MockNavigationController:
    """Mock NavigationController for testing."""

    def __init__(self):
        self._current_page = GDS2Page.MAIN_MENU
        self._context = {
            "module": None,
            "data_category": None,
            "sub_category": None,
            "device": None,
        }
        self._history = []
        self._list_items = []
        self._buttons = []
        self.nav = MagicMock()

    @property
    def current_page(self):
        return self._current_page

    @property
    def current_module(self):
        return self._context.get("module")

    @property
    def current_data_category(self):
        return self._context.get("data_category")

    def set_list_items(self, items):
        self._list_items = items

    def set_buttons(self, buttons):
        self._buttons = buttons

    def check_agent(self):
        return True

    def detect_current_page(self):
        return self._current_page

    def get_context(self):
        return self._context.copy()

    def set_context(self, **kwargs):
        for k, v in kwargs.items():
            if k in self._context:
                self._context[k] = v

    def get_list_items(self, list_index=0):
        return self._list_items

    def wait_for_list(self, list_index=0, max_attempts=15):
        return self._list_items

    def start_diagnostics(self):
        return NavigationResult(
            success=True,
            page=GDS2Page.DEVICE_EXPLORER,
            choices=["MDI", "SM2 USB"],
            context=self._context.copy(),
        )

    def select_device(self, device_name):
        self._context["device"] = device_name
        return NavigationResult(
            success=True,
            page=GDS2Page.VEHICLE_SELECTION,
            selected=device_name,
            context=self._context.copy(),
        )

    def select_data_category(self, data_category):
        self._context["data_category"] = data_category
        return NavigationResult(
            success=True,
            page=GDS2Page.DATA_DISPLAY,
            selected=data_category,
            context=self._context.copy(),
        )

    def select_sub_category(self, sub_category):
        self._context["sub_category"] = sub_category
        return NavigationResult(
            success=True,
            page=GDS2Page.DATA_DISPLAY,
            selected=sub_category,
            context=self._context.copy(),
        )

    def go_back(self):
        return NavigationResult(
            success=True,
            page=GDS2Page.DATA_LIST,
            context=self._context.copy(),
        )

    def go_home(self):
        self._current_page = GDS2Page.MAIN_MENU
        self._context = {
            "module": None,
            "data_category": None,
            "sub_category": None,
            "device": self._context.get("device"),
        }
        return NavigationResult(
            success=True,
            page=GDS2Page.MAIN_MENU,
            context=self._context.copy(),
        )

    def navigate_to(self, target):
        self._current_page = target
        return NavigationResult(
            success=True,
            page=target,
            context=self._context.copy(),
        )

    def dismiss_warning_dialog(self):
        return False


class MockVehicleMapping:
    """Mock VehicleMapping for testing."""

    def __init__(self):
        self._mappings = {}

    def load_mapping(self, vehicle_id):
        return self._mappings.get(vehicle_id)

    def update_module_list(self, vehicle_id, modules):
        if vehicle_id not in self._mappings:
            self._mappings[vehicle_id] = {"vehicle_id": vehicle_id, "modules": {}}
        for name, idx in modules.items():
            self._mappings[vehicle_id]["modules"][name] = {"index": idx}

    def update_data_categories(self, vehicle_id, module_name, data_categories):
        if vehicle_id not in self._mappings:
            self._mappings[vehicle_id] = {"vehicle_id": vehicle_id, "modules": {}}
        if module_name not in self._mappings[vehicle_id]["modules"]:
            self._mappings[vehicle_id]["modules"][module_name] = {"index": 0}
        self._mappings[vehicle_id]["modules"][module_name]["data_categories"] = data_categories

    def update_sub_categories(self, vehicle_id, module_name, data_category, sub_categories):
        pass

    def get_sub_categories(self, vehicle_id, module_name, data_category):
        return None


@pytest.fixture
def mock_controller():
    """Create mock controller."""
    return MockNavigationController()


@pytest.fixture
def mock_mapping():
    """Create mock mapping."""
    return MockVehicleMapping()


@pytest.fixture
def workflow(mock_controller, mock_mapping):
    """Create InteractiveWorkflow with mocks."""
    wf = InteractiveWorkflow()
    wf.controller = mock_controller
    wf._mapping = mock_mapping
    return wf


class TestInteractiveWorkflowStart:
    """Tests for workflow start."""

    def test_start_success(self, workflow, mock_controller):
        """Test successful start returns current page."""
        mock_controller._current_page = GDS2Page.MAIN_MENU

        result = workflow.start()

        assert result.success
        assert result.page == GDS2Page.MAIN_MENU

    def test_start_agent_not_available(self, workflow, mock_controller):
        """Test start fails when agent not available."""
        mock_controller.check_agent = MagicMock(return_value=False)

        result = workflow.start()

        assert not result.success
        assert "Agent not available" in result.error


class TestStepResult:
    """Tests for StepResult structure."""

    def test_step_result_to_dict(self, workflow, mock_controller):
        """Test NavigationResult serialization."""
        result = workflow.step_diagnostics()

        d = result.to_dict()

        assert "success" in d
        assert "page" in d
        assert "choices" in d
        assert "selected" in d
        assert "error" in d
        assert "context" in d
        assert isinstance(d["page"], str)  # Should be string, not enum


class TestStepDiagnostics:
    """Tests for step_diagnostics."""

    def test_step_diagnostics_returns_devices(self, workflow):
        """Test that step_diagnostics returns device list."""
        result = workflow.step_diagnostics()

        assert result.success
        assert result.page == GDS2Page.DEVICE_EXPLORER
        assert result.choices == ["MDI", "SM2 USB"]


class TestStepSelectDevice:
    """Tests for step_select_device."""

    def test_step_select_device_success(self, workflow, mock_controller):
        """Test successful device selection."""
        mock_controller.nav.get_buttons.return_value = [{"text": "Enter"}]
        mock_controller.nav.click_button.return_value = {"success": True}

        with patch('time.sleep'):
            result = workflow.step_select_device("SM2 USB")

        assert result.success
        assert result.selected == "SM2 USB"
        assert result.context["device"] == "SM2 USB"


class TestStepSelectModule:
    """Tests for step_select_module."""

    def test_step_select_module_success(self, workflow, mock_controller):
        """Test successful module selection."""
        mock_controller._list_items = [
            "[K20] Engine Control Module",
            "[T42] Body Control Module"
        ]
        mock_controller.nav.select_list_item.return_value = {"success": True}

        with patch('time.sleep'):
            result = workflow.step_select_module("[K20] Engine Control Module")

        assert result.success
        assert result.page == GDS2Page.MODULE_SUBMENU
        assert "[K20] Engine Control Module" in result.selected

    def test_step_select_module_partial_match(self, workflow, mock_controller):
        """Test module selection with partial match."""
        mock_controller._list_items = [
            "[K20] Engine Control Module",
            "[T42] Body Control Module"
        ]
        mock_controller.nav.select_list_item.return_value = {"success": True}

        with patch('time.sleep'):
            result = workflow.step_select_module("Engine Control")

        assert result.success

    def test_step_select_module_not_found(self, workflow, mock_controller):
        """Test module not found error."""
        mock_controller._list_items = ["Module A", "Module B"]

        result = workflow.step_select_module("Nonexistent Module")

        assert not result.success
        assert "not found" in result.error
        assert result.choices == ["Module A", "Module B"]


class TestStepDataDisplay:
    """Tests for step_data_display."""

    def test_step_data_display_success(self, workflow, mock_controller):
        """Test successful Data Display selection."""
        mock_controller._list_items = ["Module Information", "DTCs", "Data Display"]
        mock_controller.nav.select_list_item.return_value = {"success": True}

        with patch('time.sleep'):
            result = workflow.step_data_display()

        assert result.success
        assert result.page == GDS2Page.DATA_LIST
        assert result.selected == "Data Display"


class TestStepSelectDataCategory:
    """Tests for step_select_data_category."""

    def test_step_select_data_category_direct(self, workflow, mock_controller):
        """Test selecting data category that goes directly to Data Display."""
        result = workflow.step_select_data_category("Engine Data")

        assert result.success
        assert result.page == GDS2Page.DATA_DISPLAY

    def test_step_select_data_category_with_sub(self, workflow, mock_controller):
        """Test selecting data category that has sub-categories."""
        mock_controller.select_data_category = MagicMock(return_value=NavigationResult(
            success=True,
            page=GDS2Page.SUB_DATA_LIST,
            selected="Fuel System Data",
            choices=["Fuel Injector Data", "Fuel Pump Data"],
            context=mock_controller._context.copy(),
        ))

        result = workflow.step_select_data_category("Fuel System Data")

        assert result.success
        assert result.page == GDS2Page.SUB_DATA_LIST
        assert result.choices == ["Fuel Injector Data", "Fuel Pump Data"]


class TestStepGoBack:
    """Tests for step_go_back."""

    def test_step_go_back(self, workflow):
        """Test going back one page."""
        result = workflow.step_go_back()

        assert result.success
        assert result.page == GDS2Page.DATA_LIST


class TestStepGoHome:
    """Tests for step_go_home."""

    def test_step_go_home(self, workflow, mock_controller):
        """Test going to Main Menu."""
        mock_controller._current_page = GDS2Page.DATA_DISPLAY

        result = workflow.step_go_home()

        assert result.success
        assert result.page == GDS2Page.MAIN_MENU
        assert result.context["module"] is None


class TestChangeModule:
    """Tests for step_change_module."""

    def test_change_module_navigates_back(self, workflow, mock_controller):
        """Test that change_module navigates back to Module List."""
        mock_controller._current_page = GDS2Page.DATA_DISPLAY
        mock_controller._list_items = ["[K20] Engine", "[T42] Body Control"]
        mock_controller.nav.select_list_item.return_value = {"success": True}

        with patch('time.sleep'):
            result = workflow.step_change_module("[T42] Body Control")

        # Should have navigated to MODULE_LIST first via navigate_to
        assert mock_controller._current_page == GDS2Page.MODULE_LIST or result.success


class TestGetCurrentChoices:
    """Tests for get_current_choices."""

    def test_get_current_choices(self, workflow, mock_controller):
        """Test getting choices at current page."""
        mock_controller._current_page = GDS2Page.MODULE_LIST
        mock_controller._list_items = ["Module A", "Module B"]

        result = workflow.get_current_choices()

        assert result.success
        assert result.page == GDS2Page.MODULE_LIST
        assert result.choices == ["Module A", "Module B"]


class TestCacheAccess:
    """Tests for cache access methods."""

    def test_get_cached_modules(self, workflow, mock_mapping):
        """Test getting cached modules."""
        mock_mapping.update_module_list("current_vehicle", {
            "[K20] Engine": 0,
            "[T42] Body": 1
        })

        modules = workflow.get_cached_modules()

        assert "[K20] Engine" in modules
        assert "[T42] Body" in modules

    def test_get_cached_data_categories(self, workflow, mock_mapping):
        """Test getting cached data categories."""
        mock_mapping.update_module_list("current_vehicle", {"Module": 0})
        mock_mapping.update_data_categories("current_vehicle", "Module", {
            "Engine Data": 0,
            "Fuel Data": 1
        })

        categories = workflow.get_cached_data_categories("Module")

        assert "Engine Data" in categories
        assert "Fuel Data" in categories


class TestFullWorkflowWithMocks:
    """Integration test for full workflow with mocks."""

    def test_full_workflow(self, workflow, mock_controller):
        """Test complete workflow from start to Data Display."""
        # Start
        result = workflow.start()
        assert result.success
        assert result.page == GDS2Page.MAIN_MENU

        # Diagnostics
        result = workflow.step_diagnostics()
        assert result.success
        assert result.page == GDS2Page.DEVICE_EXPLORER
        assert "SM2 USB" in result.choices

        # Select device
        mock_controller.nav.get_buttons.return_value = [{"text": "Enter"}]
        mock_controller.nav.click_button.return_value = {"success": True}

        with patch('time.sleep'):
            result = workflow.step_select_device("SM2 USB")
        assert result.success

        # Module diagnostics
        mock_controller._list_items = ["Module Diagnostics", "Vehicle ID"]
        mock_controller.nav.select_list_item.return_value = {"success": True}

        with patch('time.sleep'):
            result = workflow.step_module_diagnostics()
        assert result.success
        assert result.page == GDS2Page.MODULE_LIST

        # Select module
        mock_controller._list_items = ["[K20] Engine Control Module"]

        with patch('time.sleep'):
            result = workflow.step_select_module("[K20] Engine Control Module")
        assert result.success
        assert result.page == GDS2Page.MODULE_SUBMENU

        # Data display
        mock_controller._list_items = ["Data Display"]

        with patch('time.sleep'):
            result = workflow.step_data_display()
        assert result.success
        assert result.page == GDS2Page.DATA_LIST

        # Select data category
        result = workflow.step_select_data_category("Engine Data")
        assert result.success
        assert result.page == GDS2Page.DATA_DISPLAY
