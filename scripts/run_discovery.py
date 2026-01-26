#!/usr/bin/env python
"""
Discovery script for vehicle module and data mappings.

Run this script to discover and save module/data mappings for a vehicle.
Mappings are saved to the mappings/ directory as JSON files.

Usage:
    # Discover modules (run when at Module List page)
    python scripts/run_discovery.py --page modules --save

    # Discover data categories for a module (run when at Data List page)
    python scripts/run_discovery.py --page data --module "[K20] Engine Control Module" --save

    # Just print current page items (no save)
    python scripts/run_discovery.py --page modules
"""

import sys
import argparse
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.discovery import VehicleDiscovery, VehicleMapping


def discover_modules():
    """Discover all modules on Module List page."""
    print("Discovering modules on current page...")
    print("=" * 60)

    discovery = VehicleDiscovery()
    if not discovery.connect():
        print("ERROR: Could not connect to GDS2")
        return None

    items = discovery.get_list_items()
    print(f"Found {len(items)} modules:")
    print()

    modules = {}
    for item in items:
        status = "VISIBLE" if item["visible"] else "hidden"
        print(f"{item['index']:2}. {item['name']:50} {status}")
        modules[item['name']] = item['index']

    return modules


def discover_data_categories():
    """Discover all data categories on Data List page."""
    print("Discovering data categories on current page...")
    print("=" * 60)

    discovery = VehicleDiscovery()
    if not discovery.connect():
        print("ERROR: Could not connect to GDS2")
        return None

    items = discovery.get_list_items()
    print(f"Found {len(items)} data categories:")
    print()

    data_categories = {}
    for item in items:
        status = "VISIBLE" if item["visible"] else "hidden"
        print(f"{item['index']:2}. {item['name']:50} {status}")
        data_categories[item['name']] = item['index']

    return data_categories


def main():
    parser = argparse.ArgumentParser(description="Discover vehicle mappings")
    parser.add_argument(
        "--page",
        choices=["modules", "data"],
        required=True,
        help="Type of page to discover"
    )
    parser.add_argument(
        "--module",
        help="Module name (required for data discovery with --save)"
    )
    parser.add_argument(
        "--vehicle",
        default="current_vehicle",
        help="Vehicle identifier for saving mappings"
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Save discovered mappings to JSON file"
    )

    args = parser.parse_args()

    if args.page == "modules":
        modules = discover_modules()
        if modules and args.save:
            mapping = VehicleMapping()
            mapping.update_module_list(args.vehicle, modules)
            print(f"\nSaved to {mapping.get_mapping_path(args.vehicle)}")

    elif args.page == "data":
        data_categories = discover_data_categories()
        if data_categories and args.save:
            if not args.module:
                print("\nERROR: --module is required when saving data categories")
                return 1
            mapping = VehicleMapping()
            mapping.update_data_categories(args.vehicle, args.module, data_categories)
            print(f"\nSaved to {mapping.get_mapping_path(args.vehicle)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
