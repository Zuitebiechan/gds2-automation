"""Structured benchmark helpers for proxy latency and throughput runs."""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path
from statistics import fmean
from typing import Any, Iterable

from .protocol import HEADER_SIZE, MSG_NAMES, MsgType, ProtocolDecoder


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)

    rank = (len(ordered) - 1) * percentile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return round(ordered[lower], 3)

    lower_value = ordered[lower]
    upper_value = ordered[upper]
    blended = lower_value + ((upper_value - lower_value) * (rank - lower))
    return round(blended, 3)


def _decode_channel_id(msg_type: int, req_body: bytes) -> int | None:
    try:
        if msg_type in (
            MsgType.CLOSE_REQ,
            MsgType.DISCONNECT_REQ,
            MsgType.READ_VERSION_REQ,
            MsgType.CONNECT_REQ,
            MsgType.READ_MSGS_REQ,
            MsgType.WRITE_MSGS_REQ,
            MsgType.IOCTL_REQ,
            MsgType.START_FILTER_REQ,
            MsgType.STOP_FILTER_REQ,
        ) and len(req_body) >= 4:
            return struct.unpack(">I", req_body[:4])[0]
    except struct.error:
        return None
    return None


def _decode_response_metrics(resp_type: int | None, resp_body: bytes) -> dict[str, Any]:
    if resp_type is None:
        return {}

    metrics: dict[str, Any] = {}
    try:
        if resp_type == MsgType.READ_MSGS_RSP:
            return_code, messages = ProtocolDecoder.decode_read_msgs_rsp(resp_body)
            metrics["return_code"] = return_code
            metrics["message_count"] = len(messages)
            metrics["payload_bytes"] = sum(len(msg.get("data", b"")) for msg in messages)
        elif resp_type == MsgType.WRITE_MSGS_RSP:
            return_code, num_written = ProtocolDecoder.decode_write_msgs_rsp(resp_body)
            metrics["return_code"] = return_code
            metrics["message_count"] = num_written
        elif resp_type == MsgType.CONNECT_RSP:
            return_code, result_channel_id = ProtocolDecoder.decode_connect_rsp(resp_body)
            metrics["return_code"] = return_code
            metrics["result_channel_id"] = result_channel_id
        elif resp_type == MsgType.OPEN_RSP:
            return_code, device_id = ProtocolDecoder.decode_open_rsp(resp_body)
            metrics["return_code"] = return_code
            metrics["device_id"] = device_id
        elif resp_type == MsgType.CLOSE_RSP:
            metrics["return_code"] = ProtocolDecoder.decode_close_rsp(resp_body)
        elif resp_type == MsgType.DISCONNECT_RSP:
            metrics["return_code"] = ProtocolDecoder.decode_disconnect_rsp(resp_body)
        elif resp_type == MsgType.READ_VERSION_RSP:
            return_code, firmware_version, dll_version, api_version = (
                ProtocolDecoder.decode_read_version_rsp(resp_body)
            )
            metrics["return_code"] = return_code
            metrics["firmware_version"] = firmware_version
            metrics["dll_version"] = dll_version
            metrics["api_version"] = api_version
        elif resp_type == MsgType.START_FILTER_RSP:
            return_code, filter_id = ProtocolDecoder.decode_start_filter_rsp(resp_body)
            metrics["return_code"] = return_code
            metrics["filter_id"] = filter_id
        elif resp_type == MsgType.STOP_FILTER_RSP:
            metrics["return_code"] = ProtocolDecoder.decode_stop_filter_rsp(resp_body)
        elif resp_type == MsgType.IOCTL_RSP:
            return_code, output_data = ProtocolDecoder.decode_ioctl_rsp(resp_body)
            metrics["return_code"] = return_code
            metrics["payload_bytes"] = len(output_data or b"")
    except Exception:
        metrics["decode_error"] = True

    return metrics


def make_proxy_benchmark_event(
    *,
    run_label: str,
    source: str,
    started_at_s: float,
    duration_ms: float,
    msg_type: int,
    req_body: bytes,
    resp_type: int | None,
    resp_body: bytes,
    cache_hit: bool,
    status: str,
) -> dict[str, Any]:
    """Build a normalized benchmark event for one proxied J2534 call."""
    event = {
        "run_label": run_label,
        "source": source,
        "started_at_s": round(started_at_s, 6),
        "completed_at_s": round(started_at_s + (duration_ms / 1000.0), 6),
        "duration_ms": round(duration_ms, 3),
        "msg_type": int(msg_type),
        "msg_name": MSG_NAMES.get(msg_type, f"0x{int(msg_type):04x}"),
        "resp_type": int(resp_type) if resp_type is not None else None,
        "resp_name": MSG_NAMES.get(resp_type, f"0x{int(resp_type):04x}") if resp_type is not None else None,
        "status": status,
        "cache_hit": cache_hit,
        "channel_id": _decode_channel_id(int(msg_type), req_body),
    }
    event.update(_decode_response_metrics(resp_type, resp_body))
    return event


