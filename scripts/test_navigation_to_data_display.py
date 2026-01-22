#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Test Navigation to Data Display Page

This script tests the full navigation from Main Menu to Data Display page
for Engine Control Module.

Approach:
- PyAutoGUI for clicking known buttons (Diagnostics, Enter, Create Report)
- Tesseract OCR for matching variable text (module names, Data Display option)

GDS2 must be open at Main Menu with hardware connected.

Usage:
    python scripts/test_navigation_to_data_display.py
"""

import sys
import time
import logging
import io
from pathlib import Path

# Fix console encoding for Windows
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

import pyautogui
import pytesseract
from PIL import ImageGrab
import cv2
import numpy as np

# Configure Tesseract path (Windows)
tesseract_path = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
pytesseract.pytesseract.tesseract_cmd = tesseract_path

# Path to button images
IMAGES_DIR = Path(r"C:\Users\shsww\projects\RPA_demo\images\buttons")

from src.core.driver import GDS2Driver
from src.core.locators import Loc
from src.pages import (
    MainMenuPage,
    DeviceExplorerPage,
    VehicleSelectionPage,
    DiagnosticsMenuPage,
)


def find_button_on_screen(button_name: str, confidence: float = 0.8) -> tuple:
    """
    Find a button on screen using image matching.

    Args:
        button_name: Name of button image file (without .png)
        confidence: Matching confidence threshold (0-1)

    Returns:
        (x, y) center coordinates if found, None otherwise
    """
    image_path = IMAGES_DIR / f"{button_name}.png"
    if not image_path.exists():
        logger.warning(f"Button image not found: {image_path}")
        return None

    try:
        location = pyautogui.locateOnScreen(str(image_path), confidence=confidence)
        if location:
            center = pyautogui.center(location)
            logger.info(f"Found button '{button_name}' at ({center.x}, {center.y})")
            return (center.x, center.y)
    except Exception as e:
        logger.debug(f"Error finding button '{button_name}': {e}")

    return None


def is_button_enabled(button_name: str, confidence: float = 0.9) -> bool:
    """
    Check if a button is enabled.

    Uses pywinauto for accurate state detection since image templates
    for enabled/disabled buttons are too similar.

    Args:
        button_name: Base name of button (e.g., 'home', 'clear_dtcs')
        confidence: Not used, kept for API compatibility

    Returns:
        True if button is enabled, False otherwise
    """
    # Map button names to pywinauto titles
    button_title_map = {
        'home': 'Home',
        'back': 'Back',
        'enter': 'Enter',
        'clear_dtcs': 'Clear DTCs',
        'create_report': 'Create Report',
        'diagnostics': 'Diagnostics',
    }

    title = button_title_map.get(button_name, button_name.replace('_', ' ').title())

    try:
        from src.core.driver import GDS2Driver
        # Use a quick connection to check button state
        driver = GDS2Driver()
        driver.connect()
        window = driver.get_window()

        btn = window.child_window(title=title, control_type='Button')
        if btn.exists(timeout=1):
            is_enabled = btn.is_enabled()
            logger.info(f"Button '{button_name}' ({title}) is {'ENABLED' if is_enabled else 'DISABLED'}")
            driver.disconnect()
            return is_enabled
        else:
            logger.warning(f"Button '{button_name}' ({title}) not found")
            driver.disconnect()
            return False
    except Exception as e:
        logger.error(f"Error checking button state: {e}")
        return False


def click_button_by_image(button_name: str, confidence: float = 0.8, timeout: float = 10) -> bool:
    """
    Find and click a button using image matching.

    Args:
        button_name: Name of button image file (without .png)
        confidence: Matching confidence threshold
        timeout: Maximum time to search

    Returns:
        True if found and clicked, False otherwise
    """
    start_time = time.time()

    while time.time() - start_time < timeout:
        coords = find_button_on_screen(button_name, confidence)
        if coords:
            x, y = coords
            logger.info(f"Clicking button '{button_name}' at ({x}, {y})")
            pyautogui.click(x, y)
            return True
        time.sleep(0.5)

    logger.warning(f"Could not find button '{button_name}' within {timeout}s")
    return False


def wait_for_button_enabled(button_name: str, timeout: float = 60) -> bool:
    """
    Wait for a button to become enabled.

    Args:
        button_name: Base name of button
        timeout: Maximum wait time

    Returns:
        True if button became enabled, False if timeout
    """
    logger.info(f"Waiting for button '{button_name}' to become enabled...")
    start_time = time.time()

    while time.time() - start_time < timeout:
        if is_button_enabled(button_name):
            logger.info(f"Button '{button_name}' is now enabled")
            return True
        time.sleep(1)

    logger.warning(f"Button '{button_name}' did not become enabled within {timeout}s")
    return False


def find_text_on_screen(target_text: str, confidence_threshold: float = 60) -> tuple:
    """
    Use Tesseract OCR to find text on screen and return its center coordinates.

    Args:
        target_text: Text to search for (case-insensitive)
        confidence_threshold: Minimum confidence for OCR match (0-100)

    Returns:
        (x, y) center coordinates if found, None otherwise
    """
    # Capture screen
    screenshot = ImageGrab.grab()
    screenshot_np = np.array(screenshot)

    # Convert to grayscale for better OCR
    gray = cv2.cvtColor(screenshot_np, cv2.COLOR_RGB2GRAY)

    # Get OCR data with bounding boxes
    data = pytesseract.image_to_data(gray, output_type=pytesseract.Output.DICT)

    target_lower = target_text.lower()
    target_words = target_lower.split()
    n_boxes = len(data['text'])

    # Build lines of text with their positions
    # Group text by approximate Y position (same line)
    lines = {}  # y_bucket -> [(x, y, w, h, text), ...]

    for i in range(n_boxes):
        text = data['text'][i].strip()
        conf = int(data['conf'][i]) if data['conf'][i] != '-1' else 0

        if conf >= confidence_threshold and text:
            x = data['left'][i]
            y = data['top'][i]
            w = data['width'][i]
            h = data['height'][i]

            # Bucket by Y position (group items on same line)
            y_bucket = y // 20 * 20  # Group by 20px bands

            if y_bucket not in lines:
                lines[y_bucket] = []
            lines[y_bucket].append((x, y, w, h, text.lower()))

    # Search each line for the target text
    for y_bucket, items in lines.items():
        # Sort items by X position (left to right)
        items.sort(key=lambda item: item[0])

        # Build the line text
        line_text = ' '.join(item[4] for item in items)

        # Check if all target words are in this line in order
        if all(word in line_text for word in target_words):
            # Find the specific items that match the target words
            matching_items = []
            for word in target_words:
                for item in items:
                    if word in item[4] and item not in matching_items:
                        matching_items.append(item)
                        break

            if matching_items:
                # Calculate bounding box of all matching items
                min_x = min(item[0] for item in matching_items)
                max_x = max(item[0] + item[2] for item in matching_items)
                min_y = min(item[1] for item in matching_items)
                max_y = max(item[1] + item[3] for item in matching_items)

                center_x = (min_x + max_x) // 2
                center_y = (min_y + max_y) // 2

                matched_text = ' '.join(item[4] for item in matching_items)
                logger.info(f"Found '{matched_text}' matching '{target_text}' at ({center_x}, {center_y})")
                return (center_x, center_y)

    return None


def find_and_click_text(target_text: str, timeout: float = 10, confidence_threshold: float = 60) -> bool:
    """
    Find text on screen using OCR and click on it.

    Args:
        target_text: Text to find and click
        timeout: Maximum time to search (seconds)
        confidence_threshold: Minimum OCR confidence (0-100)

    Returns:
        True if found and clicked, False otherwise
    """
    start_time = time.time()

    while time.time() - start_time < timeout:
        coords = find_text_on_screen(target_text, confidence_threshold)
        if coords:
            x, y = coords
            logger.info(f"Clicking on '{target_text}' at ({x}, {y})")
            pyautogui.click(x, y)
            return True
        time.sleep(0.5)

    logger.warning(f"Could not find '{target_text}' on screen within {timeout}s")
    return False


def main():
    """Run the navigation test."""
    print("=" * 60)
    print("GDS2 Navigation Test: Main Menu -> Data Display")
    print("Using PyAutoGUI + Tesseract OCR")
    print("=" * 60)
    print()

    vci_device = "SM2 USB"
    target_module = "Engine Control Module"
    print(f"VCI Device: {vci_device}")
    print(f"Target Module: {target_module}")
    print()

    try:
        with GDS2Driver() as driver:
            # Step 1: Verify Main Menu
            print("Step 1: Verifying Main Menu...")
            main_menu = MainMenuPage(driver)
            if not main_menu.is_displayed():
                print("  [ERROR] Not at Main Menu. Please navigate GDS2 to Main Menu.")
                return 1
            print("  [OK] At Main Menu")
            time.sleep(0.5)

            # Step 2: Click Diagnostics (PyAutoGUI via page object)
            print("Step 2: Clicking Diagnostics...")
            device_explorer = main_menu.click_diagnostics()
            print("  [OK] Device Explorer appeared")
            time.sleep(0.5)

            # Step 3: Select VCI device (PyAutoGUI via page object)
            print(f"Step 3: Selecting VCI device ({vci_device})...")
            vehicle_selection = device_explorer.select_device(vci_device)
            print("  [OK] Vehicle Selection page")
            time.sleep(0.5)

            # Step 4: Click Enter (PyAutoGUI via page object)
            print("Step 4: Clicking Enter...")
            diagnostics_menu = vehicle_selection.click_enter()
            if not diagnostics_menu.is_displayed():
                print("  [ERROR] Not at Diagnostics Menu")
                return 1
            print("  [OK] Diagnostics Menu")
            time.sleep(0.5)

            # Step 5: Select Module Diagnostics (click the list item directly - no Enter!)
            # Note: Clicking Enter would cause GDS2 to auto-select the first module
            print("Step 5: Selecting Module Diagnostics...")
            driver.click_list_item(Loc.DiagnosticsMenu.MODULE_DIAGNOSTICS)
            print("  [OK] Module Diagnostics selected")
            time.sleep(2)  # Wait for module list to appear

            # Step 6: Use OCR to find and click Engine Control Module directly
            # The module list items are directly clickable
            print(f"Step 6: Finding '{target_module}' using OCR...")

            if find_and_click_text(target_module, timeout=15, confidence_threshold=50):
                print(f"  [OK] Clicked on {target_module}")
                time.sleep(2)  # Wait for module options to load
            else:
                print(f"  [ERROR] Could not find '{target_module}' on screen")
                return 1

            # Step 7: Use OCR to find and click Data Display
            print("Step 7: Finding 'Data Display' using OCR...")

            if find_and_click_text("Data Display", timeout=10, confidence_threshold=50):
                print("  [OK] Clicked on Data Display")
                time.sleep(3)  # Wait for data selection to load
            else:
                print("  [ERROR] Could not find 'Data Display' on screen")
                return 1

            # Step 8: Select a data category (e.g., "Engine Data") and click Enter
            print("Step 8: Selecting data category 'Engine Data'...")
            if find_and_click_text("Engine Data", timeout=10, confidence_threshold=50):
                print("  [OK] Selected Engine Data")
                time.sleep(1)

                # Click Enter to confirm selection
                if driver.element_exists(Loc.Navigation.ENTER_BTN, timeout=2):
                    driver.click_button(Loc.Navigation.ENTER_BTN)
                    print("  [OK] Clicked Enter")
                    time.sleep(5)  # Wait for Data Display page to load with data
            else:
                print("  [WARN] Could not find 'Engine Data', trying to proceed...")

            # Step 9: Click Create Report (PyAutoGUI via driver)
            print("Step 9: Clicking Create Report...")
            time.sleep(2)  # Wait for data to populate

            if driver.element_exists(Loc.DataDisplay.CREATE_REPORT_BTN, timeout=10):
                driver.click_button(Loc.DataDisplay.CREATE_REPORT_BTN)
                print("  [OK] Clicked Create Report!")
                time.sleep(2)

                print()
                print("=" * 60)
                print("SUCCESS: Navigated to Data Display and created report!")
                print("=" * 60)
                return 0
            else:
                # Fallback: try OCR for Create Report button
                print("  [INFO] Trying OCR for Create Report...")
                if find_and_click_text("Create Report", timeout=10, confidence_threshold=50):
                    print("  [OK] Clicked Create Report (via OCR)!")
                    time.sleep(2)

                    print()
                    print("=" * 60)
                    print("SUCCESS: Navigated to Data Display and created report!")
                    print("=" * 60)
                    return 0
                else:
                    print("  [ERROR] Could not find 'Create Report' button")
                    return 1

    except Exception as e:
        logger.exception("Navigation test failed")
        print(f"\n[ERROR] {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
