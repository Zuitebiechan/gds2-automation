from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "analyze_battery_voltage_freshness.py"


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        cwd=cwd or ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def _event(ts: str, component: str, event_type: str, **overrides) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "observability.v1",
        "ts": ts,
        "component": component,
        "component_instance_id": f"{component}:pid:startup",
        "event_type": event_type,
        "session_id": None,
        "connection_epoch": None,
        "dll_seq": None,
        "proxy_seq": None,
        "worker_request_id": None,
        "operation_kind": None,
        "status": "ok",
        "failure_code": None,
        "failure_domain": "unknown",
        "reason": None,
        "duration_ms": None,
        "hw_ms": None,
        "network_ms": None,
        "page": None,
        "module": None,
        "data_category": None,
        "symptom": None,
        "impact_scope": None,
        "next_checks": [],
        "redaction_applied": [],
    }
    payload.update(overrides)
    return payload


def test_battery_voltage_freshness_script_reports_significant_changes(tmp_path: Path) -> None:
    cloud_root = tmp_path / "cloud"
    raw_dir = cloud_root / "raw"
    raw_dir.mkdir(parents=True)

    events = [
        _event(
            "2026-04-22T00:00:00Z",
            "session_runtime",
            "session.lifecycle.started",
            session_id="session-fresh-1",
            connection_epoch="epoch-fresh-1",
        ),
        _event(
            "2026-04-22T00:00:05Z",
            "session_runtime",
            "session.live_data.started",
            session_id="session-fresh-1",
            connection_epoch="epoch-fresh-1",
            page="data_display",
            module="Engine Control Module",
            data_category="Engine Data",
        ),
        _event(
            "2026-04-22T00:00:09Z",
            "reverse_server",
            "proxy.request.response_received",
            session_id="session-fresh-1",
            connection_epoch="epoch-fresh-1",
            network_ms=10.0,
            msg_name="READ_MSGS_REQ",
        ),
        _event(
            "2026-04-22T00:00:10Z",
            "reverse_server",
            "proxy.request.cache_decision",
            session_id="session-fresh-1",
            connection_epoch="epoch-fresh-1",
            reason="cache_hit",
        ),
        _event(
            "2026-04-22T00:00:10.050000Z",
            "reverse_server",
            "proxy.request.cache_decision",
            session_id="session-fresh-1",
            connection_epoch="epoch-fresh-1",
            reason="prefetch_miss",
            prefetch_miss_detail="fifo_empty_after_prefetch_exhausted",
            read_collect_blocked_reason="transaction_guard_active",
        ),
        _event(
            "2026-04-22T00:00:10.100000Z",
            "reverse_server",
            "proxy.request.forwarded_to_tunnel",
            session_id="session-fresh-1",
            connection_epoch="epoch-fresh-1",
            reason="write_collect_transaction",
        ),
        _event(
            "2026-04-22T00:00:10.150000Z",
            "reverse_server",
            "proxy.request.active_replay_armed",
            session_id="session-fresh-1",
            connection_epoch="epoch-fresh-1",
        ),
        _event(
            "2026-04-22T00:00:10.200000Z",
            "agent_data_collector",
            "agent.collector.focus_value_changed",
            session_id="session-fresh-1",
            connection_epoch="epoch-fresh-1",
            page="data_display",
            module="Engine Control Module",
            data_category="Engine Data",
            focus_key="battery_voltage",
            previous_value_number=12.0,
            current_value_number=10.4,
            delta_value_number=-1.6,
            change_threshold_number=0.5,
            collector_lag_ms=55.0,
        ),
        _event(
            "2026-04-22T00:00:10.300000Z",
            "reverse_server",
            "proxy.request.active_replay_served",
            session_id="session-fresh-1",
            connection_epoch="epoch-fresh-1",
        ),
        _event(
            "2026-04-22T00:00:10.500000Z",
            "reverse_server",
            "proxy.request.response_received",
            session_id="session-fresh-1",
            connection_epoch="epoch-fresh-1",
            network_ms=40.0,
            msg_name="READ_MSGS_REQ",
        ),
        _event(
            "2026-04-22T00:00:11Z",
            "agent_data_collector",
            "agent.collector.focus_value_changed",
            session_id="session-fresh-1",
            connection_epoch="epoch-fresh-1",
            page="data_display",
            module="Engine Control Module",
            data_category="Engine Data",
            focus_key="battery_voltage",
            previous_value_number=10.4,
            current_value_number=10.9,
            delta_value_number=0.5,
            change_threshold_number=0.5,
            collector_lag_ms=45.0,
        ),
        _event(
            "2026-04-22T00:00:20Z",
            "session_runtime",
            "session.lifecycle.aborted",
            session_id="session-fresh-1",
            connection_epoch="epoch-fresh-1",
            status="error",
            failure_code="aborted",
            failure_domain="session_runtime",
            reason="Aborted by user",
            page="data_display",
            module="Engine Control Module",
            data_category="Engine Data",
        ),
    ]
    (raw_dir / "cloud.jsonl").write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events),
        encoding="utf-8",
    )

    report_path = tmp_path / "freshness.md"
    json_path = tmp_path / "freshness.json"
    _run(
        str(SCRIPT),
        "--cloud-root",
        str(cloud_root),
        "--json",
        str(json_path),
        "--report",
        str(report_path),
    )

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["session_count_scanned"] == 1
    assert payload["session_count_reported"] == 1
    session = payload["sessions"][0]
    assert session["session_id"] == "session-fresh-1"
    assert session["status"] == "aborted"
    assert len(session["battery_voltage_changes"]) == 1
    change = session["battery_voltage_changes"][0]
    assert change["delta_value_number"] == -1.6
    assert change["window"]["network_ms"]["p95"] == 40.0
    assert change["window"]["write_collect_transaction_count"] == 1
    assert change["window"]["active_replay_armed_count"] == 1
    assert change["window"]["active_replay_served_count"] == 1
    assert change["window"]["forwarded_to_tunnel_count"] == 1
    assert change["window"]["cache_decision_counts"]["cache_hit"] == 1
    assert change["window"]["cache_decision_counts"]["prefetch_miss"] == 1
    assert change["window"]["prefetch_miss_detail_counts"] == {
        "fifo_empty_after_prefetch_exhausted": 1
    }
    assert change["window"]["read_collect_blocked_counts"] == {
        "transaction_guard_active": 1
    }
    assert "# Battery Voltage Freshness Report" in report_path.read_text(encoding="utf-8")
    assert "fifo_empty_after_prefetch_exhausted:1" in report_path.read_text(encoding="utf-8")


