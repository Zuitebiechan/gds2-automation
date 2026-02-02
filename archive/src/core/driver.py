"""
GDS2 Driver - Low-level UI automation

This module provides low-level UI automation functions using pywinauto.
It wraps pywinauto operations and handles:
- Element finding
- Clicking with DPI awareness
- Screenshot comparison for action verification
- Window management

Pages and workflows should use this driver instead of pywinauto directly.
"""

import time
import logging
import subprocess
from pathlib import Path
from typing import Optional, Callable, Any, Tuple, Union, TYPE_CHECKING

if TYPE_CHECKING:
    from .locators import Locator

logger = logging.getLogger(__name__)


class GDS2Driver:
    """
    Low-level UI automation driver for GDS2.

    Handles:
    - Connection to GDS2 application
    - Element finding and interaction
    - DPI-aware clicking
    - Screenshot-based action verification
    """

    def __init__(self, config: Optional[dict] = None):
        """
        Initialize driver.

        Args:
            config: Configuration dictionary
        """
        self.config = config or {}

        # pywinauto objects
        self._app = None
        self._window = None
        self._is_connected = False

        # Settings
        self.backend = self.config.get("backend", "uia")
        self.window_title = self.config.get("window_title", "GDS 2")
        self.default_timeout = self.config.get("default_timeout", 30)

        # Auto-detect exe path
        self.exe_path = self.config.get("exe_path", self._detect_exe_path())

        # DPI scaling
        self._dpi_scale = None

        # Screenshot comparator (optional)
        self._screenshot_comparator = None
        self._init_screenshot_comparator()

    def _detect_exe_path(self) -> str:
        """Detect GDS2 installation path."""
        paths = [
            r"C:\Program Files (x86)\GDS 2\bin\GDS2Launcher.exe",
            r"C:\Program Files\GDS 2\bin\GDS2Launcher.exe",
            r"C:\Program Files (x86)\GM\GDS2\bin\GDS2Launcher.exe",
            r"C:\Program Files\GM\GDS2\bin\GDS2Launcher.exe",
        ]
        for path in paths:
            if Path(path).exists():
                return path
        return ""

    def _init_screenshot_comparator(self):
        """Initialize screenshot comparator if available."""
        try:
            from src.vision.screenshot_comparator import OpenCVScreenshotComparator
            screenshot_dir = self.config.get("screenshot_dir", "screenshots")
            self._screenshot_comparator = OpenCVScreenshotComparator(
                screenshot_dir=screenshot_dir,
            )
            logger.debug("Screenshot comparator initialized")
        except ImportError:
            logger.debug("Screenshot comparator not available")

    # ==================== Connection ====================

    def connect(self) -> bool:
        """
        Connect to running GDS2 or launch it.

        Returns:
            True if connected successfully
        """
        from pywinauto import Application

        logger.info("Connecting to GDS2...")

        # Try to connect to existing process
        try:
            self._app = Application(backend=self.backend).connect(
                title=self.window_title,
                timeout=5,
            )
            logger.info("Connected to existing GDS2 process")
            self._is_connected = True
            return True
        except Exception as e:
            logger.debug(f"No existing GDS2 process: {e}")

        # Launch GDS2
        if self.exe_path and Path(self.exe_path).exists():
            logger.info(f"Launching GDS2 from: {self.exe_path}")
            subprocess.Popen([self.exe_path], shell=True)

            # Wait for window to appear
            max_wait = 120
            start_time = time.time()
            while time.time() - start_time < max_wait:
                try:
                    self._app = Application(backend=self.backend).connect(
                        title=self.window_title,
                        timeout=5,
                    )
                    logger.info(f"Connected after {time.time() - start_time:.1f}s")
                    self._is_connected = True
                    return True
                except Exception:
                    logger.debug("Waiting for GDS2 window...")
                    time.sleep(3)

            raise TimeoutError(f"GDS2 window did not appear after {max_wait}s")
        else:
            raise FileNotFoundError(f"GDS2 exe not found: {self.exe_path}")

    def disconnect(self):
        """Disconnect from GDS2."""
        logger.info("Disconnecting from GDS2...")
        self._app = None
        self._window = None
        self._is_connected = False

    @property
    def is_connected(self) -> bool:
        """Check if connected to GDS2."""
        return self._is_connected and self._app is not None

    # ==================== Window Management ====================

    def get_window(self):
        """
        Get main GDS2 window.

        Returns:
            pywinauto window object
        """
        if not self._app:
            raise RuntimeError("Not connected to GDS2")
        return self._app.window(title=self.window_title)

    def is_window_ready(self) -> bool:
        """Check if window is ready for interaction."""
        if not self.is_connected:
            return False
        try:
            window = self.get_window()
            return window is not None and window.exists()
        except Exception:
            return False

    # ==================== Element Finding ====================

    def _resolve_locator(self, locator_or_title: Union[str, 'Locator'], control_type: str = None) -> Tuple[str, str]:
        """
        Resolve locator to (title, control_type) tuple.

        Args:
            locator_or_title: Either a Locator object or title string
            control_type: Control type (required if locator_or_title is string)

        Returns:
            (title, control_type) tuple
        """
        if hasattr(locator_or_title, 'as_tuple'):
            return locator_or_title.as_tuple()
        return (locator_or_title, control_type)

    def find_element(
        self,
        title_or_locator: Union[str, 'Locator'],
        control_type: str = None,
        parent=None,
        timeout: float = None,
    ):
        """
        Find element by title and control type, or by Locator.

        Args:
            title_or_locator: Element title string or Locator object
            control_type: Control type (Button, ListItem, etc.) - not needed if using Locator
            parent: Parent element (defaults to main window)
            timeout: Timeout in seconds

        Returns:
            pywinauto element
        """
        title, ctrl_type = self._resolve_locator(title_or_locator, control_type)
        timeout = timeout or self.default_timeout
        parent = parent or self.get_window()

        element = parent.child_window(
            title=title,
            control_type=ctrl_type,
        )

        if not element.exists(timeout=timeout):
            raise ElementNotFoundError(f"Element not found: {title} ({ctrl_type})")

        return element

    def find_element_by_title(self, title: str, parent=None, timeout: float = None):
        """Find element by title only."""
        timeout = timeout or self.default_timeout
        parent = parent or self.get_window()

        element = parent.child_window(title=title)
        if not element.exists(timeout=timeout):
            raise ElementNotFoundError(f"Element not found: {title}")

        return element

    def element_exists(
        self,
        title_or_locator: Union[str, 'Locator'],
        control_type: str = None,
        parent=None,
        timeout: float = 0.5,
    ) -> bool:
        """Check if element exists. Accepts Locator object or title string."""
        try:
            title, ctrl_type = self._resolve_locator(title_or_locator, control_type)
            parent = parent or self.get_window()
            if ctrl_type:
                element = parent.child_window(title=title, control_type=ctrl_type)
            else:
                element = parent.child_window(title=title)
            return element.exists(timeout=timeout)
        except Exception:
            return False

    # ==================== Element Interaction ====================

    def click_element(
        self,
        element,
        use_invoke: bool = True,
        wait_for_change: bool = True,
    ) -> bool:
        """
        Click an element.

        Args:
            element: pywinauto element
            use_invoke: Use invoke() instead of click_input()
            wait_for_change: Wait for UI change after click

        Returns:
            True if click succeeded
        """
        if wait_for_change and self._screenshot_comparator:
            # Use screenshot comparison to verify click worked
            def do_click():
                if use_invoke:
                    element.invoke()
                else:
                    self._click_with_dpi_adjustment(element)

            changed, elapsed = self._screenshot_comparator.capture_and_wait_for_change(
                action_callback=do_click,
                timeout=10,
                poll_interval=0.3,
                threshold=0.90,
            )
            logger.debug(f"Click: UI {'changed' if changed else 'unchanged'} after {elapsed:.2f}s")
            return True
        else:
            # Direct click
            if use_invoke:
                element.invoke()
            else:
                self._click_with_dpi_adjustment(element)
            return True

    def click_button(
        self,
        title_or_locator: Union[str, 'Locator'],
        parent=None,
        wait_for_change: bool = True,
    ) -> bool:
        """
        Click a button. Accepts Locator object or title string.

        Args:
            title_or_locator: Button title or Locator object
            parent: Parent element
            wait_for_change: Wait for UI change

        Returns:
            True if click succeeded
        """
        title, _ = self._resolve_locator(title_or_locator, "Button")
        element = self.find_element(title, "Button", parent)
        return self.click_element(element, wait_for_change=wait_for_change)

    def click_list_item(
        self,
        title_or_locator: Union[str, 'Locator'],
        parent=None,
        wait_for_change: bool = True,
    ) -> bool:
        """
        Click a list item. Accepts Locator object or title string.

        Args:
            title_or_locator: List item title or Locator object
            parent: Parent element
            wait_for_change: Wait for UI change

        Returns:
            True if click succeeded
        """
        title, _ = self._resolve_locator(title_or_locator, "ListItem")
        element = self.find_element(title, "ListItem", parent)
        return self.click_element(element, use_invoke=False, wait_for_change=wait_for_change)

    # ==================== DPI Handling ====================

    def _get_dpi_scale(self) -> float:
        """Get DPI scale factor."""
        if self._dpi_scale is not None:
            return self._dpi_scale

        try:
            import ctypes
            user32 = ctypes.windll.user32
            user32.SetProcessDPIAware()
            dc = user32.GetDC(0)
            dpi = ctypes.windll.gdi32.GetDeviceCaps(dc, 88)
            user32.ReleaseDC(0, dc)
            self._dpi_scale = dpi / 96.0
            logger.debug(f"DPI scale: {self._dpi_scale}")
        except Exception as e:
            logger.debug(f"Could not detect DPI: {e}")
            self._dpi_scale = 1.0

        return self._dpi_scale

    def _click_with_dpi_adjustment(self, element):
        """Click element with DPI adjustment."""
        import pyautogui

        rect = element.rectangle()
        center_x = rect.left + rect.width() // 2
        center_y = rect.top + rect.height() // 2

        dpi_scale = self._get_dpi_scale()
        if dpi_scale > 1.0:
            y_offset = int((dpi_scale - 1.0) * rect.top * 0.3)
            if y_offset < 20:
                y_offset = int((dpi_scale - 1.0) * 150)
            adjusted_y = center_y + y_offset
            logger.debug(f"DPI adjusted click: ({center_x}, {center_y}) -> ({center_x}, {adjusted_y})")
        else:
            adjusted_y = center_y

        pyautogui.click(center_x, adjusted_y)

    # ==================== Waiting ====================

    def wait_for_element(
        self,
        title_or_locator: Union[str, 'Locator'],
        control_type: str = None,
        timeout: float = None,
        parent=None,
    ) -> bool:
        """
        Wait for element to appear. Accepts Locator object or title string.

        Returns:
            True if element appeared
        """
        title, ctrl_type = self._resolve_locator(title_or_locator, control_type)
        timeout = timeout or self.default_timeout
        parent = parent or self.get_window()

        start = time.time()
        while time.time() - start < timeout:
            try:
                if ctrl_type:
                    elem = parent.child_window(title=title, control_type=ctrl_type)
                else:
                    elem = parent.child_window(title=title)
                if elem.exists(timeout=0.5):
                    return True
            except Exception:
                pass
            time.sleep(0.3)

        return False

    def wait_for_element_enabled(
        self,
        title_or_locator: Union[str, 'Locator'],
        control_type: str = None,
        timeout: float = None,
        parent=None,
    ) -> bool:
        """
        Wait for element to be enabled. Accepts Locator object or title string.

        Returns:
            True if element is enabled
        """
        title, ctrl_type = self._resolve_locator(title_or_locator, control_type)
        timeout = timeout or self.default_timeout
        parent = parent or self.get_window()

        start = time.time()
        while time.time() - start < timeout:
            try:
                elem = parent.child_window(title=title, control_type=ctrl_type)
                if elem.exists(timeout=0.5) and elem.is_enabled():
                    return True
            except Exception:
                pass
            time.sleep(1)

        return False

    def wait_for_ui_stable(self, timeout: float = 10, stable_duration: float = 0.5) -> bool:
        """
        Wait for UI to stop changing.

        Returns:
            True if UI stabilized
        """
        if self._screenshot_comparator:
            is_stable, elapsed = self._screenshot_comparator.wait_for_ui_stable(
                timeout=timeout,
                stable_duration=stable_duration,
                poll_interval=0.3,
                threshold=0.98,
            )
            return is_stable
        else:
            time.sleep(stable_duration)
            return True

    # ==================== Keyboard ====================

    def send_keys(self, keys: str):
        """Send keyboard input."""
        from pywinauto.keyboard import send_keys
        send_keys(keys)

    # ==================== Context Manager ====================

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()
        return False


class ElementNotFoundError(Exception):
    """Element not found exception."""
    pass
