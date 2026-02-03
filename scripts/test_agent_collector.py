#!/usr/bin/env python
"""
Test script for AgentDataCollector.

Tests:
1. Agent availability check
2. JSON parsing
3. Snapshot creation
4. Change detection
"""

import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import time
import logging
from pathlib import Path
from src.streaming import AgentDataCollector, AgentSnapshot, DTCInfo

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def test_availability():
    """Test agent availability check."""
    print("\n" + "=" * 60)
    print("Test 1: Agent Availability Check")
    print("=" * 60)

    collector = AgentDataCollector()
    status = collector.check_agent_available()

    print(f"  Path: {status['path']}")
    print(f"  Exists: {status['exists']}")
    print(f"  Available: {status['available']}")
    if status['age_seconds'] is not None:
        print(f"  Age: {status['age_seconds']}s")
    if status.get('extraction_count'):
        print(f"  Extractions: {status['extraction_count']}")
    if status.get('version'):
        print(f"  Version: {status['version']}")

    return status['exists']


def test_json_parsing():
    """Test JSON parsing with sample data."""
    print("\n" + "=" * 60)
    print("Test 2: JSON Parsing")
    print("=" * 60)

    # Create test data matching enhanced Agent output
    test_data = {
        "version": "2.0",
        "timestamp": int(time.time() * 1000),
        "extractionCount": 42,
        "extractionDurationMs": 15,
        "pageContext": {
            "detected": True,
            "pageName": "Data Display",
            "windowTitle": "GDS 2 - Data Display",
            "moduleName": "Engine Control Module"
        },
        "tables": [
            {
                "tableType": "data_display",
                "columns": ["Module", "Parameter Name", "Value", "Unit"],
                "rowCount": 3,
                "rows": [
                    {"Module": "ECM", "Parameter Name": "Engine Speed", "Value": "750", "Unit": "RPM"},
                    {"Module": "ECM", "Parameter Name": "Coolant Temp", "Value": "85", "Unit": "°C"},
                    {"Module": "ECM", "Parameter Name": "Battery Voltage", "Value": "14.2", "Unit": "V"}
                ]
            },
            {
                "tableType": "dtc",
                "columns": ["Control Module", "DTC Type", "DTC", "Symptom Byte", "Description", "Symptom Description", "Status"],
                "rowCount": 2,
                "rows": [
                    {
                        "Control Module": "ECM",
                        "DTC Type": "Powertrain",
                        "DTC": "P0300",
                        "Symptom Byte": "00",
                        "Description": "Random/Multiple Cylinder Misfire",
                        "Symptom Description": "General Electrical Failure",
                        "Status": "History"
                    },
                    {
                        "Control Module": "TCM",
                        "DTC Type": "Powertrain",
                        "DTC": "P0700",
                        "Symptom Byte": "00",
                        "Description": "Transmission Control System Malfunction",
                        "Symptom Description": "General Electrical Failure",
                        "Status": "Current"
                    }
                ]
            }
        ],
        "tableCount": 2
    }

    # Parse using internal function
    from src.streaming.agent_data_collector import _parse_agent_json
    snapshot = _parse_agent_json(test_data)

    print(f"  Timestamp: {snapshot.timestamp}")
    print(f"  Extraction Count: {snapshot.extraction_count}")
    print(f"  Duration: {snapshot.extraction_duration_ms}ms")
    print(f"  Page Context: {snapshot.page_context}")
    print(f"  Parameters: {len(snapshot.parameters)}")
    for p in snapshot.parameters:
        print(f"    - {p['name']}: {p['value']} {p['unit']}")
    print(f"  DTCs: {len(snapshot.dtcs)}")
    for d in snapshot.dtcs:
        print(f"    - {d.code}: {d.description} ({d.status})")

    assert len(snapshot.parameters) == 3, f"Expected 3 params, got {len(snapshot.parameters)}"
    assert len(snapshot.dtcs) == 2, f"Expected 2 DTCs, got {len(snapshot.dtcs)}"
    print("\n  [OK] JSON parsing works correctly!")
    return True


