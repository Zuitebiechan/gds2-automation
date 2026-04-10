"""
Data Viewer Workflow for GDS2.

Simplified three-step workflow: Device -> Module -> Data Category.
Handles all GDS2 navigation automatically in the background.
"""

import logging
import re
import time
import json
from pathlib import Path
from typing import Callable, Dict, List, Optional, Any

from backends.gds2.planner import (
    BranchDecisionRequiredError,
    ConstrainedPlanner,
    DecisionDomain,
)
from ..navigation import NavigationController, NavigationResult, GDS2Page

logger = logging.getLogger(__name__)

StatusCallback = Optional[Callable[[str], None]]
CONNECTED_PAGES = (
    GDS2Page.VEHICLE_SELECTION,
    GDS2Page.DIAGNOSTICS_MENU,
    GDS2Page.MODULE_LIST,
    GDS2Page.MODULE_SUBMENU,
    GDS2Page.DATA_LIST,
    GDS2Page.SUB_DATA_LIST,
    GDS2Page.DATA_DISPLAY,
)


class DataViewerWorkflow:
    """Simplified three-step workflow: Device -> Module -> Data."""

    def __init__(self, nav=None):
        self.controller = NavigationController(nav)
        self._branch_planner = ConstrainedPlanner()
        self._mapping = None
        self._vehicle_id = "current_vehicle"
        self._vin = None
        self._device = None
        self._module = None
        self._data_category = None
        self._cancel_checker: Optional[Callable[[], None]] = None

    def set_cancel_checker(self, cancel_checker: Optional[Callable[[], None]]) -> None:
        self._cancel_checker = cancel_checker
        if hasattr(self.controller, "set_cancel_checker"):
            self.controller.set_cancel_checker(cancel_checker)

    def _check_cancel(self) -> None:
        cancel_checker = getattr(self, "_cancel_checker", None)
        if cancel_checker is not None:
            cancel_checker()

    def _sleep(self, seconds: float, poll_interval: float = 0.1) -> None:
        deadline = time.time() + max(0.0, seconds)
        while time.time() < deadline:
            self._check_cancel()
            time.sleep(min(poll_interval, max(0.0, deadline - time.time())))

    @property
    def mapping(self):
        if self._mapping is None:
            from ..discovery import VehicleMapping
            self._mapping = VehicleMapping()
        return self._mapping

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
        if current in CONNECTED_PAGES:
            status("Device already connected")
            return self._navigate_to_module_list_from(current, status)

        # At Main Menu - click Diagnostics
        status("Opening Diagnostics...")
        result = self.controller.start_diagnostics()
        return self._handle_start_diagnostics_result(result, status)

    def _navigate_to_module_list_from(self, current: GDS2Page, status) -> dict:
        """
        Navigate from the given page all the way to Module List.

        Handles: Vehicle Selection → Enter → Diagnostics Menu → Module Diagnostics → Module List
        Also handles being at Diagnostics Menu, Module List, Data List, etc.

        Returns: {"modules": [...], "device_connected": True, ...}
        """
        self._check_cancel()
        self._advance_connected_page_to_module_list(current, status)

        return self._finalize_module_list_state(status, include_device_connected=True)

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

        self._check_cancel()

        # Stop any active monitoring
        self.stop_monitoring()

        status("Connecting...")

        current = self._prepare_device_connection(device_name, status)

        logger.info(f"Navigating to Module List from {current.value}")
        self._advance_connected_page_to_module_list(
            current,
            status,
            prefer_single_back_from_module_submenu=True,
        )

        return self._finalize_module_list_state(status, requested_device_name=device_name)

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

        items, resolved_module, target_index, matched = self._prepare_module_selection(module_name)
        status(f"Selecting {resolved_module}...")
        self._select_module_from_list(target_index=target_index, matched_module=matched)

        # Select target submenu entry (typically Data Display) from Module Submenu.
        # If ambiguous, raise BranchDecisionRequiredError for HITL decision flow.
        status("Opening module submenu target...")
        submenu = self.controller.wait_for_list(previous_items=items)
        if submenu:
            resolved_sub_module = self._resolve_branch_choice(
                domain=DecisionDomain.SUB_MODULE,
                target="Data Display",
                choices=submenu,
                fallback_to_first=False,
            )
            self._open_sub_module_target(submenu, resolved_sub_module, status)

        data_categories = self._collect_data_categories(
            previous_items=submenu if submenu else None,
            module_name=matched,
            status=status,
        )

        return {"data_categories": data_categories}

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

        self._return_to_data_list_if_needed(status)

        list_items = self.controller.wait_for_list()
        if not list_items:
            raise RuntimeError("No data categories found")

        resolved_category = self._resolve_branch_choice(
            domain=DecisionDomain.DATA_CATEGORY,
            target=data_category,
            choices=list_items,
            fallback_to_first=False,
        )
        status(f"Selecting {resolved_category}...")
        result = self.controller.select_data_category(resolved_category)
        if not result.success:
            raise RuntimeError(f"Failed to select data category: {result.error}")

        sub_categories = self._select_data_category_sub_category_if_needed(
            data_category=resolved_category,
            result=result,
            status=status,
        )

        self._data_category = resolved_category

        status(f"Monitoring {resolved_category}")

        return {
            "monitoring": True,
            "sub_categories": sub_categories,
        }

    def select_sub_module(self, sub_module_name: str, on_status: StatusCallback = None) -> dict:
        """Select sub-module entry (Module Submenu -> Data List).

        Used when module submenu requires explicit user choice.

        Returns: {"data_categories": [...], "sub_module": "..."}
        """

        def status(msg):
            logger.info(msg)
            if on_status:
                on_status(msg)

        self.stop_monitoring()

        current = self.controller.detect_current_page()
        if current != GDS2Page.MODULE_SUBMENU:
            raise RuntimeError(
                f"Not at Module Submenu page (current={current.value}). "
                "Please re-select module first."
            )

        submenu = self.controller.wait_for_list()
        if not submenu:
            raise RuntimeError("No sub-module choices found")

        resolved_sub_module = self._resolve_branch_choice(
            domain=DecisionDomain.SUB_MODULE,
            target=sub_module_name,
            choices=submenu,
            fallback_to_first=False,
        )

        status(f"Opening {resolved_sub_module}...")
        self._open_sub_module_target(submenu, resolved_sub_module, status)
        data_categories = self._collect_data_categories(
            previous_items=submenu,
            module_name=getattr(self.controller, "current_module", None) or self._module,
            status=status,
        )
        return {"data_categories": data_categories, "sub_module": resolved_sub_module}

    def select_sub_category(self, sub_category: str, on_status: StatusCallback = None) -> dict:
        """Select sub-data category from Sub Data List to Data Display.

        Returns: {"monitoring": True, "sub_category": "..."}
        """

        def status(msg):
            logger.info(msg)
            if on_status:
                on_status(msg)

        self.stop_monitoring()

        current = self.controller.detect_current_page()
        if current != GDS2Page.SUB_DATA_LIST:
            raise RuntimeError(
                f"Not at Sub Data List page (current={current.value}). "
                "Please re-select data category first."
            )

        status(f"Selecting {sub_category}...")
        result = self.controller.select_sub_category(sub_category)
        if not result.success:
            raise RuntimeError(f"Failed to select sub-category: {result.error}")

        if result.page != GDS2Page.DATA_DISPLAY:
            raise RuntimeError(
                f"Sub-category selection did not reach data display (got {result.page.value})"
            )

        status("Monitoring data display")
        return {
            "monitoring": True,
            "sub_category": sub_category,
        }

    def stop_monitoring(self):
        """Clear monitoring state."""
        self._data_category = None

    def recover_data_display_connection(
        self,
        *,
        allow_backtrack: bool = True,
        on_status: StatusCallback = None,
    ) -> dict:
        """Recover from the J2534 disconnect page back to Data Display."""
        def status(msg):
            logger.info(msg)
            if on_status:
                on_status(msg)

        data_category = self._data_category or self.controller.current_data_category
        if not data_category:
            raise RuntimeError("No active data category stored for recovery.")

        status("Attempting Data Display recovery...")
        result = self.controller.recover_data_display_connection(
            data_category=data_category,
            allow_backtrack=allow_backtrack,
        )
        if not result.success:
            raise RuntimeError(result.error or "Data Display recovery failed")
        if result.page != GDS2Page.DATA_DISPLAY:
            raise RuntimeError(
                f"Recovery did not restore Data Display (got {result.page.value})"
            )

        status("Data Display recovered")
        return {
            "monitoring": True,
            "data_category": data_category,
            "page": result.page.value,
        }

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

        self._navigate_to_main_menu(status)

        current = self.controller.detect_current_page()
        logger.info(f"After navigation, at page: {current.value}")
        current = self._advance_to_device_selection_or_explorer(current, status)

        if current == GDS2Page.DEVICE_EXPLORER:
            self._clear_connection_context()
            return self._get_devices_from_explorer(status)

        explorer = self._open_device_explorer_after_disconnect(status)
        return self._get_devices_from_explorer(status, explorer=explorer)

    def _get_devices_from_explorer(self, status, explorer=None) -> dict:
        """Get device list from Device Explorer dialog."""
        if explorer is None:
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

    def _handle_start_diagnostics_result(self, result: NavigationResult, status) -> dict:
        """Normalize Diagnostics startup results into either device-pick or connected flow."""
        if not result.success:
            raise RuntimeError(f"Failed to start diagnostics: {result.error}")

        if result.page == GDS2Page.DEVICE_EXPLORER:
            return {
                **self._get_devices_from_explorer(status),
                "device_connected": False,
            }

        if result.page in CONNECTED_PAGES:
            status("Device already connected")
            return self._navigate_to_module_list_from(result.page, status)

        raise RuntimeError(f"Unexpected page after Diagnostics: {result.page.value}")

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

        snapshot = _parse_agent_json(self._load_agent_json_payload(collector.json_path))
        self._ensure_agent_snapshot_matches_page(snapshot.page_context)

        return {
            "dtcs": [d.to_dict() for d in snapshot.dtcs],
            "dtc_count": len(snapshot.dtcs),
            "page_context": snapshot.page_context,
        }

    def clear_dtcs(self, on_status: StatusCallback = None) -> dict:
        """Clear DTCs from the current Data Display context using the Java Agent."""
        def status(msg):
            logger.info(msg)
            if on_status:
                on_status(msg)

        current = self.controller.detect_current_page()
        if current != GDS2Page.DATA_DISPLAY:
            raise RuntimeError(
                "Not at Data Display page. Select module and data category first."
            )

        snapshot = self.read_all_dtcs()
        pre_clear_count = int(snapshot.get("dtc_count") or 0)
        if pre_clear_count <= 0:
            status("No DTCs detected. Nothing to clear.")
            return {
                "success": True,
                "cleared_count": 0,
                "message": "No DTCs detected; nothing to clear",
                "page_context": current.value,
            }

        button_states = self._get_button_states()
        if not button_states.get("Clear DTCs", False):
            raise RuntimeError("Clear DTCs button is not visible on the current Data Display page.")

        status("Opening Clear DTCs...")
        self._click_agent_button("Clear DTCs")

        dialog_page = self._wait_for_clear_dtcs_page(timeout_sec=10.0)
        dialog_page = self._handle_clear_dtcs_selection_page(dialog_page, status)

        if dialog_page == GDS2Page.CLEAR_DTCS_CONFIRMATION:
            self._submit_clear_dtcs_confirmation(status)
        elif dialog_page == GDS2Page.DATA_DISPLAY:
            status("Clear DTCs completed")
            return self._build_clear_dtcs_success_payload(
                pre_clear_count=pre_clear_count,
                page=GDS2Page.DATA_DISPLAY,
            )

        final_page = self._wait_for_data_display_restore(timeout_sec=30.0)
        status("Clear DTCs completed")
        return self._build_clear_dtcs_success_payload(
            pre_clear_count=pre_clear_count,
            page=final_page,
        )

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
        if self._select_device_in_existing_explorer(device_name, status):
            return

        status("Navigating back...")
        self._navigate_to_main_menu(status)

        current = self.controller.detect_current_page()
        logger.info(f"After navigation, at page: {current.value}")
        self._select_device_from_navigation_page(current, device_name, status)

    def _select_device_in_existing_explorer(self, device_name: str, status) -> bool:
        """Use an already-open Device Explorer dialog when available."""
        from ..native.device_explorer import DeviceExplorerController

        explorer = DeviceExplorerController()
        if not explorer.find_dialog(timeout_sec=2.0):
            return False

        self._clear_connection_context()
        self._select_in_explorer(explorer, device_name, status)
        return True

    def _select_device_from_navigation_page(
        self,
        current: GDS2Page,
        device_name: str,
        status,
    ) -> None:
        """Select a device after navigating back toward a stable entry page."""
        if current == GDS2Page.VEHICLE_SELECTION:
            explorer = self._open_device_explorer_after_disconnect(status)
            self._select_in_explorer(explorer, device_name, status)
            return

        if current == GDS2Page.MAIN_MENU:
            result = self._start_diagnostics_for_device_selection(status)
            self._handle_device_selection_diagnostics_result(result, device_name, status)
            return

        raise RuntimeError(
            f"Unexpected page after navigation: {current.value}. "
            f"Expected Main Menu or Vehicle Selection."
        )

    def _start_diagnostics_for_device_selection(self, status) -> NavigationResult:
        """Start Diagnostics while preparing to open or re-open Device Explorer."""
        status("Opening Diagnostics...")
        result = self.controller.start_diagnostics()
        if not result.success:
            raise RuntimeError(f"Failed to start diagnostics: {result.error}")
        return result

    def _advance_to_device_selection_or_explorer(
        self,
        current: GDS2Page,
        status,
    ) -> GDS2Page:
        """Advance from Main Menu to either Vehicle Selection or Device Explorer."""
        if current != GDS2Page.MAIN_MENU:
            if current != GDS2Page.VEHICLE_SELECTION:
                raise RuntimeError(f"Expected Vehicle Selection, got {current.value}")
            return current

        result = self._start_diagnostics_for_device_selection(status)
        current = result.page
        if current in (GDS2Page.VEHICLE_SELECTION, GDS2Page.DEVICE_EXPLORER):
            return current

        raise RuntimeError(f"Expected Vehicle Selection, got {current.value}")

    def _handle_device_selection_diagnostics_result(
        self,
        result: NavigationResult,
        device_name: str,
        status,
    ) -> None:
        """Continue device selection based on the page reached after Diagnostics."""
        if result.page == GDS2Page.DEVICE_EXPLORER:
            self._clear_connection_context()
            explorer = self._require_device_explorer_dialog(
                "Device Explorer detected but dialog not found",
            )
            self._select_in_explorer(explorer, device_name, status)
            return

        if result.page == GDS2Page.VEHICLE_SELECTION:
            explorer = self._open_device_explorer_after_disconnect(status)
            self._select_in_explorer(explorer, device_name, status)
            return

        raise RuntimeError(
            f"Unexpected page after Diagnostics: {result.page.value}. "
            f"Expected Device Explorer or Vehicle Selection."
        )

    @staticmethod
    def _require_device_explorer_dialog(error_message: str):
        """Return a visible Device Explorer dialog or raise a descriptive error."""
        from ..native.device_explorer import DeviceExplorerController

        explorer = DeviceExplorerController()
        if not explorer.find_dialog(timeout_sec=5.0):
            raise RuntimeError(error_message)
        return explorer

    def _open_device_explorer_after_disconnect(self, status):
        """Disconnect the active device and verify the Device Explorer dialog is available."""
        status("Disconnecting current device...")
        disc_result = self.controller.disconnect_device()
        if not disc_result.success:
            raise RuntimeError(f"Failed to disconnect: {disc_result.error}")
        self._clear_connection_context()
        self._sleep(1)

        status("Opening Device Explorer...")
        sel_result = self.controller.open_device_selector()
        if not sel_result.success:
            raise RuntimeError(f"Failed to open Device Explorer: {sel_result.error}")

        return self._require_device_explorer_dialog(
            "Device Explorer dialog not found after Select Device",
        )

    def _select_in_explorer(self, explorer, device_name: str, status):
        """Select device in Device Explorer and click Continue."""
        self._check_cancel()
        status(f"Selecting device: {device_name}...")
        if not explorer.select_device_by_name(device_name):
            raise RuntimeError(f"Device '{device_name}' not found in Device Explorer")
        self._sleep(0.5)

        self._check_cancel()
        status("Clicking Continue...")
        self._check_cancel()
        if not explorer.click_continue():
            raise RuntimeError("Failed to click Continue in Device Explorer")
        self._sleep(2)  # Wait for device to connect and page to load

    def _wait_for_button_enabled(self, button_text: str, timeout_sec: float = 30, _recursion_depth: int = 0) -> bool:
        """
        Wait for a button to become enabled.

        Args:
            button_text: Text of the button to wait for
            timeout_sec: Maximum time to wait
            _recursion_depth: Internal recursion counter (max 2 retries)

        Returns:
            True if button became enabled, False if timeout
        """
        # Prevent infinite recursion
        MAX_RECURSION = 2
        if _recursion_depth >= MAX_RECURSION:
            logger.warning(f"Max recursion depth ({MAX_RECURSION}) reached for button wait")
            return False

        start_time = time.time()

        while time.time() - start_time < timeout_sec:
            self._check_cancel()
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
            self._sleep(1)

        # Timeout
        elapsed = time.time() - start_time
        logger.warning(f"Timeout waiting for button '{button_text}' after {elapsed:.1f}s")

        return False

    def _get_button_states(self) -> Dict[str, bool]:
        """Return visible button texts mapped to enabled state."""
        buttons = self.controller.nav.get_buttons()
        button_states: Dict[str, bool] = {}
        for btn in buttons or []:
            name = str(btn.get('text') or '').strip()
            if not name:
                continue
            button_states[name] = bool(btn.get('enabled', True))
        return button_states

    def _wait_for_button_state(
        self,
        button_text: str,
        *,
        enabled: Optional[bool] = None,
        timeout_sec: float = 10.0,
        poll_interval: float = 0.2,
    ) -> bool:
        """Wait for one visible button to appear with the expected enabled state."""
        deadline = time.time() + max(0.0, timeout_sec)
        while time.time() < deadline:
            self._check_cancel()
            button_states = self._get_button_states()
            if button_text in button_states:
                if enabled is None or button_states[button_text] is enabled:
                    return True
            self._sleep(poll_interval)
        return False

    def _click_agent_button(self, button_text: str) -> None:
        """Click one Java-Agent button without assuming a page transition."""
        self._check_cancel()
        result = self.controller.nav.click_button(button_text)
        if not result.get('success'):
            raise RuntimeError(f"Failed to click '{button_text}': {result.get('message')}")
        self._sleep(0.2)

    def _detect_current_page_fast(self) -> GDS2Page:
        """Detect the current page without retry when the controller supports it."""
        try:
            return self.controller.detect_current_page(retries=0)
        except TypeError:
            return self.controller.detect_current_page()

    def _classify_clear_dtcs_page_from_buttons(
        self,
        button_states: Dict[str, bool],
        *,
        allow_data_display: bool = False,
    ) -> Optional[GDS2Page]:
        """Best-effort Clear-DTC page classification from visible buttons."""
        button_names = set(button_states.keys())
        if any(
            name in button_names
            for name in ("Add All", "Add", "Remove", "Remove All")
        ):
            return GDS2Page.CLEAR_DTCS_SELECTION
        if (
            "OK" in button_names
            and (
                "Cancel" in button_names
                or any(
                    name in button_names
                    for name in ("Clear Records", "Save and Clear", "Yes", "No")
                )
            )
        ):
            return GDS2Page.CLEAR_DTCS_CONFIRMATION
        if allow_data_display and button_states.get("Clear DTCs", False):
            return GDS2Page.DATA_DISPLAY
        return None

    def _wait_for_clear_dtcs_page(
        self,
        *,
        timeout_sec: float = 10.0,
        allow_data_display: bool = False,
    ) -> GDS2Page:
        """Wait for the next Clear-DTC page using explicit page enums first."""
        deadline = time.time() + max(0.0, timeout_sec)
        last_page = GDS2Page.UNKNOWN
        last_buttons: list[str] = []
        while time.time() < deadline:
            self._check_cancel()
            page = self._detect_current_page_fast()
            button_states = self._get_button_states()
            last_page = page
            last_buttons = list(button_states.keys())

            if page in {
                GDS2Page.CLEAR_DTCS_SELECTION,
                GDS2Page.CLEAR_DTCS_CONFIRMATION,
            }:
                return page
            if allow_data_display and page == GDS2Page.DATA_DISPLAY:
                return page

            fallback_page = self._classify_clear_dtcs_page_from_buttons(
                button_states,
                allow_data_display=allow_data_display,
            )
            if fallback_page is not None:
                return fallback_page
            self._sleep(0.2)
        raise RuntimeError(
            "Timed out waiting for Clear DTCs page. "
            f"Last page={last_page.value}; buttons={last_buttons}"
        )

    def _wait_for_data_display_restore(self, timeout_sec: float = 30.0) -> GDS2Page:
        """Wait for Clear DTCs flow to return to the Data Display page."""
        deadline = time.time() + max(0.0, timeout_sec)
        last_page = GDS2Page.UNKNOWN
        last_buttons: list[str] = []
        while time.time() < deadline:
            self._check_cancel()
            page = self._detect_current_page_fast()
            button_states = self._get_button_states()
            last_page = page
            last_buttons = list(button_states.keys())
            if button_states.get("Clear DTCs", False):
                if "Create Report" in button_states:
                    return GDS2Page.DATA_DISPLAY
                if page == GDS2Page.DATA_DISPLAY:
                    return page
            self._sleep(0.5)
        raise RuntimeError(
            "Timed out waiting to return to Data Display after Clear DTCs. "
            f"Last page={last_page.value}; buttons={last_buttons}"
        )

    def _read_dtc_count_with_fallback(self, *, default: int) -> int:
        """Read the latest DTC count after a clear operation, falling back when needed."""
        try:
            snapshot = self.read_all_dtcs()
            return int(snapshot.get("dtc_count") or 0)
        except Exception:
            return default

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
            self._check_cancel()
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
                self._check_cancel()
                status("Clicking Home...")
                result = self.controller.go_home()
                if result.success:
                    if result.page == GDS2Page.MAIN_MENU:
                        return
                    logger.info(
                        "Home landed on %s instead of main_menu; re-evaluating",
                        result.page.value,
                    )
                    if result.page == GDS2Page.VEHICLE_SELECTION:
                        logger.info("Home landed on Vehicle Selection - waiting for final destination")
                        if hasattr(self.controller, "wait_for_page_stable"):
                            try:
                                stabilized = self.controller.wait_for_page_stable(
                                    timeout=3.0,
                                    stable_duration=0.8,
                                    poll_interval=0.2,
                                )
                                logger.info(
                                    "Home stabilized at %s after transient vehicle_selection",
                                    stabilized.value,
                                )
                            except TimeoutError:
                                logger.info("Home stabilization timed out; rechecking current page")
                        else:
                            self._sleep(1.0)
                        continue
                    self._sleep(0.5)
                    continue
                else:
                    logger.warning(f"Home click failed: {result.error}")

            # Home is disabled or not found - click Back
            if back_btn and back_btn.get('enabled', False):
                self._check_cancel()
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
            self._sleep(1)

        # Exhausted attempts
        current = self.controller.detect_current_page()
        buttons = self.controller.get_visible_buttons()
        raise RuntimeError(
            f"Cannot navigate to Main Menu after {MAX_BACK_CLICKS} attempts. "
            f"Current page: {current.value}. "
            f"Available buttons: {', '.join(buttons) if buttons else 'none'}. "
            f"Please manually navigate GDS2 to Main Menu and try again."
        )

    def _read_module_list_with_retry(self, status, max_attempts: int = 3) -> List[str]:
        items: List[str] = []
        for attempt in range(max_attempts):
            self._check_cancel()
            items = self.controller.wait_for_list()
            if items:
                return items

            if attempt == max_attempts - 1:
                break

            logger.warning(
                "Module list empty on attempt %s/%s; waiting for UI to settle",
                attempt + 1,
                max_attempts,
            )
            status("Waiting for module list...")
            if hasattr(self.controller, "wait_for_page_stable"):
                try:
                    self.controller.wait_for_page_stable(timeout=5.0, stable_duration=0.8)
                except TimeoutError:
                    pass
            self._sleep(1.0)

        return items

    def _finalize_module_list_state(
        self,
        status,
        *,
        requested_device_name: Optional[str] = None,
        include_device_connected: bool = False,
    ) -> dict:
        """Scan module list, refresh cached state, and normalize the connected device label."""
        status("Scanning modules...")
        modules = self._read_module_list_with_retry(status)
        if not modules:
            raise RuntimeError("No modules found at Module List")

        self.controller._current_page = GDS2Page.MODULE_LIST

        module_indices = {name: idx for idx, name in enumerate(modules)}
        self.mapping.update_module_list(self._vehicle_id, module_indices)

        self._vin = self._extract_vin()
        if self._vin:
            self._vehicle_id = self._vin
            self.mapping.update_module_list(self._vehicle_id, module_indices)

        self._device = self._resolve_device_name(requested_device_name)
        self._module = None
        self._data_category = None
        self.controller.set_context(device=self._device)

        status("Ready")

        payload = {
            "modules": modules,
            "vin": self._vin,
            "device": self._device,
        }
        if include_device_connected:
            payload["device_connected"] = True
        return payload

    def _resolve_device_name(self, requested_device_name: Optional[str]) -> str:
        """Prefer an explicit device request, otherwise preserve the known connected device label."""
        if requested_device_name and requested_device_name not in ("default", "Connected Device"):
            return requested_device_name
        return self._device or "Connected Device"

    def _prepare_device_connection(self, device_name: str, status) -> GDS2Page:
        """Return the connected starting page, selecting a device first when required."""
        current = self.controller.detect_current_page()
        if self._can_continue_with_connected_device(device_name, current):
            logger.info(f"Device already connected at {current.value}, continuing...")
            status("Device already connected, continuing...")
            return current

        if not self._has_explicit_device_name(device_name):
            raise RuntimeError("Device name required when not at a connected page")

        self._ensure_device_selected(device_name, status)
        return self.controller.detect_current_page()

    @staticmethod
    def _has_explicit_device_name(device_name: Optional[str]) -> bool:
        """Return whether the caller explicitly named a concrete device to connect."""
        return bool(device_name and device_name != "default")

    @staticmethod
    def _can_continue_with_connected_device(device_name: Optional[str], current: GDS2Page) -> bool:
        """Return whether the current page already represents a connected device session."""
        return not DataViewerWorkflow._has_explicit_device_name(device_name) and current in CONNECTED_PAGES

    def _clear_connection_context(self) -> None:
        """Drop stale connection-local state after disconnecting the active device."""
        self._device = None
        self._module = None
        self._data_category = None
        if hasattr(self.controller, "set_context"):
            self.controller.set_context(device=None, module=None, data_category=None)

    def _prepare_module_selection(
        self,
        module_name: str,
    ) -> tuple[List[str], str, int, str]:
        """Load available modules and resolve which one should be opened."""
        items = self.controller.wait_for_list()
        if not items:
            raise RuntimeError("No modules found")

        resolved_module = self._resolve_branch_choice(
            domain=DecisionDomain.MODULE,
            target=module_name,
            choices=items,
            fallback_to_first=False,
        )
        target_index = self._find_item_index(items, resolved_module)
        if target_index is None:
            raise RuntimeError(f"Module '{module_name}' not found")

        return items, resolved_module, target_index, items[target_index]

    def _select_module_from_list(self, *, target_index: int, matched_module: str) -> None:
        """Open one module from the module list and refresh local workflow state."""
        result = self.controller.nav.select_list_item(0, target_index, double_click=True)
        if not result.get('success'):
            raise RuntimeError(f"Failed to select module: {result.get('message')}")

        try:
            self.controller.wait_for_page_transition(GDS2Page.MODULE_LIST, timeout=30)
        except TimeoutError:
            logger.warning("Page did not transition after module selection")

        self.controller.set_context(module=matched_module)
        self._module = matched_module
        self.controller._current_page = self.controller.detect_current_page()

    def _handle_clear_dtcs_selection_page(self, dialog_page: GDS2Page, status) -> GDS2Page:
        """Handle the intermediate Clear DTCs module-selection dialog when present."""
        if dialog_page != GDS2Page.CLEAR_DTCS_SELECTION:
            return dialog_page

        status("Selecting all modules for DTC clear...")
        if not self._wait_for_button_state("Add All", enabled=True, timeout_sec=5.0):
            raise RuntimeError("Add All button did not become enabled on Clear DTCs page.")
        self._click_agent_button("Add All")
        if not self._wait_for_button_state("OK", enabled=True, timeout_sec=5.0):
            raise RuntimeError("OK button did not become enabled after selecting modules.")

        self._sleep(0.8)
        status("Confirming module selection...")
        self._click_agent_button("OK")
        return self._wait_for_clear_dtcs_confirmation_page()

    def _wait_for_clear_dtcs_confirmation_page(self) -> GDS2Page:
        """Wait for the final Clear DTCs confirmation step or a direct data-display return."""
        dialog_deadline = time.time() + 15.0
        dialog_page = GDS2Page.CLEAR_DTCS_SELECTION
        while time.time() < dialog_deadline:
            dialog_page = self._wait_for_clear_dtcs_page(
                timeout_sec=2.0,
                allow_data_display=True,
            )
            if dialog_page != GDS2Page.CLEAR_DTCS_SELECTION:
                return dialog_page
            self._sleep(0.5)

        raise RuntimeError("Timed out waiting for final Clear DTCs confirmation page.")

    def _submit_clear_dtcs_confirmation(self, status) -> None:
        """Submit the final Clear DTCs confirmation action."""
        if not self._wait_for_button_state("OK", enabled=True, timeout_sec=5.0):
            raise RuntimeError("Confirmation OK button did not become enabled.")

        self._sleep(1.0)
        status("Submitting Clear DTCs command...")
        self._click_agent_button("OK")

    def _build_clear_dtcs_success_payload(
        self,
        *,
        pre_clear_count: int,
        page: GDS2Page,
    ) -> dict:
        """Compute the clear count delta and return the normalized success payload."""
        post_clear_count = self._read_dtc_count_with_fallback(default=pre_clear_count)
        cleared_count = max(0, pre_clear_count - post_clear_count)
        return {
            "success": True,
            "cleared_count": cleared_count,
            "message": "Clear DTCs completed",
            "page_context": page.value,
        }

    @staticmethod
    def _load_agent_json_payload(json_path_str: str) -> Any:
        """Load the Agent JSON payload using the first decoding that succeeds."""
        raw = None
        json_path = Path(json_path_str)
        if not json_path.exists():
            raise RuntimeError(f"Agent JSON file not found: {json_path}")

        for encoding in ['gbk', 'utf-8', 'utf-8-sig', 'latin-1', 'cp1252']:
            try:
                with json_path.open('r', encoding=encoding) as f:
                    raw = json.load(f)
                break
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            except OSError as exc:
                raise RuntimeError(f"Failed to read Agent JSON file: {exc}") from exc

        if raw is None:
            raise RuntimeError("Failed to decode Agent JSON file")

        return raw

    def _return_to_data_list_if_needed(self, status) -> None:
        """Leave Data Display before selecting another data category."""
        current = self.controller.detect_current_page()
        if current == GDS2Page.DATA_DISPLAY:
            status("Going back to Data List...")
            self.controller.go_back()

    def _select_data_category_sub_category_if_needed(
        self,
        *,
        data_category: str,
        result: NavigationResult,
        status,
    ) -> Optional[List[str]]:
        """Handle optional sub-category selection after choosing a data category."""
        if result.page != GDS2Page.SUB_DATA_LIST:
            return None

        sub_categories = self._handle_sub_category_result(
            data_category=data_category,
            choices=result.choices,
        )
        if not sub_categories:
            return sub_categories

        chosen_sub = self._resolve_branch_choice(
            domain=DecisionDomain.SUB_CATEGORY,
            target=data_category,
            choices=sub_categories,
            fallback_to_first=False,
        )
        if chosen_sub is None:
            return sub_categories

        status(f"Selecting {chosen_sub}...")
        sub_result = self.controller.select_sub_category(chosen_sub)
        if not sub_result.success:
            raise RuntimeError(f"Failed to select sub-category: {sub_result.error}")

        return sub_categories

    def _handle_sub_category_result(
        self,
        *,
        data_category: str,
        choices: Optional[List[str]],
    ) -> List[str]:
        """Validate and cache sub-category choices discovered for a data category."""
        sub_categories = list(choices or [])
        if not sub_categories:
            raise RuntimeError("Sub-categories detected but list is empty")

        module_name = getattr(self.controller, "current_module", None) or self._module
        if module_name:
            sub_indices = {name: idx for idx, name in enumerate(sub_categories)}
            self.mapping.update_sub_categories(
                self._vehicle_id,
                module_name,
                data_category,
                sub_indices,
            )

        return sub_categories

    @staticmethod
    def _ensure_agent_snapshot_matches_page(page_context: Optional[dict]) -> None:
        """Reject stale Agent snapshots that explicitly report a non-Data Display page."""
        if not isinstance(page_context, dict):
            return

        reported_page = (
            page_context.get("page")
            or page_context.get("currentPage")
            or page_context.get("current_page")
        )
        if reported_page is None:
            return

        normalized_page = str(reported_page).strip().lower()
        if normalized_page and normalized_page != GDS2Page.DATA_DISPLAY.value:
            raise RuntimeError(
                f"Detected stale Agent snapshot for page '{normalized_page}', expected data_display"
            )

    def _open_sub_module_target(
        self,
        submenu: List[str],
        resolved_sub_module: str,
        status,
    ) -> None:
        """Open one module submenu target and ensure the workflow leaves the submenu page."""
        status(f"Opening {resolved_sub_module}...")
        sub_index = self._find_item_index(submenu, resolved_sub_module)
        if sub_index is None:
            raise RuntimeError(f"Sub-module '{resolved_sub_module}' not found")

        nav_result = self.controller.nav.select_list_item(0, sub_index, double_click=True)
        if not nav_result.get("success"):
            raise RuntimeError(f"Failed to select sub-module: {nav_result.get('message')}")

        try:
            current = self.controller.wait_for_page_transition(GDS2Page.MODULE_SUBMENU, timeout=30)
        except TimeoutError:
            logger.warning("Page did not transition after sub-module selection")
            current = self.controller.detect_current_page()

        self.controller._current_page = current
        if current == GDS2Page.MODULE_SUBMENU:
            raise RuntimeError("Sub-module selection did not leave module submenu")

    def _collect_data_categories(
        self,
        *,
        previous_items: Optional[List[str]],
        module_name: Optional[str],
        status,
    ) -> List[str]:
        """Read, cache, and normalize the current data-category list."""
        self.controller.dismiss_warning_dialog()

        status("Scanning data categories...")
        data_categories = self.controller.wait_for_list(previous_items=previous_items)
        if not data_categories:
            raise RuntimeError("No data categories found")

        self.controller._current_page = GDS2Page.DATA_LIST
        self._data_category = None

        if module_name:
            cat_indices: Dict[str, int | Dict[str, int]] = {
                name: idx for idx, name in enumerate(data_categories)
            }
            self.mapping.update_data_categories(self._vehicle_id, module_name, cat_indices)

        status("Ready")
        return data_categories

    def _advance_connected_page_to_module_list(
        self,
        current: GDS2Page,
        status,
        *,
        prefer_single_back_from_module_submenu: bool = False,
    ) -> None:
        """Advance any already-connected GDS2 page back to the Module List."""
        current = self._enter_vehicle_selection_if_needed(current, status)
        current = self._open_module_diagnostics_if_needed(current, status)

        if current in (GDS2Page.DATA_DISPLAY, GDS2Page.DATA_LIST, GDS2Page.SUB_DATA_LIST):
            status("Navigating to Module List...")
            self._navigate_to_module_list(status)
            return

        if current == GDS2Page.MODULE_SUBMENU:
            if prefer_single_back_from_module_submenu:
                status("Going back to Module List...")
                self.controller.go_back()
            else:
                status("Navigating to Module List...")
                self._navigate_to_module_list(status)

    def _enter_vehicle_selection_if_needed(self, current: GDS2Page, status) -> GDS2Page:
        """Advance from Vehicle Selection to the next connected page."""
        if current != GDS2Page.VEHICLE_SELECTION:
            return current

        status("Waiting for vehicle to load...")
        enter_enabled = self._wait_for_button_enabled("Enter", timeout_sec=30)
        if not enter_enabled:
            raise RuntimeError("Enter button did not become enabled within 30 seconds")

        self._check_cancel()
        status("Entering vehicle...")
        self._check_cancel()
        result = self.controller.click_enter()
        if not result.success:
            raise RuntimeError(f"Failed to enter vehicle: {result.error}")
        return result.page

    def _open_module_diagnostics_if_needed(self, current: GDS2Page, status) -> GDS2Page:
        """Open Module Diagnostics when currently parked on the diagnostics menu."""
        if current != GDS2Page.DIAGNOSTICS_MENU:
            return current

        status("Selecting Module Diagnostics...")
        items = self.controller.wait_for_list()
        item_index = self._find_required_list_item(
            items,
            "Module Diagnostics",
            context_label="diagnostics menu",
        )
        self.controller.nav.select_list_item(0, item_index, double_click=True)
        try:
            current = self.controller.wait_for_page_transition(
                GDS2Page.DIAGNOSTICS_MENU,
                timeout=30,
            )
        except TimeoutError:
            logger.warning("Page did not transition after Module Diagnostics selection")
            current = self.controller.detect_current_page()
        self.controller._current_page = current
        if current == GDS2Page.DIAGNOSTICS_MENU:
            raise RuntimeError("Module Diagnostics selection did not leave diagnostics menu")
        return current

    @staticmethod
    def _find_required_list_item(
        items: List[str],
        expected_text: str,
        *,
        context_label: str,
    ) -> int:
        """Return the first matching list-item index or raise a descriptive error."""
        for index, item in enumerate(items):
            if expected_text in item:
                return index
        raise RuntimeError(f"{expected_text} entry not found in {context_label}")

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

    def reset_startup_state(self) -> None:
        """Best-effort cleanup after cancelled or failed startup."""
        self.set_cancel_checker(None)

        try:
            self.stop_monitoring()
        except Exception as exc:
            logger.debug("Ignoring monitoring cleanup failure during startup reset: %s", exc)

        try:
            self.controller.dismiss_warning_dialog()
        except Exception as exc:
            logger.debug("Ignoring warning-dialog cleanup failure: %s", exc)

        try:
            from ..native.device_explorer import DeviceExplorerController

            explorer = DeviceExplorerController()
            if explorer.is_visible() and explorer.find_dialog(timeout_sec=1.0):
                if not explorer.click_cancel():
                    explorer.close()
                self._sleep(1.0)
        except Exception as exc:
            logger.debug("Ignoring device-explorer cleanup failure: %s", exc)

        try:
            self._navigate_to_main_menu(lambda msg: logger.info(msg))
        except Exception as exc:
            logger.warning("Failed to normalize startup state: %s", exc)

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

    def _resolve_branch_choice(
        self,
        domain: DecisionDomain,
        target: str,
        choices: List[str],
        fallback_to_first: bool,
    ) -> str:
        """Resolve a branch choice using constrained planner heuristics."""
        if not choices:
            raise RuntimeError(f"No choices available for {domain.value} resolution")

        if domain == DecisionDomain.MODULE:
            decision = self._branch_planner.decide_module(target, choices)
        elif domain == DecisionDomain.SUB_MODULE:
            decision = self._branch_planner.decide_sub_module(target, choices)
        elif domain == DecisionDomain.DATA_CATEGORY:
            decision = self._branch_planner.decide_data_category(target, choices)
        else:
            decision = self._branch_planner.decide_sub_category(target, choices)

        if decision.requires_human:
            logger.warning(
                "Planner requires human decision for %s target '%s': %s",
                domain.value,
                target,
                decision.reason,
            )
            if fallback_to_first:
                fallback = choices[0]
                logger.warning(
                    "Using deterministic fallback for %s: '%s'",
                    domain.value,
                    fallback,
                )
                return fallback
            raise BranchDecisionRequiredError(decision=decision, choices=choices)

        logger.info(
            "Planner selected %s '%s' for target '%s' (confidence=%.2f)",
            domain.value,
            decision.selected_option,
            target,
            decision.confidence,
        )
        if decision.selected_option is None:
            raise RuntimeError(
                f"Planner returned no selection for {domain.value} target '{target}'"
            )
        return decision.selected_option

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
