#!/usr/bin/env python
"""
Debug script to inspect GDS2 UI controls via Java Agent.
"""

import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.streaming import AgentNavigator

def main():
    print("=" * 60)
    print("  GDS2 UI Inspector (via Java Agent)")
    print("=" * 60)

    nav = AgentNavigator()

    if not nav.check_agent():
        print("\n[ERROR] Agent not responding!")
        print("Please restart GDS2 with the agent.")
        return 1

    print("\n[OK] Agent connected\n")

    # Inspect controls
    print("Inspecting all controls...")
    result = nav.inspect_controls(max_depth=50)

    if not result.get('success'):
        print(f"[ERROR] Inspection failed: {result.get('message')}")
        return 1

    data = result.get('data', {})
    controls = data.get('controls', [])
    type_counts = data.get('typeCounts', {})

    print(f"\nTotal controls: {data.get('totalCount', 0)}")
    print("\n--- Control Type Counts ---")
    for ctrl_type, count in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"  {ctrl_type}: {count}")

    print("\n--- Interactive Controls ---")
    for ctrl in controls:
        ctrl_type = ctrl.get('type', '?')
        text = ctrl.get('text', '')
        item_count = ctrl.get('itemCount')
        preview = ctrl.get('preview')
        tabs = ctrl.get('tabs')

        if ctrl_type == 'Button':
            graphic = ctrl.get('graphic')
            disabled = ctrl.get('disabled')
            state = " (disabled)" if disabled else ""
            if text:
                print(f"  Button: '{text}'{state}")
            elif graphic:
                print(f"  Button: [icon: {graphic}]{state}")
            else:
                print(f"  Button: [no text, id={ctrl.get('id')}]{state}")

        elif ctrl_type == 'ListView':
            print(f"  ListView: {item_count} items")
            if preview:
                for i, item in enumerate(preview):
                    print(f"    [{i}] {item}")
                if item_count > 5:
                    print(f"    ... and {item_count - 5} more")

        elif ctrl_type == 'TableView':
            print(f"  TableView: {ctrl.get('rowCount')} rows, columns={ctrl.get('columns')}")

        elif ctrl_type == 'TabPane':
            print(f"  TabPane: tabs={tabs}, selected={ctrl.get('selectedTab')}")

        elif ctrl_type == 'Label':
            if text and len(text) > 3:  # Skip very short labels
                print(f"  Label: '{text[:60]}...')" if len(text) > 60 else f"  Label: '{text}'")

        elif ctrl_type == 'ComboBox':
            print(f"  ComboBox: {item_count} items, selected='{ctrl.get('selected')}'")

        elif ctrl_type == 'TextField':
            print(f"  TextField: '{ctrl.get('text')}'")

        elif ctrl_type == 'CheckBox':
            print(f"  CheckBox: '{text}' (selected={ctrl.get('selected')})")

    print("\n" + "=" * 60)

    # Save full data for analysis
    with open('gds2_inspect.json', 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print("Full inspection data saved to: gds2_inspect.json")

    return 0


if __name__ == "__main__":
    sys.exit(main())
