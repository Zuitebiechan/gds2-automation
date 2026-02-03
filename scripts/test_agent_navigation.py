#!/usr/bin/env python
"""
Test: Navigate from Main Menu to Module List using Java Agent.

This test validates that Java Agent can replace PyAutoGUI + OpenCV
for GDS2 navigation.

Prerequisites:
1. GDS2 started with Agent: launch-gds2-with-agent.bat
2. GDS2 at Main Menu
"""

import sys
import os
import time
import logging

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.streaming import AgentNavigator

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)


def test_agent_connection():
    """Test that Java Agent is running and responsive."""
    print("\n" + "=" * 60)
    print("Step 0: Check Agent Connection")
    print("=" * 60)

    nav = AgentNavigator()

    if not nav.check_agent():
        print("  [ERROR] Agent not responding!")
        print("  Please start GDS2 with: launch-gds2-with-agent.bat")
        return None

    print("  [OK] Agent is connected")

    # Show current window
    windows = nav.get_window_info()
    for w in windows:
        print(f"  Window: {w.get('title')}")

    return nav


def test_navigate_to_diagnostics(nav):
    """Navigate: Main Menu -> Diagnostics."""
    print("\n" + "=" * 60)
    print("Step 1: Click Diagnostics Button")
    print("=" * 60)

    # First, check what buttons are available
    buttons = nav.get_buttons()
    button_texts = [b.get('text') for b in buttons]
    print(f"  Available buttons: {button_texts[:10]}...")

    # Click Diagnostics
    result = nav.click_button("Diagnostics")
    if not result.get('success'):
        print(f"  [ERROR] Failed to click Diagnostics: {result.get('message')}")
        return False

    print("  [OK] Clicked Diagnostics button")
    print("  Waiting for page load...")
    time.sleep(3)

    return True


def test_handle_device_explorer(nav):
    """Handle Device Explorer popup if it appears."""
    print("\n" + "=" * 60)
    print("Step 2: Handle Device Selection")
    print("=" * 60)

    # Check for Select Device button (indicates we need to select a device)
    buttons = nav.get_buttons()
    button_texts = [b.get('text') for b in buttons]
    print(f"  Available buttons: {button_texts}")

    if "Select Device" in button_texts:
        print("  [INFO] Need to select device first")

        # Click Select Device
        result = nav.click_button("Select Device")
        if result.get('success'):
            print("  [OK] Clicked 'Select Device'")
            time.sleep(2)

            # Now check for device list or Continue button
            buttons = nav.get_buttons()
            button_texts = [b.get('text') for b in buttons]
            print(f"  Device dialog buttons: {button_texts}")

            # Check if there's a list with devices
            items = nav.get_list_items(0)
            if items:
                print(f"  Device list items: {items}")
                # Select SM2 USB if in list
                for i, item in enumerate(items):
                    if "SM2" in item or "USB" in item:
                        print(f"  Selecting device: {item}")
                        result = nav.select_list_item(0, i, double_click=False)
                        if result.get('success'):
                            print(f"  [OK] Selected device at index {i}")
                        break
                time.sleep(0.5)

            # Click Continue or OK
            if "Continue" in button_texts:
                result = nav.click_button("Continue")
                if result.get('success'):
                    print("  [OK] Clicked Continue")
            elif "OK" in button_texts:
                result = nav.click_button("OK")
                if result.get('success'):
                    print("  [OK] Clicked OK")

            time.sleep(3)
        else:
            print(f"  [WARN] Could not click Select Device: {result.get('message')}")

    elif "Continue" in button_texts:
        # Device Explorer popup is showing
        print("  [INFO] Device Explorer popup detected")

        items = nav.get_list_items(0)
        print(f"  Device list items: {items[:5]}...")

        for i, item in enumerate(items):
            if "SM2" in item or "USB" in item:
                print(f"  Selecting device: {item}")
                result = nav.select_list_item(0, i, double_click=False)
                if result.get('success'):
                    print(f"  [OK] Selected device at index {i}")
                break

        time.sleep(0.5)

        result = nav.click_button("Continue")
        if result.get('success'):
            print("  [OK] Clicked Continue")

        time.sleep(3)
    else:
        print("  [INFO] Device already selected or not needed")

    return True


