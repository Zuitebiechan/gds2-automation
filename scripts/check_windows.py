#!/usr/bin/env python
"""Check all windows."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.streaming import AgentNavigator

nav = AgentNavigator()
windows = nav.get_window_info()
print(f"Found {len(windows)} windows:")
for w in windows:
    print(f"  Title: {w.get('title')}")
    print(f"  Focused: {w.get('focused')}")
    print(f"  Size: {w.get('width')}x{w.get('height')}")
    print()

# Also get buttons
buttons = nav.get_buttons()
print(f"Buttons ({len(buttons)}):")
for b in buttons:
    print(f"  - {b.get('text') or '[no text]'}")
