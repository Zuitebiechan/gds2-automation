#!/usr/bin/env python3
"""
Extract real-time data from GDS2 Data Display page using pywinauto.

This script reads the data table that shows:
- Parameter Name (e.g., "Engine Load", "MAP Sensor")
- Value (e.g., "0.0", "63.2")
- Unit (e.g., "%", "PSI")
- Control Module (e.g., "Engine Control Module")

Requirements:
- GDS2 is running and at Data Display page
- Data Display page has focus and is showing data
"""

import sys
import time
import json
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional

# Set UTF-8 encoding for Windows console
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from pywinauto import Application
from pywinauto.findwindows import ElementAmbiguousError


class DataDisplayReader:
    """Read real-time data from GDS2 Data Display page."""

    def __init__(self):
        self.app = None
        self.window = None
        self._table = None  # Cache the table reference

    def connect(self):
        """Connect to GDS2."""
        try:
            print("Connecting to GDS2...")
            self.app = Application(backend="uia").connect(title="GDS 2", timeout=5)
            self.window = self.app.window(title="GDS 2")
            print(f"✓ Connected to GDS2")
            return True
        except Exception as e:
            print(f"✗ Failed to connect to GDS2: {e}")
            return False

    def find_data_table(self, quiet=False):
        """
        Find the data table control.

        Based on inspection, the data table has headers:
        Parameter Name | Value | Unit | Control Module
        and its first child Custom control text is "Parameter Name".
        """
        try:
            tables = self.window.descendants(control_type="Table")

            for table in tables:
                children = table.children()

                header_texts = []
                for child in children[:5]:
                    try:
                        text = child.window_text().strip()
                        header_texts.append(text)
                    except:
                        pass

                if ("Parameter Name" in header_texts and "Value" in header_texts
                        and header_texts and header_texts[0] == "Parameter Name"):
                    if not quiet:
                        print(f"✓ Found data table")
                    return table

            if not quiet:
                print("✗ Data table not found")
            return None

        except Exception as e:
            if not quiet:
                print(f"✗ Error finding data table: {e}")
            return None

    def extract_data_from_table(self, table) -> List[Dict[str, str]]:
        """
        Extract data from the table using DataItem controls.

        DataItems are always in groups of 4 per row:
        [parameter_name, value, unit, control_module]

        But they are NOT sorted by position in the control tree.
        We sort by Y position first to get proper row order.
        """
        try:
            data_items = table.descendants(control_type="DataItem")

            # Get text and position for each DataItem
            items_with_pos = []
            for item in data_items:
                try:
                    text = item.window_text()
                    rect = item.rectangle()
                    items_with_pos.append({
                        'text': text.strip() if text else '',
                        'left': rect.left,
                        'top': rect.top,
                    })
                except:
                    pass

            # Sort by Y position, then X position
            items_with_pos.sort(key=lambda x: (x['top'], x['left']))

            # Group into rows (items with same Y within 5px)
            grouped_rows = []
            current_row = []
            last_top = -999

            for item in items_with_pos:
                if abs(item['top'] - last_top) > 5:
                    if current_row:
                        grouped_rows.append(current_row)
                    current_row = [item]
                    last_top = item['top']
                else:
                    current_row.append(item)

            if current_row:
                grouped_rows.append(current_row)

            # Convert to data rows (expect 4 columns per row)
            rows = []
            for group in grouped_rows:
                if len(group) == 4:
                    rows.append({
                        'parameter_name': group[0]['text'],
                        'value': group[1]['text'],
                        'unit': group[2]['text'],
                        'control_module': group[3]['text'],
                    })
                elif len(group) == 8:
                    # Two rows merged at same Y (e.g., MAP Sensor + Boost Pressure)
                    rows.append({
                        'parameter_name': group[0]['text'],
                        'value': group[1]['text'],
                        'unit': group[2]['text'],
                        'control_module': group[3]['text'],
                    })
                    rows.append({
                        'parameter_name': group[4]['text'],
                        'value': group[5]['text'],
                        'unit': group[6]['text'],
                        'control_module': group[7]['text'],
                    })

            return rows

        except Exception as e:
            print(f"✗ Error extracting data: {e}")
            import traceback
            traceback.print_exc()
            return []

    def extract_data_by_position(self) -> List[Dict[str, str]]:
        """
        Alternative method: Extract data by analyzing positions of text controls.

        This is more robust than relying on table structure.
        """
        try:
            # Get all visible text controls
            texts = self.window.descendants(control_type="Text")

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

            # Filter rows that have exactly 4 columns (parameter, value, unit, module)
            data_rows = []
            for row in rows:
                if len(row) == 4:
                    # Check if this looks like a data row (not a header or filter row)
                    # Skip: headers, "All" filter rows
                    param_name = row[0]['text']
                    if param_name not in ['Parameter Name', 'Category', 'Legend', 'All']:
                        data_row = {
                            'parameter_name': param_name,
                            'value': row[1]['text'],
                            'unit': row[2]['text'],
                            'control_module': row[3]['text'],
                        }
                        data_rows.append(data_row)

            return data_rows

        except Exception as e:
            print(f"✗ Error extracting data by position: {e}")
            import traceback
            traceback.print_exc()
            return []

    def read_data(self) -> Optional[Dict]:
        """
        Read data from Data Display page.

        Returns dict with:
        - timestamp: ISO format timestamp
        - data: list of parameter dicts
        """
        if not self.window:
            if not self.connect():
                return None

        # Use cached table reference, or find it
        if not self._table:
            self._table = self.find_data_table(quiet=False)

        if self._table:
            try:
                data_rows = self.extract_data_from_table(self._table)
            except Exception:
                # Table reference may be stale, re-find
                self._table = self.find_data_table(quiet=True)
                if self._table:
                    data_rows = self.extract_data_from_table(self._table)
                else:
                    data_rows = []
        else:
            # Fallback to position-based extraction
            data_rows = self.extract_data_by_position()

        if data_rows:
            result = {
                'timestamp': datetime.now().isoformat(),
                'data': data_rows,
                'count': len(data_rows)
            }
            return result
        else:
            print("✗ Failed to extract any data")
            return None


