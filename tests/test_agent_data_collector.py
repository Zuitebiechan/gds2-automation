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


def _write_agent_payload(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="gbk")


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


def test_read_and_parse_emits_focus_parameter_samples(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PRODUCT_LOG_CLOUD_ROOT", raising=False)
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    monkeypatch.setattr(collector_module.time, "time", lambda: 1_710_000_000.223)
    ActiveSessionSnapshotStore().write(
        {
            "session_id": "session-collector-values",
            "backend_name": "gds2",
            "operation_kind": "live_data.start",
            "selected_module": "Engine Control Module",
            "selected_data_category": "Engine Data",
            "current_page": "data_display",
            "navigation_session_id": None,
            "ai_session_id": None,
            "live_data_active": True,
            "connection_epoch": "epoch-collector-values",
        }
    )
    json_path = tmp_path / "latest.json"
    _write_agent_payload(
        json_path,
        {
            "timestamp": 1_710_000_000_123,
            "extractionCount": 11,
            "extractionDurationMs": 8,
            "pageContext": {"page": "data_display"},
            "tables": [
                {
                    "tableType": "data_display",
                    "columns": ["Control Module", "Parameter Name", "Value", "Unit"],
                    "rows": [
                        {
                            "Control Module": " Engine Control Module",
                            "Parameter Name": " Engine Speed",
                            "Value": "900 ",
                            "Unit": " RPM",
                        },
                        {
                            "Control Module": " Engine Control Module",
                            "Parameter Name": " Accelerator Pedal Position",
                            "Value": "12 ",
                            "Unit": " %",
                        },
                        {
                            "Control Module": " Engine Control Module",
                            "Parameter Name": " Coolant Temp",
                            "Value": "88 ",
                            "Unit": " C",
                        },
                    ],
                }
            ],
        },
    )
    collector = AgentDataCollector(json_path=json_path)

    snapshot = collector._read_and_parse()

    assert snapshot is not None
    events = _read_cloud_events(tmp_path)
    focus = next(
        event
        for event in events
        if event["event_type"] == "agent.collector.focus_parameters_sampled"
    )
    assert focus["session_id"] == "session-collector-values"
    assert focus["connection_epoch"] == "epoch-collector-values"
    assert focus["operation_kind"] == "agent_data_value_sample"
    assert focus["page"] == "data_display"
    assert focus["module"] == "Engine Control Module"
    assert focus["data_category"] == "Engine Data"
    assert focus["extraction_count"] == 11
    assert focus["extraction_duration_ms"] == 8
    assert focus["agent_timestamp_s"] == 1_710_000_000.123
    assert focus["collector_lag_ms"] == 100.0
    assert focus["missing_target_keys"] == ["battery_voltage"]
    assert focus["parameter_values"] == {
        "engine_speed": "900",
        "accelerator_pedal_position": "12",
    }
    assert focus["parameter_value_sources"] == {
        "engine_speed": "Engine Speed",
        "accelerator_pedal_position": "Accelerator Pedal Position",
    }
    assert focus["parameters"] == [
        {
            "key": "engine_speed",
            "name": "Engine Speed",
            "value": "900",
            "unit": "RPM",
            "module": "Engine Control Module",
            "changed": False,
        },
        {
            "key": "accelerator_pedal_position",
            "name": "Accelerator Pedal Position",
            "value": "12",
            "unit": "%",
            "module": "Engine Control Module",
            "changed": False,
        },
    ]
    assert [
        event
        for event in events
        if event["event_type"] == "agent.collector.focus_value_changed"
    ] == []

    _write_agent_payload(
        json_path,
        {
            "timestamp": 1_710_000_000_223,
            "extractionCount": 12,
            "extractionDurationMs": 7,
            "pageContext": {"page": "data_display"},
            "tables": [
                {
                    "tableType": "data_display",
                    "columns": ["Control Module", "Parameter Name", "Value", "Unit"],
                    "rows": [
                        {
                            "Control Module": " Engine Control Module",
                            "Parameter Name": " Engine Speed",
                            "Value": "1100 ",
                            "Unit": " RPM",
                        },
                        {
                            "Control Module": " Engine Control Module",
                            "Parameter Name": " Accelerator Pedal Position",
                            "Value": "38 ",
                            "Unit": " %",
                        },
                    ],
                }
            ],
        },
    )
    next_mtime = json_path.stat().st_mtime + 1.0
    os.utime(json_path, (next_mtime, next_mtime))

    collector._read_and_parse()

    focus_events = [
        event
        for event in _read_cloud_events(tmp_path)
        if event["event_type"] == "agent.collector.focus_parameters_sampled"
    ]
    change_events = [
        event
        for event in _read_cloud_events(tmp_path)
        if event["event_type"] == "agent.collector.focus_value_changed"
    ]
    assert focus_events[-1]["parameter_values"] == {
        "engine_speed": "1100",
        "accelerator_pedal_position": "38",
    }
    assert focus_events[-1]["parameter_value_sources"] == {
        "engine_speed": "Engine Speed",
        "accelerator_pedal_position": "Accelerator Pedal Position",
    }
    assert focus_events[-1]["parameters"][0]["changed"] is True
    assert focus_events[-1]["parameters"][0]["previous_value"] == "900"
    assert {event["focus_key"] for event in change_events} == {
        "engine_speed",
        "accelerator_pedal_position",
    }
    engine_speed_change = next(
        event for event in change_events if event["focus_key"] == "engine_speed"
    )
    assert engine_speed_change["previous_value"] == "900"
    assert engine_speed_change["current_value"] == "1100"
    assert engine_speed_change["source_parameter_name"] == "Engine Speed"
    assert engine_speed_change["previous_value_number"] == 900.0
    assert engine_speed_change["current_value_number"] == 1100.0
    pedal_change = next(
        event
        for event in change_events
        if event["focus_key"] == "accelerator_pedal_position"
    )
    assert pedal_change["previous_value"] == "12"
    assert pedal_change["current_value"] == "38"
    assert pedal_change["source_parameter_name"] == "Accelerator Pedal Position"
    assert pedal_change["previous_value_number"] == 12.0
    assert pedal_change["current_value_number"] == 38.0


def test_read_and_parse_emits_oem_voltage_aliases_as_battery_voltage(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("PRODUCT_LOG_CLOUD_ROOT", raising=False)
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    monkeypatch.setattr(collector_module.time, "time", lambda: 1_710_000_100.200)
    ActiveSessionSnapshotStore().write(
        {
            "session_id": "session-collector-voltage",
            "backend_name": "gds2",
            "operation_kind": "live_data.start",
            "selected_module": "Engine Control Module",
            "selected_data_category": "Engine Data",
            "current_page": "data_display",
            "navigation_session_id": None,
            "ai_session_id": None,
            "live_data_active": True,
            "connection_epoch": "epoch-collector-voltage",
        }
    )
    json_path = tmp_path / "latest.json"
    _write_agent_payload(
        json_path,
        {
            "timestamp": 1_710_000_100_120,
            "extractionCount": 21,
            "extractionDurationMs": 6,
            "pageContext": {"page": "data_display"},
            "tables": [
                {
                    "tableType": "data_display",
                    "columns": ["Control Module", "Parameter Name", "Value", "Unit"],
                    "rows": [
                        {
                            "Control Module": " Engine Control Module",
                            "Parameter Name": " Ignition 1 Signal",
                            "Value": "12.4 ",
                            "Unit": " V",
                        },
                        {
                            "Control Module": " Engine Control Module",
                            "Parameter Name": " Engine Controls Ignition Relay Feedback 2 Signal",
                            "Value": "12.4 ",
                            "Unit": " V",
                        },
                        {
                            "Control Module": " Engine Control Module",
                            "Parameter Name": " Reference Voltage",
                            "Value": "5.0 ",
                            "Unit": " V",
                        },
                        {
                            "Control Module": " Engine Control Module",
                            "Parameter Name": " Engine Speed",
                            "Value": "900 ",
                            "Unit": " RPM",
                        },
                    ],
                }
            ],
        },
    )
    collector = AgentDataCollector(json_path=json_path)

    snapshot = collector._read_and_parse()

    assert snapshot is not None
    focus = next(
        event
        for event in _read_cloud_events(tmp_path)
        if event["event_type"] == "agent.collector.focus_parameters_sampled"
    )
    assert focus["missing_target_keys"] == ["accelerator_pedal_position"]
    assert focus["parameter_values"] == {
        "engine_speed": "900",
        "battery_voltage": "12.4",
    }
    assert focus["parameter_value_sources"] == {
        "engine_speed": "Engine Speed",
        "battery_voltage": "Engine Controls Ignition Relay Feedback 2 Signal",
    }
    assert focus["parameters"] == [
        {
            "key": "engine_speed",
            "name": "Engine Speed",
            "value": "900",
            "unit": "RPM",
            "module": "Engine Control Module",
            "changed": False,
        },
        {
            "key": "battery_voltage",
            "name": "Engine Controls Ignition Relay Feedback 2 Signal",
            "value": "12.4",
            "unit": "V",
            "module": "Engine Control Module",
            "changed": False,
        },
        {
            "key": "battery_voltage",
            "name": "Ignition 1 Signal",
            "value": "12.4",
            "unit": "V",
            "module": "Engine Control Module",
            "changed": False,
        },
    ]

    _write_agent_payload(
        json_path,
        {
            "timestamp": 1_710_000_100_220,
            "extractionCount": 22,
            "extractionDurationMs": 5,
            "pageContext": {"page": "data_display"},
            "tables": [
                {
                    "tableType": "data_display",
                    "columns": ["Control Module", "Parameter Name", "Value", "Unit"],
                    "rows": [
                        {
                            "Control Module": " Engine Control Module",
                            "Parameter Name": " Ignition 1 Signal",
                            "Value": "12.9 ",
                            "Unit": " V",
                        },
                        {
                            "Control Module": " Engine Control Module",
                            "Parameter Name": " Engine Controls Ignition Relay Feedback 2 Signal",
                            "Value": "12.9 ",
                            "Unit": " V",
                        },
                        {
                            "Control Module": " Engine Control Module",
                            "Parameter Name": " Engine Speed",
                            "Value": "950 ",
                            "Unit": " RPM",
                        },
                    ],
                }
            ],
        },
    )
    next_mtime = json_path.stat().st_mtime + 1.0
    os.utime(json_path, (next_mtime, next_mtime))

    collector._read_and_parse()

    focus_events = [
        event
        for event in _read_cloud_events(tmp_path)
        if event["event_type"] == "agent.collector.focus_parameters_sampled"
    ]
    change_events = [
        event
        for event in _read_cloud_events(tmp_path)
        if event["event_type"] == "agent.collector.focus_value_changed"
    ]
    assert focus_events[-1]["parameter_values"] == {
        "engine_speed": "950",
        "battery_voltage": "12.9",
    }
    assert focus_events[-1]["parameter_value_sources"] == {
        "engine_speed": "Engine Speed",
        "battery_voltage": "Engine Controls Ignition Relay Feedback 2 Signal",
    }
    assert focus_events[-1]["parameters"][1]["changed"] is True
    assert focus_events[-1]["parameters"][1]["previous_value"] == "12.4"
    assert focus_events[-1]["parameters"][2]["changed"] is True
    assert focus_events[-1]["parameters"][2]["previous_value"] == "12.4"
    assert {event["focus_key"] for event in change_events} == {
        "engine_speed",
        "battery_voltage",
    }
    battery_change = next(
        event for event in change_events if event["focus_key"] == "battery_voltage"
    )
    assert battery_change["previous_value"] == "12.4"
    assert battery_change["current_value"] == "12.9"
    assert (
        battery_change["source_parameter_name"]
        == "Engine Controls Ignition Relay Feedback 2 Signal"
    )
    assert battery_change["source_parameter_unit"] == "V"
    assert battery_change["source_parameter_module"] == "Engine Control Module"
    assert battery_change["previous_value_number"] == 12.4
    assert battery_change["current_value_number"] == 12.9


def test_battery_voltage_prefers_numeric_alias_over_state_value(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("PRODUCT_LOG_CLOUD_ROOT", raising=False)
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    monkeypatch.setattr(collector_module.time, "time", lambda: 1_710_000_200.200)
    ActiveSessionSnapshotStore().write(
        {
            "session_id": "session-collector-voltage-priority",
            "backend_name": "gds2",
            "operation_kind": "live_data.start",
            "selected_module": "Engine Control Module",
            "selected_data_category": "Engine Data",
            "current_page": "data_display",
            "navigation_session_id": None,
            "ai_session_id": None,
            "live_data_active": True,
            "connection_epoch": "epoch-collector-voltage-priority",
        }
    )
    json_path = tmp_path / "latest.json"
    _write_agent_payload(
        json_path,
        {
            "timestamp": 1_710_000_200_120,
            "extractionCount": 31,
            "extractionDurationMs": 4,
            "pageContext": {"page": "data_display"},
            "tables": [
                {
                    "tableType": "data_display",
                    "columns": ["Control Module", "Parameter Name", "Value", "Unit"],
                    "rows": [
                        {
                            "Control Module": " Engine Control Module",
                            "Parameter Name": " Ignition 1 Signal",
                            "Value": " On ",
                            "Unit": "",
                        },
                        {
                            "Control Module": " Engine Control Module",
                            "Parameter Name": " Engine Controls Ignition Relay Feedback 2 Signal",
                            "Value": "11.8 ",
                            "Unit": " V",
                        },
                    ],
                }
            ],
        },
    )
    collector = AgentDataCollector(json_path=json_path)

    snapshot = collector._read_and_parse()

    assert snapshot is not None
    focus = next(
        event
        for event in _read_cloud_events(tmp_path)
        if event["event_type"] == "agent.collector.focus_parameters_sampled"
    )
    assert focus["parameter_values"]["battery_voltage"] == "11.8"
    assert (
        focus["parameter_value_sources"]["battery_voltage"]
        == "Engine Controls Ignition Relay Feedback 2 Signal"
    )


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
