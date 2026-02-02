"""
Native Windows API controllers for GDS2 components.
"""

from .device_explorer import (
    DeviceExplorerController,
    handle_device_explorer,
)


def get_available_devices(timeout: float = 3.0):
    """
    Convenience function to get available devices from Device Explorer.

    Args:
        timeout: Maximum time to wait for dialog

    Returns:
        List of device names, or empty list if dialog not found
    """
    controller = DeviceExplorerController()
    if controller.find_dialog(timeout_sec=timeout):
        return controller.get_device_names()
    return []


__all__ = [
    'DeviceExplorerController',
    'handle_device_explorer',
    'get_available_devices',
]
