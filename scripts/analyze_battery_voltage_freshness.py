"""Analyze focused Data Display value freshness from cloud observability artifacts."""

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
DEFAULT_MIN_DELTA_BY_FOCUS_KEY: dict[str, float] = {
    "battery_voltage": DEFAULT_MIN_DELTA_V,
}
FOCUS_KEY_REPORT_LABELS: dict[str, str] = {
    "battery_voltage": "Battery Voltage",
    "engine_speed": "Engine Speed",
    "accelerator_pedal_position": "Accelerator Pedal Position",
}
FOCUS_KEY_REPORT_UNITS: dict[str, str] = {
    "battery_voltage": "V",
    "engine_speed": "RPM",
    "accelerator_pedal_position": "%",
}
FOCUS_KEY_SWEEP_IDENTIFIERS: dict[str, tuple[str, int]] = {
    "engine_speed": ("uds_did", 0x000C),
}
SLOW_FOCUS_SWEEP_CADENCE_MS = 5000.0


def _normalize_cloud_root(path_like: str | Path | None) -> Path:
    if path_like is None:
        return ROOT / "vci_proxy" / "cloud_mirror" / "observability" / "cloud"
    path = Path(path_like)
    if path.name == "cloud":
        return path
    if (path / "observability" / "cloud").exists():
        return path / "observability" / "cloud"
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


def _flatten_numbers(value: Any) -> list[float]:
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, (list, tuple)):
        values: list[float] = []
        for item in value:
            values.extend(_flatten_numbers(item))
        return values
    return []


