#!/usr/bin/env python
"""
GDS2 RPA Demo Main Entry

Provides multiple run modes:
    python main.py web                                      - Start Web UI (recommended)
    python main.py demo                                     - Run demo workflow (default: Engine Data)
    python main.py demo --module "..." --data "..."         - Run with custom module and data
    python main.py inspect                                  - Inspect GDS2 UI structure
    python main.py test-connection                          - Test GDS2 connection
    python main.py discover                                 - Run discovery utility
"""

import sys
import io
import argparse
import logging

# Fix console encoding for Windows
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')


def run_web(args):
    """Start the Web UI server."""
    print("=" * 60)
    print("  GDS2 Automation Web UI")
    print("=" * 60)
    print()
    print(f"  Open in browser: http://localhost:{args.port}")
    print()
    print("  If using proxy, add 'localhost' to bypass list")
    print("=" * 60)
    print()

    # Import and run Flask app
    from app import app

    # Use 0.0.0.0 to allow access from other devices (e.g., phone)
    app.run(debug=args.debug, host='0.0.0.0', port=args.port, use_reloader=False)
    return 0


def run_demo(args):
    """Run the GDS2 RPA demo workflow using Java Agent."""
    # Setup logging
    log_level = logging.INFO if args.verbose else logging.WARNING
    logging.basicConfig(level=log_level, format='%(asctime)s - %(levelname)s - %(message)s')

    print("=" * 60)
    print("GDS2 RPA Demo (Agent-based)")
    print("=" * 60)
    print()
    print("Architecture:")
    print("  - Java Agent: UI navigation and data extraction")
    print("  - Windows API: Device Explorer handling")
    print("  - No PyAutoGUI or screen dependency")
    print()
    print(f"VCI Device: {args.vci}")
    print(f"Target Module: {args.module}")
    print(f"Data Category: {args.data}")
    print(f"Vehicle ID: {args.vehicle}")
    print("=" * 60)
    print()

    from src.workflows import ReadDataDisplayAgentWorkflow

    workflow = ReadDataDisplayAgentWorkflow(vehicle_id=args.vehicle)
    result = workflow.execute(
        vci_device=args.vci,
        target_module=args.module,
        data_category=args.data,
    )

    print()
    print("=" * 60)
    print("Demo Result")
    print("=" * 60)

    if result["success"]:
        print("[OK] Success!")
        print(f"Module: {result.get('module')}")
        print(f"Data Category: {result.get('data_category')}")
        return 0
    else:
        print(f"[FAILED] {result.get('error', 'Unknown error')}")
        return 1


def main():
    parser = argparse.ArgumentParser(
        description="GDS2 RPA Demo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python main.py web                                               # Start Web UI (recommended)
    python main.py web --port 8000                                   # Start Web UI on custom port
    python main.py demo                                              # Run with default settings
    python main.py demo --module "[K20] Engine Control Module" --data "Misfire Data"
    python main.py demo -v                                           # Run with verbose logging
    python main.py inspect                                           # Inspect GDS2 UI
    python main.py test-connection                                   # Test GDS2 connection
    python main.py discover                                          # Run discovery utility
        """,
    )

    parser.add_argument(
        "command",
        choices=["web", "demo", "inspect", "test-connection", "discover"],
        help="Command to run",
    )

    # Web-specific arguments
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Web UI port (default: 8080)"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable Flask debug mode"
    )

    # Demo-specific arguments
    parser.add_argument(
        "--vci",
        default="SM2 USB",
        help="VCI device name (default: SM2 USB)"
    )
    parser.add_argument(
        "--module",
        default="[K20] Engine Control Module",
        help="Target module (default: [K20] Engine Control Module)"
    )
    parser.add_argument(
        "--data",
        default="Engine Data",
        help="Data category (default: Engine Data)"
    )
    parser.add_argument(
        "--vehicle",
        default="current_vehicle",
        help="Vehicle ID for mapping storage (default: current_vehicle)"
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging"
    )

    args = parser.parse_args()

    if args.command == "web":
        return run_web(args)

    elif args.command == "demo":
        return run_demo(args)

    elif args.command == "inspect":
        from scripts.inspect_gds2 import main as inspect_gds2
        return inspect_gds2()

    elif args.command == "test-connection":
        from scripts.test_connection import test_connection
        return test_connection()

    elif args.command == "discover":
        from scripts.run_discovery import main as run_discovery
        return run_discovery()

    return 0


if __name__ == "__main__":
    sys.exit(main())
