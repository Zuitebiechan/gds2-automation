"""Structured benchmark helpers for proxy latency and throughput runs."""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path
from statistics import fmean
from typing import Any, Iterable

from .protocol import HEADER_SIZE, MSG_NAMES, MsgType, ProtocolDecoder

# Timing trailer: 4-byte magic + 8-byte double (hw_ms)
TIMING_MAGIC = 0x544D4530  # "TME0"
TIMING_TRAILER_SIZE = 12


def attach_timing_trailer(encoded_response: bytes, hw_ms: float) -> bytes:
    """Append a timing trailer to an encoded protocol response.

    The trailer is 12 bytes: 4-byte magic (TIMING_MAGIC) + 8-byte double (hw_ms).
    The header length field is updated to account for the extra bytes.
    """
    if len(encoded_response) < HEADER_SIZE:
        return encoded_response
    trailer = struct.pack(">Id", TIMING_MAGIC, hw_ms)
    # Update length field at offset 4 (4 bytes, big-endian unsigned int)
    old_length = struct.unpack(">I", encoded_response[4:8])[0]
    new_length = old_length + TIMING_TRAILER_SIZE
    return (
        encoded_response[:4]
        + struct.pack(">I", new_length)
        + encoded_response[8:]
        + trailer
    )


def strip_timing_trailer(body: bytes) -> tuple[bytes, float | None]:
    """Strip the timing trailer from a response body if present.

    Returns (clean_body, hw_ms).  hw_ms is None if no valid trailer found.
    """
    if len(body) < TIMING_TRAILER_SIZE:
        return body, None
    marker = struct.unpack(">I", body[-TIMING_TRAILER_SIZE:-8])[0]
    if marker != TIMING_MAGIC:
        return body, None
    hw_ms = struct.unpack(">d", body[-8:])[0]
    return body[:-TIMING_TRAILER_SIZE], hw_ms


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
    hw_ms: float | None = None,
) -> dict[str, Any]:
    """Build a normalized benchmark event for one proxied J2534 call.

    Args:
        hw_ms: Client-side J2534 hardware execution time in milliseconds.
               When provided, ``network_ms`` is computed as
               ``duration_ms - hw_ms`` (round-trip network transit only).
    """
    network_ms: float | None = None
    if hw_ms is not None and duration_ms > 0:
        network_ms = round(max(duration_ms - hw_ms, 0.0), 3)

    event = {
        "run_label": run_label,
        "source": source,
        "started_at_s": round(started_at_s, 6),
        "completed_at_s": round(started_at_s + (duration_ms / 1000.0), 6),
        "duration_ms": round(duration_ms, 3),
        "hw_ms": round(hw_ms, 3) if hw_ms is not None else None,
        "network_ms": network_ms,
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


def _build_latency_block(durations: list[float]) -> dict[str, float]:
    """Build a latency statistics block from a list of durations."""
    if not durations:
        return {"min": 0.0, "avg": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
    return {
        "min": round(min(durations), 3),
        "avg": round(fmean(durations), 3),
        "p50": _percentile(durations, 0.5),
        "p95": _percentile(durations, 0.95),
        "p99": _percentile(durations, 0.99),
        "max": round(max(durations), 3),
    }


def _build_message_summary(
    msg_rows: list[dict[str, Any]], window_s: float
) -> dict[str, Any]:
    """Build summary stats for a group of benchmark rows."""
    durations = [float(row["duration_ms"]) for row in msg_rows]
    message_count_total = int(
        sum(int(row.get("message_count", 0) or 0) for row in msg_rows)
    )

    hw_values = [float(row["hw_ms"]) for row in msg_rows if row.get("hw_ms") is not None]
    net_values = [float(row["network_ms"]) for row in msg_rows if row.get("network_ms") is not None]

    result: dict[str, Any] = {
        "count": len(msg_rows),
        "success_count": sum(1 for row in msg_rows if row.get("status") == "success"),
        "cache_hit_count": sum(1 for row in msg_rows if row.get("cache_hit")),
        "message_count_total": message_count_total,
        "payload_bytes_total": int(
            sum(int(row.get("payload_bytes", 0) or 0) for row in msg_rows)
        ),
        "request_rate_hz": round(len(msg_rows) / window_s, 3),
        "message_rate_hz": round(message_count_total / window_s, 3),
        "latency_ms": _build_latency_block(durations),
    }

    if hw_values:
        result["hw_ms"] = _build_latency_block(hw_values)
    if net_values:
        result["network_ms"] = _build_latency_block(net_values)

    return result


def summarize_benchmark_events(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Summarize one benchmark run into latency and throughput metrics.

    READ_MSGS_REQ events are additionally split into two sub-buckets:
    - ``READ_MSGS_REQ(empty)``: responses with ``message_count == 0``
      (BUFFER_EMPTY polls)
    - ``READ_MSGS_REQ(data)``: responses with ``message_count > 0``
      (actual vehicle data)

    When ``hw_ms`` is present on events, each message group also reports
    ``hw_ms`` and ``network_ms`` latency distribution blocks.
    """
    rows = sorted(
        (dict(event) for event in events), key=lambda row: row["started_at_s"]
    )
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

    summary: dict[str, Any] = {
        "label": label,
        "overall": {
            "event_count": len(rows),
            "window_s": round(window_s, 3),
            "success_count": sum(
                1 for row in rows if row.get("status") == "success"
            ),
            "cache_hit_count": sum(1 for row in rows if row.get("cache_hit")),
        },
        "by_message": {},
    }

    for msg_name, msg_rows in by_message.items():
        summary["by_message"][msg_name] = _build_message_summary(msg_rows, window_s)

        # READ_MSGS bucketing: split into empty vs data
        if msg_name == "READ_MSGS_REQ":
            empty_rows = [
                r for r in msg_rows if int(r.get("message_count", 0) or 0) == 0
            ]
            data_rows = [
                r for r in msg_rows if int(r.get("message_count", 0) or 0) > 0
            ]
            if empty_rows:
                summary["by_message"]["READ_MSGS_REQ(empty)"] = (
                    _build_message_summary(empty_rows, window_s)
                )
            if data_rows:
                summary["by_message"]["READ_MSGS_REQ(data)"] = (
                    _build_message_summary(data_rows, window_s)
                )

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


# ---------------------------------------------------------------------------
# Markdown report generation
# ---------------------------------------------------------------------------

def _latency_table_row(label: str, block: dict[str, float]) -> str:
    """Format one row in a latency markdown table."""
    return (
        f"| {label} "
        f"| {block['min']:.1f} "
        f"| {block['avg']:.1f} "
        f"| {block['p50']:.1f} "
        f"| {block['p95']:.1f} "
        f"| {block['p99']:.1f} "
        f"| {block['max']:.1f} |"
    )


_LATENCY_TABLE_HEADER = (
    "| Metric | min | avg | p50 | p95 | p99 | max |\n"
    "|--------|-----|-----|-----|-----|-----|-----|"
)


def generate_benchmark_report(
    summary: dict[str, Any],
    *,
    title: str = "VCI Proxy Benchmark Report",
) -> str:
    """Generate a Markdown benchmark report from a summary dict.

    The report includes:
    - Measurement methodology
    - Overall statistics
    - Per-message-type latency tables
    - READ_MSGS empty vs data bucketing
    - Network vs hardware latency breakdown (when available)
    - Cache effectiveness
    """
    lines: list[str] = []
    _a = lines.append  # shorthand

    label = summary.get("label") or "unnamed"
    overall = summary.get("overall", {})
    by_msg = summary.get("by_message", {})

    # --- Title ---
    _a(f"# {title}")
    _a("")
    _a(f"**Run label:** `{label}`")
    _a("")

    # --- Methodology ---
    _a("## Measurement Methodology")
    _a("")
    _a("Each J2534 API call made by the cloud-side OEM diagnostic software")
    _a("(e.g. GDS2) is intercepted at the Reverse Proxy Server and timed.")
    _a("")
    _a("```")
    _a("Cloud GDS2 ─► virtual_j2534.dll ─► localhost:9001")
    _a("                                        │")
    _a("                            ┌────────────▼────────────────┐")
    _a("                            │  ReverseProxyServer          │")
    _a("                            │  t_start ──────── t_end      │")
    _a("                            │     duration_ms (monotonic)  │")
    _a("                            └────────────┬────────────────┘")
    _a("                                         │  TCP tunnel")
    _a("                            ┌────────────▼────────────────┐")
    _a("                            │  ReverseProxyClient (local)  │")
    _a("                            │  hw_ms = J2534 driver time   │")
    _a("                            │  ─► VCI ─► Vehicle           │")
    _a("                            └─────────────────────────────┘")
    _a("```")
    _a("")
    _a("- **duration_ms**: Full round-trip measured with `time.monotonic()` at")
    _a("  the proxy server — includes network transit (both legs) plus")
    _a("  client-side J2534 hardware execution.")
    _a("- **hw_ms**: J2534 driver execution time measured at the client with")
    _a("  `time.monotonic()`. Reported only when the client sends a timing")
    _a("  trailer (12-byte `TME0` magic + double).")
    _a("- **network_ms**: `duration_ms − hw_ms` — pure network round-trip")
    _a("  transit time (cloud → local + local → cloud).")
    _a("- **Cache hits**: Certain high-frequency calls (ReadMsgs BUFFER_EMPTY,")
    _a("  duplicate StartFilter, READ_VBATT) are served from server-side cache")
    _a("  with `duration_ms = 0`.")
    _a("")

    # --- Overall ---
    _a("## Overall Statistics")
    _a("")
    event_count = overall.get("event_count", 0)
    success_count = overall.get("success_count", 0)
    cache_count = overall.get("cache_hit_count", 0)
    window_s = overall.get("window_s", 0.0)
    _a(f"| Metric | Value |")
    _a(f"|--------|-------|")
    _a(f"| Total events | {event_count} |")
    _a(f"| Successful | {success_count} ({_pct(success_count, event_count)}) |")
    _a(f"| Cache hits | {cache_count} ({_pct(cache_count, event_count)}) |")
    _a(f"| Measurement window | {window_s:.1f}s |")
    if event_count and window_s:
        _a(f"| Overall request rate | {event_count / window_s:.1f} req/s |")
    _a("")

    # --- Per-message-type ---
    _a("## Latency by Message Type")
    _a("")

    # Determine display order: regular types first, then buckets
    regular_names = sorted(
        n for n in by_msg if "(" not in n
    )
    bucket_names = sorted(
        n for n in by_msg if "(" in n
    )

    for msg_name in regular_names + bucket_names:
        stats = by_msg[msg_name]
        count = stats.get("count", 0)
        if count == 0:
            continue

        _a(f"### {msg_name}")
        _a("")
        _a(f"- **Requests:** {count}"
           f" (success: {stats.get('success_count', 0)},"
           f" cache: {stats.get('cache_hit_count', 0)})")
        msg_total = stats.get("message_count_total", 0)
        if msg_total:
            _a(f"- **J2534 messages:** {msg_total}"
               f" ({stats.get('message_rate_hz', 0):.1f} msg/s)")
        payload = stats.get("payload_bytes_total", 0)
        if payload:
            _a(f"- **Payload:** {payload:,} bytes")
        _a(f"- **Request rate:** {stats.get('request_rate_hz', 0):.1f} req/s")
        _a("")

        _a("**Round-trip latency (ms):**")
        _a("")
        _a(_LATENCY_TABLE_HEADER)
        _a(_latency_table_row("duration", stats["latency_ms"]))
        if "hw_ms" in stats:
            _a(_latency_table_row("hw (J2534)", stats["hw_ms"]))
        if "network_ms" in stats:
            _a(_latency_table_row("network", stats["network_ms"]))
        _a("")

    # --- READ_MSGS bucketing explanation ---
    has_empty = "READ_MSGS_REQ(empty)" in by_msg
    has_data = "READ_MSGS_REQ(data)" in by_msg
    if has_empty or has_data:
        _a("## READ_MSGS Bucketing")
        _a("")
        _a("GDS2 polls `PassThruReadMsgs` at high frequency. Most calls return")
        _a("`BUFFER_EMPTY` (no vehicle data). To separate signal from noise,")
        _a("READ_MSGS_REQ is split into two sub-buckets based on")
        _a("`message_count` in the response:")
        _a("")
        _a("- **READ_MSGS_REQ(empty)**: `message_count == 0` — polling noise")
        _a("- **READ_MSGS_REQ(data)**: `message_count > 0` — actual vehicle data")
        _a("")
        if has_empty and has_data:
            e = by_msg["READ_MSGS_REQ(empty)"]
            d = by_msg["READ_MSGS_REQ(data)"]
            total = e["count"] + d["count"]
            _a(f"| Bucket | Count | % of ReadMsgs | Avg latency |")
            _a(f"|--------|-------|---------------|-------------|")
            _a(f"| empty | {e['count']} | {_pct(e['count'], total)} "
               f"| {e['latency_ms']['avg']:.1f} ms |")
            _a(f"| data | {d['count']} | {_pct(d['count'], total)} "
               f"| {d['latency_ms']['avg']:.1f} ms |")
            _a("")

    # --- Network vs Hardware breakdown ---
    has_hw = any("hw_ms" in by_msg.get(n, {}) for n in by_msg)
    if has_hw:
        _a("## Network vs Hardware Latency")
        _a("")
        _a("When the client reports `hw_ms` (J2534 driver execution time),")
        _a("we can decompose the round-trip into network transit and")
        _a("hardware execution:")
        _a("")
        _a("| Message | Avg duration (no cache) | Avg hw | Avg network | Network % |")
        _a("|---------|------------------------|--------|-------------|-----------|")
        for msg_name in regular_names:
            stats = by_msg[msg_name]
            if "hw_ms" not in stats:
                continue
            hw_avg = stats["hw_ms"]["avg"]
            net_avg = stats["network_ms"]["avg"]
            # Use hw + network as the effective non-cached duration.
            # This avoids the distortion from cache hits (duration=0)
            # pulling down the overall avg duration.
            effective_dur = hw_avg + net_avg
            net_pct = f"{net_avg / effective_dur * 100:.0f}%" if effective_dur > 0 else "—"
            _a(f"| {msg_name} | {effective_dur:.1f} ms | {hw_avg:.1f} ms "
               f"| {net_avg:.1f} ms | {net_pct} |")
        _a("")

    # --- Cache effectiveness ---
    if cache_count > 0:
        _a("## Cache Effectiveness")
        _a("")
        _a("| Message | Total | Cache hits | Hit rate |")
        _a("|---------|-------|------------|----------|")
        for msg_name in regular_names:
            stats = by_msg[msg_name]
            ch = stats.get("cache_hit_count", 0)
            if ch > 0:
                _a(f"| {msg_name} | {stats['count']} | {ch} "
                   f"| {_pct(ch, stats['count'])} |")
        _a("")

    return "\n".join(lines)


def _pct(numerator: int, denominator: int) -> str:
    """Format a percentage string, guarding against division by zero."""
    if denominator == 0:
        return "—"
    return f"{numerator / denominator * 100:.1f}%"
