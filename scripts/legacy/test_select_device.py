#!/usr/bin/env python
"""Quick test: Click Select Device and inspect dialog."""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.streaming import AgentNavigator

nav = AgentNavigator()

print("Clicking 'Select Device'...")
result = nav.click_button("Select Device")
print(f"Result: {result.get('success')} - {result.get('message')}")

time.sleep(2)

print("\nInspecting device dialog...")
result = nav.inspect_controls(50)
if result.get('success'):
    controls = result.get('data', {}).get('controls', [])
    print(f"Found {len(controls)} controls:")
    for ctrl in controls:
        ctrl_type = ctrl.get('type')
        text = ctrl.get('text', '')
        item_count = ctrl.get('itemCount')
        preview = ctrl.get('preview')

        if ctrl_type == 'Button':
            print(f"  Button: '{text}'")
        elif ctrl_type == 'ListView':
            print(f"  ListView: {item_count} items")
            if preview:
                for i, item in enumerate(preview):
                    print(f"    [{i}] {item}")
        elif ctrl_type == 'Label' and text:
            print(f"  Label: '{text}'")
        elif ctrl_type == 'TableView':
            print(f"  TableView: {ctrl.get('rowCount')} rows")
