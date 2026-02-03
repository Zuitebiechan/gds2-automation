#!/usr/bin/env python
"""
Full E2E test - Java Agent + Windows API (No PyAutoGUI)

Uses:
  - Java Agent for GDS2 main window (buttons, lists)
  - Windows API for Device Explorer (Win32 native dialog)
  - NO PyAutoGUI, OpenCV, or screen dependency
"""
import sys, os, time, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

from src.streaming import AgentNavigator, AgentDataCollector
from src.native import handle_device_explorer

nav = AgentNavigator()

def step(num, title):
    print(f"\n{'='*60}")
    print(f"Step {num}: {title}")
    print(f"{'='*60}")

def show_state():
    """Show current buttons and lists."""
    buttons = nav.get_buttons()
    btn_texts = [b.get('text') or '[icon]' for b in buttons]
    print(f"  Buttons: {btn_texts}")

    items = nav.get_list_items(0)
    if items:
        print(f"  List ({len(items)} items): {items[:5]}...")
    else:
        print(f"  List: (empty or not found)")

# Step 0: Agent check
step(0, "Check Agent")
if not nav.check_agent():
    print("  [FAIL] Agent not responding!")
    sys.exit(1)
print("  [OK] Agent connected")
show_state()

# Step 1: Click Diagnostics
step(1, "Click Diagnostics (Java Agent)")
result = nav.click_button("Diagnostics")
print(f"  Result: {result.get('success')} - {result.get('message')}")
time.sleep(3)
show_state()

# Step 2: Handle Device Explorer (Windows API - NO PyAutoGUI!)
step(2, "Handle Device Explorer (Windows API)")
print("  Using Windows API to control Win32 native dialog...")

# Windows API handles Device Explorer without any screen dependency
if handle_device_explorer(device_name="SM2 USB", timeout=5.0):
    print("  [OK] Device Explorer handled via Windows API")
else:
    print("  [INFO] No Device Explorer popup (or already handled)")

time.sleep(3)
show_state()

# Step 3: Click Enter
step(3, "Click Enter (Java Agent)")
# Wait for Enter to become enabled
for attempt in range(15):
    buttons = nav.get_buttons()
    enter_btn = next((b for b in buttons if b.get('text') == 'Enter'), None)
    if enter_btn:
        result = nav.click_button("Enter")
        print(f"  Result: {result.get('success')} - {result.get('message')}")
        time.sleep(3)
        break
    print(f"  Waiting for Enter button... (attempt {attempt+1})")
    time.sleep(1)

show_state()

# Step 4: Dismiss warning dialog
step(4, "Dismiss Warning Dialog")
for _ in range(3):
    buttons = nav.get_buttons()
    if any(b.get('text') == 'OK' for b in buttons):
        result = nav.click_button("OK")
        print(f"  Warning dialog dismissed: {result.get('success')}")
        time.sleep(2)
        break
    time.sleep(0.3)
else:
    print("  [INFO] No warning dialog")

# Wait for page to fully load
print("  Waiting for page to load...")
time.sleep(5)
show_state()

# Step 5: Select Module Diagnostics
step(5, "Select Module Diagnostics (Java Agent)")

# Retry getting list items with wait
items = []
for attempt in range(10):
    items = nav.get_list_items(0)
    if items:
        break
    print(f"  Waiting for list to load... (attempt {attempt+1})")
    time.sleep(1)
if not items:
    print("  [ERROR] No list items found! Inspecting page...")
    result = nav.inspect_controls(50)
    if result.get('success'):
        for ctrl in result.get('data', {}).get('controls', []):
            if ctrl.get('type') in ('Button', 'ListView', 'Label'):
                print(f"    {ctrl.get('type')}: {ctrl.get('text', ctrl.get('itemCount', ''))}")
    sys.exit(1)

for i, item in enumerate(items):
    if "Module Diagnostics" in item:
        result = nav.select_list_item(0, i, double_click=True)
        print(f"  Selected '{item}': {result.get('success')}")
        time.sleep(3)
        break
else:
    print(f"  [ERROR] Module Diagnostics not in: {items}")
    sys.exit(1)

show_state()

# Step 6: Select Engine Control Module
step(6, "Select [K20] Engine Control Module (Java Agent)")
items = nav.get_list_items(0)
print(f"  Found {len(items)} modules")
for i, item in enumerate(items):
    if "Engine Control Module" in item:
        result = nav.select_list_item(0, i, double_click=True)
        print(f"  Selected '{item}' at index {i}: {result.get('success')}")
        time.sleep(3)
        break

show_state()

# Step 7: Select Data Display
step(7, "Select Data Display (Java Agent)")
items = nav.get_list_items(0)
for i, item in enumerate(items):
    if "Data Display" in item:
        result = nav.select_list_item(0, i, double_click=True)
        print(f"  Selected '{item}': {result.get('success')}")
        time.sleep(3)
        break

# Dismiss warning if present
buttons = nav.get_buttons()
if any(b.get('text') == 'OK' for b in buttons):
    nav.click_button("OK")
    print("  Dismissed warning dialog")
    time.sleep(2)

show_state()

# Step 8: Select Engine Data
step(8, "Select Engine Data (Java Agent)")
items = nav.get_list_items(0)
print(f"  Found {len(items)} data categories")
for i, item in enumerate(items):
    if "Engine Data" == item.strip():
        result = nav.select_list_item(0, i, double_click=True)
        print(f"  Selected '{item}' at index {i}: {result.get('success')}")
        time.sleep(5)
        break

# Step 9: Monitor with Agent
step(9, "Agent Data Monitoring (5s)")
collector = AgentDataCollector(interval_ms=500)
status = collector.check_agent_available()
print(f"  Agent available: {status.get('available')}")

snapshot_count = [0]
def on_snap(snap):
    snapshot_count[0] += 1
    if snapshot_count[0] == 1:
        print(f"  First snapshot: {len(snap.parameters)} params, {len(snap.dtcs)} DTCs")
        for p in snap.parameters[:3]:
            print(f"    {p['name']}: {p['value']} {p['unit']}")

collector2 = AgentDataCollector(on_snapshot=on_snap, interval_ms=500)
collector2.start()
time.sleep(5)
collector2.stop()

snap = collector2.last_snapshot
if snap:
    print(f"\n  Final: {len(snap.parameters)} params, {len(snap.dtcs)} DTCs, extraction #{snap.extraction_count}")

# Summary
print("\n" + "=" * 60)
print("  E2E TEST RESULT")
print("=" * 60)
passed = snap and len(snap.parameters) > 0
print(f"  Navigation: {'PASS' if passed else 'FAIL'}")
print(f"  Parameters: {len(snap.parameters) if snap else 0}")
print(f"  DTCs: {len(snap.dtcs) if snap else 0}")
print(f"  Snapshots: {snapshot_count[0]}")
print("=" * 60)
if passed:
    print("\n  *** ALL TESTS PASSED ***")
else:
    print("\n  *** TESTS FAILED ***")
    sys.exit(1)
