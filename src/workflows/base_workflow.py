"""
Base Workflow

Abstract base class for workflows using PyAutoGUI+OpenCV approach.

Architecture:
- PyAutoGUI+OpenCV: for clicking buttons and fixed list items (template matching)
- pywinauto: for discovering list items and checking button state
- Keyboard navigation: for selecting items in lists (DOWN + ENTER)
"""

import logging
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

import pyautogui
import cv2
import numpy as np
from PIL import ImageGrab

logger = logging.getLogger(__name__)

# Image directories (relative to this file's location)
IMAGES_DIR = Path(__file__).parent.parent.parent / "images"
BUTTONS_DIR = IMAGES_DIR / "buttons"
LIST_ITEMS_DIR = IMAGES_DIR / "list_items"
DEVICES_DIR = IMAGES_DIR / "devices"


class BaseWorkflow(ABC):
    """
    Base class for GDS2 workflows using PyAutoGUI+OpenCV.

    This approach provides reliable UI automation by using
    visual element detection and keyboard navigation.
    """

    def __init__(self):
        """Initialize workflow."""
        self._vlm_finder = None  # Lazy initialization

    @property
    @abstractmethod
    def name(self) -> str:
        """Workflow name."""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Workflow description."""
        pass

    @abstractmethod
    def execute(self, **kwargs) -> Dict[str, Any]:
        """
        Execute the workflow.

        Returns:
            Dictionary with results
        """
        pass

    # =========================================================================
    # PyAutoGUI + OpenCV Methods (for buttons and fixed list items)
    # =========================================================================

    def find_button(
        self,
        button_name: str,
        confidence: float = 0.8,
        region: Optional[Tuple[int, int, int, int]] = None,
    ) -> Optional[Tuple[int, int]]:
        """
        Find a button on screen using grayscale template matching.

        Args:
            button_name: Name of button image file (without .png)
            confidence: Matching confidence threshold (0-1)
            region: Optional (left, top, width, height) to search in specific area

        Returns:
            (x, y) center coordinates if found, None otherwise
        """
        image_path = BUTTONS_DIR / f"{button_name}.png"
        if not image_path.exists():
            logger.warning(f"Button image not found: {image_path}")
            return None

        return self._find_template(image_path, confidence, region)

    def find_list_item(
        self,
        item_name: str,
        confidence: float = 0.9,
    ) -> Optional[Tuple[int, int]]:
        """
        Find a list item on screen using grayscale template matching.

        Args:
            item_name: Name of list item image file (without .png)
            confidence: Matching confidence threshold (0-1)

        Returns:
            (x, y) center coordinates if found, None otherwise
        """
        image_path = LIST_ITEMS_DIR / f"{item_name}.png"
        if not image_path.exists():
            logger.warning(f"List item image not found: {image_path}")
            return None

        return self._find_template(image_path, confidence)

    def find_device(
        self,
        device_name: str,
        confidence: float = 0.85,
    ) -> Optional[Tuple[int, int]]:
        """
        Find a device on screen using grayscale template matching.

        Args:
            device_name: Name of device image file (without .png)
            confidence: Matching confidence threshold (0-1)

        Returns:
            (x, y) center coordinates if found, None otherwise
        """
        image_path = DEVICES_DIR / f"{device_name}.png"
        if not image_path.exists():
            logger.warning(f"Device image not found: {image_path}")
            return None

        return self._find_template(image_path, confidence)

    def click_device(
        self,
        device_name: str,
        confidence: float = 0.85,
        timeout: float = 5,
    ) -> bool:
        """
        Find and click a device using template matching.

        Args:
            device_name: Name of device image file (without .png)
            confidence: Matching confidence threshold
            timeout: Maximum time to search

        Returns:
            True if clicked, False otherwise
        """
        start_time = time.time()
        while time.time() - start_time < timeout:
            coords = self.find_device(device_name, confidence)
            if coords:
                logger.info(f"Clicking device '{device_name}' at {coords}")
                pyautogui.click(coords[0], coords[1])
                return True
            time.sleep(0.5)

        logger.warning(f"Could not find device '{device_name}' within {timeout}s")
        return False

    def _find_template(
        self,
        image_path: Path,
        confidence: float,
        region: Optional[Tuple[int, int, int, int]] = None,
    ) -> Optional[Tuple[int, int]]:
        """
        Find template on screen using OpenCV grayscale matching.

        Args:
            image_path: Path to template image
            confidence: Matching confidence threshold
            region: Optional search region

        Returns:
            (x, y) center coordinates if found, None otherwise
        """
        try:
            # Load template in grayscale
            template = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
            if template is None:
                logger.warning(f"Could not load template: {image_path}")
                return None

            # Capture screen and convert to grayscale
            screenshot = ImageGrab.grab()
            if region:
                screenshot = screenshot.crop(
                    (region[0], region[1], region[0] + region[2], region[1] + region[3])
                )
            screenshot_gray = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2GRAY)

            # Perform template matching
            result = cv2.matchTemplate(screenshot_gray, template, cv2.TM_CCOEFF_NORMED)
            min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)

            if max_val >= confidence:
                h, w = template.shape
                center_x = max_loc[0] + w // 2
                center_y = max_loc[1] + h // 2
                # Adjust for region offset if used
                if region:
                    center_x += region[0]
                    center_y += region[1]
                logger.info(
                    f"Found template '{image_path.stem}' at ({center_x}, {center_y}) "
                    f"with confidence {max_val:.3f}"
                )
                return (center_x, center_y)
            else:
                logger.debug(
                    f"Template '{image_path.stem}' best match {max_val:.3f} "
                    f"below threshold {confidence}"
                )
        except Exception as e:
            logger.debug(f"Error finding template '{image_path.stem}': {e}")

        return None

    def click_button(
        self,
        button_name: str,
        confidence: float = 0.8,
        timeout: float = 10,
        region: Optional[Tuple[int, int, int, int]] = None,
    ) -> bool:
        """
        Find and click a button using template matching.

        Args:
            button_name: Name of button image file (without .png)
            confidence: Matching confidence threshold
            timeout: Maximum time to search
            region: Optional search region

        Returns:
            True if found and clicked, False otherwise
        """
        start_time = time.time()

        while time.time() - start_time < timeout:
            coords = self.find_button(button_name, confidence, region)
            if coords:
                x, y = coords
                logger.info(f"Clicking button '{button_name}' at ({x}, {y})")
                pyautogui.click(x, y)
                return True
            time.sleep(0.5)

        logger.warning(f"Could not find button '{button_name}' within {timeout}s")
        return False

    def click_list_item(
        self,
        item_name: str,
        confidence: float = 0.9,
        timeout: float = 10,
    ) -> bool:
        """
        Find and click a list item using template matching.

        Args:
            item_name: Name of list item image file (without .png)
            confidence: Matching confidence threshold
            timeout: Maximum time to search

        Returns:
            True if found and clicked, False otherwise
        """
        start_time = time.time()

        while time.time() - start_time < timeout:
            coords = self.find_list_item(item_name, confidence)
            if coords:
                x, y = coords
                logger.info(f"Clicking list item '{item_name}' at ({x}, {y})")
                pyautogui.click(x, y)
                return True
            time.sleep(0.5)

        logger.warning(f"Could not find list item '{item_name}' within {timeout}s")
        return False

    # =========================================================================
    # pywinauto Methods (ONLY for button state checking)
    # =========================================================================

    def is_button_enabled(self, button_title: str) -> bool:
        """
        Check if a button is enabled using pywinauto.

        Args:
            button_title: Button title text (e.g., "Create Report", "Clear DTCs")

        Returns:
            True if button is enabled, False otherwise
        """
        try:
            from src.core.driver import GDS2Driver

            driver = GDS2Driver()
            driver.connect()
            window = driver.get_window()

            btn = window.child_window(title=button_title, control_type="Button")
            if btn.exists(timeout=1):
                is_enabled = btn.is_enabled()
                logger.info(
                    f"Button '{button_title}' is "
                    f"{'ENABLED' if is_enabled else 'DISABLED'}"
                )
                driver.disconnect()
                return is_enabled
            else:
                logger.warning(f"Button '{button_title}' not found")
                driver.disconnect()
                return False
        except Exception as e:
            logger.error(f"Error checking button state: {e}")
            return False

    def wait_for_button_enabled(
        self,
        button_title: str,
        timeout: float = 60,
    ) -> bool:
        """
        Wait for a button to become enabled.

        Args:
            button_title: Button title text
            timeout: Maximum wait time in seconds

        Returns:
            True if button became enabled, False if timeout
        """
        logger.info(f"Waiting for button '{button_title}' to become enabled...")
        start_time = time.time()

        while time.time() - start_time < timeout:
            if self.is_button_enabled(button_title):
                logger.info(f"Button '{button_title}' is now enabled")
                return True
            time.sleep(1)

        logger.warning(
            f"Button '{button_title}' did not become enabled within {timeout}s"
        )
        return False

    # =========================================================================
    # VLM Methods (for variable text like module names)
    # =========================================================================

    @property
    def vlm_finder(self):
        """Lazy-initialized VLM finder."""
        if self._vlm_finder is None:
            from src.vision.vlm_finder import VLMFinder

            logger.info("Initializing VLM finder (Claude)...")
            self._vlm_finder = VLMFinder(provider="claude", apply_dpi_transform=True)
        return self._vlm_finder

    def find_text_vlm(
        self,
        target_text: str,
        apply_offset: bool = True,
    ) -> Optional[Tuple[int, int]]:
        """
        Find text on screen using VLM (Vision Language Model).

        Args:
            target_text: Text to find (e.g., "Engine Control Module")
            apply_offset: Whether to apply Y offset (page-dependent)

        Returns:
            (x, y) coordinates if found, None otherwise
        """
        coords = self.vlm_finder.find_text_position(target_text, apply_offset=apply_offset)
        if coords:
            logger.info(f"VLM found '{target_text}' at ({coords[0]}, {coords[1]})")
        else:
            logger.warning(f"VLM could not find '{target_text}'")
        return coords

    def click_text_vlm(
        self,
        target_text: str,
        apply_offset: bool = True,
    ) -> bool:
        """
        Find and click text using VLM.

        Args:
            target_text: Text to find and click
            apply_offset: Whether to apply Y offset (page-dependent)

        Returns:
            True if found and clicked, False otherwise
        """
        coords = self.find_text_vlm(target_text, apply_offset)
        if coords:
            x, y = coords
            logger.info(f"Clicking '{target_text}' at ({x}, {y})")
            pyautogui.click(x, y)
            return True
        return False

    def click_text_vlm_with_scroll(
        self,
        target_text: str,
        apply_offset: bool = True,
        max_scrolls: int = 5,
        scroll_amount: int = -3,
        scroll_pause: float = 0.5,
    ) -> bool:
        """
        Find and click text using VLM, scrolling if needed.

        If the text is not visible, scrolls down and tries again.

        Args:
            target_text: Text to find and click
            apply_offset: Whether to apply Y offset (page-dependent)
            max_scrolls: Maximum number of scroll attempts
            scroll_amount: Scroll amount (negative = down, positive = up)
            scroll_pause: Pause between scrolls in seconds

        Returns:
            True if found and clicked, False otherwise
        """
        # First try without scrolling
        if self.click_text_vlm(target_text, apply_offset):
            return True

        # Try scrolling down to find the text
        logger.info(f"'{target_text}' not visible, scrolling to find it...")

        for i in range(max_scrolls):
            logger.info(f"  Scroll attempt {i + 1}/{max_scrolls}")
            self.scroll(scroll_amount)
            self.wait(scroll_pause)

            if self.click_text_vlm(target_text, apply_offset):
                return True

        logger.warning(f"Could not find '{target_text}' after {max_scrolls} scroll attempts")
        return False

    # =========================================================================
    # Scrolling Methods
    # =========================================================================

    def scroll(self, amount: int = -3):
        """
        Scroll the current view.

        Args:
            amount: Scroll amount (negative = down, positive = up)
        """
        pyautogui.scroll(amount)
        logger.debug(f"Scrolled by {amount}")

    def scroll_down(self, amount: int = 3):
        """Scroll down by the specified amount."""
        self.scroll(-abs(amount))

    def scroll_up(self, amount: int = 3):
        """Scroll up by the specified amount."""
        self.scroll(abs(amount))

    def scroll_to_top(self, scroll_amount: int = 5, max_scrolls: int = 10):
        """
        Scroll to the top of the current list/view.

        Args:
            scroll_amount: Amount to scroll each time
            max_scrolls: Maximum scroll attempts
        """
        logger.info("Scrolling to top of list...")
        for _ in range(max_scrolls):
            self.scroll_up(scroll_amount)
            self.wait(0.2)
        self.wait(0.5)

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def wait(self, seconds: float):
        """Wait for specified seconds."""
        time.sleep(seconds)

    def press_key(self, key: str):
        """Press a keyboard key."""
        pyautogui.press(key)

    def _error_result(self, message: str) -> Dict[str, Any]:
        """Create error result dictionary."""
        return {
            "success": False,
            "error": message,
        }
