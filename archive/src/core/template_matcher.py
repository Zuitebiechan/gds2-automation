"""
Multi-scale Template Matching for PyAutoGUI+OpenCV

Handles different screen resolutions and DPI scaling by matching templates
at multiple scale levels.
"""

import cv2
import numpy as np
import pyautogui
import time
import logging
from pathlib import Path
from PIL import ImageGrab
from typing import Optional, Tuple, List
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Base images directory (relative to this file's location)
IMAGES_DIR = Path(__file__).parent.parent.parent / "images"
BUTTONS_DIR = IMAGES_DIR / "buttons"
LIST_ITEMS_DIR = IMAGES_DIR / "list_items"
DEVICES_DIR = IMAGES_DIR / "devices"
PAGES_DIR = IMAGES_DIR / "pages"


@dataclass
class MatchResult:
    """Result of template matching."""
    found: bool
    confidence: float
    x: int
    y: int
    scale: float
    width: int
    height: int


def get_template_path(name: str, category: str = "auto") -> Optional[Path]:
    """
    Find template image path.

    Args:
        name: Template name without extension
        category: "buttons", "list_items", "devices", "pages", or "auto" (searches all)

    Returns:
        Path to template or None if not found
    """
    if category == "auto":
        # Search in order: buttons, list_items, devices, pages
        for dir_path in [BUTTONS_DIR, LIST_ITEMS_DIR, DEVICES_DIR, PAGES_DIR]:
            path = dir_path / f"{name}.png"
            if path.exists():
                return path
        return None
    else:
        dir_map = {
            "buttons": BUTTONS_DIR,
            "list_items": LIST_ITEMS_DIR,
            "devices": DEVICES_DIR,
            "pages": PAGES_DIR,
        }
        dir_path = dir_map.get(category)
        if dir_path:
            path = dir_path / f"{name}.png"
            return path if path.exists() else None
    return None


def multi_scale_match(
    screenshot_gray: np.ndarray,
    template: np.ndarray,
    scales: List[float] = None,
    min_confidence: float = 0.8
) -> MatchResult:
    """
    Perform multi-scale template matching.

    Args:
        screenshot_gray: Grayscale screenshot
        template: Grayscale template image
        scales: List of scale factors to try (default: 0.5 to 2.0)
        min_confidence: Minimum confidence threshold

    Returns:
        MatchResult with best match info
    """
    if scales is None:
        # Common scale factors for different DPI settings
        # 100% = 1.0, 125% = 1.25, 150% = 1.5, 175% = 1.75, 200% = 2.0
        scales = [0.5, 0.6, 0.7, 0.75, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0]

    best_match = MatchResult(
        found=False, confidence=0, x=0, y=0, scale=1.0, width=0, height=0
    )

    orig_h, orig_w = template.shape[:2]

    for scale in scales:
        # Resize template
        new_w = int(orig_w * scale)
        new_h = int(orig_h * scale)

        # Skip if template becomes too small or larger than screenshot
        if new_w < 10 or new_h < 10:
            continue
        if new_w > screenshot_gray.shape[1] or new_h > screenshot_gray.shape[0]:
            continue

        resized_template = cv2.resize(template, (new_w, new_h), interpolation=cv2.INTER_AREA)

        # Match
        result = cv2.matchTemplate(screenshot_gray, resized_template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)

        if max_val > best_match.confidence:
            best_match = MatchResult(
                found=max_val >= min_confidence,
                confidence=max_val,
                x=max_loc[0] + new_w // 2,
                y=max_loc[1] + new_h // 2,
                scale=scale,
                width=new_w,
                height=new_h
            )

    return best_match


def find_template(
    name: str,
    confidence: float = 0.8,
    category: str = "auto",
    scales: List[float] = None
) -> MatchResult:
    """
    Find a template on screen using multi-scale matching.

    Args:
        name: Template name without extension
        confidence: Minimum confidence threshold
        category: Template category (auto, buttons, list_items, devices, pages)
        scales: Custom scale factors to try

    Returns:
        MatchResult with match info
    """
    template_path = get_template_path(name, category)
    if not template_path:
        logger.error(f"Template not found: {name}")
        return MatchResult(found=False, confidence=0, x=0, y=0, scale=1.0, width=0, height=0)

    template = cv2.imread(str(template_path), cv2.IMREAD_GRAYSCALE)
    if template is None:
        logger.error(f"Failed to load template: {template_path}")
        return MatchResult(found=False, confidence=0, x=0, y=0, scale=1.0, width=0, height=0)

    screenshot = ImageGrab.grab()
    screenshot_gray = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2GRAY)

    return multi_scale_match(screenshot_gray, template, scales, confidence)