def test_focus_value_freshness_script_reports_engine_speed_changes(tmp_path: Path) -> None:
    cloud_root = tmp_path / "cloud"
    raw_dir = cloud_root / "raw"
    raw_dir.mkdir(parents=True)

    events = [
        _event(
            "2026-05-12T00:00:00Z",
            "session_runtime",
            "session.lifecycle.started",
            session_id="session-engine-1",
            connection_epoch="epoch-engine-1",
        ),
        _event(
            "2026-05-12T00:00:05Z",
            "session_runtime",
            "session.live_data.started",
            session_id="session-engine-1",
            connection_epoch="epoch-engine-1",
            page="data_display",
            module="Engine Control Module",
            data_category="Engine Data",
        ),
        _event(
            "2026-05-12T00:00:09.800000Z",
            "reverse_server",
            "proxy.request.response_received",
            session_id="session-engine-1",
            connection_epoch="epoch-engine-1",
            network_ms=18.0,
            msg_name="READ_MSGS_REQ",
        ),
        _event(
            "2026-05-12T00:00:09.900000Z",
            "reverse_server",
            "proxy.request.cache_decision",
            session_id="session-engine-1",
            connection_epoch="epoch-engine-1",
            reason="prefetch_miss",
            prefetch_miss_detail="fifo_empty_after_confirmed_empty",
        ),
        _event(
            "2026-05-12T00:00:10Z",
            "agent_data_collector",
            "agent.collector.focus_value_changed",
            session_id="session-engine-1",
            connection_epoch="epoch-engine-1",
            page="data_display",
            module="Engine Control Module",
            data_category="Engine Data",
            focus_key="engine_speed",
            source_parameter_name="Engine Speed",
            source_parameter_unit="RPM",
            previous_value="700",
            current_value="760",
            previous_value_number=700.0,
            current_value_number=760.0,
            delta_value_number=60.0,
            collector_lag_ms=42.0,
        ),
        _event(
            "2026-05-12T00:00:11Z",
            "agent_data_collector",
            "agent.collector.focus_value_changed",
            session_id="session-engine-1",
            connection_epoch="epoch-engine-1",
            page="data_display",
            module="Engine Control Module",
            data_category="Engine Data",
            focus_key="engine_speed",
            source_parameter_name="Engine Speed",
            source_parameter_unit="RPM",
            previous_value="760",
            current_value="1500",
            previous_value_number=760.0,
            current_value_number=1500.0,
            delta_value_number=740.0,
            collector_lag_ms=35.0,
        ),
        _event(
            "2026-05-12T00:00:11.100000Z",
            "reverse_server",
            "proxy.request.forwarded_to_tunnel",
            session_id="session-engine-1",
            connection_epoch="epoch-engine-1",
            reason="read_collect_transaction",
        ),
        _event(
            "2026-05-12T00:00:11.200000Z",
            "reverse_server",
            "proxy.request.response_received",
            session_id="session-engine-1",
            connection_epoch="epoch-engine-1",
            network_ms=55.0,
            msg_name="READ_MSGS_REQ",
        ),
    ]
    (raw_dir / "cloud.jsonl").write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events),
        encoding="utf-8",
    )

    report_path = tmp_path / "engine_freshness.md"
    json_path = tmp_path / "engine_freshness.json"
    _run(
        str(SCRIPT),
        "--cloud-root",
        str(cloud_root),
        "--focus-key",
        "engine_speed",
        "--min-delta",
        "100",
        "--json",
        str(json_path),
        "--report",
        str(report_path),
    )

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["focus_key"] == "engine_speed"
    assert payload["focus_label"] == "Engine Speed"
    assert payload["focus_unit"] == "RPM"
    assert payload["min_delta"] == 100.0
    assert payload["session_count_scanned"] == 1
    assert payload["session_count_reported"] == 1
    session = payload["sessions"][0]
    assert session["session_id"] == "session-engine-1"
    assert "battery_voltage_changes" not in session
    assert len(session["focus_value_changes"]) == 1
    assert len(session["engine_speed_changes"]) == 1
    change = session["focus_value_changes"][0]
    assert change["source_parameter_name"] == "Engine Speed"
    assert change["previous_value_number"] == 760.0
    assert change["current_value_number"] == 1500.0
    assert change["delta_value_number"] == 740.0
    assert change["window"]["network_ms"]["p95"] == 55.0
    assert change["window"]["forwarded_to_tunnel_count"] == 1

    report = report_path.read_text(encoding="utf-8")
    assert "# Engine Speed Freshness Report" in report
    assert "Significant Engine Speed changes: `1`" in report
    assert "Engine Speed" in report


