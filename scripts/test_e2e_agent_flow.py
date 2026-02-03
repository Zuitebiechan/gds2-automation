#!/usr/bin/env python
"""
End-to-End Test: Main Menu -> Data Display -> Agent Monitoring

Tests the full workflow:
1. Navigate from Main Menu to Data Display (via Java Agent)
2. Use Java Agent to monitor data
3. Verify parameters and DTCs are captured
"""

import sys
import os
import time
import json

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.workflows import ReadDataDisplayAgentWorkflow
from src.streaming import AgentDataCollector, AgentSnapshot


def test_agent_available():
    """Test that Java Agent is running and producing data."""
    print("\n" + "=" * 60)
    print("Step 0: Check Agent Availability")
    print("=" * 60)

    collector = AgentDataCollector()
    status = collector.check_agent_available()

    print(f"  Path: {status['path']}")
    print(f"  Exists: {status['exists']}")
    print(f"  Available: {status['available']}")
    if status.get('age_seconds') is not None:
        print(f"  Age: {status['age_seconds']}s")
    if status.get('version'):
        print(f"  Version: {status['version']}")

    if not status['available']:
        print("\n  [ERROR] Agent not available!")
        print("  Please start GDS2 with: launch-gds2-with-agent.bat")
        return False

    print("  [OK] Agent is available")
    return True


def test_navigation():
    """Test navigation from Main Menu to Data Display."""
    print("\n" + "=" * 60)
    print("Step 1: Navigate to Data Display")
    print("=" * 60)

    print("  Starting workflow...")
    print("  Target: Engine Control Module -> Engine Data")

    workflow = ReadDataDisplayAgentWorkflow(vehicle_id="current_vehicle")

    try:
        result = workflow.execute(
            vci_device="SM2 USB",
            target_module="[K20] Engine Control Module",
            data_category="Engine Data",
        )

        if result.get("success"):
            print(f"  [OK] Navigation successful!")
            print(f"  Now at Data Display page - ready for Agent monitoring")
            return True
        else:
            print(f"  [ERROR] Navigation failed: {result.get('error', 'Unknown')}")
            return False

    except Exception as e:
        print(f"  [ERROR] Exception during navigation: {e}")
        return False


def test_agent_monitoring(duration_seconds=10):
    """Test Agent-based data monitoring."""
    print("\n" + "=" * 60)
    print(f"Step 2: Agent Monitoring ({duration_seconds}s)")
    print("=" * 60)

    snapshots = []
    param_changes = []
    dtc_changes = []

    def on_snapshot(snap: AgentSnapshot):
        snapshots.append(snap)
        if len(snapshots) == 1:
            print(f"  First snapshot: {len(snap.parameters)} params, {len(snap.dtcs)} DTCs")
        elif len(snapshots) % 50 == 0:
            print(f"  Snapshot #{len(snapshots)}: {len(snap.parameters)} params")

    def on_param_change(changes):
        param_changes.extend(changes)
        for c in changes:
            print(f"  CHANGE: {c['parameter']}: {c['old_value']} -> {c['new_value']} {c.get('unit', '')}")

    def on_dtc_change(added, removed):
        if added:
            dtc_changes.extend([('added', d) for d in added])
            for d in added:
                print(f"  DTC ADDED: {d.code} - {d.description}")
        if removed:
            dtc_changes.extend([('removed', d) for d in removed])
            for d in removed:
                print(f"  DTC REMOVED: {d.code}")

    collector = AgentDataCollector(
        on_snapshot=on_snapshot,
        on_param_change=on_param_change,
        on_dtc_change=on_dtc_change,
        interval_ms=100,  # 100ms high-frequency
    )

    print(f"  Starting Agent monitoring (100ms interval)...")
    collector.start()

    # Wait for monitoring period
    start_time = time.time()
    while time.time() - start_time < duration_seconds:
        time.sleep(0.5)
        elapsed = time.time() - start_time
        if int(elapsed) % 2 == 0 and int(elapsed) > 0:
            # Print progress every 2 seconds
            pass

    collector.stop()

    print(f"\n  --- Monitoring Results ---")
    print(f"  Total snapshots: {len(snapshots)}")
    print(f"  Parameter changes: {len(param_changes)}")
    print(f"  DTC changes: {len(dtc_changes)}")

    if snapshots:
        last = snapshots[-1]
        print(f"\n  Final snapshot:")
        print(f"    Extraction #{last.extraction_count}")
        print(f"    Duration: {last.extraction_duration_ms}ms")
        print(f"    Parameters: {len(last.parameters)}")
        print(f"    DTCs: {len(last.dtcs)}")

        if last.parameters:
            print(f"\n  Sample parameters:")
            for p in last.parameters[:5]:
                print(f"    {p['name']}: {p['value']} {p['unit']}")

        if last.dtcs:
            print(f"\n  Sample DTCs:")
            for d in last.dtcs[:3]:
                print(f"    {d.code}: {d.description} ({d.status})")

    return len(snapshots) > 0 and (len(snapshots[-1].parameters) > 0 or len(snapshots[-1].dtcs) > 0)


def main():
    print("=" * 60)
    print("  E2E Test: Main Menu -> Data Display -> Agent Monitor")
    print("=" * 60)

    # Step 0: Check agent
    if not test_agent_available():
        print("\n[FAILED] Agent not available. Aborting.")
        return 1

    # Note: User should ensure GDS2 is at Main Menu before running
    print("\n" + "-" * 60)
    print("NOTE: GDS2 should be at MAIN MENU before running this test")
    print("-" * 60)

    # Step 1: Navigate to Data Display
    nav_success = test_navigation()
    if not nav_success:
        print("\n[FAILED] Navigation failed. Please check GDS2 state.")
        # Continue anyway to test agent monitoring

    # Wait for Data Display to fully load
    print("\n  Waiting 3s for Data Display to load...")
    time.sleep(3)

    # Step 2: Agent Monitoring
    monitor_success = test_agent_monitoring(duration_seconds=10)

    # Summary
    print("\n" + "=" * 60)
    print("  Test Results")
    print("=" * 60)
    print(f"  Agent Available: [PASS]")
    print(f"  Navigation: {'[PASS]' if nav_success else '[FAIL]'}")
    print(f"  Agent Monitoring: {'[PASS]' if monitor_success else '[FAIL]'}")
    print("=" * 60)

    if nav_success and monitor_success:
        print("\n  *** ALL TESTS PASSED ***")
        return 0
    else:
        print("\n  *** SOME TESTS FAILED ***")
        return 1


if __name__ == "__main__":
    sys.exit(main())
