from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backends.gds2.registry_navigation_runtime import (
    DEFAULT_GRAPH_PATH,
    DEFAULT_REPORT_ROOT,
    run_probe,
)


def write_report(result: dict[str, object], output_root: str | Path = DEFAULT_REPORT_ROOT) -> Path:
    output_dir = Path(output_root) / datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "result.json"
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prove one GDS2 navigation registry entry can drive live navigation.")
    parser.add_argument("entry", help="Registry page_key or alias, for example 'Fuel Trim Enable'.")
    parser.add_argument(
        "--registry",
        default=str(ROOT / "data" / "gds2_navigation_registry.sqlite"),
        help="SQLite registry path.",
    )
    parser.add_argument(
        "--graph",
        default=str(DEFAULT_GRAPH_PATH),
        help="Merged route graph path.",
    )
    parser.add_argument("--rebuild-registry", action="store_true", help="Rebuild the registry before probing.")
    parser.add_argument(
        "--no-bootstrap",
        action="store_true",
        help="Do not first navigate through SM2 USB / Engine Control Module / Engine Data.",
    )
    parser.add_argument("--max-iterations", type=int, default=24)
    parser.add_argument("--max-backtracks", type=int, default=8)
    parser.add_argument("--loading-timeout", type=float, default=None)
    parser.add_argument("--max-loading-restarts", type=int, default=None)
    parser.add_argument("--output-root", default=str(DEFAULT_REPORT_ROOT))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = run_probe(
            entry_key=args.entry,
            registry_path=args.registry,
            graph_path=args.graph,
            rebuild_registry=args.rebuild_registry,
            bootstrap=not args.no_bootstrap,
            max_iterations=args.max_iterations,
            max_backtracks=args.max_backtracks,
            loading_timeout_sec=args.loading_timeout,
            max_loading_restarts=args.max_loading_restarts,
        )
    except Exception as exc:
        result = {
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "completed_at": datetime.now().isoformat(timespec="seconds"),
            "entry_key": args.entry,
            "success": False,
            "failure_reason": str(exc),
        }
    output_path = write_report(result, args.output_root)
    print(json.dumps({"report": str(output_path), **result}, ensure_ascii=False, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
