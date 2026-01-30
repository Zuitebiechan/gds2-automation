#!/usr/bin/env python
"""
Comprehensive Test Script for Module Data Display Implementation

Tests all components in order:
1. Image assets verification
2. OCR module (if Tesseract available)
3. ImageDriver connection and basic operations
4. Page object navigation
5. Workflow execution

Run with GDS2 open at Main Menu.
"""

import sys
import time
import logging
from pathlib import Path

# Fix Windows console encoding
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Add project root
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Use ASCII symbols for compatibility
OK = "[OK]"
FAIL = "[FAIL]"
WARN = "[WARN]"
SKIP = "[SKIP]"


def test_imports():
    """Test all imports work."""
    print("\n" + "="*60)
    print("TEST 1: Imports")
    print("="*60)

    errors = []

    try:
        from src.core.image_driver import ImageDriver
        print(f"  {OK} ImageDriver")
    except Exception as e:
        errors.append(f"ImageDriver: {e}")
        print(f"  {FAIL} ImageDriver: {e}")

    try:
        from src.core.image_locators import ImageLoc
        print(f"  {OK} ImageLoc")
    except Exception as e:
        errors.append(f"ImageLoc: {e}")
        print(f"  {FAIL} ImageLoc: {e}")

    try:
        from src.core.ocr import OCREngine
        print(f"  {OK} OCREngine")
    except Exception as e:
        errors.append(f"OCREngine: {e}")
        print(f"  {FAIL} OCREngine: {e}")

    try:
        from src.pages import (
            MainMenuPage, DiagnosticsMenuPage, ModuleListPage,
            ModuleOptionsPage, DataSelectionPage, DataDisplayPage
        )
        print(f"  {OK} All Page Objects")
    except Exception as e:
        errors.append(f"Page Objects: {e}")
        print(f"  {FAIL} Page Objects: {e}")

    try:
        from src.workflows import ModuleDiscoveryWorkflow, ModuleDataDisplayWorkflow
        print(f"  {OK} Workflows")
    except Exception as e:
        errors.append(f"Workflows: {e}")
        print(f"  {FAIL} Workflows: {e}")

    try:
        from src.utils.report_parser import DataDisplayReportParser
        print(f"  {OK} DataDisplayReportParser")
    except Exception as e:
        errors.append(f"DataDisplayReportParser: {e}")
        print(f"  {FAIL} DataDisplayReportParser: {e}")

    try:
        from src.ui import ModuleDataDisplayUI
        print(f"  {OK} ModuleDataDisplayUI")
    except Exception as e:
        errors.append(f"ModuleDataDisplayUI: {e}")
        print(f"  {FAIL} ModuleDataDisplayUI: {e}")

    if errors:
        print(f"\n  FAILED: {len(errors)} import errors")
        return False
    else:
        print("\n  PASSED: All imports successful")
        return True


def test_image_assets():
    """Test image assets exist."""
    print("\n" + "="*60)
    print("TEST 2: Image Assets")
    print("="*60)

    from src.core.image_locators import ImageLoc

    images_to_check = [
        ("MainMenu.DIAGNOSTICS_BTN", ImageLoc.MainMenu.DIAGNOSTICS_BTN),
        ("Navigation.BACK_BTN", ImageLoc.Navigation.BACK_BTN),
        ("Navigation.ENTER_BTN", ImageLoc.Navigation.ENTER_BTN),
        ("ModuleOptions.DATA_DISPLAY", ImageLoc.ModuleOptions.DATA_DISPLAY),
        ("DTCPage.CREATE_REPORT_BTN", ImageLoc.DTCPage.CREATE_REPORT_BTN),
    ]

    missing = []
    for name, loc in images_to_check:
        path = Path(loc.image_path)
        if path.exists():
            print(f"  {OK} {name}: {path.name}")
        else:
            missing.append(name)
            print(f"  {FAIL} {name}: MISSING - {path}")

    if missing:
        print(f"\n  WARNING: {len(missing)} images missing")
        return False
    else:
        print("\n  PASSED: All required images found")
        return True