def test_v1_fallback():
    """Test v1 JSON format fallback."""
    print("\n" + "=" * 60)
    print("Test 3: V1 Format Fallback")
    print("=" * 60)

    # V1 format (windows -> controls)
    test_data = {
        "timestamp": int(time.time() * 1000),
        "extractionCount": 10,
        "windows": [
            {
                "title": "GDS 2",
                "controls": [
                    {
                        "type": "TableView",
                        "columns": ["Module", "Parameter Name", "Value", "Unit"],
                        "rows": [
                            {"Module": "ECM", "Parameter Name": "Engine Speed", "Value": "800", "Unit": "RPM"}
                        ]
                    }
                ]
            }
        ]
    }

    from src.streaming.agent_data_collector import _parse_agent_json
    snapshot = _parse_agent_json(test_data)

    print(f"  Parameters: {len(snapshot.parameters)}")
    for p in snapshot.parameters:
        print(f"    - {p['name']}: {p['value']} {p['unit']}")

    assert len(snapshot.parameters) == 1, f"Expected 1 param, got {len(snapshot.parameters)}"
    print("\n  [OK] V1 fallback works correctly!")
    return True


def test_real_agent_data():
    """Test reading real Agent data if available."""
    print("\n" + "=" * 60)
    print("Test 4: Real Agent Data")
    print("=" * 60)

    json_path = Path.home() / 'gds2-data' / 'latest.json'

    if not json_path.exists():
        print(f"  [SKIP] Agent data file not found: {json_path}")
        print("  Start GDS2 with agent to generate data.")
        return True

    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            raw_data = json.load(f)

        from src.streaming.agent_data_collector import _parse_agent_json
        snapshot = _parse_agent_json(raw_data)

        print(f"  Version: {raw_data.get('version', '1.0')}")
        print(f"  Extraction Count: {snapshot.extraction_count}")
        print(f"  Duration: {snapshot.extraction_duration_ms}ms")
        print(f"  Parameters: {len(snapshot.parameters)}")
        if snapshot.parameters:
            for p in snapshot.parameters[:5]:
                print(f"    - {p['name']}: {p['value']} {p['unit']}")
            if len(snapshot.parameters) > 5:
                print(f"    ... and {len(snapshot.parameters) - 5} more")
        print(f"  DTCs: {len(snapshot.dtcs)}")
        if snapshot.dtcs:
            for d in snapshot.dtcs[:3]:
                print(f"    - {d.code}: {d.description} ({d.status})")

        print("\n  [OK] Real agent data parsed successfully!")
        return True

    except Exception as e:
        print(f"  [ERROR] {e}")
        return False


def test_streaming():
    """Test live streaming if agent available."""
    print("\n" + "=" * 60)
    print("Test 5: Live Streaming (5 seconds)")
    print("=" * 60)

    collector = AgentDataCollector()
    status = collector.check_agent_available()

    if not status['available']:
        print("  [SKIP] Agent not available for streaming test")
        return True

    snapshots = []
    changes = []

    def on_snapshot(snap):
        snapshots.append(snap)
        print(f"  Snapshot #{snap.extraction_count}: {len(snap.parameters)} params, {len(snap.dtcs)} DTCs")

    def on_change(change_list):
        changes.extend(change_list)
        for c in change_list:
            print(f"    CHANGE: {c['parameter']}: {c['old_value']} -> {c['new_value']}")

    collector = AgentDataCollector(
        on_snapshot=on_snapshot,
        on_param_change=on_change,
        interval_ms=200,
    )

    collector.start()
    time.sleep(5)
    collector.stop()

    print(f"\n  Collected {len(snapshots)} snapshots, {len(changes)} changes")
    print("  [OK] Streaming test complete!")
    return True


def main():
    print("\n" + "=" * 60)
    print("  GDS2 Agent Data Collector Tests")
    print("=" * 60)

    results = []

    results.append(("Availability Check", test_availability()))
    results.append(("JSON Parsing", test_json_parsing()))
    results.append(("V1 Fallback", test_v1_fallback()))
    results.append(("Real Agent Data", test_real_agent_data()))

    # Only run streaming test if agent is available
    collector = AgentDataCollector()
    if collector.check_agent_available()['available']:
        results.append(("Live Streaming", test_streaming()))

    print("\n" + "=" * 60)
    print("  Test Results Summary")
    print("=" * 60)

    all_passed = True
    for name, passed in results:
        status = "[PASS]" if passed else "[FAIL]"
        print(f"  {status} {name}")
        if not passed:
            all_passed = False

    print("\n" + "=" * 60)
    if all_passed:
        print("  All tests passed!")
    else:
        print("  Some tests failed!")
    print("=" * 60 + "\n")

    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
