#!/usr/bin/env python
"""Select Module Diagnostics and continue to Module List."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.streaming import AgentNavigator

nav = AgentNavigator()

print("=" * 60)
print("Step 1: Select 'Module Diagnostics' (index 0)")
print("=" * 60)
result = nav.select_list_item(0, 0, double_click=True)
print(f"Result: {result.get('success')} - {result.get('message')}")

time.sleep(3)

print("\n" + "=" * 60)
print("Step 2: Check current page (Module List)")
print("=" * 60)

# Get list items
items = nav.get_list_items(0)
print(f"List items ({len(items)}):")
for i, item in enumerate(items[:15]):
    print(f"  [{i}] {item}")
if len(items) > 15:
    print(f"  ... and {len(items) - 15} more")

# Check if these look like modules
if items:
    module_keywords = ["Control Module", "Module", "ECM", "TCM", "BCM"]
    is_module_list = any(any(kw in item for kw in module_keywords) for item in items)
    if is_module_list:
        print("\n*** SUCCESS: Arrived at Module List! ***")
    else:
        print("\n[INFO] List found but may not be Module List")
