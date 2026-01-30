#!/usr/bin/env python
"""Navigate from Module List to Data Display page."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.streaming import AgentNavigator

nav = AgentNavigator()

print("=" * 60)
print("Step 1: Select '[K20] Engine Control Module' (index 5)")
print("=" * 60)

# First verify we're at Module List
items = nav.get_list_items(0)
print(f"Current list has {len(items)} items")

# Find Engine Control Module
ecm_index = None
for i, item in enumerate(items):
    if "Engine Control Module" in item or "K20" in item:
        ecm_index = i
        print(f"Found Engine Control Module at index {i}: {item}")
        break

if ecm_index is None:
    print("[ERROR] Engine Control Module not found!")
    sys.exit(1)

result = nav.select_list_item(0, ecm_index, double_click=True)
print(f"Result: {result.get('success')} - {result.get('message')}")

time.sleep(3)

print("\n" + "=" * 60)
print("Step 2: Check Module Submenu (looking for 'Data Display')")
print("=" * 60)

items = nav.get_list_items(0)
print(f"Submenu items ({len(items)}):")
for i, item in enumerate(items):
    print(f"  [{i}] {item}")

# Find Data Display
data_display_index = None
for i, item in enumerate(items):
    if "Data Display" in item:
        data_display_index = i
        print(f"\nFound 'Data Display' at index {i}")
        break

if data_display_index is None:
    print("[ERROR] Data Display not found in submenu!")
    sys.exit(1)

print("\n" + "=" * 60)
print("Step 3: Select 'Data Display'")
print("=" * 60)

result = nav.select_list_item(0, data_display_index, double_click=True)
print(f"Result: {result.get('success')} - {result.get('message')}")

time.sleep(3)

# Check for warning dialog
buttons = nav.get_buttons()
button_texts = [b.get('text') for b in buttons]
if "OK" in button_texts:
    print("\nWarning dialog detected, clicking OK...")
    nav.click_button("OK")
    time.sleep(2)

print("\n" + "=" * 60)
print("Step 4: Check Data Category List")
print("=" * 60)

items = nav.get_list_items(0)
print(f"Data categories ({len(items)}):")
for i, item in enumerate(items[:20]):
    print(f"  [{i}] {item}")
if len(items) > 20:
    print(f"  ... and {len(items) - 20} more")

if items:
    print("\n*** SUCCESS: Arrived at Data Category List! ***")
    print(f"Total {len(items)} data categories available")