def find_and_click(
    name: str,
    confidence: float = 0.8,
    timeout: float = 10.0,
    category: str = "auto",
    scales: List[float] = None
) -> bool:
    """
    Find template and click on it using multi-scale matching.

    Args:
        name: Template name without extension
        confidence: Minimum confidence threshold
        timeout: Maximum time to wait for match
        category: Template category
        scales: Custom scale factors

    Returns:
        True if found and clicked, False otherwise
    """
    template_path = get_template_path(name, category)
    if not template_path:
        logger.error(f"Template not found: {name}")
        return False

    template = cv2.imread(str(template_path), cv2.IMREAD_GRAYSCALE)
    if template is None:
        logger.error(f"Failed to load template: {template_path}")
        return False

    start_time = time.time()
    while time.time() - start_time < timeout:
        screenshot = ImageGrab.grab()
        screenshot_gray = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2GRAY)

        result = multi_scale_match(screenshot_gray, template, scales, confidence)

        if result.found:
            logger.debug(f"Found {name} at ({result.x}, {result.y}) with confidence {result.confidence:.3f}, scale {result.scale}")
            pyautogui.click(result.x, result.y)
            return True

        time.sleep(0.5)

    logger.warning(f"Template {name} not found within {timeout}s (best confidence: {result.confidence:.3f})")
    return False


def find_button(
    name: str,
    confidence: float = 0.8,
    category: str = "auto",
    scales: List[float] = None
) -> bool:
    """
    Check if a template exists on screen (without clicking).

    Args:
        name: Template name without extension
        confidence: Minimum confidence threshold
        category: Template category
        scales: Custom scale factors

    Returns:
        True if found, False otherwise
    """
    result = find_template(name, confidence, category, scales)
    return result.found


def click_device(
    device_name: str,
    confidence: float = 0.85,
    timeout: float = 10.0
) -> bool:
    """
    Click on a device in Device Explorer using multi-scale matching.
    Tries highlighted version first, then normal version.

    Args:
        device_name: Device name (e.g., "sm2_usb")
        confidence: Minimum confidence threshold
        timeout: Maximum time to wait

    Returns:
        True if found and clicked, False otherwise
    """
    # Try highlighted version first
    for variant in [f"{device_name}_highlight", device_name]:
        template_path = get_template_path(variant, "devices")
        if not template_path:
            continue

        template = cv2.imread(str(template_path), cv2.IMREAD_GRAYSCALE)
        if template is None:
            continue

        start_time = time.time()
        while time.time() - start_time < timeout:
            screenshot = ImageGrab.grab()
            screenshot_gray = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2GRAY)

            result = multi_scale_match(screenshot_gray, template, confidence=confidence)

            if result.found:
                logger.info(f"Found device {variant} at ({result.x}, {result.y}) with confidence {result.confidence:.3f}, scale {result.scale}")
                pyautogui.click(result.x, result.y)
                return True

            time.sleep(0.3)

    logger.warning(f"Device {device_name} not found")
    return False


def focus_gds2_window() -> bool:
    """
    Focus the GDS2 window.

    Returns:
        True if successful, False otherwise
    """
    try:
        from pywinauto import Application
        app = Application(backend="uia").connect(title_re=".*GDS 2.*")
        window = app.top_window()
        window.set_focus()
        time.sleep(0.3)
        return True
    except Exception as e:
        logger.warning(f"Could not focus GDS2: {e}")
        return False


# Convenience function for common workflow
def wait_and_click(
    name: str,
    confidence: float = 0.8,
    timeout: float = 10.0,
    pre_delay: float = 0,
    post_delay: float = 0.5
) -> bool:
    """
    Wait for template, click it, with optional delays.

    Args:
        name: Template name
        confidence: Minimum confidence
        timeout: Maximum wait time
        pre_delay: Delay before searching
        post_delay: Delay after clicking

    Returns:
        True if successful, False otherwise
    """
    if pre_delay > 0:
        time.sleep(pre_delay)

    success = find_and_click(name, confidence, timeout)

    if success and post_delay > 0:
        time.sleep(post_delay)

    return success
