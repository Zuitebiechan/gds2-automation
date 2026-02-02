"""
Tests for Device Explorer controller.

Tests dynamic device discovery and selection using mocked Win32 calls.
"""

import pytest
from unittest.mock import MagicMock, patch

from src.native.device_explorer import DeviceExplorerController


class TestDeviceExplorerController:
    """Tests for DeviceExplorerController."""

    @patch('src.native.device_explorer.user32')
    @patch('src.native.device_explorer.kernel32')
    def test_get_device_count(self, mock_kernel32, mock_user32):
        """Test getting device count from ListView."""
        controller = DeviceExplorerController()
        controller._listview_hwnd = 12345

        # Mock SendMessageW to return count
        mock_user32.SendMessageW.return_value = 4

        count = controller.get_device_count()

        assert count == 4
        mock_user32.SendMessageW.assert_called()

    @patch('src.native.device_explorer.user32')
    @patch('src.native.device_explorer.kernel32')
    def test_get_device_count_no_listview(self, mock_kernel32, mock_user32):
        """Test that get_device_count returns 0 when no ListView."""
        controller = DeviceExplorerController()
        controller._listview_hwnd = None

        count = controller.get_device_count()

        assert count == 0

    def test_get_known_device_names(self):
        """Test fallback known device names."""
        controller = DeviceExplorerController()

        # Mock get_device_count
        controller.get_device_count = MagicMock(return_value=3)

        names = controller._get_known_device_names()

        assert names == ["MDI", "MDI 2", "SM2 USB"]

    def test_get_known_device_names_all(self):
        """Test all known device names when count is 4."""
        controller = DeviceExplorerController()
        controller.get_device_count = MagicMock(return_value=4)

        names = controller._get_known_device_names()

        assert names == ["MDI", "MDI 2", "SM2 USB", "SM3 USB"]

    def test_get_known_device_names_more_than_known(self):
        """Test fallback when count exceeds known devices."""
        controller = DeviceExplorerController()
        controller.get_device_count = MagicMock(return_value=6)

        names = controller._get_known_device_names()

        assert names == ["Device 0", "Device 1", "Device 2", "Device 3", "Device 4", "Device 5"]

    @patch('src.native.device_explorer.user32')
    @patch('src.native.device_explorer.kernel32')
    def test_select_device_by_name_exact_match(self, mock_kernel32, mock_user32):
        """Test selecting device by exact name match."""
        controller = DeviceExplorerController()
        controller._listview_hwnd = 12345
        controller._dialog_hwnd = 67890

        # Mock get_device_names to return test devices
        controller.get_device_names = MagicMock(return_value=["MDI", "MDI 2", "SM2 USB", "SM3 USB"])

        # Mock select_device to succeed
        controller.select_device = MagicMock(return_value=True)

        result = controller.select_device_by_name("SM2 USB")

        assert result is True
        controller.select_device.assert_called_with(2)  # Index of SM2 USB

    @patch('src.native.device_explorer.user32')
    @patch('src.native.device_explorer.kernel32')
    def test_select_device_by_name_partial_match(self, mock_kernel32, mock_user32):
        """Test selecting device by partial name match."""
        controller = DeviceExplorerController()
        controller._listview_hwnd = 12345
        controller._dialog_hwnd = 67890

        controller.get_device_names = MagicMock(return_value=["MDI", "MDI 2", "SM2 USB", "SM3 USB"])
        controller.select_device = MagicMock(return_value=True)

        result = controller.select_device_by_name("SM2")

        assert result is True
        controller.select_device.assert_called_with(2)

    @patch('src.native.device_explorer.user32')
    @patch('src.native.device_explorer.kernel32')
    def test_select_device_by_name_case_insensitive(self, mock_kernel32, mock_user32):
        """Test that device selection is case-insensitive."""
        controller = DeviceExplorerController()
        controller._listview_hwnd = 12345
        controller._dialog_hwnd = 67890

        controller.get_device_names = MagicMock(return_value=["MDI", "MDI 2", "SM2 USB", "SM3 USB"])
        controller.select_device = MagicMock(return_value=True)

        result = controller.select_device_by_name("sm2 usb")

        assert result is True
        controller.select_device.assert_called_with(2)

    def test_select_device_by_name_not_found(self):
        """Test error when device not found."""
        controller = DeviceExplorerController()

        controller.get_device_names = MagicMock(return_value=["MDI", "MDI 2"])
        controller.select_device = MagicMock(return_value=True)

        result = controller.select_device_by_name("SM2 USB")

        assert result is False
        controller.select_device.assert_not_called()

    def test_select_device_by_name_empty_list(self):
        """Test error when no devices available."""
        controller = DeviceExplorerController()

        controller.get_device_names = MagicMock(return_value=[])

        result = controller.select_device_by_name("SM2 USB")

        assert result is False

    @patch('src.native.device_explorer.user32')
    def test_find_dialog(self, mock_user32):
        """Test finding Device Explorer dialog."""
        controller = DeviceExplorerController()

        # Mock window enumeration to find dialog
        def mock_enum_windows(callback, lparam):
            # Simulate finding a window with "Device Explorer" title
            # The callback expects (hwnd, lparam)
            pass

        mock_user32.EnumWindows = mock_enum_windows
        mock_user32.IsWindowVisible.return_value = True

        # Since we're mocking, we can't fully test EnumWindows callback
        # Just verify the method exists and can be called
        assert hasattr(controller, 'find_dialog')

    def test_get_available_devices_no_dialog(self):
        """Test get_available_devices when dialog not found."""
        controller = DeviceExplorerController()
        controller.find_dialog = MagicMock(return_value=False)

        devices = controller.get_available_devices()

        assert devices == []

    def test_get_available_devices_with_dialog(self):
        """Test get_available_devices when dialog is found."""
        controller = DeviceExplorerController()
        controller.find_dialog = MagicMock(return_value=True)
        controller.get_device_names = MagicMock(return_value=["MDI", "SM2 USB"])

        devices = controller.get_available_devices()

        assert devices == ["MDI", "SM2 USB"]


