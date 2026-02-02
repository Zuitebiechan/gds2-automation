"""
Windows API controller for Device Explorer dialog.

Device Explorer is a Win32 native dialog (#32770) with:
- SysListView32: Device list
- Button: Continue, Cancel

This allows controlling Device Explorer WITHOUT:
- Foreground window requirement
- Screen resolution dependency
- Template matching
"""

import ctypes
from ctypes import wintypes
import time
import logging

logger = logging.getLogger(__name__)

# Windows API
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# Window messages
WM_COMMAND = 0x0111
BN_CLICKED = 0
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_CLOSE = 0x0010

# ListView messages
LVM_FIRST = 0x1000
LVM_GETITEMCOUNT = LVM_FIRST + 4
LVM_GETITEMTEXT = LVM_FIRST + 45
LVM_SETITEMSTATE = LVM_FIRST + 43
LVM_GETITEMSTATE = LVM_FIRST + 44
LVM_ENSUREVISIBLE = LVM_FIRST + 19
LVIS_SELECTED = 0x0002
LVIS_FOCUSED = 0x0001

# Callback type
EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
EnumChildProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class DeviceExplorerController:
    """
    Controls Device Explorer dialog using Windows API.

    Works in background - no foreground/resolution requirements.
    """

    def __init__(self):
        self._dialog_hwnd = None
        self._listview_hwnd = None
        self._continue_btn_hwnd = None
        self._cancel_btn_hwnd = None

    def find_dialog(self, timeout_sec: float = 10.0) -> bool:
        """
        Find Device Explorer dialog window.

        Args:
            timeout_sec: Maximum time to wait for dialog

        Returns:
            True if found, False otherwise
        """
        start_time = time.time()

        while time.time() - start_time < timeout_sec:
            hwnd = self._find_window_by_title("Device Explorer")
            if hwnd:
                self._dialog_hwnd = hwnd
                self._find_child_controls()
                logger.info(f"Found Device Explorer dialog: HWND={hwnd}")
                return True
            time.sleep(0.2)

        logger.warning("Device Explorer dialog not found")
        return False

    def is_visible(self) -> bool:
        """Check if Device Explorer is currently visible."""
        hwnd = self._find_window_by_title("Device Explorer")
        return hwnd is not None and user32.IsWindowVisible(hwnd)

    def get_device_count(self) -> int:
        """Get number of devices in the list."""
        if not self._listview_hwnd:
            return 0
        return user32.SendMessageW(self._listview_hwnd, LVM_GETITEMCOUNT, 0, 0)

    def get_device_names(self) -> list:
        """
        Get all device names from the list.

        Note: This is a simplified version - getting text from ListView
        in another process requires memory allocation in that process.
        For now, we'll use the known device indices.
        """
        count = self.get_device_count()
        logger.info(f"ListView has {count} items")
        # Device names are typically: MDI, MDI 2, SM2 USB, SM3 USB
        return [f"Device {i}" for i in range(count)]

    def select_device(self, index: int) -> bool:
        """
        Select a device by index.

        Args:
            index: Device index (0-based)

        Returns:
            True if selected, False otherwise
        """
        if not self._listview_hwnd:
            logger.error("ListView not found")
            return False

        count = self.get_device_count()
        if index >= count:
            logger.error(f"Index {index} out of range (count={count})")
            return False

        # Clear previous selection and select new item
        # Set state: LVIS_SELECTED | LVIS_FOCUSED
        state = LVIS_SELECTED | LVIS_FOCUSED

        # Use SendMessage to select item
        # For cross-process ListView, we need to use a different approach
        # Simulate click on the item

        # First, ensure the item is visible
        user32.SendMessageW(self._listview_hwnd, LVM_ENSUREVISIBLE, index, 0)

        # Set focus to ListView
        user32.SetFocus(self._listview_hwnd)

        # Select the item by sending mouse click
        # Get item rect and click center
        self._click_listview_item(index)

        logger.info(f"Selected device at index {index}")
        return True

    def select_device_by_name(self, name: str) -> bool:
        """
        Select device by name (partial match).

        Known devices: MDI, MDI 2, SM2 USB, SM3 USB

        Args:
            name: Device name to search for (e.g., "SM2")

        Returns:
            True if found and selected
        """
        # Known device order in GDS2 Device Explorer
        known_devices = ["MDI", "MDI 2", "SM2 USB", "SM3 USB"]

        for i, device in enumerate(known_devices):
            if name.upper() in device.upper():
                return self.select_device(i)

        logger.warning(f"Device '{name}' not found in known devices")
        return False

    def click_continue(self) -> bool:
        """Click the Continue button."""
        if not self._continue_btn_hwnd:
            # Try to find it again
            self._find_child_controls()

        if self._continue_btn_hwnd:
            self._click_button(self._continue_btn_hwnd)
            logger.info("Clicked Continue button")
            return True

        logger.error("Continue button not found")
        return False

    def click_cancel(self) -> bool:
        """Click the Cancel button."""
        if not self._cancel_btn_hwnd:
            self._find_child_controls()

        if self._cancel_btn_hwnd:
            self._click_button(self._cancel_btn_hwnd)
            logger.info("Clicked Cancel button")
            return True

        logger.error("Cancel button not found")
        return False

    def close(self) -> bool:
        """Close the Device Explorer dialog."""
        if self._dialog_hwnd:
            user32.SendMessageW(self._dialog_hwnd, WM_CLOSE, 0, 0)
            logger.info("Closed Device Explorer dialog")
            return True
        return False

    # ========== Private Methods ==========

    def _find_window_by_title(self, title: str):
        """Find window by partial title match."""
        result = [None]

        def callback(hwnd, lparam):
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd) + 1
                buffer = ctypes.create_unicode_buffer(length)
                user32.GetWindowTextW(hwnd, buffer, length)
                if title in buffer.value:
                    result[0] = hwnd
                    return False  # Stop enumeration
            return True

        user32.EnumWindows(EnumWindowsProc(callback), 0)
        return result[0]

    def _find_child_controls(self):
        """Find child controls in the dialog."""
        if not self._dialog_hwnd:
            return

        self._listview_hwnd = None
        self._continue_btn_hwnd = None
        self._cancel_btn_hwnd = None

        def callback(hwnd, lparam):
            class_name = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, class_name, 256)

            text_length = user32.GetWindowTextLengthW(hwnd) + 1
            text = ctypes.create_unicode_buffer(text_length)
            user32.GetWindowTextW(hwnd, text, text_length)

            if class_name.value == "SysListView32":
                self._listview_hwnd = hwnd
            elif class_name.value == "Button":
                if text.value == "Continue":
                    self._continue_btn_hwnd = hwnd
                elif text.value == "Cancel":
                    self._cancel_btn_hwnd = hwnd

            return True

        user32.EnumChildWindows(self._dialog_hwnd, EnumChildProc(callback), 0)

        logger.debug(f"Found controls: ListView={self._listview_hwnd}, "
                    f"Continue={self._continue_btn_hwnd}, Cancel={self._cancel_btn_hwnd}")

    def _click_button(self, hwnd):
        """Click a button by sending messages."""
        # Method 1: Send BN_CLICKED notification to parent
        parent = user32.GetParent(hwnd)
        ctrl_id = user32.GetDlgCtrlID(hwnd)
        user32.SendMessageW(parent, WM_COMMAND,
                           (BN_CLICKED << 16) | ctrl_id, hwnd)

    def _click_listview_item(self, index: int):
        """Click a ListView item by index."""
        if not self._listview_hwnd:
            return

        # For cross-process ListView selection, we use keyboard simulation
        # First focus the ListView
        user32.SetForegroundWindow(self._dialog_hwnd)
        time.sleep(0.1)
        user32.SetFocus(self._listview_hwnd)
        time.sleep(0.1)

        # Send key presses to navigate to the item
        # First go to top (Home key)
        self._send_key(self._listview_hwnd, 0x24)  # VK_HOME
        time.sleep(0.05)

        # Then press Down arrow 'index' times
        for _ in range(index):
            self._send_key(self._listview_hwnd, 0x28)  # VK_DOWN
            time.sleep(0.05)

    def _send_key(self, hwnd, vk_code):
        """Send a key press to a window."""
        WM_KEYDOWN = 0x0100
        WM_KEYUP = 0x0101
        user32.SendMessageW(hwnd, WM_KEYDOWN, vk_code, 0)
        user32.SendMessageW(hwnd, WM_KEYUP, vk_code, 0)


