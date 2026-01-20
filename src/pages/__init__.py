"""
GDS2 Page Objects

Page Object pattern implementation for GDS2 navigation.
Each page only contains UI operations, not business logic.
"""

from .base_page import BasePage
from .main_menu_page import MainMenuPage
from .device_explorer_page import DeviceExplorerPage
from .vehicle_selection_page import VehicleSelectionPage
from .diagnostics_menu_page import DiagnosticsMenuPage
from .vehicle_diagnostics_page import VehicleDiagnosticsPage
from .dtc_page import DTCPage

__all__ = [
    "BasePage",
    "MainMenuPage",
    "DeviceExplorerPage",
    "VehicleSelectionPage",
    "DiagnosticsMenuPage",
    "VehicleDiagnosticsPage",
    "DTCPage",
]
