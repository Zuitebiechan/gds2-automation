#!/usr/bin/env python
"""Select data category and enter Data Display page, then monitor with Agent."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.streaming import AgentNavigator, AgentDataCollector

nav = AgentNavigator()

print("=" * 60)
print("Step 1: Select 'Engine Data' (index 0)")
print("=" * 60)

result = nav.select_list_item(0, 0, double_click=True)
print(f"Result: {result.get('success')} - {result.get('message')}")

print("Waiting for Data Display page to load...")
time.sleep(5)

print("\n" + "=" * 60)
print("Step 2: Verify Data Display page")
print("=" * 60)

# Inspect the page
result = nav.inspect_controls(50)
if result.get('success'):
    controls = result.get('data', {}).get('controls', [])

    # Look for TableView with data
    for ctrl in controls:
        if ctrl.get('type') == 'TableView':
            print(f"TableView found: {ctrl.get('rowCount')} rows")
            print(f"Columns: {ctrl.get('columns')}")

print("\n" + "=" * 60)
print("Step 3: Monitor data with Java Agent (5 seconds)")
print("=" * 60)

collector = AgentDataCollector(interval_ms=500)

# Check agent data
status = collector.check_agent_available()
print(f"Agent available: {status.get('available')}")
print(f"Extraction count: {status.get('extraction_count')}")

if status.get('available'):
    snapshot_count = 0

    def on_snapshot(snap):
        global snapshot_count
        snapshot_count += 1
        if snapshot_count == 1:
            print(f"\nFirst snapshot: {len(snap.parameters)} params, {len(snap.dtcs)} DTCs")
            print("Sample parameters:")
            for p in snap.parameters[:5]:
                print(f"  {p['name']}: {p['value']} {p['unit']}")

    collector = AgentDataCollector(on_snapshot=on_snapshot, interval_ms=500)
    collector.start()

    time.sleep(5)

    collector.stop()

    print(f"\nCollected {snapshot_count} snapshots in 5 seconds")

    if collector.last_snapshot:
        snap = collector.last_snapshot
        print(f"\nFinal snapshot:")
        print(f"  Parameters: {len(snap.parameters)}")
        print(f"  DTCs: {len(snap.dtcs)}")
        print(f"  Extraction #: {snap.extraction_count}")
else:
    print("Agent not available!")

print("\n" + "=" * 60)
print("*** NAVIGATION TEST COMPLETE ***")
print("=" * 60)