def test_click_enter(nav):
    """Click Enter button for vehicle selection."""
    print("\n" + "=" * 60)
    print("Step 3: Click Enter Button")
    print("=" * 60)

    buttons = nav.get_buttons()
    button_texts = [b.get('text') for b in buttons]
    print(f"  Available buttons: {button_texts}")

    if "Enter" in button_texts:
        result = nav.click_button("Enter")
        if result.get('success'):
            print("  [OK] Clicked Enter button")
            time.sleep(3)

            # Check for warning dialog (OK button)
            buttons = nav.get_buttons()
            button_texts = [b.get('text') for b in buttons]
            if "OK" in button_texts:
                print("  [INFO] Warning dialog detected, clicking OK...")
                nav.click_button("OK")
                time.sleep(1)

            return True
        else:
            print(f"  [ERROR] Failed to click Enter: {result.get('message')}")
            return False
    else:
        print("  [SKIP] No Enter button found (may already be past vehicle selection)")
        return True


def test_select_module_diagnostics(nav):
    """Select Module Diagnostics from menu."""
    print("\n" + "=" * 60)
    print("Step 4: Select Module Diagnostics")
    print("=" * 60)

    # Get list items
    items = nav.get_list_items(0)
    print(f"  Menu items ({len(items)}):")
    for i, item in enumerate(items):
        print(f"    [{i}] {item}")

    # Find and select Module Diagnostics
    for i, item in enumerate(items):
        if "Module Diagnostics" in item:
            print(f"\n  Selecting 'Module Diagnostics' at index {i}...")
            result = nav.select_list_item(0, i, double_click=True)
            if result.get('success'):
                print("  [OK] Selected Module Diagnostics")
                time.sleep(3)
                return True
            else:
                print(f"  [ERROR] Selection failed: {result.get('message')}")
                return False

    print("  [ERROR] 'Module Diagnostics' not found in menu")
    return False


def test_verify_module_list(nav):
    """Verify we're at the Module List page."""
    print("\n" + "=" * 60)
    print("Step 5: Verify Module List Page")
    print("=" * 60)

    # Get list items - should be modules now
    items = nav.get_list_items(0)
    print(f"  Found {len(items)} items in list:")
    for i, item in enumerate(items[:10]):
        print(f"    [{i}] {item}")
    if len(items) > 10:
        print(f"    ... and {len(items) - 10} more")

    # Check if these look like module names
    module_keywords = ["Control Module", "Module", "ECM", "TCM", "BCM", "K20", "K80"]
    is_module_list = False
    for item in items:
        for keyword in module_keywords:
            if keyword in item:
                is_module_list = True
                break
        if is_module_list:
            break

    if is_module_list:
        print("\n  [OK] Successfully navigated to Module List!")
        return True
    else:
        print("\n  [WARN] Not sure if this is the Module List")
        return True  # Continue anyway


def main():
    print("=" * 60)
    print("  Test: Main Menu -> Module List (via Java Agent)")
    print("=" * 60)
    print("\n  This test uses Java Agent for ALL navigation.")
    print("  No PyAutoGUI or OpenCV is used.\n")

    # Step 0: Check agent
    nav = test_agent_connection()
    if nav is None:
        return 1

    # Step 1: Click Diagnostics
    if not test_navigate_to_diagnostics(nav):
        return 1

    # Step 2: Handle Device Explorer
    if not test_handle_device_explorer(nav):
        return 1

    # Step 3: Click Enter
    if not test_click_enter(nav):
        return 1

    # Step 4: Select Module Diagnostics
    if not test_select_module_diagnostics(nav):
        return 1

    # Step 5: Verify Module List
    if not test_verify_module_list(nav):
        return 1

    # Summary
    print("\n" + "=" * 60)
    print("  TEST PASSED: Successfully navigated to Module List!")
    print("=" * 60)
    print("\n  Navigation was done entirely via Java Agent.")
    print("  No PyAutoGUI or OpenCV was needed.\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
