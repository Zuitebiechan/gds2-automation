from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backends.gds2.navigation_registry import (
    DEFAULT_REGISTRY_PATH,
    DEFAULT_SEED_PATH,
    connect_registry,
    list_entries,
    list_page_states,
    list_recovery_policies,
    lookup_entry,
    rebuild_registry_database,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build or query the GDS2 navigation registry SQLite database.")
    parser.add_argument(
        "--seed",
        default=str(DEFAULT_SEED_PATH),
        help="Seed JSON file containing important GDS2 page paths.",
    )
    parser.add_argument(
        "--output",
        default=str(ROOT / DEFAULT_REGISTRY_PATH),
        help="SQLite database output path.",
    )
    parser.add_argument(
        "--lookup",
        help="Lookup a page_key or alias after building the database.",
    )
    parser.add_argument(
        "--no-rebuild",
        action="store_true",
        help="Query the existing database without rebuilding it first.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all registry entries after building the database.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = Path(args.output)
    if args.no_rebuild:
        if not output.exists():
            raise SystemExit(f"Database does not exist: {output}")
    else:
        output = rebuild_registry_database(output_path=output, seed_path=args.seed)
    with connect_registry(output) as connection:
        entries = list_entries(connection)
        page_states = list_page_states(connection)
        recovery_policies = list_recovery_policies(connection)
        payload: dict[str, object] = {
            "database": str(output),
            "seed": str(args.seed),
            "entry_count": len(entries),
            "page_state_count": len(page_states),
            "recovery_policy_count": len(recovery_policies),
        }
        if args.lookup:
            payload["lookup"] = lookup_entry(connection, args.lookup)
        if args.list:
            payload["entries"] = [
                {
                    "page_key": entry["page_key"],
                    "title": entry["title"],
                    "category": entry["category"],
                    "canonical_path": entry["canonical_path"],
                    "confidence": entry["confidence"],
                }
                for entry in entries
            ]
            payload["page_states"] = [
                {
                    "state_key": state["state_key"],
                    "page_id": state["page_id"],
                    "action_hint": state["action_hint"],
                }
                for state in page_states
            ]
            payload["recovery_policies"] = [
                {
                    "policy_key": policy["policy_key"],
                    "action_key": policy["action_key"],
                    "applies_to": policy["applies_to"],
                }
                for policy in recovery_policies
            ]
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
