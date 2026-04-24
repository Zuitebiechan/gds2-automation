"""Compare two GDS2 A-B run directories collected by collect_gds2_ab_run.py."""

from __future__ import annotations

import argparse
import gzip
import json
import math
import re
import statistics
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


GDS2_LOG_RE = re.compile(r"^(?P<ts>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}): \[(?P<level>[^:]+):(?P<context>[^\]]+)\] (?P<message>.*)$")
SESSION_FILE_RE = re.compile(
    r"(?P<timestamp>\d{14})_(?P<vin>[^_]+)_(?P<module>[^_]+)_(?P<application>[^_]+)_(?P<machine>[^.]+)\.(?P<ext>bin|zip)$",
    re.IGNORECASE,
)
DLL_LINE_RE = re.compile(r"<< (?P<method>[A-Za-z0-9_()]+).*?\((?P<duration_ms>\d+(?:\.\d+)?)ms\)")
DLL_READ_MSGS_RE = re.compile(r"<< ReadMsgs\(.*?\) -> (?P<status>[^,]+), msgs=(?P<message_count>\d+) \((?P<duration_ms>\d+(?:\.\d+)?)ms\)")
KEYWORDS = {
    "disconnect": "Disconnect",
    "vci_status": "Vci status",
    "vin_read": "VIN Read",
    "vin_parameter_set": "VIN Parameter has been set",
    "waiting_for_data": "Waiting for Data",
    "data_display": "Data Display",
    "dtc_display": "DTC Display",
    "decode_error": "No function is set error information to decode",
    "option_missing": "Could not find option",
    "tis2web_error": "Error connecting to tis2web",
    "j2534": "J2534",
    "vci": "VCI",
}


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_local_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return None


def _percentile(values: list[float], ratio: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * ratio) - 1))
    return round(float(ordered[index]), 3)


def _latency_block(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"count": 0, "min": None, "avg": None, "p50": None, "p95": None, "p99": None, "max": None}
    return {
        "count": len(values),
        "min": round(min(values), 3),
        "avg": round(statistics.fmean(values), 3),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
        "max": round(max(values), 3),
    }


def _read_manifest(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))


def _artifact_paths(run_dir: Path, manifest: dict[str, Any], *, suffixes: tuple[str, ...] | None = None) -> list[Path]:
    paths: list[Path] = []
    for item in manifest.get("captured_artifacts") or []:
        rel = item.get("artifact_path")
        if not rel:
            continue
        path = run_dir / rel
        if not path.exists() or not path.is_file():
            continue
        if suffixes and not any(path.name.lower().endswith(suffix.lower()) for suffix in suffixes):
            continue
        paths.append(path)
    return sorted(paths)


def _iter_text_lines(path: Path) -> Iterable[str]:
    if path.suffix.lower() == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
            yield from handle
        return
    with path.open("rt", encoding="utf-8", errors="replace") as handle:
        yield from handle


