#!/usr/bin/env python
"""
GDS2 RPA Demo - New Architecture

Uses the refactored architecture with:
- core/driver.py - Low-level UI automation
- pages/ - Page Objects
- workflows/ - Business logic
- utils/ - Helpers

Usage:
    python scripts/run_demo_v2.py
"""

import sys
import json
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.core.driver import GDS2Driver
from src.workflows import ReadVehicleDTCWorkflow


def main():
    """Run the demo using new architecture."""
    print("=" * 60)
    print("GDS2 RPA Demo - New Architecture v2")
    print("=" * 60)
    print("Features:")
    print("  - Auto-launches GDS2 if not running")
    print("  - Auto-navigates to main menu")
    print("  - Reads all vehicle DTCs")
    print("  - Generates HTML report")
    print("=" * 60)
    print()

    # VCI device configuration
    vci_device = "SM2 USB"
    print(f"VCI Device: {vci_device}")
    print()

    # Use context manager for driver
    with GDS2Driver() as driver:
        # Create and execute workflow
        workflow = ReadVehicleDTCWorkflow(driver)
        result = workflow.execute(vci_device=vci_device)

        # Display results
        print()
        print("=" * 60)
        print("Demo Result")
        print("=" * 60)

        if result["success"]:
            print(f"[OK] Success!")
            print(f"    DTCs found: {len(result['dtc_list'])}")
            print()

            # Show DTCs
            if result["dtc_list"]:
                print("DTC List:")
                for dtc in result["dtc_list"]:
                    print(f"    [{dtc['code']}] {dtc['description']} ({dtc['status']})")
                print()

            # Show report path
            if result.get("report_path"):
                print(f"Report: {result['report_path']}")

            return 0
        else:
            print(f"[FAILED] {result.get('error', 'Unknown error')}")
            return 1


if __name__ == "__main__":
    sys.exit(main())
