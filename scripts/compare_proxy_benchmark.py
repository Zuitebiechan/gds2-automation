"""Compare proxy benchmark JSONL files for baseline vs candidate runs."""

from __future__ import annotations

import argparse
import json
import sys

from vci_proxy.benchmark import (
    compare_benchmark_summaries,
    generate_benchmark_report,
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
    parser.add_argument(
        "--report",
        nargs="?",
        const="-",
        default=None,
        metavar="FILE",
        help="Generate a Markdown report. Writes to FILE or stdout when no path given.",
    )
    parser.add_argument(
        "--report-candidate",
        nargs="?",
        const="-",
        default=None,
        metavar="FILE",
        help="Generate a standalone Markdown report for the candidate run only.",
    )
    args = parser.parse_args()

    baseline_summary = summarize_benchmark_events(load_benchmark_events(args.baseline))
    candidate_summary = summarize_benchmark_events(load_benchmark_events(args.candidate))

    # Standalone report for candidate
    if args.report_candidate is not None:
        md = generate_benchmark_report(
            candidate_summary,
            title=f"VCI Proxy Benchmark Report — {candidate_summary.get('label', 'candidate')}",
        )
        _write_output(md, args.report_candidate)
        return

    # Comparison report (baseline vs candidate)
    if args.report is not None:
        sections = [
            generate_benchmark_report(
                baseline_summary,
                title=f"Baseline — {baseline_summary.get('label', 'baseline')}",
            ),
            "\n---\n",
            generate_benchmark_report(
                candidate_summary,
                title=f"Candidate — {candidate_summary.get('label', 'candidate')}",
            ),
        ]
        md = "\n".join(sections)
        _write_output(md, args.report)
        return

    # Default: JSON comparison
    comparison = compare_benchmark_summaries(baseline_summary, candidate_summary)

    if args.pretty:
        print(json.dumps(comparison, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(json.dumps(comparison, ensure_ascii=False, sort_keys=True))


def _write_output(content: str, dest: str) -> None:
    """Write *content* to *dest* (a file path, or ``"-"`` for stdout)."""
    if dest == "-":
        sys.stdout.write(content)
        sys.stdout.write("\n")
    else:
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.write("\n")
        print(f"Report written to {dest}", file=sys.stderr)


if __name__ == "__main__":
    main()