def _iter_jsonl_events(paths: Iterable[Path]) -> Iterable[dict[str, Any]]:
    for path in paths:
        for line_number, line in enumerate(_iter_text_lines(path), start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload["_artifact_path"] = str(path)
            payload["_source_line"] = line_number
            yield payload


def _is_abnormal(event: dict[str, Any]) -> bool:
    return str(event.get("status") or "").lower() == "error" or event.get("failure_code") not in (None, "")


def _counter_to_dict(counter: Counter) -> dict[str, int]:
    return {str(key): int(value) for key, value in counter.most_common()}


def _summarize_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    proxy_rows = [
        event for event in events
        if event.get("component") == "reverse_server"
        and event.get("event_type") == "proxy.request.response_received"
    ]
    by_msg_values: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for event in proxy_rows:
        msg_name = str(event.get("msg_name") or event.get("operation_kind") or "unknown")
        for key in ("duration_ms", "hw_ms", "network_ms"):
            if event.get(key) not in (None, ""):
                by_msg_values[msg_name][key].append(float(event[key]))

    proxy_by_message = {
        msg_name: {
            metric_name: _latency_block(values)
            for metric_name, values in metric_values.items()
        }
        for msg_name, metric_values in sorted(by_msg_values.items())
    }

    cache_decisions = [
        event for event in events
        if event.get("component") == "reverse_server"
        and event.get("event_type") == "proxy.request.cache_decision"
    ]
    cache_hits = sum(1 for event in cache_decisions if event.get("reason") == "cache_hit" or event.get("cache_hit") is True)
    cache_misses = sum(1 for event in cache_decisions if event.get("reason") == "cache_miss" or event.get("cache_hit") is False)

    tunnel_events = [
        event for event in events
        if str(event.get("event_type") or "").startswith("tunnel.")
        or event.get("operation_kind") == "reverse_tunnel"
    ]

    return {
        "event_count": len(events),
        "error_count": sum(1 for event in events if _is_abnormal(event)),
        "component_counts": _counter_to_dict(Counter(event.get("component") or "unknown" for event in events)),
        "event_type_counts": _counter_to_dict(Counter(event.get("event_type") or "unknown" for event in events)),
        "failure_domain_counts": _counter_to_dict(
            Counter(
                event.get("failure_domain") or "unknown"
                for event in events
                if _is_abnormal(event)
            )
        ),
        "failure_code_counts": _counter_to_dict(
            Counter(str(event.get("failure_code")) for event in events if event.get("failure_code") not in (None, ""))
        ),
        "proxy": {
            "response_count": len(proxy_rows),
            "cache_hit_count": cache_hits,
            "cache_miss_count": cache_misses,
            "by_message": proxy_by_message,
        },
        "tunnel": {
            "event_count": len(tunnel_events),
            "quality_changes": [
                {
                    "ts": event.get("ts"),
                    "grade": event.get("network_grade"),
                    "status": event.get("tunnel_status"),
                    "reason": event.get("reason"),
                    "connection_epoch": event.get("connection_epoch"),
                }
                for event in tunnel_events
                if event.get("event_type") == "tunnel.quality.changed"
            ][-10:],
        },
    }


def _parse_gds2_logs(paths: Iterable[Path]) -> dict[str, Any]:
    level_counts: Counter = Counter()
    keyword_counts: Counter = Counter()
    sample_errors: list[str] = []
    line_count = 0
    for path in paths:
        name = path.name.lower()
        if not ("gds2errors" in name or "servicelogiqerrors" in name or "hardwareusagelog" in name):
            continue
        for line in _iter_text_lines(path):
            line_count += 1
            match = GDS2_LOG_RE.match(line.strip())
            if match:
                level = str(match.group("level")).strip()
                level_counts[level] += 1
                if level.lower().startswith("error") and len(sample_errors) < 20:
                    sample_errors.append(line.strip())
            lower = line.lower()
            for key, needle in KEYWORDS.items():
                if needle.lower() in lower:
                    keyword_counts[key] += 1
    return {
        "line_count": line_count,
        "level_counts": _counter_to_dict(level_counts),
        "keyword_counts": _counter_to_dict(keyword_counts),
        "sample_errors": sample_errors,
    }


def _parse_session_file_name(path: Path) -> dict[str, Any] | None:
    match = SESSION_FILE_RE.match(path.name)
    if not match:
        return None
    data = match.groupdict()
    timestamp_text = data["timestamp"]
    try:
        dt = datetime.strptime(timestamp_text, "%Y%m%d%H%M%S")
    except ValueError:
        dt = None
    return {
        "timestamp": timestamp_text,
        "datetime_local": dt.isoformat() if dt else None,
        "vin": data["vin"],
        "module": data["module"],
        "application": data["application"],
        "machine": data["machine"],
        "file": str(path),
    }


def _parse_summary_sessions(path: Path, *, start_local: datetime | None, end_local: datetime | None) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for session in root.iter("Session"):
        timestamp = str(session.attrib.get("Timestamp") or "")
        dt = None
        if timestamp:
            try:
                dt = datetime.strptime(timestamp, "%Y%m%d%H%M%S")
            except ValueError:
                dt = None
        if dt is not None:
            if start_local and dt < start_local:
                continue
            if end_local and dt > end_local:
                continue
        header = session.find("Header")
        row = {
            "timestamp": timestamp,
            "datetime_local": dt.isoformat() if dt else None,
            "vin": str(session.attrib.get("FileName") or "").split("_")[1] if "_" in str(session.attrib.get("FileName") or "") else None,
            "module": None,
            "application": None,
            "machine": None,
            "release": session.attrib.get("Release"),
            "session_file": session.attrib.get("Path", "") + "/" + session.attrib.get("FileName", ""),
        }
        if header is not None:
            for key in ("Make", "Model", "Module", "Application", "Machine", "Year", "EngYear"):
                element = header.find(key)
                if element is not None and element.text is not None:
                    row[key[:1].lower() + key[1:]] = element.text
        rows.append(row)
    return rows


def _summarize_gds2_artifacts(run_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    paths = _artifact_paths(run_dir, manifest)
    start_local = _parse_local_dt(manifest.get("started_at_local"))
    end_local = _parse_local_dt(manifest.get("finished_at_local"))
    session_files = []
    for path in paths:
        parsed = _parse_session_file_name(path)
        if parsed:
            session_files.append(parsed)

    summary_sessions = []
    for path in paths:
        if path.suffix.lower() in {".vsf", ".xml"}:
            summary_sessions.extend(_parse_summary_sessions(path, start_local=start_local, end_local=end_local))

    latest_snapshots = []
    for path in paths:
        if path.name.lower() in {"latest.json", "result.json", "tunnel_quality.json", "active_session_snapshot.json"}:
            try:
                latest_snapshots.append({"name": path.name, "path": str(path), "payload": json.loads(path.read_text(encoding="utf-8"))})
            except Exception:
                continue

    return {
        "logs": _parse_gds2_logs(paths),
        "session_file_count": len(session_files),
        "session_files": session_files[-50:],
        "summary_session_count": len(summary_sessions),
        "summary_sessions": summary_sessions[-50:],
        "snapshots": latest_snapshots,
    }


def _parse_virtual_dll_log(paths: Iterable[Path]) -> dict[str, Any]:
    durations_by_method: dict[str, list[float]] = defaultdict(list)
    read_empty = 0
    read_data = 0
    for path in paths:
        if path.name.lower() not in {"vci_proxy_dll.log", "vci_proxy_dll.log.old"}:
            continue
        for line in _iter_text_lines(path):
            read_match = DLL_READ_MSGS_RE.search(line)
            if read_match:
                count = int(read_match.group("message_count"))
                duration = float(read_match.group("duration_ms"))
                durations_by_method["ReadMsgs"].append(duration)
                if count > 0:
                    read_data += 1
                else:
                    read_empty += 1
                continue
            match = DLL_LINE_RE.search(line)
            if match:
                durations_by_method[match.group("method")].append(float(match.group("duration_ms")))
    return {
        "by_method": {method: _latency_block(values) for method, values in sorted(durations_by_method.items())},
        "read_msgs_data_count": read_data,
        "read_msgs_empty_count": read_empty,
    }


def summarize_run(run_dir: Path) -> dict[str, Any]:
    manifest = _read_manifest(run_dir)
    jsonl_paths = _artifact_paths(run_dir, manifest, suffixes=(".jsonl", ".jsonl.gz", ".gz"))
    events = list(_iter_jsonl_events(jsonl_paths))
    all_paths = _artifact_paths(run_dir, manifest)
    return {
        "run_dir": str(run_dir),
        "manifest": {
            key: manifest.get(key)
            for key in (
                "run_id",
                "mode",
                "label",
                "scenario",
                "vehicle",
                "vin",
                "session_id",
                "connection_epoch",
                "started_at_utc",
                "finished_at_utc",
            )
        },
        "artifact_count": len(manifest.get("captured_artifacts") or []),
        "events": _summarize_events(events),
        "gds2": _summarize_gds2_artifacts(run_dir, manifest),
        "virtual_dll": _parse_virtual_dll_log(all_paths),
    }


def _get_path(payload: dict[str, Any], path: str, default: Any = None) -> Any:
    value: Any = payload
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return default
        value = value[part]
    return value


def _delta(candidate: Any, baseline: Any) -> Any:
    if candidate is None or baseline is None:
        return None
    try:
        return round(float(candidate) - float(baseline), 3)
    except (TypeError, ValueError):
        return None


def build_comparison(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    baseline_network = _get_path(baseline, "events.proxy.by_message.READ_MSGS_REQ.network_ms.p95")
    candidate_network = _get_path(candidate, "events.proxy.by_message.READ_MSGS_REQ.network_ms.p95")
    if candidate_network is None:
        candidate_network = _get_path(candidate, "events.proxy.by_message.PING_REQ.network_ms.p95")

    cloud_failure_domains = candidate["events"].get("failure_domain_counts", {})
    cloud_specific_failures = {
        key: value
        for key, value in cloud_failure_domains.items()
        if key in {"cloud_proxy_tunnel", "cloud_dll_local_proxy", "local_reverse_client", "local_worker_rpc"}
    }

    baseline_sessions = {
        (row.get("vin"), row.get("module"), row.get("application"), row.get("machine"))
        for row in baseline["gds2"].get("summary_sessions", []) + baseline["gds2"].get("session_files", [])
    }
    candidate_sessions = {
        (row.get("vin"), row.get("module"), row.get("application"), row.get("machine"))
        for row in candidate["gds2"].get("summary_sessions", []) + candidate["gds2"].get("session_files", [])
    }
    common_sessions = sorted(item for item in baseline_sessions & candidate_sessions if any(item))

    findings: list[dict[str, str]] = []
    if candidate_network is not None:
        if float(candidate_network) <= 80:
            findings.append({"severity": "info", "area": "network", "message": "Cloud tunnel p95 is within the good threshold."})
        elif float(candidate_network) <= 150:
            findings.append({"severity": "warn", "area": "network", "message": "Cloud tunnel p95 is in the warn range; expect visible slowdown."})
        else:
            findings.append({"severity": "high", "area": "network", "message": "Cloud tunnel p95 exceeds the block threshold; business reliability is at risk."})
    else:
        findings.append({"severity": "warn", "area": "network", "message": "No cloud proxy network_ms p95 was available in the candidate run."})

    if cloud_specific_failures:
        findings.append({"severity": "high", "area": "cloud_path", "message": f"Cloud-specific failure domains appeared: {cloud_specific_failures}."})
    if not common_sessions:
        findings.append({"severity": "warn", "area": "functional_equivalence", "message": "No matching GDS2 native session records were found between runs."})

    baseline_error_count = baseline["events"].get("error_count", 0) + sum(baseline["gds2"]["logs"].get("level_counts", {}).values())
    candidate_error_count = candidate["events"].get("error_count", 0) + sum(candidate["gds2"]["logs"].get("level_counts", {}).values())
    if candidate_error_count > baseline_error_count:
        findings.append({"severity": "warn", "area": "errors", "message": "Candidate run produced more structured/GDS2 log errors than baseline."})

    return {
        "baseline_run_id": baseline["manifest"].get("run_id"),
        "candidate_run_id": candidate["manifest"].get("run_id"),
        "network_p95_delta_ms": _delta(candidate_network, baseline_network),
        "baseline_network_p95_ms": baseline_network,
        "candidate_network_p95_ms": candidate_network,
        "common_gds2_sessions": common_sessions[:25],
        "cloud_specific_failures": cloud_specific_failures,
        "event_error_delta": int(candidate["events"].get("error_count", 0)) - int(baseline["events"].get("error_count", 0)),
        "gds2_error_keyword_delta": {
            key: int(candidate["gds2"]["logs"].get("keyword_counts", {}).get(key, 0))
            - int(baseline["gds2"]["logs"].get("keyword_counts", {}).get(key, 0))
            for key in sorted(set(baseline["gds2"]["logs"].get("keyword_counts", {})) | set(candidate["gds2"]["logs"].get("keyword_counts", {})))
        },
        "findings": findings,
    }


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    return str(value)


def generate_markdown_report(payload: dict[str, Any]) -> str:
    baseline = payload["baseline"]
    candidate = payload["candidate"]
    comparison = payload["comparison"]
    lines = [
        "# GDS2 Local vs Cloud Comparison",
        "",
        "## Runs",
        "",
        "| Role | Run ID | Mode | Label | Scenario | Session |",
        "| --- | --- | --- | --- | --- | --- |",
        f"| Baseline | {_fmt(baseline['manifest'].get('run_id'))} | {_fmt(baseline['manifest'].get('mode'))} | {_fmt(baseline['manifest'].get('label'))} | {_fmt(baseline['manifest'].get('scenario'))} | {_fmt(baseline['manifest'].get('session_id'))} |",
        f"| Candidate | {_fmt(candidate['manifest'].get('run_id'))} | {_fmt(candidate['manifest'].get('mode'))} | {_fmt(candidate['manifest'].get('label'))} | {_fmt(candidate['manifest'].get('scenario'))} | {_fmt(candidate['manifest'].get('session_id'))} |",
        "",
        "## Verdict Signals",
        "",
    ]
    for finding in comparison["findings"]:
        lines.append(f"- **{finding['severity']} / {finding['area']}**: {finding['message']}")
    if not comparison["findings"]:
        lines.append("- No high-level findings were generated.")

    lines.extend(
        [
            "",
            "## Network And Proxy",
            "",
            "| Metric | Baseline | Candidate | Delta |",
            "| --- | ---: | ---: | ---: |",
            f"| READ_MSGS/PING network p95 ms | {_fmt(comparison['baseline_network_p95_ms'])} | {_fmt(comparison['candidate_network_p95_ms'])} | {_fmt(comparison['network_p95_delta_ms'])} |",
            f"| Structured event errors | {_fmt(baseline['events'].get('error_count'))} | {_fmt(candidate['events'].get('error_count'))} | {_fmt(comparison['event_error_delta'])} |",
            f"| Candidate cache hits | n/a | {_fmt(candidate['events']['proxy'].get('cache_hit_count'))} | n/a |",
            "",
            "## Failure Domains",
            "",
            "Baseline:",
            "",
            "```json",
            json.dumps(baseline["events"].get("failure_domain_counts", {}), ensure_ascii=False, indent=2),
            "```",
            "",
            "Candidate:",
            "",
            "```json",
            json.dumps(candidate["events"].get("failure_domain_counts", {}), ensure_ascii=False, indent=2),
            "```",
            "",
            "## GDS2 Native Evidence",
            "",
            f"- Baseline GDS2 session records: {baseline['gds2'].get('session_file_count', 0)} files, {baseline['gds2'].get('summary_session_count', 0)} summary entries.",
            f"- Candidate GDS2 session records: {candidate['gds2'].get('session_file_count', 0)} files, {candidate['gds2'].get('summary_session_count', 0)} summary entries.",
            f"- Matching session tuples found: {len(comparison.get('common_gds2_sessions') or [])}.",
            "",
            "GDS2 keyword deltas, candidate minus baseline:",
            "",
            "```json",
            json.dumps(comparison["gds2_error_keyword_delta"], ensure_ascii=False, indent=2),
            "```",
            "",
            "## Method",
            "",
            "- Functional equivalence is inferred from GDS2 native SessionLogs/SummaryFiles plus final Java-agent snapshots.",
            "- Cloud-specific overhead is inferred from `reverse_server` `proxy.request.response_received` events using `network_ms = duration_ms - hw_ms`.",
            "- Cloud-specific risk is inferred from failure domains such as `cloud_proxy_tunnel`, `local_reverse_client`, `local_worker_rpc`, and `cloud_dll_local_proxy`.",
        ]
    )
    return "\n".join(lines) + "\n"


def compare_runs(baseline_dir: Path, candidate_dir: Path) -> dict[str, Any]:
    baseline = summarize_run(baseline_dir)
    candidate = summarize_run(candidate_dir)
    return {
        "baseline": baseline,
        "candidate": candidate,
        "comparison": build_comparison(baseline, candidate),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare collected GDS2 A-B run artifacts.")
    parser.add_argument("--baseline", required=True, help="Baseline run directory, usually local direct GDS2")
    parser.add_argument("--candidate", required=True, help="Candidate run directory, usually cloud GDS2")
    parser.add_argument("--json", default=None, help="Write full JSON analysis to this file")
    parser.add_argument("--report", default=None, help="Write Markdown report to this file")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON to stdout")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    payload = compare_runs(Path(args.baseline), Path(args.candidate))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(generate_markdown_report(payload), encoding="utf-8")
    if not args.json and not args.report:
        indent = 2 if args.pretty else None
        print(json.dumps(payload["comparison"], ensure_ascii=False, indent=indent, sort_keys=True))


if __name__ == "__main__":
    main(sys.argv[1:])
