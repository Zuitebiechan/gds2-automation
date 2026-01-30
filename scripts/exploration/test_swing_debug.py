#!/usr/bin/env python
"""Test Swing detection with debug info."""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.streaming import AgentNavigator

nav = AgentNavigator()

print("=" * 60)
print("Inspecting Swing controls (with debug)...")
print("=" * 60)
result = nav.inspect_swing()

if result.get('success'):
    data = result.get('data', {})

    # Print debug info
    print("\n--- Debug Info ---")
    for line in data.get('debug', []):
        print(line)

    # Print controls
    controls = data.get('controls', [])
    print(f"\n--- Found {len(controls)} Swing controls ---")
    for ctrl in controls:
        print(f"  {ctrl.get('type')}: {ctrl.get('text', ctrl.get('rowCount', ''))}")
else:
    print(f"Error: {result.get('message')}")
