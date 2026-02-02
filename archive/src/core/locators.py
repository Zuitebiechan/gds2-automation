"""
GDS2 UI Locators

Centralized definitions for all UI elements.
Update this file when UI changes - no need to modify page classes.

Button names are from the official GM GDS2 User Guide (res/GM-GDS2-User-Guide.pdf).
"""

from dataclasses import dataclass
from typing import Tuple, Optional


@dataclass
class Locator:
    """UI element locator."""
    title: str
    control_type: str
    description: str = ""

    def as_tuple(self) -> Tuple[str, str]:
        """Return (title, control_type) tuple for driver methods."""
        return (self.title, self.control_type)


class GDS2Locators:
    """
    All GDS2 UI element definitions.

    Organized by page/screen for easy maintenance.
    Official button names from GM GDS2 User Guide.
    """

    # ==================== Bottom Navigation Bar ====================
    # These buttons appear at the bottom of most GDS2 screens
    class Navigation:
        BACK_BTN = Locator("Back", "Button", "Return to previous screen")
        FEEDBACK_BTN = Locator("Feedback", "Button", "Submit feedback to GM")
        HOME_BTN = Locator("Home", "Button", "Return to Main Menu")
        VEHICLE_MENU_BTN = Locator("Vehicle Menu", "Button", "Open vehicle selection menu")
        ENTER_BTN = Locator("Enter", "Button", "Proceed with current selection")
        MODULE_BTN = Locator("Module", "Button", "Return to module list")

    # ==================== Main Menu ====================
    class MainMenu:
        DIAGNOSTICS_BTN = Locator("Diagnostics", "Button", "Vehicle diagnostics functions")
        CALIBRATIONS_BTN = Locator("Calibrations", "Button", "Module calibration functions")
        REPROGRAM_BTN = Locator("Reprogram", "Button", "Module reprogramming")
        SERVICE_PROGRAMMING_BTN = Locator("Service Programming System", "Button", "SPS functions")
        # Navigation buttons
        HOME_BTN = Locator("Home", "Button", "Return to main menu")
        BACK_BTN = Locator("Back", "Button", "Go back one screen")

    # ==================== Device Explorer ====================
    # The popup window for selecting VCI device
    class DeviceExplorer:
        # Device list items are dynamic - searched by name containing device string
        CONNECT_BTN = Locator("Connect", "Button", "Connect to selected device")
        DISCONNECT_BTN = Locator("Disconnect", "Button", "Disconnect from device")
        CONTINUE_BTN = Locator("Continue", "Button", "Continue with selected device")
        NAVIGATE_WITHOUT_DEVICE_BTN = Locator("Navigate Without Device", "Button", "Browse without VCI connection")

    # ==================== Vehicle Selection ====================
    class VehicleSelection:
        ENTER_BTN = Locator("Enter", "Button", "Enter vehicle diagnostics")
        READ_VIN_BTN = Locator("Read VIN", "Button", "Read VIN from vehicle")
        DECODE_VIN_BTN = Locator("Decode VIN", "Button", "Manually enter and decode VIN")
        CLEAR_VEHICLE_BTN = Locator("Clear Vehicle Selection", "Button", "Clear current vehicle data")
        OK_BTN = Locator("OK", "Button", "Dismiss warning dialog")

    # ==================== Diagnostics Menu ====================
    class DiagnosticsMenu:
        MODULE_DIAGNOSTICS = Locator("Module Diagnostics", "ListItem", "Diagnose specific module")
        VEHICLE_DIAGNOSTICS = Locator("Vehicle Diagnostics", "ListItem", "Vehicle-wide diagnostic functions")
        SYSTEM_DIAGNOSTICS = Locator("System Diagnostics", "ListItem", "System-level diagnostics")
        SESSION_MANAGER = Locator("Session Manager", "ListItem", "Manage diagnostic sessions")
        ENTER_BTN = Locator("Enter", "Button", "Enter selected menu")

    # ==================== Vehicle Diagnostics Submenu ====================
    class VehicleDiagnostics:
        SUPPORTED_MODULES = Locator("Supported Modules", "ListItem", "List of modules on vehicle")
        VEHICLE_DTC_INFO = Locator("Vehicle DTC Information", "ListItem", "View DTCs from all modules")
        CLEAR_VEHICLE_DTCS = Locator("Clear Vehicle DTCs", "ListItem", "Clear all vehicle DTCs")
        ENTER_BTN = Locator("Enter", "Button", "Enter selected option")

    # ==================== DTC Display Page ====================
    # The Vehicle DTC Information screen
    class DTCPage:
        CLEAR_DTCS_BTN = Locator("Clear DTCs", "Button", "Clear displayed DTCs")
        REFRESH_BTN = Locator("Refresh", "Button", "Refresh DTC list")
        DETAILS_BTN = Locator("Details", "Button", "Show DTC details view")
        SUMMARY_BTN = Locator("Summary", "Button", "Show DTC summary view")
        CREATE_REPORT_BTN = Locator("Create Report", "Button", "Generate HTML report")

    # ==================== Data Display (PID/Data) ====================
    class DataDisplay:
        CREATE_REPORT_BTN = Locator("Create Report", "Button", "Generate data report")
        ADD_BOOKMARK_BTN = Locator("Add Bookmark", "Button", "Add bookmark to recording")
        ADD_SNAPSHOT_BOOKMARK_BTN = Locator("Add Snapshot Bookmark", "Button", "Add snapshot bookmark")
        PAUSE_BTN = Locator("Pause", "Button", "Pause data recording")
        RECORD_BTN = Locator("Record", "Button", "Start data recording")
        STOP_BTN = Locator("Stop", "Button", "Stop data recording")

    # ==================== Module Diagnostics ====================
    class ModuleDiagnostics:
        DTC_INFO = Locator("DTC Information", "ListItem", "View module DTCs")
        CLEAR_DTC = Locator("Clear DTC Information", "ListItem", "Clear module DTCs")
        DATA_DISPLAY = Locator("Data Display", "ListItem", "View live data")
        SPECIAL_FUNCTIONS = Locator("Special Functions", "ListItem", "Module special functions")
        ID_INFO = Locator("I/D Information", "ListItem", "Module identification info")
        ENTER_BTN = Locator("Enter", "Button", "Enter selected function")

    # ==================== Common Dialogs ====================
    class Dialog:
        OK_BTN = Locator("OK", "Button", "Confirm dialog")
        CANCEL_BTN = Locator("Cancel", "Button", "Cancel dialog")
        YES_BTN = Locator("Yes", "Button", "Confirm action")
        NO_BTN = Locator("No", "Button", "Decline action")
        CLOSE_BTN = Locator("Close", "Button", "Close dialog")

    # ==================== Common (Aliases for Convenience) ====================
    class Common:
        ENTER_BTN = Locator("Enter", "Button", "Generic enter button")
        OK_BTN = Locator("OK", "Button", "Generic OK button")
        CANCEL_BTN = Locator("Cancel", "Button", "Generic cancel button")
        BACK_BTN = Locator("Back", "Button", "Generic back button")
        HOME_BTN = Locator("Home", "Button", "Generic home button")


# Shorthand aliases for convenience
Loc = GDS2Locators