def test_engine_speed_freshness_verdict_reports_inventory_no_go(
    tmp_path: Path,
) -> None:
    cloud_root = tmp_path / "cloud"
    raw_dir = cloud_root / "raw"
    raw_dir.mkdir(parents=True)

    events = [
        _event(
            "2026-05-15T00:00:00Z",
            "session_runtime",
            "session.lifecycle.started",
            session_id="session-gm-only",
            connection_epoch="epoch-gm-only",
        ),
        _event(
            "2026-05-15T00:00:05Z",
            "session_runtime",
            "session.live_data.started",
            session_id="session-gm-only",
            connection_epoch="epoch-gm-only",
            page="data_display",
            module="Engine Control Module",
            data_category="Engine Data",
        ),
        _event(
            "2026-05-15T00:00:08Z",
            "reverse_server",
            "sweep.inventory.summary",
            connection_epoch="epoch-gm-only",
            sweep_inventory_sequence=12,
            sweep_inventory_signature_count=1,
            sweep_inventory_learned_signature_count=1,
            sweep_inventory_replay_candidate_signature_count=0,
            sweep_inventory_foreground_write_count=80,
            sweep_inventory_accepted_write_count=12,
            sweep_inventory_rejected_write_count=68,
            sweep_inventory_replay_candidate_request_count=0,
            sweep_inventory_replay_candidate_coverage_pct=0.0,
            sweep_inventory_request_count_by_kind={"gm_a9_packet": 12},
            sweep_inventory_replay_candidate_request_count_by_kind={},
            sweep_inventory_rejection_count_by_reason={
                "not_allowlisted_read_only_shape": 68
            },
        ),
        _event(
            "2026-05-15T00:00:09Z",
            "agent_data_collector",
            "agent.collector.focus_value_changed",
            session_id="session-gm-only",
            connection_epoch="epoch-gm-only",
            page="data_display",
            module="Engine Control Module",
            data_category="Engine Data",
            focus_key="engine_speed",
            source_parameter_name="Engine Speed",
            source_parameter_unit="RPM",
            previous_value="800",
            current_value="1800",
            previous_value_number=800.0,
            current_value_number=1800.0,
            delta_value_number=1000.0,
            collector_lag_ms=45.0,
        ),
        _event(
            "2026-05-15T00:00:20Z",
            "session_runtime",
            "session.lifecycle.aborted",
            session_id="session-gm-only",
            connection_epoch="epoch-gm-only",
            status="error",
            failure_code="aborted",
            failure_domain="session_runtime",
            reason="Aborted by user",
        ),
    ]
    (raw_dir / "cloud.jsonl").write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events),
        encoding="utf-8",
    )

    report_path = tmp_path / "gm_only.md"
    json_path = tmp_path / "gm_only.json"
    _run(
        str(SCRIPT),
        "--cloud-root",
        str(cloud_root),
        "--session-id",
        "session-gm-only",
        "--focus-key",
        "engine_speed",
        "--min-delta",
        "100",
        "--json",
        str(json_path),
        "--report",
        str(report_path),
    )

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    verdict = payload["sessions"][0]["verdict"]
    assert verdict["status"] == "inventory_no_go"
    assert "no_non_gm_a9_replay_candidates" in verdict["reasons"]
    assert verdict["sweep_inventory"]["non_gm_replay_candidate_request_count"] == 0
    assert verdict["sweep_inventory"]["request_count_by_kind"] == {
        "gm_a9_packet": 12
    }

    report = report_path.read_text(encoding="utf-8")
    assert "Inventory verdict" in report
    assert "no_go_no_non_gm_replay_candidates" in report
    assert "non_gm_candidates=`0`" in report
    assert "request_by_kind=`gm_a9_packet:12`" in report


