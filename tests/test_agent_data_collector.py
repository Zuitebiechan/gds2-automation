import json
import os
from pathlib import Path

from diagnostic_platform.observability import ActiveSessionSnapshotStore, flush_product_log_writers
from src.streaming import agent_data_collector as collector_module
from src.streaming.agent_data_collector import (
    AgentDataCollector,
    _guard_event_signature,
    _parse_agent_json,
)


class _StableValue:
    def __str__(self) -> str:
        return "stable-value"


def _write_latest_json(path, *, extraction_count=1, version="2.0"):
    path.write_text(
        json.dumps(
            {
                "extractionCount": extraction_count,
                "version": version,
            },
            ensure_ascii=False,
        ),
        encoding="gbk",
    )


def _read_cloud_events(tmp_path: Path) -> list[dict[str, object]]:
    flush_product_log_writers()
    raw_dir = tmp_path / "RPA_Diagnostic" / "observability" / "cloud" / "raw"
    records: list[dict[str, object]] = []
    for path in sorted(raw_dir.glob("*.jsonl")):
        records.extend(
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    return records


def test_check_agent_available_retries_transient_json_error(tmp_path, monkeypatch):
    json_path = tmp_path / "latest.json"
    _write_latest_json(json_path, extraction_count=42, version="2.0")

    now = 1_000.0
    mtime = now - 2.0
    os.utime(json_path, (mtime, mtime))

    collector = AgentDataCollector(json_path=json_path)
    real_json_load = collector_module.json.load
    attempts = {"count": 0}

    def flaky_json_load(handle):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise json.JSONDecodeError("partial write", "{}", 0)
        return real_json_load(handle)

    monkeypatch.setattr(collector_module.time, "time", lambda: now)
    monkeypatch.setattr(collector_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(collector_module.json, "load", flaky_json_load)

    result = collector.check_agent_available()

    assert result["available"] is True
    assert result["extraction_count"] == 42
    assert attempts["count"] >= 2
    assert result["attempts"] >= 1
    assert result["error"] is None


def test_check_agent_available_reports_error_after_retry_exhaustion(tmp_path, monkeypatch):
    json_path = tmp_path / "latest.json"
    _write_latest_json(json_path, extraction_count=7)

    now = 1_000.0
    mtime = now - 1.0
    os.utime(json_path, (mtime, mtime))

    collector = AgentDataCollector(json_path=json_path)
    attempts = {"count": 0}

    def always_bad_json(_handle):
        attempts["count"] += 1
        raise json.JSONDecodeError("partial write", "{}", 0)

    monkeypatch.setattr(collector_module.time, "time", lambda: now)
    monkeypatch.setattr(collector_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(collector_module.json, "load", always_bad_json)

    result = collector.check_agent_available()

    assert result["available"] is False
    assert result["exists"] is True
    assert result["attempts"] >= 2
    assert "partial write" in result["error"]


def test_check_agent_available_still_rejects_stale_file(tmp_path, monkeypatch):
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    json_path = tmp_path / "latest.json"
    _write_latest_json(json_path, extraction_count=9)

    now = 1_000.0
    mtime = now - 12.0
    os.utime(json_path, (mtime, mtime))

    collector = AgentDataCollector(json_path=json_path)
    monkeypatch.setattr(collector_module.time, "time", lambda: now)

    result = collector.check_agent_available()

    assert result["available"] is False
    assert result["age_seconds"] == 12.0
    assert result["extraction_count"] == 9
    assert "agent.collector.availability" in [event["event_type"] for event in _read_cloud_events(tmp_path)]


def test_run_page_guard_emits_guard_failed_event(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    ActiveSessionSnapshotStore().write(
        {
            "session_id": "session-collector-1",
            "backend_name": "gds2",
            "operation_kind": "live_data.start",
            "selected_module": "Engine Control Module",
            "selected_data_category": "Engine Data",
            "current_page": "data_display",
            "navigation_session_id": None,
            "ai_session_id": None,
            "live_data_active": True,
            "connection_epoch": "epoch-collector-1",
        }
    )
    collector = AgentDataCollector(
        page_guard=lambda: {"ok": False, "error": "Data Display guard failed."},
        on_error=lambda _message: None,
        json_path=tmp_path / "latest.json",
    )
    collector._running = True

    result = collector._run_page_guard()

    assert result == {"ok": False, "error": "Data Display guard failed."}
    assert collector.fatal_error == "Data Display guard failed."
    events = _read_cloud_events(tmp_path)
    guard_failed = next(event for event in events if event["event_type"] == "agent.collector.guard_failed")
    assert guard_failed["session_id"] == "session-collector-1"
    assert guard_failed["connection_epoch"] == "epoch-collector-1"
    assert guard_failed["module"] == "Engine Control Module"
    assert guard_failed["data_category"] == "Engine Data"


def test_guard_event_signature_stringifies_non_json_values() -> None:
    signature = _guard_event_signature(
        {
            "ok": True,
            "detail": _StableValue(),
            "error": RuntimeError("boom"),
        }
    )

    assert signature == '{"detail": "stable-value", "error": "boom", "ok": true}'


def test_parse_agent_json_reads_v2_tables_and_normalizes_timestamp() -> None:
    snapshot = _parse_agent_json(
        {
            "timestamp": 1_710_000_000_123,
            "extractionCount": 7,
            "extractionDurationMs": 45,
            "pageContext": {"page": "data_display"},
            "tables": [
                {
                    "tableType": "data_display",
                    "columns": ["Module", "Parameter Name", "Value", "Units"],
                    "rows": [
                        {"Module": "ECM", "Parameter Name": "RPM", "Value": 900, "Units": "rpm"},
                        {"Module": "ECM", "Parameter Name": "", "Value": 1, "Units": ""},
                    ],
                },
                {
                    "tableType": "dtc",
                    "columns": ["Control Module", "DTC", "Description", "Status"],
                    "rows": [
                        {
                            "Control Module": "ECM",
                            "DTC": "P0101",
                            "Description": "MAF performance",
                            "Status": "Active",
                        },
                        {
                            "Control Module": "ECM",
                            "DTC": "",
                            "Description": "ignored",
                            "Status": "History",
                        },
                    ],
                },
            ],
        }
    )

    assert snapshot.extraction_count == 7
    assert snapshot.extraction_duration_ms == 45
    assert snapshot.page_context == {"page": "data_display"}
    assert snapshot.table_count == 2
    assert len(snapshot.raw_tables) == 2
    assert snapshot.agent_timestamp_s == 1_710_000_000.123
    assert snapshot.parameters == [
        {"module": "ECM", "name": "RPM", "value": "900", "unit": "rpm"}
    ]
    assert [dtc.code for dtc in snapshot.dtcs] == ["P0101"]
    assert snapshot.dtcs[0].description == "MAF performance"


def test_parse_agent_json_falls_back_to_v1_tableview_controls() -> None:
    snapshot = _parse_agent_json(
        {
            "timestamp": 123.0,
            "windows": [
                {
                    "controls": [
                        {
                            "type": "Label",
                            "text": "ignored",
                        },
                        {
                            "type": "TableView",
                            "columns": ["Module", "Parameter Name", "Value", "Unit"],
                            "rows": [
                                {
                                    "Module": "ECM",
                                    "Parameter Name": "Coolant Temp",
                                    "Value": 88,
                                    "Unit": "C",
                                }
                            ],
                        },
                        {
                            "type": "TableView",
                            "columns": ["Control Module", "DTC", "Description", "Status"],
                            "rows": [
                                {
                                    "Control Module": "ABS",
                                    "DTC": "C0035",
                                    "Description": "Wheel speed sensor",
                                    "Status": "Current",
                                }
                            ],
                        },
                    ]
                }
            ],
        }
    )

    assert snapshot.table_count == 0
    assert snapshot.raw_tables == []
    assert snapshot.agent_timestamp_s == 123.0
    assert snapshot.parameters == [
        {"module": "ECM", "name": "Coolant Temp", "value": "88", "unit": "C"}
    ]
    assert [dtc.code for dtc in snapshot.dtcs] == ["C0035"]
