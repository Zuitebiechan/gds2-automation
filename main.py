#!/usr/bin/env python
"""
GDS2 RPA Demo Main Entry

Provides multiple run modes:
    python main.py demo            - Run demo workflow
    python main.py inspect         - Inspect GDS2 UI structure
    python main.py test-connection - Test GDS2 connection
"""

import sys
import argparse


def main():
    parser = argparse.ArgumentParser(
        description="GDS2 RPA Demo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python main.py demo              # Run demo workflow
    python main.py inspect           # Inspect GDS2 UI
    python main.py test-connection   # Test GDS2 connection
        """,
    )

    parser.add_argument(
        "command",
        choices=["demo", "inspect", "test-connection"],
        help="Command to run",
    )

    args = parser.parse_args()

    if args.command == "demo":
        from scripts.run_demo import main as run_demo
        return run_demo()

    elif args.command == "inspect":
        from scripts.inspect_gds2 import main as inspect_gds2
        return inspect_gds2()

    elif args.command == "test-connection":
        from scripts.test_connection import test_connection
        return test_connection()

    return 0


if __name__ == "__main__":
    sys.exit(main())