def test_engine_speed_freshness_verdict_flags_shadow_timeout_and_slow_cadence(
    tmp_path: Path,
) -> None:
    cloud_root = tmp_path / "cloud"
    raw_dir = cloud_root / "raw"
    raw_dir.mkdir(parents=True)

    events = [
        _event(
            "2026-05-14T08:00:00Z",
            "session_runtime",
            "session.lifecycle.started",
            session_id="session-engine-verdict",
            connection_epoch="epoch-engine-verdict",
        ),
        _event(
            "2026-05-14T08:00:01Z",
            "reverse_server",
            "process.lifecycle.started",
            session_id=None,
            connection_epoch="epoch-engine-verdict",
            local_sweep_enabled=True,
            local_sweep_mode="shadow_local",
            local_sweep_read_timeout_ms=0,
            local_sweep_shadow_allow_gm_a9_packet=False,
        ),
        _event(
            "2026-05-14T08:00:01.100000Z",
            "reverse_server",
            "sweep.config.warning",
            session_id=None,
            connection_epoch="epoch-engine-verdict",
            failure_code="local_sweep_shadow_read_timeout_zero",
            reason="shadow_read_timeout_zero",
            local_sweep_read_timeout_ms=0,
            local_sweep_validation_blocked=True,
        ),
        _event(
            "2026-05-14T08:00:10Z",
            "session_runtime",
            "session.live_data.started",
            session_id="session-engine-verdict",
            connection_epoch="epoch-engine-verdict",
            page="data_display",
            module="Engine Control Module",
            data_category="Engine Data",
        ),
        _event(
            "2026-05-14T08:00:20Z",
            "reverse_server",
            "sweep.did.cadence",
            session_id=None,
            connection_epoch="epoch-engine-verdict",
            sweep_identifier_kind="uds_did",
            sweep_identifier=0x000C,
            sweep_cadence_ms=None,
        ),
        _event(
            "2026-05-14T08:00:28Z",
            "reverse_server",
            "sweep.did.cadence",
            session_id=None,
            connection_epoch="epoch-engine-verdict",
            sweep_identifier_kind="uds_did",
            sweep_identifier=0x000C,
            sweep_cadence_ms=8000.0,
        ),
        _event(
            "2026-05-14T08:00:28.001000Z",
            "reverse_server",
            "sweep.inventory.signature",
            session_id=None,
            connection_epoch="epoch-engine-verdict",
            sweep_identifier_kind="uds_did",
            sweep_identifier=0x000C,
            sweep_inventory_write_observed_count=2,
            sweep_inventory_read_data_count=2,
            sweep_inventory_shadow_eligible=True,
            sweep_inventory_replay_candidate=True,
            sweep_inventory_eligibility_reason="learned_safe_signature",
            sweep_inventory_projected_pair_rtt_savings_ms=140.0,
        ),
        _event(
            "2026-05-14T08:00:28.100000Z",
            "reverse_server",
            "sweep.plan.started",
            session_id=None,
            connection_epoch="epoch-engine-verdict",
            sweep_item_count=1,
            sweep_plan_read_timeout_ms=[0],
        ),
        _event(
            "2026-05-14T08:00:28.200000Z",
            "reverse_server",
            "sweep.shadow.mismatch",
            session_id=None,
            connection_epoch="epoch-engine-verdict",
            sweep_identifier_kind="uds_did",
            sweep_identifier=0x000C,
            sweep_real_message_lengths=[4, 9],
            sweep_shadow_message_lengths=[4],
        ),
        _event(
            "2026-05-14T08:00:28.300000Z",
            "reverse_server",
            "proxy.request.forwarded_to_tunnel",
            session_id=None,
            connection_epoch="epoch-engine-verdict",
            reason="write_collect_transaction",
        ),
        _event(
            "2026-05-14T08:00:28.400000Z",
            "agent_data_collector",
            "agent.collector.focus_value_changed",
            session_id="session-engine-verdict",
            connection_epoch="epoch-engine-verdict",
            page="data_display",
            module="Engine Control Module",
            data_category="Engine Data",
            focus_key="engine_speed",
            source_parameter_name="Engine Speed",
            source_parameter_unit="RPM",
            previous_value="850",
            current_value="1650",
            previous_value_number=850.0,
            current_value_number=1650.0,
            delta_value_number=800.0,
            collector_lag_ms=40.0,
        ),
        _event(
            "2026-05-14T08:00:40Z",
            "session_runtime",
            "session.lifecycle.aborted",
            session_id="session-engine-verdict",
            connection_epoch="epoch-engine-verdict",
            status="error",
            failure_code="aborted",
            failure_domain="session_runtime",
            reason="Aborted by user",
        ),
    ]
    (raw_dir / "cloud.jsonl").write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events),
        encoding="utf-8",
    )

    report_path = tmp_path / "engine_verdict.md"
    json_path = tmp_path / "engine_verdict.json"
    _run(
        str(SCRIPT),
        "--cloud-root",
        str(cloud_root),
        "--session-id",
        "session-engine-verdict",
        "--focus-key",
        "engine_speed",
        "--min-delta",
        "100",
        "--json",
        str(json_path),
        "--report",
        str(report_path),
    )

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    verdict = payload["sessions"][0]["verdict"]
    assert verdict["status"] == "config_not_applied"
    assert "local_sweep_shadow_read_timeout_zero" in verdict["reasons"]
    assert "focus_sweep_cadence_still_slow" in verdict["reasons"]
    assert "shadow_results_not_comparison_clean" in verdict["reasons"]
    assert verdict["focus_sweep"]["cadence_event_count"] == 2
    assert verdict["focus_sweep_cadence_p50_ms"] == 8000.0
    assert verdict["shadow"]["tail_read_validation_blocked"] is True
    assert verdict["shadow"]["plan_read_timeout_ms"] == [0]
    assert verdict["shadow"]["shadow_mismatch_count"] == 1
    assert verdict["foreground_forwarded_reasons"] == {"write_collect_transaction": 1}

    report = report_path.read_text(encoding="utf-8")
    assert "### Verdict" in report
    assert "config_not_applied" in report
    assert "VCI_PROXY_LOCAL_SWEEP_READ_TIMEOUT_MS=1" in report
