#!/usr/bin/env python3
"""
Inspect GDS2 Data Display page to find table structure and data fields.

This script helps us understand how to read real-time data from the Data Display page
using pywinauto.

Requirements:
- GDS2 is running and at Data Display page
- Data Display page has focus
"""

import sys
import time
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from pywinauto import Application
from pywinauto.findwindows import ElementAmbiguousError


def find_gds2_main_window():
    """Find the main GDS2 window."""
    try:
        # Try to connect to GDS2
        print("Connecting to GDS2...")
        app = Application(backend="uia").connect(title_re=".*GDS 2.*", timeout=5)

        # Get all windows
        windows = app.windows()
        print(f"\nFound {len(windows)} GDS2 windows")

        # Find the main window (usually the largest or titled "GDS 2")
        main_window = None
        for i, win in enumerate(windows):
            title = win.window_text()
            print(f"  [{i}] {title}")
            if "GDS 2" in title and "Device Explorer" not in title:
                main_window = win

        return app, main_window

    except ElementAmbiguousError as e:
        print(f"Multiple GDS2 windows found. This is expected.")
        # Get the first one that matches
        app = Application(backend="uia").connect(title="GDS 2", timeout=5)
        return app, app.window(title="GDS 2")

    except Exception as e:
        print(f"Error connecting to GDS2: {e}")
        return None, None


def explore_control_types(window, max_depth=5):
    """Explore all control types in the window."""
    print("\n" + "="*80)
    print("EXPLORING CONTROL TYPES")
    print("="*80)

    control_types = {}

    try:
        # Get all descendants
        print("Enumerating all descendants (this may take a moment)...")
        descendants = window.descendants()
        print(f"Found {len(descendants)} total descendants")

        # Count by control type
        for desc in descendants:
            try:
                ctrl_type = desc.element_info.control_type
                if ctrl_type not in control_types:
                    control_types[ctrl_type] = []
                control_types[ctrl_type].append(desc)
            except:
                pass

        # Print summary
        print("\nControl Type Summary:")
        print("-" * 80)
        for ctrl_type, items in sorted(control_types.items()):
            print(f"  {ctrl_type:30s} : {len(items):4d} items")

        return control_types

    except Exception as e:
        print(f"Error exploring control types: {e}")
        return {}


def inspect_table_controls(control_types):
    """Inspect table-related controls in detail."""
    print("\n" + "="*80)
    print("INSPECTING TABLE CONTROLS")
    print("="*80)

    # Look for common table control types
    table_related = ['Table', 'DataGrid', 'List', 'DataItem', 'Custom', 'Pane', 'Group']

    for ctrl_type in table_related:
        if ctrl_type in control_types:
            print(f"\n{ctrl_type} Controls ({len(control_types[ctrl_type])} found):")
            print("-" * 80)

            for i, ctrl in enumerate(control_types[ctrl_type][:5]):  # Show first 5
                try:
                    print(f"\n  [{i}] {ctrl_type}:")
                    print(f"      Text: {ctrl.window_text()}")
                    print(f"      Class: {ctrl.class_name()}")
                    print(f"      Rect: {ctrl.rectangle()}")
                    print(f"      Visible: {ctrl.is_visible()}")
                    print(f"      Enabled: {ctrl.is_enabled()}")

                    # Try to get children
                    children = ctrl.children()
                    if children:
                        print(f"      Children: {len(children)}")
                        for j, child in enumerate(children[:3]):  # Show first 3 children
                            try:
                                child_type = child.element_info.control_type
                                child_text = child.window_text()
                                print(f"        [{j}] {child_type}: {child_text}")
                            except:
                                pass

                except Exception as e:
                    print(f"      Error: {e}")


