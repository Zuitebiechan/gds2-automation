#!/usr/bin/env python
"""Test Swing controls detection for Device Explorer."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.streaming import AgentNavigator

nav = AgentNavigator()

print("=" * 60)
print("Checking all windows (JavaFX + Swing)...")
print("=" * 60)
windows = nav.get_window_info()
for w in windows:
    print(f"  [{w.get('type')}] {w.get('title')} (focused={w.get('focused')})")

print("\n" + "=" * 60)
print("Inspecting Swing controls...")
print("=" * 60)
result = nav.inspect_swing()
if result.get('success'):
    controls = result.get('data', {}).get('controls', [])
    print(f"Found {len(controls)} Swing controls:")
    for ctrl in controls:
        ctrl_type = ctrl.get('type')
        text = ctrl.get('text', '')
        if ctrl_type == 'JButton':
            enabled = ctrl.get('enabled', True)
            state = "" if enabled else " (disabled)"
            print(f"  JButton: '{text}'{state}")
        elif ctrl_type == 'JLabel' and text:
            print(f"  JLabel: '{text}'")
        elif ctrl_type == 'JTable':
            print(f"  JTable: {ctrl.get('rowCount')} rows, preview={ctrl.get('preview')}")
else:
    print(f"Error: {result.get('message')}")

print("\n" + "=" * 60)
print("Getting Swing table data...")
print("=" * 60)
result = nav.get_swing_table(0)
if result.get('success'):
    data = result.get('data', {})
    print(f"Columns: {data.get('columns')}")
    print(f"Rows ({data.get('rowCount')}):")
    for i, row in enumerate(data.get('rows', [])[:5]):
        print(f"  [{i}] {row}")
else:
    print(f"Error: {result.get('message')}")