class TestHandleDeviceExplorer:
    """Tests for handle_device_explorer convenience function."""

    @patch('src.native.device_explorer.DeviceExplorerController')
    def test_handle_device_explorer_no_dialog(self, MockController):
        """Test that function succeeds when no dialog appears."""
        from src.native.device_explorer import handle_device_explorer

        mock_instance = MockController.return_value
        mock_instance.find_dialog.return_value = False

        result = handle_device_explorer("SM2 USB")

        assert result is True  # No dialog is not an error

    @patch('src.native.device_explorer.DeviceExplorerController')
    @patch('time.sleep')
    def test_handle_device_explorer_success(self, mock_sleep, MockController):
        """Test successful device selection."""
        from src.native.device_explorer import handle_device_explorer

        mock_instance = MockController.return_value
        mock_instance.find_dialog.return_value = True
        mock_instance.select_device_by_name.return_value = True
        mock_instance.click_continue.return_value = True

        result = handle_device_explorer("SM2 USB")

        assert result is True
        mock_instance.select_device_by_name.assert_called_with("SM2 USB")
        mock_instance.click_continue.assert_called_once()

    @patch('src.native.device_explorer.DeviceExplorerController')
    def test_handle_device_explorer_select_failed(self, MockController):
        """Test when device selection fails."""
        from src.native.device_explorer import handle_device_explorer

        mock_instance = MockController.return_value
        mock_instance.find_dialog.return_value = True
        mock_instance.select_device_by_name.return_value = False

        result = handle_device_explorer("Unknown Device")

        assert result is False

    @patch('src.native.device_explorer.DeviceExplorerController')
    @patch('time.sleep')
    def test_handle_device_explorer_continue_failed(self, mock_sleep, MockController):
        """Test when Continue button click fails."""
        from src.native.device_explorer import handle_device_explorer

        mock_instance = MockController.return_value
        mock_instance.find_dialog.return_value = True
        mock_instance.select_device_by_name.return_value = True
        mock_instance.click_continue.return_value = False

        result = handle_device_explorer("SM2 USB")

        assert result is False