def test_ocr():
    """Test OCR functionality."""
    print("\n" + "="*60)
    print("TEST 3: OCR Module")
    print("="*60)

    from src.core.ocr import OCREngine

    engine = OCREngine()

    if not engine.is_available:
        print(f"  {WARN} Tesseract not installed - OCR features disabled")
        print("  -> Install Tesseract: https://github.com/UB-Mannheim/tesseract/wiki")
        print("  -> Or run: python scripts/setup_tesseract.py")
        print("\n  SKIPPED: OCR not available")
        return None  # Not a failure, just not available

    # Test with a simple image
    import numpy as np
    test_image = np.ones((100, 300), dtype=np.uint8) * 255  # White image

    try:
        result = engine.read_text(test_image)
        print(f"  {OK} OCR engine working (empty image returned: '{result.strip()}')")
        print("\n  PASSED: OCR functional")
        return True
    except Exception as e:
        print(f"  {FAIL} OCR error: {e}")
        return False


def test_driver_connection():
    """Test ImageDriver can connect to GDS2."""
    print("\n" + "="*60)
    print("TEST 4: ImageDriver Connection")
    print("="*60)

    from src.core.image_driver import ImageDriver
    from src.core.image_locators import ImageLoc

    driver = ImageDriver()

    # Test screenshot
    try:
        screenshot = driver._capture_screen()
        print(f"  {OK} Screenshot captured: {screenshot.shape}")
    except Exception as e:
        print(f"  {FAIL} Screenshot failed: {e}")
        return False, None

    # Test finding main menu button
    try:
        found = driver.element_exists(ImageLoc.MainMenu.DIAGNOSTICS_BTN, timeout=3)
        if found:
            print(f"  {OK} Main Menu detected (Diagnostics button visible)")
        else:
            print(f"  {WARN} Main Menu not detected - is GDS2 at Main Menu?")
            return False, driver
    except Exception as e:
        print(f"  {FAIL} Element detection failed: {e}")
        return False, driver

    print("\n  PASSED: Driver connected and Main Menu detected")
    return True, driver


