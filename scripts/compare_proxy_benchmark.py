"""Compare proxy benchmark JSONL files for baseline vs candidate runs."""

from __future__ import annotations

import argparse
import json

from vci_proxy.benchmark import (
    compare_benchmark_summaries,
    load_benchmark_events,
    summarize_benchmark_events,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare two proxy benchmark JSONL runs.")
    parser.add_argument("--baseline", required=True, help="Baseline JSONL benchmark log")
    parser.add_argument("--candidate", required=True, help="Candidate JSONL benchmark log")
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output with indentation",
    )
    args = parser.parse_args()

    baseline_summary = summarize_benchmark_events(load_benchmark_events(args.baseline))
    candidate_summary = summarize_benchmark_events(load_benchmark_events(args.candidate))
    comparison = compare_benchmark_summaries(baseline_summary, candidate_summary)

    if args.pretty:
        print(json.dumps(comparison, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(json.dumps(comparison, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
