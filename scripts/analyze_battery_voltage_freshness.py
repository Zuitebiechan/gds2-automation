"""Analyze battery-voltage freshness markers from cloud observability artifacts."""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from diagnostic_platform.observability_analysis import assemble_session_trace


DEFAULT_WINDOW_MS = 1000
DEFAULT_MIN_DELTA_V = 1.0


def _normalize_cloud_root(path_like: str | Path | None) -> Path:
    if path_like is None:
        return ROOT / "vci_proxy" / "cloud_mirror" / "observability" / "cloud"
    path = Path(path_like)
    if path.name == "cloud":
        return path
    if (path / "cloud").exists():
        return path / "cloud"
    if (path / "raw").exists():
        return path
    return path


def _parse_ts(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _latency_percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * ratio) - 1))
    return round(float(ordered[index]), 3)


def _latency_block(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {
            "count": 0,
            "min": None,
            "avg": None,
            "p50": None,
            "p95": None,
            "p99": None,
            "max": None,
        }
    return {
        "count": len(values),
        "min": round(min(values), 3),
        "avg": round(sum(values) / len(values), 3),
        "p50": _latency_percentile(values, 0.50),
        "p95": _latency_percentile(values, 0.95),
        "p99": _latency_percentile(values, 0.99),
        "max": round(max(values), 3),
    }


def _discover_session_ids(cloud_root: Path) -> list[str]:
    raw_dir = cloud_root / "raw"
    if not raw_dir.exists():
        return []
    session_first_ts: dict[str, datetime] = {}
    for path in sorted(raw_dir.glob("*.jsonl")) + sorted(raw_dir.glob("*.jsonl.gz")):
        opener = open
        if path.suffix == ".gz":
            import gzip

            opener = gzip.open
        with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                session_id = str(payload.get("session_id") or "").strip()
                if not session_id or session_id.lower() == "no-session":
                    continue
                ts = _parse_ts(payload.get("ts")) or datetime.max.replace(tzinfo=timezone.utc)
                previous = session_first_ts.get(session_id)
                if previous is None or ts < previous:
                    session_first_ts[session_id] = ts
    return [
        session_id
        for session_id, _ts in sorted(session_first_ts.items(), key=lambda item: item[1])
    ]


def _window_events(
    timeline: list[dict[str, Any]],
    *,
    center_ts: datetime,
    window_ms: int,
) -> list[dict[str, Any]]:
    delta = timedelta(milliseconds=max(0, int(window_ms)))
    start = center_ts - delta
    end = center_ts + delta
    selected: list[dict[str, Any]] = []
    for event in timeline:
        ts = _parse_ts(event.get("ts"))
        if ts is None:
            continue
        if start <= ts <= end:
            selected.append(event)
    return selected


def _summarize_change_window(
    timeline: list[dict[str, Any]],
    *,
    center_ts: datetime,
    window_ms: int,
) -> dict[str, Any]:
    events = _window_events(timeline, center_ts=center_ts, window_ms=window_ms)
    network_values = [
        float(event["network_ms"])
        for event in events
        if event.get("event_type") == "proxy.request.response_received"
        and event.get("network_ms") not in (None, "")
    ]
    cache_counts = {
        "cache_hit": 0,
        "cache_miss": 0,
        "post_write_bypass": 0,
        "prefetch_hit": 0,
    }
    for event in events:
        if event.get("event_type") != "proxy.request.cache_decision":
            continue
        reason = str(event.get("reason") or "")
        if reason in cache_counts:
            cache_counts[reason] += 1
    write_collect_transaction_count = sum(
        1
        for event in events
        if event.get("event_type") == "proxy.request.forwarded_to_tunnel"
        and str(event.get("reason") or "") == "write_collect_transaction"
    )
    forwarded_to_tunnel_count = sum(
        1
        for event in events
        if event.get("event_type") == "proxy.request.forwarded_to_tunnel"
    )
    return {
        "window_ms": window_ms,
        "event_count": len(events),
        "network_ms": _latency_block(network_values),
        "write_collect_transaction_count": write_collect_transaction_count,
        "forwarded_to_tunnel_count": forwarded_to_tunnel_count,
        "cache_decision_counts": cache_counts,
    }


def _extract_voltage_changes(
    timeline: list[dict[str, Any]],
    *,
    min_delta_v: float,
    window_ms: int,
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for event in timeline:
        if event.get("event_type") != "agent.collector.focus_value_changed":
            continue
        if str(event.get("focus_key") or "") != "battery_voltage":
            continue
        delta = event.get("delta_value_number")
        if not isinstance(delta, (int, float)):
            continue
        abs_delta = abs(float(delta))
        if abs_delta < float(min_delta_v):
            continue
        ts = _parse_ts(event.get("ts"))
        if ts is None:
            continue
        changes.append(
            {
                "ts": event.get("ts"),
                "previous_value_number": event.get("previous_value_number"),
                "current_value_number": event.get("current_value_number"),
                "delta_value_number": round(float(delta), 3),
                "abs_delta_value_number": round(abs_delta, 3),
                "change_threshold_number": event.get("change_threshold_number"),
                "collector_lag_ms": event.get("collector_lag_ms"),
                "window": _summarize_change_window(
                    timeline,
                    center_ts=ts,
                    window_ms=window_ms,
                ),
            }
        )
    return changes


def _summarize_session(
    cloud_root: Path,
    *,
    session_id: str,
    min_delta_v: float,
    window_ms: int,
) -> dict[str, Any]:
    trace = assemble_session_trace(cloud_root=cloud_root, session_id=session_id)
    timeline = list(trace.get("timeline") or [])
    changes = _extract_voltage_changes(
        timeline,
        min_delta_v=min_delta_v,
        window_ms=window_ms,
    )
    return {
        "session_id": trace.get("session_id"),
        "connection_epoch": trace.get("connection_epoch"),
        "status": trace.get("status"),
        "page_context": trace.get("page_context"),
        "network_context": trace.get("network_context"),
        "key_metrics": trace.get("key_metrics"),
        "source_artifact_count": len(trace.get("source_artifacts") or []),
        "battery_voltage_changes": changes,
    }


def analyze_battery_voltage_freshness(
    cloud_root: str | Path | None,
    *,
    session_id: str | None = None,
    min_delta_v: float = DEFAULT_MIN_DELTA_V,
    window_ms: int = DEFAULT_WINDOW_MS,
) -> dict[str, Any]:
    resolved_cloud_root = _normalize_cloud_root(cloud_root)
    if session_id:
        session_ids = [session_id]
    else:
        session_ids = _discover_session_ids(resolved_cloud_root)

    sessions: list[dict[str, Any]] = []
    for discovered_session_id in session_ids:
        report = _summarize_session(
            resolved_cloud_root,
            session_id=discovered_session_id,
            min_delta_v=min_delta_v,
            window_ms=window_ms,
        )
        if session_id or report["battery_voltage_changes"]:
            sessions.append(report)

    return {
        "cloud_root": str(resolved_cloud_root),
        "min_delta_v": float(min_delta_v),
        "window_ms": int(window_ms),
        "session_count_scanned": len(session_ids),
        "session_count_reported": len(sessions),
        "sessions": sessions,
    }


def generate_markdown_report(payload: dict[str, Any]) -> str:
    lines = [
        "# Battery Voltage Freshness Report",
        "",
        f"- Cloud root: `{payload['cloud_root']}`",
        f"- Significant delta threshold: `{payload['min_delta_v']:.1f}V`",
        f"- Context window: `{payload['window_ms']}ms`",
        f"- Sessions scanned: `{payload['session_count_scanned']}`",
        f"- Sessions reported: `{payload['session_count_reported']}`",
        "",
    ]
    for session in payload.get("sessions") or []:
        lines.extend(
            [
                f"## Session `{session['session_id']}`",
                "",
                f"- Connection epoch: `{session.get('connection_epoch')}`",
                f"- Status: `{session.get('status')}`",
                f"- Page: `{(session.get('page_context') or {}).get('page')}`",
                f"- Module: `{(session.get('page_context') or {}).get('module')}`",
                f"- Data category: `{(session.get('page_context') or {}).get('data_category')}`",
                f"- Significant battery changes: `{len(session.get('battery_voltage_changes') or [])}`",
                "",
                "| TS | Prev V | Curr V | Delta V | Lag ms | RTT p95 ms | Write-collect txns |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for change in session.get("battery_voltage_changes") or []:
            lines.append(
                f"| {change.get('ts')} | {change.get('previous_value_number')} | {change.get('current_value_number')} | "
                f"{change.get('delta_value_number')} | {change.get('collector_lag_ms')} | "
                f"{(change.get('window') or {}).get('network_ms', {}).get('p95')} | "
                f"{(change.get('window') or {}).get('write_collect_transaction_count')} |"
            )
        lines.append("")
    if not payload.get("sessions"):
        lines.append("No sessions with significant battery-voltage changes were found.\n")
    return "\n".join(lines).rstrip() + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyze battery-voltage freshness markers from cloud observability artifacts."
    )
    parser.add_argument(
        "--cloud-root",
        default=None,
        help="Cloud observability root or observability parent directory. Defaults to repo mirror.",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="Analyze one specific session id instead of auto-discovering sessions.",
    )
    parser.add_argument(
        "--min-delta-v",
        type=float,
        default=DEFAULT_MIN_DELTA_V,
        help="Minimum absolute battery-voltage delta to include in the report.",
    )
    parser.add_argument(
        "--window-ms",
        type=int,
        default=DEFAULT_WINDOW_MS,
        help="Centered context window around each change point for RTT/cache stats.",
    )
    parser.add_argument(
        "--json",
        default=None,
        help="Write JSON analysis to this file.",
    )
    parser.add_argument(
        "--report",
        default=None,
        help="Write Markdown report to this file.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON to stdout when no file output is requested.",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    payload = analyze_battery_voltage_freshness(
        args.cloud_root,
        session_id=args.session_id,
        min_delta_v=args.min_delta_v,
        window_ms=args.window_ms,
    )
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(generate_markdown_report(payload), encoding="utf-8")
    if not args.json and not args.report:
        indent = 2 if args.pretty else None
        print(json.dumps(payload, ensure_ascii=False, indent=indent, sort_keys=True))


if __name__ == "__main__":
    main(sys.argv[1:])