def print_data(data: Dict):
    """Pretty print the extracted data."""
    print("\n" + "="*80)
    print(f"TIMESTAMP: {data['timestamp']}")
    print(f"DATA COUNT: {data['count']}")
    print("="*80)
    print(f"{'Parameter Name':<30} {'Value':>15} {'Unit':<10} {'Control Module':<30}")
    print("-"*80)

    for row in data['data']:
        print(f"{row['parameter_name']:<30} {row['value']:>15} {row['unit']:<10} {row['control_module']:<30}")

    print("="*80)


def stream_data(duration_seconds=60, interval_seconds=1, output_file=None, filter_params=None):
    """
    Stream data for a specified duration.

    Args:
        duration_seconds: Total duration to stream (default 60 seconds)
        interval_seconds: Sampling interval (default 1 second)
        output_file: Optional file path to save data as JSON
        filter_params: Optional list of parameter names to filter
    """
    reader = DataDisplayReader()

    if not reader.connect():
        return 1

    filter_msg = ""
    if filter_params:
        filter_msg = f", filtering: {filter_params}"
    print(f"\nStreaming data for {duration_seconds} seconds (interval: {interval_seconds}s{filter_msg})")
    print("Press Ctrl+C to stop early\n")

    data_stream = []
    start_time = time.time()
    end_time = start_time + duration_seconds
    sample_count = 0

    try:
        while time.time() < end_time:
            sample_count += 1
            read_start = time.time()

            data = reader.read_data()
            read_elapsed = time.time() - read_start

            if data:
                # Apply filter if specified
                if filter_params:
                    data['data'] = [
                        row for row in data['data']
                        if any(fp.lower() in row['parameter_name'].lower() for fp in filter_params)
                    ]
                    data['count'] = len(data['data'])

                data_stream.append(data)

                # Print first sample in detail
                if sample_count == 1:
                    print_data(data)
                    print(f"  (read time: {read_elapsed*1000:.0f}ms)")
                else:
                    # Compact output for subsequent samples
                    ts = data['timestamp'].split('T')[1][:12]
                    params = " | ".join(
                        f"{row['parameter_name']}: {row['value']} {row['unit']}"
                        for row in data['data']
                    )
                    print(f"[{sample_count:3d}] {ts} ({read_elapsed*1000:.0f}ms) {params}")
            else:
                print(f"[{sample_count:3d}] ✗ Failed to extract data")

            # Sleep until next sample
            next_sample_time = start_time + (sample_count * interval_seconds)
            sleep_time = next_sample_time - time.time()
            if sleep_time > 0:
                time.sleep(sleep_time)

    except KeyboardInterrupt:
        print("\n\nStreaming stopped by user")

    # Print summary
    elapsed = time.time() - start_time
    print("\n" + "="*80)
    print("STREAMING SUMMARY")
    print("="*80)
    print(f"Duration: {elapsed:.1f} seconds")
    print(f"Samples collected: {len(data_stream)}")
    if elapsed > 0:
        print(f"Average sample rate: {len(data_stream)/elapsed:.2f} samples/second")

    # Show value changes if filter was used
    if filter_params and len(data_stream) > 1:
        print(f"\nValue changes detected:")
        # Track unique values per parameter
        for param_filter in filter_params:
            values = []
            for sample in data_stream:
                for row in sample['data']:
                    if param_filter.lower() in row['parameter_name'].lower():
                        values.append(row['value'])
            unique_values = list(dict.fromkeys(values))  # Preserve order, deduplicate
            if len(unique_values) > 1:
                print(f"  {param_filter}: {len(unique_values)} distinct values: {unique_values[:20]}")
            else:
                print(f"  {param_filter}: constant at {unique_values[0] if unique_values else 'N/A'}")

    # Save to file if requested
    if output_file and data_stream:
        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, 'w') as f:
            json.dump(data_stream, f, indent=2)

        print(f"\n✓ Data saved to: {output_path}")
        print(f"  File size: {output_path.stat().st_size / 1024:.1f} KB")

    return 0


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Extract real-time data from GDS2 Data Display page")
    parser.add_argument('--duration', type=int, default=10, help='Duration in seconds (default: 10)')
    parser.add_argument('--interval', type=float, default=1.0, help='Sampling interval in seconds (default: 1.0)')
    parser.add_argument('--output', type=str, help='Output JSON file path')
    parser.add_argument('--single', action='store_true', help='Read data once and exit')
    parser.add_argument('--filter', type=str, nargs='+', help='Filter by parameter name (partial match)')

    args = parser.parse_args()

    if args.single:
        # Single read mode
        reader = DataDisplayReader()
        if not reader.connect():
            return 1

        print("\nReading data from Data Display page...")
        data = reader.read_data()

        if data:
            # Apply filter if specified
            if args.filter:
                data['data'] = [
                    row for row in data['data']
                    if any(fp.lower() in row['parameter_name'].lower() for fp in args.filter)
                ]
                data['count'] = len(data['data'])

            print_data(data)

            if args.output:
                output_path = Path(args.output)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                with open(output_path, 'w') as f:
                    json.dump(data, f, indent=2)
                print(f"\n✓ Data saved to: {output_path}")

            return 0
        else:
            return 1

    else:
        # Streaming mode
        return stream_data(args.duration, args.interval, args.output, args.filter)


if __name__ == "__main__":
    sys.exit(main())
