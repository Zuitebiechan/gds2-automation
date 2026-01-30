#!/usr/bin/env python3
"""
Deep inspection of Table[4] (Data Display table) to understand
all DataItem controls and their structure.
"""

import sys
import time
from pathlib import Path

if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

from pywinauto import Application


def main():
    print("Connecting to GDS2...")
    app = Application(backend="uia").connect(title="GDS 2", timeout=5)
    window = app.window(title="GDS 2")
    print("Connected.\n")

    # Find all tables
    tables = window.descendants(control_type="Table")
    print(f"Found {len(tables)} tables\n")

    # Find the data table (Table[4] with Parameter Name header)
    data_table = None
    for i, table in enumerate(tables):
        children = table.children()
        header_texts = []
        for child in children[:5]:
            try:
                header_texts.append(child.window_text().strip())
            except:
                pass
        print(f"Table[{i}] rect={table.rectangle()} headers={header_texts[:5]}")

        if "Parameter Name" in header_texts and "Value" in header_texts and "Unit" in header_texts:
            # Pick the table whose first header is "Parameter Name" (not Legend table)
            first_header = header_texts[0] if header_texts else ""
            if first_header == "Parameter Name":
                data_table = table
                print(f"  ^^^ THIS IS THE DATA TABLE ^^^")

    if not data_table:
        print("\nData table not found!")
        return

    print(f"\n{'='*100}")
    print("DATA TABLE DEEP INSPECTION")
    print(f"{'='*100}")

    # Get all children of the data table
    children = data_table.children()
    print(f"\nTotal children: {len(children)}")
    print(f"\nAll children (headers + data):")
    print(f"{'Idx':>4} {'Type':<12} {'Text':<40} {'Rect':<30}")
    print("-"*100)

    for i, child in enumerate(children):
        try:
            ctrl_type = child.element_info.control_type
            text = child.window_text().strip()
            rect = child.rectangle()
            print(f"{i:4d} {ctrl_type:<12} {text[:40]:<40} ({rect.left},{rect.top},{rect.right},{rect.bottom})")
        except Exception as e:
            print(f"{i:4d} ERROR: {e}")

    # Now get DataItems specifically
    data_items = data_table.descendants(control_type="DataItem")
    print(f"\n{'='*100}")
    print(f"DATA ITEMS: {len(data_items)}")
    print(f"{'='*100}")
    print(f"{'Idx':>4} {'Text':<50} {'Rect':<30}")
    print("-"*100)

    for i, item in enumerate(data_items):
        try:
            text = item.window_text().strip()
            rect = item.rectangle()
            print(f"{i:4d} {text[:50]:<50} ({rect.left},{rect.top},{rect.right},{rect.bottom})")
        except Exception as e:
            print(f"{i:4d} ERROR: {e}")

    # Group DataItems into rows by Y position
    print(f"\n{'='*100}")
    print("DATA ITEMS GROUPED BY ROW (Y position)")
    print(f"{'='*100}")

    items_with_pos = []
    for item in data_items:
        try:
            text = item.window_text().strip()
            rect = item.rectangle()
            items_with_pos.append({
                'text': text,
                'left': rect.left,
                'top': rect.top,
                'right': rect.right,
                'bottom': rect.bottom,
            })
        except:
            pass

    items_with_pos.sort(key=lambda x: (x['top'], x['left']))

    # Group into rows
    rows = []
    current_row = []
    last_top = -999

    for item in items_with_pos:
        if abs(item['top'] - last_top) > 5:
            if current_row:
                rows.append(current_row)
            current_row = [item]
            last_top = item['top']
        else:
            current_row.append(item)

    if current_row:
        rows.append(current_row)

    print(f"\nGrouped into {len(rows)} rows:\n")

    for i, row in enumerate(rows):
        cols = " | ".join([f"{item['text'][:25]}" for item in row])
        print(f"Row {i:2d} ({len(row)} cols, y={row[0]['top']}): {cols}")

    # Also check: are there Text controls inside the data table area?
    table_rect = data_table.rectangle()
    print(f"\n{'='*100}")
    print(f"TEXT CONTROLS WITHIN DATA TABLE AREA ({table_rect})")
    print(f"{'='*100}")

    all_texts = window.descendants(control_type="Text")
    table_texts = []
    for text_ctrl in all_texts:
        try:
            rect = text_ctrl.rectangle()
            # Check if this text is within the data table area
            if (rect.left >= table_rect.left and rect.right <= table_rect.right and
                rect.top >= table_rect.top and rect.bottom <= table_rect.bottom):
                text_value = text_ctrl.window_text().strip()
                if text_value:
                    table_texts.append({
                        'text': text_value,
                        'left': rect.left,
                        'top': rect.top,
                    })
        except:
            pass

    table_texts.sort(key=lambda x: (x['top'], x['left']))

    # Group into rows
    t_rows = []
    current_row = []
    last_top = -999

    for item in table_texts:
        if abs(item['top'] - last_top) > 5:
            if current_row:
                t_rows.append(current_row)
            current_row = [item]
            last_top = item['top']
        else:
            current_row.append(item)

    if current_row:
        t_rows.append(current_row)

    print(f"\n{len(table_texts)} Text controls, grouped into {len(t_rows)} rows:\n")

    for i, row in enumerate(t_rows):
        cols = " | ".join([f"{item['text'][:25]}" for item in row])
        print(f"Row {i:2d} ({len(row)} cols, y={row[0]['top']}): {cols}")


if __name__ == "__main__":
    main()
