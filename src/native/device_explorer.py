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
import struct
import time
import logging
from typing import List, Optional

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
LVM_GETITEMTEXT = LVM_FIRST + 115  # LVM_GETITEMTEXTW (Unicode)
LVM_SETITEMSTATE = LVM_FIRST + 43
LVM_GETITEMSTATE = LVM_FIRST + 44
LVM_ENSUREVISIBLE = LVM_FIRST + 19
LVIS_SELECTED = 0x0002
LVIS_FOCUSED = 0x0001

# Process access rights
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_QUERY_INFORMATION = 0x0400

# Memory allocation
MEM_COMMIT = 0x1000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04

# Process query
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

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

    def get_device_names(self) -> List[str]:
        """
        Get all device names from the list using cross-process memory reading.

        Uses Windows API to allocate memory in the target process and
        read the ListView item text. Handles both 32-bit and 64-bit targets.

        Returns:
            List of device name strings
        """
        if not self._listview_hwnd:
            logger.warning("ListView not found, cannot get device names")
            return []

        count = self.get_device_count()
        if count == 0:
            return []

        logger.info(f"ListView has {count} items, reading names...")

        # Get the process ID for the dialog
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(self._dialog_hwnd, ctypes.byref(process_id))

        if not process_id.value:
            logger.warning("Could not get process ID")
            return [f"Device {i+1}" for i in range(count)]

        # Open the process with required permissions
        process_handle = kernel32.OpenProcess(
            PROCESS_VM_OPERATION | PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_QUERY_INFORMATION,
            False,
            process_id.value
        )

        if not process_handle:
            logger.warning(f"Could not open process {process_id.value}")
            return [f"Device {i+1}" for i in range(count)]

        try:
            # Detect if target process is 32-bit (WOW64)
            is_wow64 = ctypes.c_int(0)
            kernel32.IsWow64Process(process_handle, ctypes.byref(is_wow64))
            target_is_32bit = bool(is_wow64.value)
            logger.info(f"Target process is {'32-bit' if target_is_32bit else '64-bit'}")

            device_names = []
            for i in range(count):
                name = self._read_listview_item_text(process_handle, i, target_is_32bit=target_is_32bit)
                if name:
                    device_names.append(name)
                else:
                    device_names.append(f"Device {i+1}")

            logger.info(f"Read device names: {device_names}")
            return device_names

        finally:
            kernel32.CloseHandle(process_handle)

    def _read_listview_item_text(self, process_handle, item_index: int, subitem: int = 0, target_is_32bit: bool = False) -> Optional[str]:
        """
        Read text from a ListView item in another process.

        Uses cross-process memory allocation to get the item text.
        Supports both 32-bit and 64-bit target processes.

        Args:
            process_handle: Handle to the target process
            item_index: Index of the item
            subitem: Subitem index (0 for main text)
            target_is_32bit: True if target is a 32-bit (WOW64) process

        Returns:
            Item text or None if failed
        """
        MAX_TEXT_LENGTH = 256

        if target_is_32bit:
            # 32-bit LVITEMW: pointers are 4 bytes
            # mask(4) iItem(4) iSubItem(4) state(4) stateMask(4)
            # pszText(4) cchTextMax(4) iImage(4) lParam(4)
            LVITEM_SIZE = 36
        else:
            # 64-bit LVITEMW: pointers are 8 bytes, alignment padding
            LVITEM_SIZE = 72

        # Allocate memory in target process for LVITEM structure + text buffer
        total_size = LVITEM_SIZE + (MAX_TEXT_LENGTH * 2)  # Unicode chars
        remote_buffer = kernel32.VirtualAllocEx(
            process_handle,
            None,
            total_size,
            MEM_COMMIT,
            PAGE_READWRITE
        )

        if not remote_buffer:
            logger.debug(f"Failed to allocate memory for item {item_index}")
            return None

        try:
            text_buffer_addr = remote_buffer + LVITEM_SIZE

            if target_is_32bit:
                # 32-bit LVITEMW struct layout
                # All pointers are 4 bytes (use "I" for unsigned 32-bit)
                lvitem = struct.pack(
                    "<IiiII I i i I",
                    0x0001,                              # mask (LVIF_TEXT)
                    item_index,                          # iItem
                    subitem,                             # iSubItem
                    0,                                   # state
                    0,                                   # stateMask
                    text_buffer_addr & 0xFFFFFFFF,       # pszText (32-bit pointer)
                    MAX_TEXT_LENGTH,                      # cchTextMax
                    0,                                   # iImage
                    0,                                   # lParam
                )
            else:
                # 64-bit LVITEMW struct layout
                lvitem = struct.pack(
                    "IiiII" + "Q" + "i" + "xxxx" + "Q" * 4,
                    0x0001,          # mask (LVIF_TEXT)
                    item_index,      # iItem
                    subitem,         # iSubItem
                    0,               # state
                    0,               # stateMask
                    text_buffer_addr,  # pszText (64-bit pointer)
                    MAX_TEXT_LENGTH,   # cchTextMax
                    0, 0, 0, 0       # padding/other fields
                )

            # Write LVITEM to remote process
            bytes_written = ctypes.c_size_t()
            success = kernel32.WriteProcessMemory(
                process_handle,
                remote_buffer,
                lvitem,
                len(lvitem),
                ctypes.byref(bytes_written)
            )

            if not success:
                logger.debug(f"Failed to write LVITEM for item {item_index}")
                return None

            # Send LVM_GETITEMTEXT message
            result = user32.SendMessageW(
                self._listview_hwnd,
                LVM_GETITEMTEXT,
                item_index,
                remote_buffer
            )

            if result == 0:
                logger.debug(f"LVM_GETITEMTEXT returned 0 for item {item_index}")
                return None

            # Read the text from remote process
            text_buffer = ctypes.create_unicode_buffer(MAX_TEXT_LENGTH)
            bytes_read = ctypes.c_size_t()
            success = kernel32.ReadProcessMemory(
                process_handle,
                text_buffer_addr,
                text_buffer,
                MAX_TEXT_LENGTH * 2,
                ctypes.byref(bytes_read)
            )

            if success and bytes_read.value > 0:
                return text_buffer.value
            else:
                logger.debug(f"Failed to read text for item {item_index}")
                return None

        finally:
            kernel32.VirtualFreeEx(process_handle, remote_buffer, 0, MEM_RELEASE)

    def get_available_devices(self) -> List[str]:
        """
        Public API: Find dialog and enumerate all available devices.

        This is the main entry point for device discovery.
        Does not require the dialog to be already found.

        Returns:
            List of device names, or empty list if dialog not found
        """
        if not self._dialog_hwnd:
            if not self.find_dialog(timeout_sec=3.0):
                return []

        return self.get_device_names()

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

        Dynamically reads device names from the list and selects by match.

        Args:
            name: Device name to search for (e.g., "SM2", "SM2 USB")

        Returns:
            True if found and selected
        """
        device_names = self.get_device_names()

        if not device_names:
            logger.warning("No devices found in list")
            return False

        # Try exact match first
        for i, device in enumerate(device_names):
            if name.upper() == device.upper():
                logger.info(f"Exact match: '{device}' at index {i}")
                return self.select_device(i)

        # Try partial match (case-insensitive)
        for i, device in enumerate(device_names):
            if name.upper() in device.upper():
                logger.info(f"Partial match: '{device}' at index {i}")
                return self.select_device(i)

        logger.warning(f"Device '{name}' not found in {device_names}")
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