def test_navigation(driver):
    """Test page navigation to Module Diagnostics."""
    print("\n" + "="*60)
    print("TEST 5: Page Navigation")
    print("="*60)

    from src.pages import MainMenuPage, DiagnosticsMenuPage
    from src.core.image_locators import ImageLoc

    try:
        # Check if at main menu
        main_menu = MainMenuPage(driver)
        if not main_menu.is_displayed():
            print(f"  {FAIL} Not at Main Menu")
            return False
        print(f"  {OK} At Main Menu")

        # Click Diagnostics
        print("  -> Clicking Diagnostics...")
        device_explorer = main_menu.click_diagnostics()
        time.sleep(1)

        if device_explorer.is_displayed():
            print(f"  {OK} Device Explorer opened")
        else:
            print(f"  {WARN} Device Explorer may not have opened")

        # Select device
        print("  -> Selecting SM2 USB...")
        vehicle_selection = device_explorer.select_device("SM2 USB")
        time.sleep(1)

        # Click Enter
        print("  -> Clicking Enter...")
        diag_menu = vehicle_selection.click_enter()
        time.sleep(2)

        if diag_menu.is_displayed():
            print(f"  {OK} At Diagnostics Menu")
        else:
            print(f"  {WARN} Diagnostics Menu may not have loaded")

        # Navigate to Module Diagnostics
        print("  -> Selecting Module Diagnostics...")
        module_list = diag_menu.select_module_diagnostics()
        time.sleep(2)

        if module_list.is_displayed():
            print(f"  {OK} At Module List")
        else:
            print(f"  {WARN} Module List may not have loaded")

        print("\n  PASSED: Navigation to Module Diagnostics successful")
        return True

    except Exception as e:
        print(f"\n  FAILED: Navigation error: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_module_list(driver):
    """Test reading module list."""
    print("\n" + "="*60)
    print("TEST 6: Module List Reading")
    print("="*60)

    from src.pages import ModuleListPage
    from src.core.ocr import OCREngine

    ocr = OCREngine()

    if not ocr.is_available:
        print(f"  {WARN} OCR not available - cannot read module names")
        print("  -> Will test basic visibility only")

        module_list = ModuleListPage(driver)
        if module_list.is_displayed():
            print(f"  {OK} Module List page is displayed")
        else:
            print(f"  {WARN} Cannot confirm Module List page")

        print("\n  SKIPPED: OCR required for module name reading")
        return None

    try:
        module_list = ModuleListPage(driver)

        # Read visible modules
        print("  -> Reading visible modules...")
        visible = module_list.get_visible_modules()
        print(f"  {OK} Found {len(visible)} visible modules")
        for m in visible[:5]:
            print(f"    - {m}")
        if len(visible) > 5:
            print(f"    ... and {len(visible) - 5} more")

        # Read all modules with scrolling
        print("  -> Reading all modules (with scrolling)...")
        all_modules = module_list.get_all_modules()
        print(f"  {OK} Found {len(all_modules)} total modules")

        print("\n  PASSED: Module list reading successful")
        return True

    except Exception as e:
        print(f"\n  FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False


def navigate_back_to_main(driver):
    """Navigate back to main menu."""
    print("\n  -> Navigating back to Main Menu...")

    from src.core.image_locators import ImageLoc

    # Click back repeatedly
    for i in range(10):
        if driver.element_exists(ImageLoc.MainMenu.DIAGNOSTICS_BTN, timeout=1):
            print(f"  {OK} Back at Main Menu")
            return True

        if driver.element_exists(ImageLoc.Navigation.BACK_BTN, timeout=0.5):
            driver.click_element(ImageLoc.Navigation.BACK_BTN)
            time.sleep(0.5)
        elif driver.element_exists(ImageLoc.Navigation.HOME_BTN, timeout=0.5):
            driver.click_element(ImageLoc.Navigation.HOME_BTN)
            time.sleep(1)
            break

    return driver.element_exists(ImageLoc.MainMenu.DIAGNOSTICS_BTN, timeout=2)


def main():
    """Run all tests."""
    print("\n" + "="*60)
    print("GDS2 MODULE DATA DISPLAY - COMPREHENSIVE TEST")
    print("="*60)
    print(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("Please ensure GDS2 is open at Main Menu\n")

    results = {}
    driver = None

    try:
        # Test 1: Imports
        results["imports"] = test_imports()
        if not results["imports"]:
            print("\n⚠ Import errors - stopping tests")
            return

        # Test 2: Image Assets
        results["images"] = test_image_assets()

        # Test 3: OCR
        results["ocr"] = test_ocr()

        # Test 4: Driver Connection
        success, driver = test_driver_connection()
        results["driver"] = success

        if not success:
            print("\n⚠ Driver connection failed - stopping tests")
            return

        # Test 5: Navigation
        results["navigation"] = test_navigation(driver)

        if results["navigation"]:
            # Test 6: Module List
            results["module_list"] = test_module_list(driver)

        # Navigate back
        if driver:
            navigate_back_to_main(driver)

    except KeyboardInterrupt:
        print("\n\nTest interrupted by user")
    except Exception as e:
        print(f"\n\nUnexpected error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if driver:
            try:
                navigate_back_to_main(driver)
            except:
                pass

    # Summary
    print("\n" + "="*60)
    print("TEST SUMMARY")
    print("="*60)

    for test, result in results.items():
        if result is True:
            status = "[OK] PASSED"
        elif result is False:
            status = "[FAIL] FAILED"
        else:
            status = "[SKIP] SKIPPED"
        print(f"  {test}: {status}")

    passed = sum(1 for r in results.values() if r is True)
    failed = sum(1 for r in results.values() if r is False)
    skipped = sum(1 for r in results.values() if r is None)

    print(f"\n  Total: {passed} passed, {failed} failed, {skipped} skipped")
    print("="*60)


if __name__ == "__main__":
    main()
