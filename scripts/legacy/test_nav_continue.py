#!/usr/bin/env python
"""Continue navigation from Vehicle Selection to Module List."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.streaming import AgentNavigator

nav = AgentNavigator()

print("=" * 60)
print("Step 1: Click Enter button")
print("=" * 60)
result = nav.click_button("Enter")
print(f"Result: {result.get('success')} - {result.get('message')}")

time.sleep(3)

# Check for warning dialog
print("\n" + "=" * 60)
print("Step 2: Check for warning dialog (OK button)")
print("=" * 60)
buttons = nav.get_buttons()
button_texts = [b.get('text') for b in buttons]
print(f"Buttons: {button_texts}")

if "OK" in button_texts:
    print("Warning dialog detected, clicking OK...")
    result = nav.click_button("OK")
    print(f"Result: {result.get('success')} - {result.get('message')}")
    time.sleep(2)

# Check current page
print("\n" + "=" * 60)
print("Step 3: Check current page (looking for Module Diagnostics)")
print("=" * 60)
buttons = nav.get_buttons()
button_texts = [b.get('text') for b in buttons]
print(f"Buttons: {button_texts[:10]}...")

# Check for list
items = nav.get_list_items(0)
print(f"\nList items ({len(items)}):")
for i, item in enumerate(items[:10]):
    print(f"  [{i}] {item}")
if len(items) > 10:
    print(f"  ... and {len(items) - 10} more")

# If we see Module Diagnostics in list, select it
if items:
    for i, item in enumerate(items):
        if "Module Diagnostics" in item:
            print(f"\nFound 'Module Diagnostics' at index {i}, selecting...")
            result = nav.select_list_item(0, i, double_click=True)
            print(f"Result: {result.get('success')} - {result.get('message')}")
            time.sleep(3)
            break
