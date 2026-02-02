"""
Interactive Workflow for GDS2 Navigation.

Provides step-by-step workflow where each step returns choices to the caller.
Replaces the monolithic execute() pattern with interactive navigation.

Usage:
    workflow = InteractiveWorkflow()

    # Start and get current state
    result = workflow.start()

    # Step through diagnostics
    result = workflow.step_diagnostics()
    if result.page == "device_explorer":
        result = workflow.step_select_device("SM2 USB")

    # Select module
    result = workflow.step_select_module("[K20] Engine Control Module")

    # Get to data list
    result = workflow.step_data_display()

    # Select data category (may have sub-categories)
    result = workflow.step_select_data_category("Engine Data")
    if result.page == "sub_data_list":
        result = workflow.step_select_sub_category("Fuel Injector Data")

    # Now at Data Display - can start streaming or create reports
"""

import logging
import time
import re
from pathlib import Path
from typing import Dict, List, Optional, Any

from ..navigation import NavigationController, NavigationResult, GDS2Page

logger = logging.getLogger(__name__)


class InteractiveWorkflow:
    """
    Step-by-step workflow where each step returns choices to the caller.

    Each step method returns a NavigationResult with:
    - success: Whether the step succeeded
    - page: Current GDS2 page after the step
    - choices: Available items to select (if applicable)
    - selected: What was just selected (if applicable)
    - error: Error message if failed
    - context: Current navigation context (module, data_category, etc.)
    """

    def __init__(self, nav=None):
        """
        Initialize interactive workflow.

        Args:
            nav: Optional AgentNavigator instance
        """
        self.controller = NavigationController(nav)
        self._mapping = None
        self._vehicle_id = "current_vehicle"

    @property
    def mapping(self):
        """Lazy-load VehicleMapping."""
        if self._mapping is None:
            from ..discovery import VehicleMapping
            self._mapping = VehicleMapping()
        return self._mapping

    @property
    def nav(self):
        """Access to underlying AgentNavigator."""
        return self.controller.nav

    @property
    def current_page(self) -> GDS2Page:
        """Get current page."""
        return self.controller.current_page

    @property
    def current_module(self) -> Optional[str]:
        """Get currently selected module."""
        return self.controller.current_module

    @property
    def current_data_category(self) -> Optional[str]:
        """Get currently selected data category."""
        return self.controller.current_data_category

    def check_agent(self) -> bool:
        """Check if Java Agent is available."""
        return self.controller.check_agent()

    # =========================================================================
    # Workflow Steps
    # =========================================================================

    def start(self) -> NavigationResult:
        """
        Begin workflow. Check agent and detect current page.

        Returns:
            NavigationResult with current page info and available choices
        """
        if not self.check_agent():
            return NavigationResult(
                success=False,
                page=GDS2Page.UNKNOWN,
                error="Java Agent not available. Start GDS2 with agent.",
                context=self.controller.get_context(),
            )

        page = self.controller.detect_current_page()
        choices = self._get_choices_for_page(page)

        return NavigationResult(
            success=True,
            page=page,
            choices=choices,
            context=self.controller.get_context(),
        )

    def step_diagnostics(self) -> NavigationResult:
        """
        Click Diagnostics button from Main Menu.

        If Device Explorer appears, returns device list in choices.
        Otherwise proceeds to Vehicle Selection.

        Returns:
            NavigationResult with:
            - page: DEVICE_EXPLORER (with device choices) or VEHICLE_SELECTION
        """
        return self.controller.start_diagnostics()

    def step_select_device(self, device_name: str) -> NavigationResult:
        """
        Select device in Device Explorer and click Continue.

        After device selection, automatically proceeds through Vehicle Selection.

        Args:
            device_name: Name of device (e.g., "SM2 USB")

        Returns:
            NavigationResult with next page info
        """
        return self.controller.select_device(device_name)

    def step_disconnect_device(self) -> NavigationResult:
        """
        Disconnect from current VCI device.

        Must be at Vehicle Selection page. After disconnect, use
        step_open_device_selector() to select a different device.

        Returns:
            NavigationResult with success status
        """
        return self.controller.disconnect_device()

    def step_open_device_selector(self) -> NavigationResult:
        """
        Open Device Explorer to select a different device.

        Must be at Vehicle Selection page after disconnecting.

        Returns:
            NavigationResult with DEVICE_EXPLORER page and device choices
        """
        return self.controller.open_device_selector()

    def step_click_enter(self) -> NavigationResult:
        """
        Click Enter button (typically at Vehicle Selection page).

        Returns:
            NavigationResult with next page after clicking Enter
        """
        return self.controller.click_enter()

    def get_available_buttons(self) -> dict:
        """
        Get available buttons for the current page.

        Returns:
            Dict mapping button name to enabled status
        """
        return self.controller.get_available_buttons()

    def step_module_diagnostics(self) -> NavigationResult:
        """
        Select Module Diagnostics from Diagnostics Menu.

        Returns:
            NavigationResult with MODULE_LIST page and module choices
        """
        items = self.controller.wait_for_list()
        if not items:
            return NavigationResult(
                success=False,
                page=self.controller.current_page,
                error="No menu items found",
                context=self.controller.get_context(),
            )

        # Find Module Diagnostics
        for i, item in enumerate(items):
            if "Module Diagnostics" in item:
                result = self.nav.select_list_item(0, i, double_click=True)
                if result.get('success'):
                    logger.info(f"Selected Module Diagnostics at index {i}")
                    time.sleep(3)

                    # Discover modules
                    modules = self.controller.wait_for_list()
                    if modules:
                        module_indices = {name: idx for idx, name in enumerate(modules)}
                        self.mapping.update_module_list(self._vehicle_id, module_indices)

                    self.controller._current_page = GDS2Page.MODULE_LIST

                    return NavigationResult(
                        success=True,
                        page=GDS2Page.MODULE_LIST,
                        selected="Module Diagnostics",
                        choices=modules,
                        context=self.controller.get_context(),
                    )

        return NavigationResult(
            success=False,
            page=self.controller.current_page,
            error="Module Diagnostics not found in menu",
            choices=items,
            context=self.controller.get_context(),
        )

    def step_select_module(self, module_name: str) -> NavigationResult:
        """
        Select a module from Module List.

        Args:
            module_name: Module name (e.g., "[K20] Engine Control Module")

        Returns:
            NavigationResult with MODULE_SUBMENU page and submenu choices
        """
        items = self.controller.wait_for_list()
        if not items:
            return NavigationResult(
                success=False,
                page=self.controller.current_page,
                error="No modules found",
                context=self.controller.get_context(),
            )

        # Find module
        target_index = None
        matched_item = None
        for i, item in enumerate(items):
            if module_name in item or item in module_name:
                target_index = i
                matched_item = item
                break

        # Try partial match with key parts
        if target_index is None:
            module_key = module_name.split("]")[-1].strip() if "]" in module_name else module_name
            for i, item in enumerate(items):
                if module_key in item:
                    target_index = i
                    matched_item = item
                    break

        if target_index is None:
            return NavigationResult(
                success=False,
                page=self.controller.current_page,
                error=f"Module '{module_name}' not found",
                choices=items,
                context=self.controller.get_context(),
            )

        result = self.nav.select_list_item(0, target_index, double_click=True)
        if not result.get('success'):
            return NavigationResult(
                success=False,
                page=self.controller.current_page,
                error=f"Failed to select module: {result.get('message')}",
                context=self.controller.get_context(),
            )

        time.sleep(3)

        # Update context
        self.controller.set_context(module=matched_item)

        # Get submenu items
        submenu_items = self.controller.wait_for_list()
        self.controller._current_page = GDS2Page.MODULE_SUBMENU

        return NavigationResult(
            success=True,
            page=GDS2Page.MODULE_SUBMENU,
            selected=matched_item,
            choices=submenu_items,
            context=self.controller.get_context(),
        )

    def step_data_display(self) -> NavigationResult:
        """
        Select Data Display from Module Submenu.

        Returns:
            NavigationResult with DATA_LIST page and data category choices
        """
        items = self.controller.wait_for_list()
        if not items:
            return NavigationResult(
                success=False,
                page=self.controller.current_page,
                error="No submenu items found",
                context=self.controller.get_context(),
            )

        # Find Data Display
        for i, item in enumerate(items):
            if "Data Display" in item:
                result = self.nav.select_list_item(0, i, double_click=True)
                if result.get('success'):
                    logger.info(f"Selected Data Display at index {i}")
                    time.sleep(3)

                    # Handle warning dialog
                    self.controller.dismiss_warning_dialog()

                    # Discover data categories
                    data_categories = self.controller.wait_for_list()
                    if data_categories:
                        module = self.controller.current_module
                        if module:
                            cat_indices = {name: idx for idx, name in enumerate(data_categories)}
                            self.mapping.update_data_categories(self._vehicle_id, module, cat_indices)

                    self.controller._current_page = GDS2Page.DATA_LIST

                    return NavigationResult(
                        success=True,
                        page=GDS2Page.DATA_LIST,
                        selected="Data Display",
                        choices=data_categories,
                        context=self.controller.get_context(),
                    )

        return NavigationResult(
            success=False,
            page=self.controller.current_page,
            error="Data Display not found in submenu",
            choices=items,
            context=self.controller.get_context(),
        )

    def step_select_data_category(self, data_category: str) -> NavigationResult:
        """
        Select a data category from Data List.

        May result in:
        - DATA_DISPLAY: Direct navigation to data display
        - SUB_DATA_LIST: Category has sub-categories to choose from

        Args:
            data_category: Data category name

        Returns:
            NavigationResult with page info (DATA_DISPLAY or SUB_DATA_LIST)
        """
        result = self.controller.select_data_category(data_category)

        # If sub-categories discovered, save them
        if result.success and result.page == GDS2Page.SUB_DATA_LIST:
            module = self.controller.current_module
            if module and result.choices:
                sub_indices = {name: idx for idx, name in enumerate(result.choices)}
                self.mapping.update_sub_categories(
                    self._vehicle_id, module, data_category, sub_indices
                )

        return result

    def step_select_sub_category(self, sub_category: str) -> NavigationResult:
        """
        Select a sub-category from Sub Data List.

        Args:
            sub_category: Sub-category name

        Returns:
            NavigationResult with DATA_DISPLAY page
        """
        return self.controller.select_sub_category(sub_category)

    def step_go_back(self) -> NavigationResult:
        """
        Go back one page.

        Returns:
            NavigationResult with new page info and available choices
        """
        result = self.controller.go_back()
        if result.success:
            result.choices = self._get_choices_for_page(result.page)
        return result

    def step_go_home(self) -> NavigationResult:
        """
        Go to Main Menu.

        Returns:
            NavigationResult with MAIN_MENU page
        """
        return self.controller.go_home()

    def step_change_module(self, new_module: str) -> NavigationResult:
        """
        Navigate back to Module List and select a different module.

        Args:
            new_module: New module name to select

        Returns:
            NavigationResult with MODULE_SUBMENU page after selecting new module
        """
        # Navigate back to Module List
        result = self.controller.navigate_to(GDS2Page.MODULE_LIST)
        if not result.success:
            return result

        # Select new module
        return self.step_select_module(new_module)

    def step_change_data_category(self, new_data_category: str) -> NavigationResult:
        """
        Navigate back to Data List and select a different data category.

        Args:
            new_data_category: New data category name

        Returns:
            NavigationResult with DATA_DISPLAY or SUB_DATA_LIST page
        """
        # Navigate back to Data List
        result = self.controller.navigate_to(GDS2Page.DATA_LIST)
        if not result.success:
            return result

        # Select new data category
        return self.step_select_data_category(new_data_category)

    def get_current_choices(self) -> NavigationResult:
        """
        Get available choices at current page without navigation.

        Returns:
            NavigationResult with current page info and choices
        """
        page = self.controller.detect_current_page()
        choices = self._get_choices_for_page(page)

        return NavigationResult(
            success=True,
            page=page,
            choices=choices,
            context=self.controller.get_context(),
        )

    # =========================================================================
    # Data Operations (at DATA_DISPLAY page)
    # =========================================================================

    def create_report(self) -> Optional[str]:
        """
        Click Create Report button and return report path.

        Must be at DATA_DISPLAY page.

        Returns:
            Path to created HTML report, or None if failed
        """
        # Wait for Create Report button
        for _ in range(30):
            buttons = self.nav.get_buttons()
            if any(b.get('text') == 'Create Report' for b in buttons):
                result = self.nav.click_button("Create Report")
                if result.get('success'):
                    logger.info("Clicked Create Report")
                    time.sleep(2)

                    # Find latest report
                    report_dir = Path.home() / "AppData" / "Local" / "Temp" / "GDS 2"
                    if report_dir.exists():
                        reports = list(report_dir.glob("Data Display_*.html"))
                        if reports:
                            return str(max(reports, key=lambda p: p.stat().st_mtime))
                    return None
            time.sleep(1)

        logger.error("Create Report button not found")
        return None

    def parse_report(self, report_path: str) -> Dict[str, Any]:
        """
        Parse HTML report for data items.

        Args:
            report_path: Path to HTML report

        Returns:
            Dict with vehicle_info and data_items
        """
        result = {"vehicle_info": {}, "data_items": []}

        try:
            with open(report_path, 'r', encoding='iso-8859-1') as f:
                html_content = f.read()

            vin_match = re.search(r'Vehicle Identification Number \(VIN\)</td><td>([^<]+)</td>', html_content)
            if vin_match:
                result["vehicle_info"]["vin"] = vin_match.group(1)

            make_match = re.search(r'<td>Make</td><td>([^<]+)</td>', html_content)
            if make_match:
                result["vehicle_info"]["make"] = make_match.group(1)

            model_match = re.search(r'<td>Model</td><td>([^<]+)</td>', html_content)
            if model_match:
                result["vehicle_info"]["model"] = model_match.group(1)

            year_match = re.search(r'<td>Model Year</td><td>([^<]+)</td>', html_content)
            if year_match:
                result["vehicle_info"]["year"] = year_match.group(1)

            data_pattern = r'<tr><td>([^<]+)</td><td>([^<]*)</td><td>([^<]*)</td></tr>'
            matches = re.findall(data_pattern, html_content)

            for match in matches:
                parameter, value, units = match
                if parameter not in ["Parameter", "Control Module", "DTC Type"]:
                    result["data_items"].append({
                        "parameter": parameter.strip(),
                        "value": value.strip(),
                        "units": units.strip(),
                    })

        except Exception as e:
            logger.error(f"Error parsing report: {e}")

        return result

    # =========================================================================
    # Cache Access
    # =========================================================================

    def get_cached_modules(self) -> List[str]:
        """Get modules from cache."""
        mapping = self.mapping.load_mapping(self._vehicle_id)
        if mapping and "modules" in mapping:
            return list(mapping["modules"].keys())
        return []

    def get_cached_data_categories(self, module_name: str) -> List[str]:
        """Get data categories for a module from cache."""
        mapping = self.mapping.load_mapping(self._vehicle_id)
        if not mapping:
            return []

        modules = mapping.get("modules", {})
        module_info = modules.get(module_name, {})
        data_categories = module_info.get("data_categories", {})
        return list(data_categories.keys())

    def get_cached_sub_categories(self, module_name: str, data_category: str) -> Optional[List[str]]:
        """Get sub-categories from cache."""
        sub_cats = self.mapping.get_sub_categories(self._vehicle_id, module_name, data_category)
        if sub_cats:
            return list(sub_cats.keys())
        return None

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _get_choices_for_page(self, page: GDS2Page) -> Optional[List[str]]:
        """Get available choices for a given page."""
        if page in (GDS2Page.MAIN_MENU, GDS2Page.VEHICLE_SELECTION):
            # No list choices, just buttons
            return None

        if page == GDS2Page.DEVICE_EXPLORER:
            from ..native import DeviceExplorerController
            device_ctrl = DeviceExplorerController()
            if device_ctrl.find_dialog(timeout_sec=1.0):
                return device_ctrl.get_device_names()
            return None

        # For list-based pages, get current list items
        return self.controller.get_list_items()