def search_for_data_table(window):
    """Search for the data table that contains parameter name, value, unit, control module."""
    print("\n" + "="*80)
    print("SEARCHING FOR DATA TABLE")
    print("="*80)

    try:
        # Strategy 1: Look for Table control
        print("\nStrategy 1: Looking for Table control...")
        tables = window.descendants(control_type="Table")
        print(f"Found {len(tables)} Table controls")

        for i, table in enumerate(tables):
            print(f"\n  Table [{i}]:")
            print(f"    Text: {table.window_text()}")
            print(f"    Class: {table.class_name()}")
            print(f"    Rect: {table.rectangle()}")

            # Try to get rows
            rows = table.descendants(control_type="DataItem")
            print(f"    DataItems: {len(rows)}")

            # Try to read first few rows
            for j, row in enumerate(rows[:5]):
                try:
                    row_text = row.window_text()
                    print(f"      Row [{j}]: {row_text}")

                    # Try to get cells
                    cells = row.children()
                    if cells:
                        cell_texts = [c.window_text() for c in cells]
                        print(f"        Cells: {cell_texts}")
                except Exception as e:
                    print(f"        Error: {e}")

        # Strategy 2: Look for Text controls with data values
        print("\n\nStrategy 2: Looking for Text controls...")
        texts = window.descendants(control_type="Text")
        print(f"Found {len(texts)} Text controls")

        # Filter for visible and non-empty text controls
        data_texts = []
        for text_ctrl in texts:
            try:
                if text_ctrl.is_visible():
                    text_value = text_ctrl.window_text()
                    if text_value and text_value.strip():
                        data_texts.append((text_ctrl, text_value))
            except:
                pass

        print(f"Found {len(data_texts)} visible non-empty Text controls")
        print("\nFirst 20 Text controls:")
        for i, (ctrl, text) in enumerate(data_texts[:20]):
            rect = ctrl.rectangle()
            print(f"  [{i:2d}] ({rect.left:4d},{rect.top:4d}) : {text}")

        # Strategy 3: Look for Custom controls (JavaFX might use these)
        print("\n\nStrategy 3: Looking for Custom controls...")
        customs = window.descendants(control_type="Custom")
        print(f"Found {len(customs)} Custom controls")

        # Look for custom controls with text
        custom_with_text = []
        for custom in customs:
            try:
                text = custom.window_text()
                if text and text.strip() and len(text) < 100:
                    custom_with_text.append((custom, text))
            except:
                pass

        print(f"Found {len(custom_with_text)} Custom controls with text")
        print("\nFirst 20 Custom controls:")
        for i, (ctrl, text) in enumerate(custom_with_text[:20]):
            try:
                rect = ctrl.rectangle()
                print(f"  [{i:2d}] ({rect.left:4d},{rect.top:4d}) : {text}")
            except:
                pass

        # Strategy 4: Look for Edit controls (data values might be editable)
        print("\n\nStrategy 4: Looking for Edit controls...")
        edits = window.descendants(control_type="Edit")
        print(f"Found {len(edits)} Edit controls")

        for i, edit in enumerate(edits[:20]):
            try:
                text = edit.window_text()
                rect = edit.rectangle()
                print(f"  [{i:2d}] ({rect.left:4d},{rect.top:4d}) : {text}")
            except:
                pass

    except Exception as e:
        print(f"Error searching for data table: {e}")
        import traceback
        traceback.print_exc()


def extract_data_by_position(window):
    """Try to extract data by analyzing positions of text controls."""
    print("\n" + "="*80)
    print("EXTRACTING DATA BY POSITION ANALYSIS")
    print("="*80)

    try:
        # Get all visible text controls with positions
        texts = window.descendants(control_type="Text")

        data_items = []
        for text_ctrl in texts:
            try:
                if text_ctrl.is_visible():
                    text_value = text_ctrl.window_text()
                    if text_value and text_value.strip():
                        rect = text_ctrl.rectangle()
                        data_items.append({
                            'text': text_value.strip(),
                            'left': rect.left,
                            'top': rect.top,
                            'right': rect.right,
                            'bottom': rect.bottom,
                        })
            except:
                pass

        print(f"Found {len(data_items)} data items")

        # Sort by vertical position (top), then horizontal (left)
        data_items.sort(key=lambda x: (x['top'], x['left']))

        # Group by rows (items with similar top position)
        rows = []
        current_row = []
        last_top = -999

        for item in data_items:
            # If this item is on a new row (top position changed by more than 5 pixels)
            if abs(item['top'] - last_top) > 5:
                if current_row:
                    rows.append(current_row)
                current_row = [item]
                last_top = item['top']
            else:
                current_row.append(item)

        if current_row:
            rows.append(current_row)

        print(f"\nGrouped into {len(rows)} rows")

        # Print rows that look like data rows (4 columns: name, value, unit, module)
        print("\nPotential data rows (with 4 columns):")
        print("-" * 80)

        for i, row in enumerate(rows):
            if len(row) == 4:
                print(f"Row {i:3d}: {row[0]['text']:30s} | {row[1]['text']:15s} | {row[2]['text']:10s} | {row[3]['text']}")

        print("\nAll rows (showing structure):")
        print("-" * 80)
        for i, row in enumerate(rows[:30]):  # Show first 30 rows
            row_text = " | ".join([item['text'][:20] for item in row])
            print(f"Row {i:3d} ({len(row)} cols): {row_text}")

    except Exception as e:
        print(f"Error extracting data by position: {e}")
        import traceback
        traceback.print_exc()


def main():
    print("="*80)
    print("GDS2 DATA DISPLAY PAGE INSPECTOR")
    print("="*80)
    print("\nThis script will inspect the Data Display page structure.")
    print("Make sure GDS2 is at the Data Display page with data showing.\n")

    # Connect to GDS2
    app, window = find_gds2_main_window()

    if not app or not window:
        print("\nFailed to connect to GDS2. Make sure GDS2 is running.")
        return 1

    print(f"\nConnected to: {window.window_text()}")
    print(f"Window class: {window.class_name()}")
    print(f"Window rect: {window.rectangle()}")

    # Explore control types
    control_types = explore_control_types(window)

    # Inspect table controls
    inspect_table_controls(control_types)

    # Search for data table
    search_for_data_table(window)

    # Extract data by position
    extract_data_by_position(window)

    print("\n" + "="*80)
    print("INSPECTION COMPLETE")
    print("="*80)

    return 0


if __name__ == "__main__":
    sys.exit(main())