def summarize_benchmark_events(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Summarize one benchmark run into latency and throughput metrics."""
    rows = sorted((dict(event) for event in events), key=lambda row: row["started_at_s"])
    if not rows:
        return {
            "label": None,
            "overall": {"event_count": 0, "window_s": 0.0},
            "by_message": {},
        }

    label = rows[0].get("run_label")
    window_s = max(rows[-1]["started_at_s"] - rows[0]["started_at_s"], 1.0)
    by_message: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_message.setdefault(str(row["msg_name"]), []).append(row)

    summary = {
        "label": label,
        "overall": {
            "event_count": len(rows),
            "window_s": round(window_s, 3),
            "success_count": sum(1 for row in rows if row.get("status") == "success"),
            "cache_hit_count": sum(1 for row in rows if row.get("cache_hit")),
        },
        "by_message": {},
    }

    for msg_name, msg_rows in by_message.items():
        durations = [float(row["duration_ms"]) for row in msg_rows]
        message_count_total = int(sum(int(row.get("message_count", 0) or 0) for row in msg_rows))
        summary["by_message"][msg_name] = {
            "count": len(msg_rows),
            "success_count": sum(1 for row in msg_rows if row.get("status") == "success"),
            "cache_hit_count": sum(1 for row in msg_rows if row.get("cache_hit")),
            "message_count_total": message_count_total,
            "payload_bytes_total": int(sum(int(row.get("payload_bytes", 0) or 0) for row in msg_rows)),
            "request_rate_hz": round(len(msg_rows) / window_s, 3),
            "message_rate_hz": round(message_count_total / window_s, 3),
            "latency_ms": {
                "min": round(min(durations), 3),
                "avg": round(fmean(durations), 3),
                "p50": _percentile(durations, 0.5),
                "p95": _percentile(durations, 0.95),
                "p99": _percentile(durations, 0.99),
                "max": round(max(durations), 3),
            },
        }

    return summary


def compare_benchmark_summaries(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Compare a candidate run against a baseline summary."""
    comparison = {
        "baseline_label": baseline.get("label"),
        "candidate_label": candidate.get("label"),
        "by_message": {},
    }

    all_msg_names = sorted(
        set(baseline.get("by_message", {})).union(candidate.get("by_message", {}))
    )
    for msg_name in all_msg_names:
        base_row = baseline.get("by_message", {}).get(msg_name, {})
        cand_row = candidate.get("by_message", {}).get(msg_name, {})
        base_latency = base_row.get("latency_ms", {})
        cand_latency = cand_row.get("latency_ms", {})
        comparison["by_message"][msg_name] = {
            "baseline_label": baseline.get("label"),
            "candidate_label": candidate.get("label"),
            "baseline_count": base_row.get("count", 0),
            "candidate_count": cand_row.get("count", 0),
            "request_rate_hz_delta": round(
                float(cand_row.get("request_rate_hz", 0.0)) - float(base_row.get("request_rate_hz", 0.0)),
                3,
            ),
            "message_rate_hz_delta": round(
                float(cand_row.get("message_rate_hz", 0.0)) - float(base_row.get("message_rate_hz", 0.0)),
                3,
            ),
            "latency_ms_delta": {
                key: round(float(cand_latency.get(key, 0.0)) - float(base_latency.get(key, 0.0)), 3)
                for key in ("min", "avg", "p50", "p95", "p99", "max")
            },
        }

    return comparison


class JsonlBenchmarkWriter:
    """Append benchmark events to a JSONL file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write_event(self, event: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False))
            handle.write("\n")


def load_benchmark_events(path: str | Path) -> list[dict[str, Any]]:
    """Read benchmark events from a JSONL file."""
    rows: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def decode_benchmark_response(encoded_response: bytes) -> tuple[int, bytes]:
    """Extract msg_type and body from an encoded protocol response."""
    if len(encoded_response) < HEADER_SIZE:
        raise ValueError("Encoded response shorter than header")
    _, _, msg_type, _ = struct.unpack(">IIHI", encoded_response[:HEADER_SIZE])
    return msg_type, encoded_response[HEADER_SIZE:]