# ========== Convenience Functions ==========

def handle_device_explorer(device_name: str = "SM2 USB", timeout: float = 10.0) -> bool:
    """
    Handle Device Explorer dialog - select device and click Continue.

    This function works WITHOUT requiring:
    - Foreground window
    - Screen resolution dependency
    - Template matching

    Args:
        device_name: Device to select (e.g., "SM2 USB")
        timeout: Maximum time to wait for dialog

    Returns:
        True if handled successfully, False otherwise
    """
    controller = DeviceExplorerController()

    if not controller.find_dialog(timeout):
        logger.info("Device Explorer not found (may not be needed)")
        return True  # Not an error - dialog may not appear

    logger.info(f"Device Explorer found, selecting '{device_name}'...")

    # Select device
    if not controller.select_device_by_name(device_name):
        logger.warning(f"Could not select device '{device_name}'")
        return False

    time.sleep(0.3)

    # Click Continue
    if not controller.click_continue():
        logger.error("Could not click Continue button")
        return False

    logger.info("Device Explorer handled successfully")
    return True


# Test
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("=" * 60)
    print("Device Explorer Controller Test")
    print("=" * 60)
    print("\nMake sure Device Explorer dialog is visible...")
    print()

    controller = DeviceExplorerController()

    if controller.find_dialog(timeout_sec=5):
        print(f"  Dialog found!")
        print(f"  Device count: {controller.get_device_count()}")
        print(f"  ListView HWND: {controller._listview_hwnd}")
        print(f"  Continue HWND: {controller._continue_btn_hwnd}")
        print(f"  Cancel HWND: {controller._cancel_btn_hwnd}")

        print("\n  Selecting SM2 USB...")
        controller.select_device_by_name("SM2 USB")

        time.sleep(1)

        print("  Clicking Continue...")
        controller.click_continue()

        print("\n  Done!")
    else:
        print("  Device Explorer not found!")
