"""
Data Viewer Workflow for GDS2.

Simplified three-step workflow: Device -> Module -> Data Category.
Handles all GDS2 navigation automatically in the background.

Supports AI-powered exception recovery (Day 6 integration).
"""

import logging
import re
import time
import json
from pathlib import Path
from typing import Callable, Dict, List, Optional, Any

from ..navigation import NavigationController, NavigationResult, GDS2Page
from ..recovery.decorators import with_recovery

logger = logging.getLogger(__name__)

StatusCallback = Optional[Callable[[str], None]]


class DataViewerWorkflow:
    """Simplified three-step workflow: Device -> Module -> Data."""

    def __init__(self, nav=None, enable_ai_recovery=False):
        self.controller = NavigationController(nav)
        self._mapping = None
        self._vehicle_id = "current_vehicle"
        self._vin = None
        self._device = None
        self._module = None
        self._data_category = None

        # AI Recovery Integration (Day 6)
        self.recovery = None
        if enable_ai_recovery:
            from ..recovery import RecoveryManager
            try:
                self.recovery = RecoveryManager(
                    agent_navigator=self.controller.nav if hasattr(self.controller, 'nav') else None,
                    navigation_controller=self.controller,
                )
                if self.recovery.enabled:
                    self.recovery.reset_session()
                    logger.info("AI recovery enabled and initialized")
                else:
                    logger.info("AI recovery disabled (check config/API key)")
                    self.recovery = None
            except Exception as e:
                logger.warning(f"Failed to initialize AI recovery: {e}")
                self.recovery = None

    @property
    def mapping(self):
        if self._mapping is None:
            from ..discovery import VehicleMapping
            self._mapping = VehicleMapping()
        return self._mapping

    @with_recovery
    def start(self, on_status: StatusCallback = None) -> dict:
        """
        Initialize the Data Viewer.

        Two possible outcomes:
        1. Device NOT connected → returns {"devices": [...]} for user to pick
        2. Device already connected → navigates all the way to Module List,
           returns {"modules": [...], "device_connected": True}

        Returns: dict with either "devices" or "modules" key
        """
        def status(msg):
            logger.info(msg)
            if on_status:
                on_status(msg)

        # Navigate to Main Menu first
        self._navigate_to_main_menu(status)

        # Check current page
        current = self.controller.detect_current_page()

        # If already past Main Menu, device is connected - go straight to Module List
        connected_pages = (GDS2Page.VEHICLE_SELECTION, GDS2Page.DIAGNOSTICS_MENU,
                          GDS2Page.MODULE_LIST, GDS2Page.MODULE_SUBMENU,
                          GDS2Page.DATA_LIST, GDS2Page.DATA_DISPLAY)

        if current in connected_pages:
            status("Device already connected")
            return self._navigate_to_module_list_from(current, status)

        # At Main Menu - click Diagnostics
        status("Opening Diagnostics...")
        result = self.controller.start_diagnostics()

        if not result.success:
            raise RuntimeError(f"Failed to start diagnostics: {result.error}")

        # Check what appeared after clicking Diagnostics
        if result.page == GDS2Page.DEVICE_EXPLORER:
            # Device not connected - show device list
            status("Scanning for devices...")
            from ..native.device_explorer import DeviceExplorerController
            explorer = DeviceExplorerController()

            if not explorer.find_dialog(timeout_sec=5.0):
                raise RuntimeError("Device Explorer dialog not found")

            devices = explorer.get_device_names()
            if not devices:
                raise RuntimeError("No devices found in Device Explorer")

            status(f"Found {len(devices)} device(s)")

            return {
                "devices": devices,
                "at_device_explorer": True,
                "device_connected": False,
            }

        # Device already connected - went past Device Explorer
        if result.page in connected_pages:
            status("Device already connected")
            return self._navigate_to_module_list_from(result.page, status)

        raise RuntimeError(f"Unexpected page after Diagnostics: {result.page.value}")

    def _navigate_to_module_list_from(self, current: GDS2Page, status) -> dict:
        """
        Navigate from the given page all the way to Module List.

        Handles: Vehicle Selection → Enter → Diagnostics Menu → Module Diagnostics → Module List
        Also handles being at Diagnostics Menu, Module List, Data List, etc.

        Returns: {"modules": [...], "device_connected": True, ...}
        """
        # Handle Vehicle Selection - click Enter
        if current == GDS2Page.VEHICLE_SELECTION:
            status("Waiting for vehicle to load...")
            enter_enabled = self._wait_for_button_enabled("Enter", timeout_sec=30)
            if not enter_enabled:
                raise RuntimeError("Enter button did not become enabled within 30 seconds")

            status("Entering vehicle...")
            result = self.controller.click_enter()
            if not result.success:
                raise RuntimeError(f"Failed to enter vehicle: {result.error}")
            current = result.page

        # Handle Diagnostics Menu - select Module Diagnostics
        if current == GDS2Page.DIAGNOSTICS_MENU:
            status("Selecting Module Diagnostics...")
            items = self.controller.wait_for_list()
            if items:
                for i, item in enumerate(items):
                    if "Module Diagnostics" in item:
                        self.controller.nav.select_list_item(0, i, double_click=True)
                        # Wait for page transition instead of blind sleep
                        try:
                            new_page = self.controller.wait_for_page_transition(
                                GDS2Page.DIAGNOSTICS_MENU, timeout=30
                            )
                            current = new_page
                        except TimeoutError:
                            logger.warning("Page did not transition after Module Diagnostics selection")
                            current = self.controller.detect_current_page()
                        break

        # If deeper than Module List, navigate back
        if current in (GDS2Page.DATA_DISPLAY, GDS2Page.DATA_LIST,
                       GDS2Page.SUB_DATA_LIST, GDS2Page.MODULE_SUBMENU):
            status("Navigating to Module List...")
            self._navigate_to_module_list(status)

        # Now at Module List - scan modules
        status("Scanning modules...")
        modules = self.controller.wait_for_list()
        if not modules:
            raise RuntimeError("No modules found at Module List")

        self.controller._current_page = GDS2Page.MODULE_LIST

        # Save modules to cache
        module_indices = {name: idx for idx, name in enumerate(modules)}
        self.mapping.update_module_list(self._vehicle_id, module_indices)

        # Extract VIN
        self._vin = self._extract_vin()
        if self._vin:
            self._vehicle_id = self._vin
            self.mapping.update_module_list(self._vehicle_id, module_indices)

        self._device = self._device or "Connected Device"
        self._module = None
        self._data_category = None
        self.controller.set_context(device=self._device)

        status("Ready")

        return {
            "modules": modules,
            "vin": self._vin,
            "device": self._device,
            "device_connected": True,
        }

    @with_recovery
    def connect_device(self, device_name: str, on_status: StatusCallback = None) -> dict:
        """
        Select device and navigate to Module List.

        Works from ANY page:
        - If device_name is empty/default and device already connected, continues from current page.
        - If Device Explorer is open (after start()), selects device directly.
        - If at any other page, navigates to Main Menu, clicks Diagnostics,
          handles disconnect/re-select as needed.

        Returns: {"modules": [...], "vin": "..."}
        """
        def status(msg):
            logger.info(msg)
            if on_status:
                on_status(msg)

        # Stop any active monitoring
        self.stop_monitoring()

        status("Connecting...")

        # Check if this is a "continue with existing device" request
        current = self.controller.detect_current_page()
        is_continue_request = (not device_name or device_name == "default")

        # Pages that indicate device is already connected
        connected_pages = (GDS2Page.VEHICLE_SELECTION, GDS2Page.DIAGNOSTICS_MENU,
                          GDS2Page.MODULE_LIST, GDS2Page.MODULE_SUBMENU,
                          GDS2Page.DATA_LIST, GDS2Page.DATA_DISPLAY)

        if is_continue_request and current in connected_pages:
            # Device already connected, continue from current page
            logger.info(f"Device already connected at {current.value}, continuing...")
            status("Device already connected, continuing...")
        else:
            # Need to select a device
            if not device_name or device_name == "default":
                raise RuntimeError("Device name required when not at a connected page")
            self._ensure_device_selected(device_name, status)
            current = self.controller.detect_current_page()

        # Now navigate from current page to Module List
        logger.info(f"Navigating to Module List from {current.value}")

        # Handle Vehicle Selection - wait for Enter to be enabled, then click
        if current == GDS2Page.VEHICLE_SELECTION:
            status("Waiting for vehicle to load...")
            enter_enabled = self._wait_for_button_enabled("Enter", timeout_sec=30)
            if not enter_enabled:
                raise RuntimeError("Enter button did not become enabled within 30 seconds")

            status("Entering vehicle...")
            result = self.controller.click_enter()
            if not result.success:
                raise RuntimeError(f"Failed to enter vehicle: {result.error}")
            current = result.page

        # Handle Diagnostics Menu - select Module Diagnostics
        if current == GDS2Page.DIAGNOSTICS_MENU:
            status("Selecting Module Diagnostics...")
            items = self.controller.wait_for_list()
            if items:
                for i, item in enumerate(items):
                    if "Module Diagnostics" in item:
                        self.controller.nav.select_list_item(0, i, double_click=True)
                        try:
                            new_page = self.controller.wait_for_page_transition(
                                GDS2Page.DIAGNOSTICS_MENU, timeout=30
                            )
                            current = new_page
                        except TimeoutError:
                            logger.warning("Page did not transition after Module Diagnostics selection")
                            current = self.controller.detect_current_page()
                        break

        # If at Data Display/Data List, navigate back to Module List
        if current in (GDS2Page.DATA_DISPLAY, GDS2Page.DATA_LIST, GDS2Page.SUB_DATA_LIST):
            status("Navigating to Module List...")
            self._navigate_to_module_list(status)

        # If at Module Submenu, go back to Module List
        if current == GDS2Page.MODULE_SUBMENU:
            status("Going back to Module List...")
            self.controller.go_back()
            # go_back now uses wait_for_page_transition internally

        # Now we should be at Module List
        status("Scanning modules...")
        modules = self.controller.wait_for_list()
        if not modules:
            raise RuntimeError("No modules found at Module List")

        self.controller._current_page = GDS2Page.MODULE_LIST

        # Save modules to cache
        module_indices = {name: idx for idx, name in enumerate(modules)}
        self.mapping.update_module_list(self._vehicle_id, module_indices)

        # Extract VIN
        self._vin = self._extract_vin()
        if self._vin:
            self._vehicle_id = self._vin
            self.mapping.update_module_list(self._vehicle_id, module_indices)

        self._device = device_name if device_name and device_name not in ("default", "Connected Device") else "Connected Device"
        self._module = None
        self._data_category = None
        self.controller.set_context(device=self._device)

        status("Ready")

        return {
            "modules": modules,
            "vin": self._vin,
            "device": self._device,
        }

    @with_recovery
    def select_module(self, module_name: str, on_status: StatusCallback = None) -> dict:
        """
        Navigate to Data List for the given module.

        Returns: {"data_categories": [...]}
        """
        def status(msg):
            logger.info(msg)
            if on_status:
                on_status(msg)

        # Stop any active monitoring
        self.stop_monitoring()

        status("Navigating to Module List...")
        self._navigate_to_module_list(status)

        # Select module from list
        status(f"Selecting {module_name}...")
        items = self.controller.wait_for_list()
        if not items:
            raise RuntimeError("No modules found")

        target_index = self._find_item_index(items, module_name)
        if target_index is None:
            raise RuntimeError(f"Module '{module_name}' not found")

        matched = items[target_index]
        result = self.controller.nav.select_list_item(0, target_index, double_click=True)
        if not result.get('success'):
            raise RuntimeError(f"Failed to select module: {result.get('message')}")

        # Wait for page transition instead of blind sleep
        try:
            self.controller.wait_for_page_transition(GDS2Page.MODULE_LIST, timeout=30)
        except TimeoutError:
            logger.warning("Page did not transition after module selection")
        self.controller.set_context(module=matched)
        self.controller._current_page = self.controller.detect_current_page()

        # Select Data Display from submenu
        status("Opening Data Display...")
        submenu = self.controller.wait_for_list(previous_items=items)
        if submenu:
            for i, item in enumerate(submenu):
                if "Data Display" in item:
                    self.controller.nav.select_list_item(0, i, double_click=True)
                    try:
                        self.controller.wait_for_page_transition(
                            GDS2Page.MODULE_SUBMENU, timeout=30
                        )
                    except TimeoutError:
                        logger.warning("Page did not transition after Data Display selection")
                    break

        # Handle warning dialog
        self.controller.dismiss_warning_dialog()

        # Discover data categories
        status("Scanning data categories...")
        data_categories = self.controller.wait_for_list(previous_items=submenu if submenu else None)
        if not data_categories:
            raise RuntimeError("No data categories found")

        self.controller._current_page = GDS2Page.DATA_LIST

        # Save to cache
        cat_indices: Dict[str, int | Dict[str, int]] = {
            name: idx for idx, name in enumerate(data_categories)
        }
        self.mapping.update_data_categories(self._vehicle_id, matched, cat_indices)

        self._module = matched
        self._data_category = None

        status("Ready")

        return {
            "data_categories": data_categories,
        }

    @with_recovery
    def select_data_category(self, data_category: str, on_status: StatusCallback = None) -> dict:
        """
        Navigate to Data Display and start monitoring.

        Returns: {"monitoring": True, "sub_categories": [...] if applicable}
        """
        def status(msg):
            logger.info(msg)
            if on_status:
                on_status(msg)

        # Stop any active monitoring
        self.stop_monitoring()

        # Navigate to Data List if needed
        current = self.controller.detect_current_page()
        if current == GDS2Page.DATA_DISPLAY:
            status("Going back to Data List...")
            self.controller.go_back()
            # go_back now uses wait_for_page_transition internally

        # Select data category
        status(f"Selecting {data_category}...")
        result = self.controller.select_data_category(data_category)
        if not result.success:
            raise RuntimeError(f"Failed to select data category: {result.error}")

        sub_categories = None
        if result.page == GDS2Page.SUB_DATA_LIST:
            sub_categories = result.choices
            # Auto-select first sub-category
            if result.choices:
                status(f"Selecting {result.choices[0]}...")
                result = self.controller.select_sub_category(result.choices[0])
                if not result.success:
                    raise RuntimeError(f"Failed to select sub-category: {result.error}")

        self._data_category = data_category

        status(f"Monitoring {data_category}")

        return {
            "monitoring": True,
            "sub_categories": sub_categories,
        }

    def stop_monitoring(self):
        """Clear monitoring state."""
        self._data_category = None

    @with_recovery
    def get_available_devices(self, on_status: StatusCallback = None) -> dict:
        """
        Navigate to Device Explorer and get all available devices.

        Use this when user wants to switch devices. This will:
        1. Navigate back to Vehicle Selection
        2. Click Disconnect
        3. Click Select Device
        4. Wait for Device Explorer to appear
        5. Return list of available devices

        Returns: {"devices": [...], "at_device_explorer": True}
        """
        def status(msg):
            logger.info(msg)
            if on_status:
                on_status(msg)

        # Stop any active monitoring
        self.stop_monitoring()

        status("Navigating to Vehicle Selection...")

        # Navigate back towards Main Menu / Vehicle Selection
        self._navigate_to_main_menu(status)

        current = self.controller.detect_current_page()
        logger.info(f"After navigation, at page: {current.value}")

        # If at Main Menu, click Diagnostics to get to Vehicle Selection or Device Explorer
        if current == GDS2Page.MAIN_MENU:
            status("Opening Diagnostics...")
            result = self.controller.start_diagnostics()
            if not result.success:
                raise RuntimeError(f"Failed to start diagnostics: {result.error}")
            current = result.page

            # If Device Explorer opened directly, we're done
            if current == GDS2Page.DEVICE_EXPLORER:
                return self._get_devices_from_explorer(status)

        # Should be at Vehicle Selection now
        if current != GDS2Page.VEHICLE_SELECTION:
            raise RuntimeError(f"Expected Vehicle Selection, got {current.value}")

        # Disconnect current device
        status("Disconnecting current device...")
        result = self.controller.disconnect_device()
        if not result.success:
            raise RuntimeError(f"Failed to disconnect: {result.error}")
        time.sleep(1)

        # Open Device Explorer
        status("Opening Device Explorer...")
        result = self.controller.open_device_selector()
        if not result.success:
            raise RuntimeError(f"Failed to open Device Explorer: {result.error}")

        return self._get_devices_from_explorer(status)

    def _get_devices_from_explorer(self, status) -> dict:
        """Get device list from Device Explorer dialog."""
        from ..native.device_explorer import DeviceExplorerController
        explorer = DeviceExplorerController()

        status("Scanning for devices...")
        if not explorer.find_dialog(timeout_sec=5.0):
            raise RuntimeError("Device Explorer dialog not found")

        devices = explorer.get_device_names()
        if not devices:
            raise RuntimeError("No devices found in Device Explorer")

        status(f"Found {len(devices)} device(s)")

        return {
            "devices": devices,
            "at_device_explorer": True,
        }

    @with_recovery
    def auto_start(self, on_status: StatusCallback = None) -> dict:
        """
        One-button start flow.

        1. Call start()
        2. If device selection is required, auto-connect to "VCI Proxy (Remote)"
        3. Return module list context
        """
        def status(msg):
            logger.info(msg)
            if on_status:
                on_status(msg)

        result = self.start(on_status=on_status)

        if "devices" in result:
            status("Auto-connecting to VCI Proxy (Remote)...")
            connected = self.connect_device("VCI Proxy (Remote)", on_status=on_status)
            return {
                "modules": connected["modules"],
                "vin": connected.get("vin"),
                "device": connected.get("device"),
            }

        if "modules" in result:
            return {
                "modules": result["modules"],
                "vin": result.get("vin"),
                "device": result.get("device"),
            }

        raise RuntimeError("Unexpected start result: missing 'devices' or 'modules'")

    def read_all_dtcs(self, on_status: StatusCallback = None) -> dict:
        """Read DTCs from Agent JSON at current Data Display page."""
        def status(msg):
            logger.info(msg)
            if on_status:
                on_status(msg)

        from ..streaming import AgentDataCollector
        from ..streaming.agent_data_collector import _parse_agent_json

        collector = AgentDataCollector()
        availability = collector.check_agent_available()

        if not availability.get("available"):
            raise RuntimeError("Java Agent not available. Start GDS2 with agent.")

        current = self.controller.detect_current_page()
        if current != GDS2Page.DATA_DISPLAY:
            raise RuntimeError(
                "Not at Data Display page. Select module and data category first."
            )

        status("Reading DTCs from current Data Display page...")

        raw = None
        for encoding in ['gbk', 'utf-8', 'utf-8-sig', 'latin-1', 'cp1252']:
            try:
                with open(collector.json_path, 'r', encoding=encoding) as f:
                    raw = json.load(f)
                break
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue

        if raw is None:
            raise RuntimeError("Failed to decode Agent JSON file")

        snapshot = _parse_agent_json(raw)

        return {
            "dtcs": [d.to_dict() for d in snapshot.dtcs],
            "dtc_count": len(snapshot.dtcs),
            "page_context": snapshot.page_context,
        }

    def get_state(self) -> dict:
        """Return current viewer state."""
        return {
            "device": self._device,
            "module": self._module,
            "data_category": self._data_category,
            "vin": self._vin,
            "page": self.controller.current_page.value,
        }

    # =========================================================================
    # Private helpers
    # =========================================================================

    def _ensure_device_selected(self, device_name: str, status):
        """
        Ensure the given device is selected in Device Explorer and Continue is clicked.

        Works from ANY page:
        1. If Device Explorer is already open → select and continue
        2. Otherwise: navigate back towards Main Menu, then:
           a. If at Main Menu → Diagnostics → Device Explorer or Vehicle Selection
           b. If at Vehicle Selection (device connected) → Disconnect → Select Device → select and continue

        After this method returns, GDS2 should be at Vehicle Selection with the new device.
        """
        from ..native.device_explorer import DeviceExplorerController

        # Case 1: Device Explorer is already open (e.g. just called start())
        explorer = DeviceExplorerController()
        if explorer.find_dialog(timeout_sec=2.0):
            self._select_in_explorer(explorer, device_name, status)
            return

        # Case 2: Navigate back towards Main Menu
        status("Navigating back...")
        self._navigate_to_main_menu(status)

        # Check where we ended up
        current = self.controller.detect_current_page()
        logger.info(f"After navigation, at page: {current.value}")

        # Case 2a: At Vehicle Selection - device is connected, need to disconnect
        if current == GDS2Page.VEHICLE_SELECTION:
            status("Disconnecting current device...")
            disc_result = self.controller.disconnect_device()
            if not disc_result.success:
                raise RuntimeError(f"Failed to disconnect: {disc_result.error}")
            time.sleep(1)

            status("Opening Device Explorer...")
            sel_result = self.controller.open_device_selector()
            if not sel_result.success:
                raise RuntimeError(f"Failed to open Device Explorer: {sel_result.error}")

            explorer = DeviceExplorerController()
            if not explorer.find_dialog(timeout_sec=5.0):
                raise RuntimeError("Device Explorer dialog not found after Select Device")
            self._select_in_explorer(explorer, device_name, status)
            return

        # Case 2b: At Main Menu - click Diagnostics
        if current == GDS2Page.MAIN_MENU:
            status("Opening Diagnostics...")
            result = self.controller.start_diagnostics()
            if not result.success:
                raise RuntimeError(f"Failed to start diagnostics: {result.error}")

            # Check what appeared
            if result.page == GDS2Page.DEVICE_EXPLORER:
                # No device connected - Device Explorer opened directly
                explorer = DeviceExplorerController()
                if not explorer.find_dialog(timeout_sec=5.0):
                    raise RuntimeError("Device Explorer detected but dialog not found")
                self._select_in_explorer(explorer, device_name, status)
                return

            if result.page == GDS2Page.VEHICLE_SELECTION:
                # Device is still connected - need to disconnect and re-select
                status("Disconnecting current device...")
                disc_result = self.controller.disconnect_device()
                if not disc_result.success:
                    raise RuntimeError(f"Failed to disconnect: {disc_result.error}")
                time.sleep(1)

                status("Opening Device Explorer...")
                sel_result = self.controller.open_device_selector()
                if not sel_result.success:
                    raise RuntimeError(f"Failed to open Device Explorer: {sel_result.error}")

                explorer = DeviceExplorerController()
                if not explorer.find_dialog(timeout_sec=5.0):
                    raise RuntimeError("Device Explorer dialog not found after Select Device")
                self._select_in_explorer(explorer, device_name, status)
                return

            raise RuntimeError(
                f"Unexpected page after Diagnostics: {result.page.value}. "
                f"Expected Device Explorer or Vehicle Selection."
            )

        raise RuntimeError(
            f"Unexpected page after navigation: {current.value}. "
            f"Expected Main Menu or Vehicle Selection."
        )

    def _select_in_explorer(self, explorer, device_name: str, status):
        """Select device in Device Explorer and click Continue."""
        status(f"Selecting device: {device_name}...")
        if not explorer.select_device_by_name(device_name):
            raise RuntimeError(f"Device '{device_name}' not found in Device Explorer")
        time.sleep(0.5)

        status("Clicking Continue...")
        if not explorer.click_continue():
            raise RuntimeError("Failed to click Continue in Device Explorer")
        time.sleep(2)  # Wait for device to connect and page to load

    def _wait_for_button_enabled(self, button_text: str, timeout_sec: float = 30, _recursion_depth: int = 0) -> bool:
        """
        Wait for a button to become enabled.

        Args:
            button_text: Text of the button to wait for
            timeout_sec: Maximum time to wait
            _recursion_depth: Internal recursion counter (max 2 retries)

        Returns:
            True if button became enabled, False if timeout

        With AI recovery enabled, handles:
        - Unexpected dialogs during wait
        - Timeout with AI decision (wait longer vs give up)
        """
        # Prevent infinite recursion
        MAX_RECURSION = 2
        if _recursion_depth >= MAX_RECURSION:
            logger.warning(f"Max recursion depth ({MAX_RECURSION}) reached for button wait")
            return False

        start_time = time.time()
        original_timeout = timeout_sec  # Save original for context

        while time.time() - start_time < timeout_sec:
            # Check for unexpected dialogs (AI Recovery - Day 6)
            if self.recovery:
                if anomaly := self.recovery.check_for_dialogs():
                    logger.warning(f"Unexpected dialog detected: {anomaly.context.get('modal_title')}")

                    # Create operation context
                    from ..recovery import OperationContext
                    context = OperationContext(
                        operation_name=f"wait_for_button:{button_text}",
                        current_page=self.controller.current_page.value,
                        visible_buttons=[button_text],
                        recent_actions=["wait_for_button_enabled"],
                        elapsed_time=time.time() - start_time,
                        expected_time=original_timeout,
                    )

                    # Let AI handle it
                    try:
                        result = self.recovery.handle_anomaly(anomaly, context)
                        if result.success:
                            logger.info(f"Dialog recovered: {result.action.name}")
                            # Continue waiting after recovery
                        else:
                            logger.error(f"Dialog recovery failed: {result.error}")
                            # Continue waiting anyway, maybe button appeared
                    except Exception as e:
                        logger.error(f"Recovery exception: {e}", exc_info=True)

            # Check button status
            buttons = self.controller.nav.get_buttons()
            for btn in buttons:
                if btn.get('text') == button_text:
                    if btn.get('enabled', False):
                        logger.info(f"Button '{button_text}' is now enabled")
                        return True
                    else:
                        logger.debug(f"Button '{button_text}' is disabled, waiting...")
                        break
            time.sleep(1)

        # Timeout - check if AI can help
        elapsed = time.time() - start_time
        logger.warning(f"Timeout waiting for button '{button_text}' after {elapsed:.1f}s")

        if self.recovery:
            from ..recovery import OperationContext
            # Detect timeout anomaly
            anomaly = self.recovery.detector.check_timeout(
                operation=f"wait_for_button:{button_text}",
                elapsed_time=elapsed,
                expected_time=original_timeout,
            )

            if anomaly:
                context = OperationContext(
                    operation_name=f"wait_for_button:{button_text}",
                    current_page=self.controller.current_page.value,
                    visible_buttons=[button_text],
                    recent_actions=["wait_for_button_enabled"],
                    elapsed_time=elapsed,
                    expected_time=original_timeout,
                )

                try:
                    result = self.recovery.handle_anomaly(anomaly, context)
                    if result.success and result.action.name == "WAIT_LONGER":
                        # AI recommends waiting longer
                        extended_timeout = 60  # Default to 60s
                        logger.info(f"AI recommends waiting {extended_timeout} more seconds")
                        # Recursively call with extended timeout (and increment depth)
                        return self._wait_for_button_enabled(
                            button_text, extended_timeout, _recursion_depth + 1
                        )
                    elif not result.success:
                        logger.error(f"Timeout recovery failed: {result.error}")
                except Exception as e:
                    logger.error(f"Recovery exception: {e}", exc_info=True)

        return False

    def _navigate_to_main_menu(self, status):
        """
        Navigate to Main Menu - works from ANY page.

        Strategy:
        - Home button is DISABLED on Data List and Data Display pages
        - Keep clicking Back until Home becomes enabled
        - Then click Home to go to Main Menu
        """
        MAX_BACK_CLICKS = 10

        for attempt in range(MAX_BACK_CLICKS):
            current = self.controller.detect_current_page()
            logger.info(f"Navigation attempt {attempt + 1}: current page = {current.value}")

            if current == GDS2Page.MAIN_MENU:
                status("At Main Menu")
                return

            # Get button states
            buttons = self.controller.nav.get_buttons()
            button_map = {b.get('text', ''): b for b in buttons}

            home_btn = button_map.get('Home')
            back_btn = button_map.get('Back')

            logger.debug(f"Home button: {home_btn}, Back button: {back_btn}")

            # Check if Home is enabled
            if home_btn and home_btn.get('enabled', False):
                status("Clicking Home...")
                result = self.controller.go_home()
                if result.success:
                    # go_home uses wait_for_page_transition internally
                    return
                else:
                    logger.warning(f"Home click failed: {result.error}")

            # Home is disabled or not found - click Back
            if back_btn and back_btn.get('enabled', False):
                status(f"Going back... ({attempt + 1})")
                result = self.controller.go_back()
                if result.success:
                    # go_back uses wait_for_page_transition internally
                    continue
                else:
                    logger.warning(f"Back click failed: {result.error}")

            # At Vehicle Selection with no Home/Back - we're stuck
            # Vehicle Selection has "Diagnostics" in the menu bar, but no Home/Back
            # Just return - caller can handle Vehicle Selection
            if current == GDS2Page.VEHICLE_SELECTION:
                logger.info("At Vehicle Selection - no Home/Back available, returning")
                return

            # Try one more detection
            time.sleep(1)

        # Exhausted attempts
        current = self.controller.detect_current_page()
        buttons = self.controller.get_visible_buttons()
        raise RuntimeError(
            f"Cannot navigate to Main Menu after {MAX_BACK_CLICKS} attempts. "
            f"Current page: {current.value}. "
            f"Available buttons: {', '.join(buttons) if buttons else 'none'}. "
            f"Please manually navigate GDS2 to Main Menu and try again."
        )

    def _navigate_to_module_list(self, status):
        """Smart navigation to Module List."""
        current = self.controller.detect_current_page()

        if current == GDS2Page.MODULE_LIST:
            return

        # Use Vehicle Menu shortcut if available
        buttons = self.controller.get_visible_buttons()
        if "Vehicle Menu" in buttons:
            status("Using Vehicle Menu shortcut...")
            result = self.controller.go_vehicle_menu()
            if result.success:
                # Now at Diagnostics Menu - select Module Diagnostics
                items = self.controller.wait_for_list()
                if items:
                    for i, item in enumerate(items):
                        if "Module Diagnostics" in item:
                            status("Selecting Module Diagnostics...")
                            self.controller.nav.select_list_item(0, i, double_click=True)
                            try:
                                self.controller.wait_for_page_transition(
                                    GDS2Page.DIAGNOSTICS_MENU, timeout=30
                                )
                            except TimeoutError:
                                logger.warning("Page did not transition after Module Diagnostics selection")
                            self.controller._current_page = self.controller.detect_current_page()
                            self.controller._current_page = GDS2Page.MODULE_LIST
                            return

        # Fallback: navigate back
        result = self.controller.navigate_to(GDS2Page.MODULE_LIST)
        if result.success:
            return

        # Last resort: go home and start over
        logger.warning("Fallback: going home to reach Module List")
        self._navigate_to_main_menu(status)

    def _extract_vin(self) -> Optional[str]:
        """Extract VIN from Agent page_context or latest HTML report."""
        # Try from latest HTML report
        report_dir = Path.home() / "AppData" / "Local" / "Temp" / "GDS 2"
        if report_dir.exists():
            reports = list(report_dir.glob("*.html"))
            if reports:
                latest = max(reports, key=lambda p: p.stat().st_mtime)
                try:
                    with open(latest, 'r', encoding='iso-8859-1') as f:
                        html = f.read()
                    vin_match = re.search(
                        r'Vehicle Identification Number \(VIN\)</td><td>([^<]+)</td>',
                        html
                    )
                    if vin_match:
                        return vin_match.group(1).strip()
                except Exception as e:
                    logger.debug(f"VIN extraction from report failed: {e}")

        return None

    @staticmethod
    def _find_item_index(items: List[str], target: str) -> Optional[int]:
        """Find index of target in items list (partial match)."""
        for i, item in enumerate(items):
            if target in item or item in target:
                return i

        # Try partial match on key part
        key = target.split("]")[-1].strip() if "]" in target else target
        for i, item in enumerate(items):
            if key in item:
                return i

        return None