def _counter_block(values: Iterable[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = str(value if value not in (None, "") else "unknown")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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


def _iter_cloud_raw_events(cloud_root: Path) -> Iterable[dict[str, Any]]:
    raw_dir = cloud_root / "raw"
    if not raw_dir.exists():
        return
    for path in sorted(raw_dir.glob("*.jsonl")) + sorted(raw_dir.glob("*.jsonl.gz")):
        opener = open
        if path.suffix == ".gz":
            import gzip

            opener = gzip.open
        with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload.setdefault("source_artifact", str(path))
                payload.setdefault("source_line", line_number)
                yield payload


def _event_identity(event: dict[str, Any]) -> tuple[Any, ...]:
    if event.get("source_artifact") and event.get("source_line"):
        return (event.get("source_artifact"), event.get("source_line"))
    return (
        event.get("ts"),
        event.get("component"),
        event.get("event_type"),
        event.get("session_id"),
        event.get("connection_epoch"),
        event.get("dll_seq"),
        event.get("proxy_seq"),
        event.get("worker_request_id"),
        event.get("reason"),
    )


def _include_epoch_only_events(
    cloud_root: Path,
    timeline: list[dict[str, Any]],
    *,
    connection_epoch: str | None,
) -> list[dict[str, Any]]:
    epoch = str(connection_epoch or "").strip()
    if not epoch:
        return timeline
    merged = list(timeline)
    seen = {_event_identity(event) for event in merged}
    raw_events = list(_iter_cloud_raw_events(cloud_root))
    for event in raw_events:
        if str(event.get("connection_epoch") or "") != epoch:
            continue
        identity = _event_identity(event)
        if identity in seen:
            continue
        seen.add(identity)
        merged.append(event)
    event_times = [
        ts
        for ts in (_parse_ts(event.get("ts")) for event in merged)
        if ts is not None
    ]
    if event_times:
        start_ts = min(event_times)
        end_ts = max(event_times)
        startup_candidates = [
            event
            for event in raw_events
            if event.get("component") == "reverse_server"
            and event.get("event_type")
            in {"process.lifecycle.started", "sweep.config.warning"}
            and event.get("connection_epoch") in (None, "", "no-epoch")
            and (ts := _parse_ts(event.get("ts"))) is not None
            and ts <= end_ts
            and (start_ts - timedelta(minutes=10)) <= ts
        ]
        for event in startup_candidates:
            identity = _event_identity(event)
            if identity in seen:
                continue
            seen.add(identity)
            merged.append(event)
    return sorted(
        merged,
        key=lambda event: _parse_ts(event.get("ts"))
        or datetime.min.replace(tzinfo=timezone.utc),
    )


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
    cache_counts: dict[str, int] = {}
    prefetch_miss_detail_counts: dict[str, int] = {}
    read_collect_blocked_counts: dict[str, int] = {}
    for event in events:
        if event.get("event_type") != "proxy.request.cache_decision":
            continue
        reason = str(event.get("reason") or "")
        cache_counts[reason] = cache_counts.get(reason, 0) + 1
        if reason == "prefetch_miss":
            detail = str(event.get("prefetch_miss_detail") or "unknown")
            prefetch_miss_detail_counts[detail] = (
                prefetch_miss_detail_counts.get(detail, 0) + 1
            )
        blocked = str(event.get("read_collect_blocked_reason") or "")
        if blocked:
            read_collect_blocked_counts[blocked] = (
                read_collect_blocked_counts.get(blocked, 0) + 1
            )
    write_collect_transaction_count = sum(
        1
        for event in events
        if event.get("event_type") == "proxy.request.forwarded_to_tunnel"
        and str(event.get("reason") or "") == "write_collect_transaction"
    )
    active_replay_armed_count = sum(
        1
        for event in events
        if event.get("event_type") == "proxy.request.active_replay_armed"
    )
    active_replay_served_count = sum(
        1
        for event in events
        if event.get("event_type") == "proxy.request.active_replay_served"
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
        "active_replay_armed_count": active_replay_armed_count,
        "active_replay_served_count": active_replay_served_count,
        "forwarded_to_tunnel_count": forwarded_to_tunnel_count,
        "cache_decision_counts": cache_counts,
        "prefetch_miss_detail_counts": prefetch_miss_detail_counts,
        "read_collect_blocked_counts": read_collect_blocked_counts,
    }


def _focus_key_label(focus_key: str) -> str:
    return FOCUS_KEY_REPORT_LABELS.get(focus_key, focus_key.replace("_", " ").title())


def _focus_key_unit(focus_key: str) -> str:
    return FOCUS_KEY_REPORT_UNITS.get(focus_key, "")


def _focus_key_changes_key(focus_key: str) -> str:
    return f"{focus_key}_changes"


def _focus_sweep_identifier(focus_key: str) -> tuple[str, int] | None:
    return FOCUS_KEY_SWEEP_IDENTIFIERS.get(focus_key)


def _matches_focus_sweep(event: dict[str, Any], focus_key: str) -> bool:
    identifier = _focus_sweep_identifier(focus_key)
    if identifier is None:
        return False
    expected_kind, expected_value = identifier
    return (
        str(event.get("sweep_identifier_kind") or "") == expected_kind
        and int(event.get("sweep_identifier") or -1) == expected_value
    )


def _summarize_focus_sweep(
    timeline: list[dict[str, Any]],
    *,
    focus_key: str,
) -> dict[str, Any]:
    identifier = _focus_sweep_identifier(focus_key)
    if identifier is None:
        return {
            "supported": False,
            "identifier_kind": None,
            "identifier": None,
            "cadence_event_count": 0,
            "cadence_ms": _latency_block([]),
        }
    cadence_events = [
        event
        for event in timeline
        if event.get("event_type") == "sweep.did.cadence"
        and _matches_focus_sweep(event, focus_key)
    ]
    cadence_events.sort(key=lambda event: _parse_ts(event.get("ts")) or datetime.min.replace(tzinfo=timezone.utc))
    observed_cadence_ms: list[float] = []
    previous_ts: datetime | None = None
    for event in cadence_events:
        value = event.get("sweep_cadence_ms")
        if isinstance(value, (int, float)):
            observed_cadence_ms.append(float(value))
        ts = _parse_ts(event.get("ts"))
        if ts is not None and previous_ts is not None:
            observed_cadence_ms.append((ts - previous_ts).total_seconds() * 1000.0)
        if ts is not None:
            previous_ts = ts
    learned_count = sum(
        1
        for event in timeline
        if event.get("event_type") == "sweep.pattern.learned"
        and _matches_focus_sweep(event, focus_key)
    )
    inventory_events = [
        event
        for event in timeline
        if event.get("event_type") == "sweep.inventory.signature"
        and _matches_focus_sweep(event, focus_key)
    ]
    latest_inventory = inventory_events[-1] if inventory_events else {}
    return {
        "supported": True,
        "identifier_kind": identifier[0],
        "identifier": identifier[1],
        "cadence_event_count": len(cadence_events),
        "cadence_ms": _latency_block(observed_cadence_ms),
        "learned_count": learned_count,
        "latest_cycles": latest_inventory.get("sweep_inventory_write_observed_count"),
        "latest_read_data_count": latest_inventory.get("sweep_inventory_read_data_count"),
        "latest_shadow_eligible": latest_inventory.get("sweep_inventory_shadow_eligible"),
        "latest_replay_candidate": latest_inventory.get("sweep_inventory_replay_candidate"),
        "latest_eligibility_reason": latest_inventory.get("sweep_inventory_eligibility_reason"),
        "projected_pair_rtt_savings_ms": latest_inventory.get(
            "sweep_inventory_projected_pair_rtt_savings_ms"
        ),
    }


def _summarize_sweep_inventory(timeline: list[dict[str, Any]]) -> dict[str, Any]:
    summary_events = [
        event
        for event in timeline
        if event.get("event_type") == "sweep.inventory.summary"
    ]
    latest_summary = summary_events[-1] if summary_events else {}
    latest_signatures: dict[str, dict[str, Any]] = {}
    for event in timeline:
        if event.get("event_type") != "sweep.inventory.signature":
            continue
        digest = str(event.get("sweep_signature_digest") or "")
        if not digest:
            continue
        latest_signatures[digest] = event

    candidate_signatures: list[dict[str, Any]] = []
    gm_a9_signature_count = 0
    for event in latest_signatures.values():
        kind = str(event.get("sweep_identifier_kind") or "unknown")
        if kind == "gm_a9_packet":
            gm_a9_signature_count += 1
        if not bool(event.get("sweep_inventory_replay_candidate")):
            continue
        candidate_signatures.append(
            {
                "signature_digest": event.get("sweep_signature_digest"),
                "identifier_kind": kind,
                "identifier": event.get("sweep_identifier"),
                "payload_prefix_hex": event.get("sweep_payload_prefix_hex"),
                "write_observed_count": event.get(
                    "sweep_inventory_write_observed_count"
                ),
                "read_data_count": event.get("sweep_inventory_read_data_count"),
                "projected_write_rtt_savings_ms": event.get(
                    "sweep_inventory_projected_write_rtt_savings_ms"
                ),
                "projected_pair_rtt_savings_ms": event.get(
                    "sweep_inventory_projected_pair_rtt_savings_ms"
                ),
                "eligibility_reason": event.get(
                    "sweep_inventory_replay_eligibility_reason",
                    event.get("sweep_inventory_eligibility_reason"),
                ),
            }
        )
    candidate_signatures.sort(
        key=lambda item: (
            _as_int(item.get("write_observed_count")),
            _as_float(item.get("projected_pair_rtt_savings_ms")),
        ),
        reverse=True,
    )

    request_count_by_kind = dict(
        latest_summary.get("sweep_inventory_request_count_by_kind") or {}
    )
    replay_candidate_by_kind = dict(
        latest_summary.get("sweep_inventory_replay_candidate_request_count_by_kind")
        or {}
    )
    non_gm_candidate_request_count = sum(
        _as_int(count)
        for kind, count in replay_candidate_by_kind.items()
        if str(kind) != "gm_a9_packet"
    )
    if not summary_events:
        non_gm_candidate_request_count = sum(
            _as_int(item.get("write_observed_count"))
            for item in candidate_signatures
            if item.get("identifier_kind") != "gm_a9_packet"
        )
    latest_foreground_write_count = _as_int(
        latest_summary.get("sweep_inventory_foreground_write_count")
    )
    latest_verdict = str(latest_summary.get("sweep_inventory_verdict") or "").strip()
    latest_next_step = str(latest_summary.get("sweep_inventory_next_step") or "").strip()
    if not latest_verdict:
        if latest_foreground_write_count <= 0:
            latest_verdict = "pending_no_foreground_writes"
            latest_next_step = "enter_data_display_and_collect_sweep_inventory"
        elif non_gm_candidate_request_count > 0:
            latest_verdict = "go_shadow_local"
            latest_next_step = "run_shadow_local_for_top_non_gm_candidate"
        else:
            latest_verdict = "no_go_no_non_gm_replay_candidates"
            latest_next_step = "choose_page_with_repeated_uds_or_obd_read_only_traffic"
    return {
        "summary_event_count": len(summary_events),
        "signature_event_count": len(latest_signatures),
        "latest_sequence": latest_summary.get("sweep_inventory_sequence"),
        "verdict": latest_verdict,
        "next_step": latest_next_step,
        "sweep_inventory_verdict": latest_verdict,
        "sweep_inventory_next_step": latest_next_step,
        "signature_count": latest_summary.get(
            "sweep_inventory_signature_count",
            len(latest_signatures),
        ),
        "learned_signature_count": latest_summary.get(
            "sweep_inventory_learned_signature_count"
        ),
        "foreground_write_count": latest_summary.get(
            "sweep_inventory_foreground_write_count"
        ),
        "accepted_write_count": latest_summary.get(
            "sweep_inventory_accepted_write_count"
        ),
        "rejected_write_count": latest_summary.get(
            "sweep_inventory_rejected_write_count"
        ),
        "request_count_by_kind": request_count_by_kind,
        "replay_candidate_request_count_by_kind": replay_candidate_by_kind,
        "replay_candidate_request_count": latest_summary.get(
            "sweep_inventory_replay_candidate_request_count",
            non_gm_candidate_request_count,
        ),
        "replay_candidate_coverage_pct": latest_summary.get(
            "sweep_inventory_replay_candidate_coverage_pct"
        ),
        "non_gm_replay_candidate_request_count": non_gm_candidate_request_count,
        "has_non_gm_replay_candidates": non_gm_candidate_request_count > 0,
        "gm_a9_request_count": _as_int(request_count_by_kind.get("gm_a9_packet")),
        "gm_a9_signature_count": gm_a9_signature_count,
        "rejection_count_by_reason": dict(
            latest_summary.get("sweep_inventory_rejection_count_by_reason") or {}
        ),
        "projected_write_rtt_savings_ms": latest_summary.get(
            "sweep_inventory_projected_write_rtt_savings_ms"
        ),
        "projected_pair_rtt_savings_ms": latest_summary.get(
            "sweep_inventory_projected_pair_rtt_savings_ms"
        ),
        "top_candidate_signature_digest": latest_summary.get(
            "sweep_inventory_top_candidate_signature_digest"
        ),
        "top_candidate_identifier_kind": latest_summary.get(
            "sweep_inventory_top_candidate_identifier_kind"
        ),
        "top_candidate_identifier": latest_summary.get(
            "sweep_inventory_top_candidate_identifier"
        ),
        "top_candidate_payload_prefix_hex": latest_summary.get(
            "sweep_inventory_top_candidate_payload_prefix_hex"
        ),
        "top_candidate_write_observed_count": latest_summary.get(
            "sweep_inventory_top_candidate_write_observed_count"
        ),
        "top_candidate_read_data_count": latest_summary.get(
            "sweep_inventory_top_candidate_read_data_count"
        ),
        "top_candidate_projected_pair_rtt_savings_ms": latest_summary.get(
            "sweep_inventory_top_candidate_projected_pair_rtt_savings_ms"
        ),
        "candidate_signatures": candidate_signatures[:10],
    }


def _summarize_shadow_and_config(timeline: list[dict[str, Any]]) -> dict[str, Any]:
    startup_events = [
        event
        for event in timeline
        if event.get("component") == "reverse_server"
        and event.get("event_type") == "process.lifecycle.started"
    ]
    startup = startup_events[-1] if startup_events else {}
    plan_started = [
        event for event in timeline if event.get("event_type") == "sweep.plan.started"
    ]
    shadow_events = [
        event
        for event in timeline
        if str(event.get("event_type") or "").startswith("sweep.shadow.")
    ]
    plan_read_timeouts: list[float] = []
    for event in plan_started:
        plan_read_timeouts.extend(_flatten_numbers(event.get("sweep_plan_read_timeout_ms")))
    local_sweep_mode = startup.get("local_sweep_mode")
    local_sweep_read_timeout_ms = startup.get("local_sweep_read_timeout_ms")
    shadow_transport_enabled = bool(
        startup.get("local_sweep_enabled")
        and str(local_sweep_mode or "") in {"shadow_local", "active_replay"}
    )
    startup_timeout_zero = (
        shadow_transport_enabled
        and isinstance(local_sweep_read_timeout_ms, (int, float))
        and int(local_sweep_read_timeout_ms) <= 0
    )
    plan_timeout_zero = bool(plan_read_timeouts) and max(plan_read_timeouts) <= 0
    warning_events = [
        event
        for event in timeline
        if event.get("event_type") == "sweep.config.warning"
    ]
    return {
        "startup": {
            "local_sweep_enabled": startup.get("local_sweep_enabled"),
            "local_sweep_mode": local_sweep_mode,
            "local_sweep_read_timeout_ms": local_sweep_read_timeout_ms,
            "local_sweep_shadow_allow_gm_a9_packet": startup.get(
                "local_sweep_shadow_allow_gm_a9_packet"
            ),
        },
        "config_warning_count": len(warning_events),
        "shadow_transport_enabled": shadow_transport_enabled,
        "tail_read_validation_blocked": bool(startup_timeout_zero or plan_timeout_zero),
        "plan_started_count": len(plan_started),
        "plan_item_counts": [
            int(event.get("sweep_item_count") or 0) for event in plan_started
        ],
        "plan_read_timeout_ms": [int(value) for value in plan_read_timeouts],
        "shadow_event_counts": _counter_block(event.get("event_type") for event in shadow_events),
        "shadow_missing_reasons": _counter_block(
            event.get("sweep_shadow_missing_reason")
            for event in shadow_events
            if event.get("event_type") == "sweep.shadow.missing"
        ),
        "shadow_mismatch_count": sum(
            1 for event in shadow_events if event.get("event_type") == "sweep.shadow.mismatch"
        ),
        "shadow_match_count": sum(
            1 for event in shadow_events if event.get("event_type") == "sweep.shadow.match"
        ),
    }


def _build_session_verdict(
    timeline: list[dict[str, Any]],
    *,
    focus_key: str,
    changes: list[dict[str, Any]],
) -> dict[str, Any]:
    focus_sweep = _summarize_focus_sweep(timeline, focus_key=focus_key)
    inventory = _summarize_sweep_inventory(timeline)
    shadow = _summarize_shadow_and_config(timeline)
    forwarded_events = [
        event for event in timeline if event.get("event_type") == "proxy.request.forwarded_to_tunnel"
    ]
    reasons: list[str] = []
    next_checks: list[str] = []
    if not changes:
        reasons.append("no_significant_focus_value_changes")
        next_checks.append("repeat the run with visible focus-value movement")
    if shadow["tail_read_validation_blocked"]:
        reasons.append("local_sweep_shadow_read_timeout_zero")
        next_checks.append(
            "restart the cloud reverse_server with VCI_PROXY_LOCAL_SWEEP_READ_TIMEOUT_MS=1"
        )
    cadence_p50 = (focus_sweep.get("cadence_ms") or {}).get("p50")
    cadence_max = (focus_sweep.get("cadence_ms") or {}).get("max")
    if isinstance(cadence_p50, (int, float)) and cadence_p50 > SLOW_FOCUS_SWEEP_CADENCE_MS:
        reasons.append("focus_sweep_cadence_still_slow")
        next_checks.append("reduce write-side tunnel crossings for the learned sweep")
    if focus_sweep.get("supported") and focus_sweep.get("cadence_event_count") == 0:
        reasons.append("no_focus_sweep_cadence_events")
        next_checks.append("verify sweep inventory sees the focus DID")
    if (
        inventory.get("summary_event_count")
        and not inventory.get("has_non_gm_replay_candidates")
    ):
        reasons.append("no_non_gm_a9_replay_candidates")
        next_checks.append(
            "choose a Data Display page with repeated UDS/OBD read-only traffic"
        )
    if (
        inventory.get("has_non_gm_replay_candidates")
        and shadow.get("shadow_transport_enabled")
        and not shadow.get("shadow_match_count")
    ):
        reasons.append("non_gm_candidates_need_shadow_match")
        next_checks.append(
            "run shadow_local narrowed to the target DID/PID before active_replay"
        )
    if shadow.get("shadow_mismatch_count"):
        reasons.append("shadow_results_not_comparison_clean")
        next_checks.append("inspect shadow echo/positive-response frame shape before replay")
    if not reasons:
        reasons.append("no_blocking_issue_detected")
    if "no_significant_focus_value_changes" in reasons:
        status = "inconclusive_no_focus_changes"
    elif "local_sweep_shadow_read_timeout_zero" in reasons:
        status = "config_not_applied"
    elif "no_non_gm_a9_replay_candidates" in reasons:
        status = "inventory_no_go"
    elif "focus_sweep_cadence_still_slow" in reasons:
        status = "freshness_still_slow"
    elif (
        "shadow_results_not_comparison_clean" in reasons
        or "non_gm_candidates_need_shadow_match" in reasons
    ):
        status = "shadow_not_replay_ready"
    else:
        status = "review"
    return {
        "status": status,
        "confidence": "high" if len(timeline) and changes else "medium",
        "reasons": reasons,
        "next_checks": list(dict.fromkeys(next_checks)),
        "sweep_inventory": inventory,
        "focus_sweep": focus_sweep,
        "shadow": shadow,
        "foreground_forwarded_count": len(forwarded_events),
        "foreground_forwarded_reasons": _counter_block(
            event.get("reason") for event in forwarded_events
        ),
        "focus_sweep_cadence_p50_ms": cadence_p50,
        "focus_sweep_cadence_max_ms": cadence_max,
    }


def _extract_focus_value_changes(
    timeline: list[dict[str, Any]],
    *,
    focus_key: str,
    min_delta: float,
    window_ms: int,
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for event in timeline:
        if event.get("event_type") != "agent.collector.focus_value_changed":
            continue
        if str(event.get("focus_key") or "") != focus_key:
            continue
        delta = event.get("delta_value_number")
        if not isinstance(delta, (int, float)):
            continue
        abs_delta = abs(float(delta))
        if abs_delta < float(min_delta):
            continue
        ts = _parse_ts(event.get("ts"))
        if ts is None:
            continue
        changes.append(
            {
                "ts": event.get("ts"),
                "focus_key": focus_key,
                "source_parameter_name": event.get("source_parameter_name"),
                "source_parameter_unit": event.get("source_parameter_unit"),
                "previous_value": event.get("previous_value"),
                "current_value": event.get("current_value"),
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
    focus_key: str,
    min_delta: float,
    window_ms: int,
) -> dict[str, Any]:
    trace = assemble_session_trace(cloud_root=cloud_root, session_id=session_id)
    timeline = _include_epoch_only_events(
        cloud_root,
        list(trace.get("timeline") or []),
        connection_epoch=trace.get("connection_epoch"),
    )
    changes = _extract_focus_value_changes(
        timeline,
        focus_key=focus_key,
        min_delta=min_delta,
        window_ms=window_ms,
    )
    report = {
        "session_id": trace.get("session_id"),
        "connection_epoch": trace.get("connection_epoch"),
        "status": trace.get("status"),
        "page_context": trace.get("page_context"),
        "network_context": trace.get("network_context"),
        "key_metrics": trace.get("key_metrics"),
        "source_artifact_count": len(trace.get("source_artifacts") or []),
        "focus_value_changes": changes,
        "verdict": _build_session_verdict(
            timeline,
            focus_key=focus_key,
            changes=changes,
        ),
        _focus_key_changes_key(focus_key): changes,
    }
    if focus_key == "battery_voltage":
        report["battery_voltage_changes"] = changes
    return report


def analyze_focus_value_freshness(
    cloud_root: str | Path | None,
    *,
    session_id: str | None = None,
    focus_key: str = "battery_voltage",
    min_delta: float | None = None,
    window_ms: int = DEFAULT_WINDOW_MS,
) -> dict[str, Any]:
    normalized_focus_key = str(focus_key or "battery_voltage").strip() or "battery_voltage"
    resolved_min_delta = (
        float(DEFAULT_MIN_DELTA_BY_FOCUS_KEY.get(normalized_focus_key, 0.0))
        if min_delta is None
        else float(min_delta)
    )
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
            focus_key=normalized_focus_key,
            min_delta=resolved_min_delta,
            window_ms=window_ms,
        )
        if session_id or report["focus_value_changes"]:
            sessions.append(report)

    payload: dict[str, Any] = {
        "cloud_root": str(resolved_cloud_root),
        "focus_key": normalized_focus_key,
        "focus_label": _focus_key_label(normalized_focus_key),
        "focus_unit": _focus_key_unit(normalized_focus_key),
        "min_delta": resolved_min_delta,
        "window_ms": int(window_ms),
        "session_count_scanned": len(session_ids),
        "session_count_reported": len(sessions),
        "sessions": sessions,
    }
    if normalized_focus_key == "battery_voltage":
        payload["min_delta_v"] = resolved_min_delta
    return payload


def analyze_battery_voltage_freshness(
    cloud_root: str | Path | None,
    *,
    session_id: str | None = None,
    min_delta_v: float = DEFAULT_MIN_DELTA_V,
    window_ms: int = DEFAULT_WINDOW_MS,
) -> dict[str, Any]:
    return analyze_focus_value_freshness(
        cloud_root,
        session_id=session_id,
        focus_key="battery_voltage",
        min_delta=min_delta_v,
        window_ms=window_ms,
    )


def _format_counts(counts: dict[str, int] | None) -> str:
    if not counts:
        return "-"
    return ", ".join(f"{key}:{value}" for key, value in sorted(counts.items()))


def _format_list(values: list[Any] | tuple[Any, ...] | None) -> str:
    if not values:
        return "-"
    return ", ".join(str(value) for value in values)


def generate_markdown_report(payload: dict[str, Any]) -> str:
    focus_key = str(payload.get("focus_key") or "battery_voltage")
    focus_label = str(payload.get("focus_label") or _focus_key_label(focus_key))
    focus_unit = str(payload.get("focus_unit") or _focus_key_unit(focus_key))
    min_delta = float(payload.get("min_delta") or 0.0)
    unit_suffix = focus_unit if focus_unit else ""
    value_column_unit = f" {focus_unit}" if focus_unit else ""
    lines = [
        f"# {focus_label} Freshness Report",
        "",
        f"- Cloud root: `{payload['cloud_root']}`",
        f"- Focus key: `{focus_key}`",
        f"- Significant delta threshold: `{min_delta:.1f}{unit_suffix}`",
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
                f"- Significant {focus_label} changes: `{len(session.get('focus_value_changes') or [])}`",
                "",
            ]
        )
        verdict = session.get("verdict") or {}
        shadow = verdict.get("shadow") or {}
        focus_sweep = verdict.get("focus_sweep") or {}
        inventory = verdict.get("sweep_inventory") or {}
        startup = shadow.get("startup") or {}
        if verdict:
            lines.extend(
                [
                    "### Verdict",
                    "",
                    f"- Status: `{verdict.get('status')}`",
                    f"- Reasons: `{_format_list(verdict.get('reasons'))}`",
                    f"- Next checks: `{_format_list(verdict.get('next_checks'))}`",
                    (
                        "- Focus sweep cadence p50/max ms: "
                        f"`{verdict.get('focus_sweep_cadence_p50_ms')}` / "
                        f"`{verdict.get('focus_sweep_cadence_max_ms')}`"
                    ),
                    (
                        "- Local sweep startup: "
                        f"mode=`{startup.get('local_sweep_mode')}`, "
                        f"read_timeout_ms=`{startup.get('local_sweep_read_timeout_ms')}`"
                    ),
                    (
                        "- Shadow plans: "
                        f"count=`{shadow.get('plan_started_count')}`, "
                        f"item_counts=`{_format_list(shadow.get('plan_item_counts'))}`, "
                        f"read_timeout_ms=`{_format_list(shadow.get('plan_read_timeout_ms'))}`"
                    ),
                    f"- Shadow events: `{_format_counts(shadow.get('shadow_event_counts'))}`",
                    (
                        "- Focus DID: "
                        f"kind=`{focus_sweep.get('identifier_kind')}`, "
                        f"id=`{focus_sweep.get('identifier')}`, "
                        f"cadence_events=`{focus_sweep.get('cadence_event_count')}`"
                    ),
                    (
                        "- Sweep inventory: "
                        f"non_gm_candidates=`{inventory.get('non_gm_replay_candidate_request_count')}`, "
                        f"candidate_by_kind=`{_format_counts(inventory.get('replay_candidate_request_count_by_kind'))}`, "
                        f"request_by_kind=`{_format_counts(inventory.get('request_count_by_kind'))}`"
                    ),
                    (
                        "- Inventory verdict: "
                        f"`{inventory.get('sweep_inventory_verdict')}` -> "
                        f"`{inventory.get('sweep_inventory_next_step')}`"
                    ),
                    "",
                ]
            )
            candidates = inventory.get("candidate_signatures") or []
            if candidates:
                lines.extend(
                    [
                        "| Candidate kind | Identifier | Writes | Read data | Projected pair RTT ms | Payload prefix |",
                        "| --- | ---: | ---: | ---: | ---: | --- |",
                    ]
                )
                for candidate in candidates[:5]:
                    lines.append(
                        f"| {candidate.get('identifier_kind')} | "
                        f"{candidate.get('identifier')} | "
                        f"{candidate.get('write_observed_count')} | "
                        f"{candidate.get('read_data_count')} | "
                        f"{candidate.get('projected_pair_rtt_savings_ms')} | "
                        f"{candidate.get('payload_prefix_hex')} |"
                    )
                lines.append("")
        lines.extend(
            [
                f"| TS | Source | Prev{value_column_unit} | Curr{value_column_unit} | Delta{value_column_unit} | Lag ms | RTT p95 ms | Write-collect txns | Replay armed | Replay served | Forwarded | Prefetch miss details |",
                "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
            ]
        )
        for change in session.get("focus_value_changes") or []:
            lines.append(
                f"| {change.get('ts')} | {change.get('source_parameter_name') or '-'} | "
                f"{change.get('previous_value_number')} | {change.get('current_value_number')} | "
                f"{change.get('delta_value_number')} | {change.get('collector_lag_ms')} | "
                f"{(change.get('window') or {}).get('network_ms', {}).get('p95')} | "
                f"{(change.get('window') or {}).get('write_collect_transaction_count')} | "
                f"{(change.get('window') or {}).get('active_replay_armed_count')} | "
                f"{(change.get('window') or {}).get('active_replay_served_count')} | "
                f"{(change.get('window') or {}).get('forwarded_to_tunnel_count')} | "
                f"{_format_counts((change.get('window') or {}).get('prefetch_miss_detail_counts'))} |"
            )
        lines.append("")
    if not payload.get("sessions"):
        lines.append(f"No sessions with significant {focus_label} changes were found.\n")
    return "\n".join(lines).rstrip() + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyze focused Data Display value freshness markers from cloud observability artifacts."
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
        "--focus-key",
        default="battery_voltage",
        help=(
            "Focused collector key to analyze, for example battery_voltage or "
            "engine_speed. Defaults to battery_voltage for compatibility."
        ),
    )
    parser.add_argument(
        "--min-delta",
        type=float,
        default=None,
        help=(
            "Minimum absolute numeric delta to include. Defaults to 1.0 for "
            "battery_voltage and 0.0 for other focus keys."
        ),
    )
    parser.add_argument(
        "--min-delta-v",
        type=float,
        default=None,
        help="Compatibility alias for --min-delta when analyzing battery_voltage.",
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
    min_delta = args.min_delta
    if min_delta is None and args.min_delta_v is not None:
        min_delta = args.min_delta_v
    payload = analyze_focus_value_freshness(
        args.cloud_root,
        session_id=args.session_id,
        focus_key=args.focus_key,
        min_delta=min_delta,
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
