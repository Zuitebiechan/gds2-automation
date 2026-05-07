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
    assert "# Battery Voltage Freshness Report" in report_path.read_text(encoding="utf-8")
