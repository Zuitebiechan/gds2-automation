from __future__ import annotations

import queue
import threading

from src.streaming.agent_data_collector import AgentSnapshot, DTCInfo
from src.streaming.diagnostic_buffer import (
    DiagnosticBuffer,
    _is_fast_parameter,
    _percentile,
    format_sampling_quality_summary,
)


def _snapshot(
    timestamp_s: float,
    *,
    parameters: list[dict[str, str]],
    dtcs: list[DTCInfo] | None = None,
    collected_at_s: float | None = None,
    collector_lag_ms: float | None = None,
) -> AgentSnapshot:
    return AgentSnapshot(
        timestamp=int(timestamp_s * 1000),
        extraction_count=int(timestamp_s * 10) + 1,
        extraction_duration_ms=12,
        page_context={"page": "data_display"},
        parameters=parameters,
        dtcs=dtcs or [],
        table_count=1,
        agent_timestamp_s=timestamp_s,
        collected_at_s=collected_at_s,
        collector_lag_ms=collector_lag_ms,
    )


def test_percentile_handles_empty_single_and_interpolated_inputs() -> None:
    assert _percentile([], 0.95) == 0.0
    assert _percentile([7.0], 0.95) == 7.0
    assert _percentile([10.0, 20.0, 40.0, 50.0], 0.5) == 30.0


def test_is_fast_parameter_matches_common_aliases_case_insensitively() -> None:
    assert _is_fast_parameter("engine speed")
    assert _is_fast_parameter("Front O2 Sensor Voltage")
    assert _is_fast_parameter("Calculated throttle position")
    assert _is_fast_parameter("Turbo Boost Requested")
    assert not _is_fast_parameter("Engine Coolant Temperature")


def test_append_snapshot_builds_delta_payload_sampling_quality_and_significant_changes() -> None:
    buffer = DiagnosticBuffer(window_seconds=30)
    dtc = DTCInfo(
        control_module="ECM",
        dtc_type="Current",
        code="P0101",
        symptom_byte="00",
        description="MAF performance",
        symptom_description="Range/performance",
        status="Active",
    )

    buffer.append_snapshot(
        _snapshot(
            100.0,
            parameters=[
                {"name": "Engine Speed", "value": "800", "unit": "RPM"},
                {"name": "Coolant Temp", "value": "90", "unit": "C"},
            ],
            dtcs=[dtc],
            collector_lag_ms=50.0,
        )
    )
    buffer.append_snapshot(
        _snapshot(
            100.5,
            parameters=[
                {"name": "Engine Speed", "value": "1200", "unit": "RPM"},
                {"name": "Coolant Temp", "value": "91", "unit": "C"},
            ],
            dtcs=[dtc],
            collector_lag_ms=250.0,
        )
    )
    buffer.append_snapshot(
        _snapshot(
            103.1,
            parameters=[
                {"name": "Engine Speed", "value": "1200", "unit": "RPM"},
            ],
            dtcs=[dtc],
            collector_lag_ms=100.0,
        )
    )

    payload = buffer.get_delta_payload()

    assert buffer.snapshot_count == 3
    assert payload["actual_duration"] == 3.1
    assert payload["dtcs"] == [dtc.to_dict()]
    assert payload["initial_state"] == [
        {"name": "Engine Speed", "value": "800", "unit": "RPM"},
        {"name": "Coolant Temp", "value": "90", "unit": "C"},
    ]
    assert payload["timeline"] == [
        {"t": "0.5s", "param": "Engine Speed", "from": "800", "to": "1200", "unit": "RPM"}
    ]
    assert payload["significant_changes"] == [
        {
            "parameter": "Engine Speed",
            "from": "800",
            "to": "1200",
            "unit": "RPM",
            "change_percent": 50.0,
            "at": "0.5s",
            "duration": "0.5s",
        }
    ]
    assert payload["sampling_quality"]["grade"] == "C"
    assert payload["sampling_quality"]["status"] == "insufficient"
    assert payload["sampling_quality"]["degradation_reasons"] == [
        "low_snapshot_count",
        "large_gap",
        "high_p95_gap",
    ]
    assert payload["sampling_quality"]["gap_count"] == 1
    assert payload["sampling_quality"]["gap_ms"] == {"avg": 1550.0, "p95": 2495.0, "max": 2600.0}
    assert payload["sampling_quality"]["lag_ms"] == {"avg": 133.3, "p95": 235.0, "max": 250.0}
    assert payload["gaps"] == [
        {
            "start_t": 0.5,
            "end_t": 3.1,
            "duration_ms": 2600.0,
            "reason": "no_new_snapshot",
        }
    ]
    assert payload["quality_summary"].startswith("[AI-Sampling] grade=C status=insufficient")


def test_append_snapshot_skips_frequent_slow_parameters_and_counts_stale_snapshots() -> None:
    buffer = DiagnosticBuffer(window_seconds=30)

    buffer.append_snapshot(
        _snapshot(
            10.0,
            parameters=[{"name": "Coolant Temp", "value": "90", "unit": "C"}],
        )
    )
    buffer.append_snapshot(
        _snapshot(
            10.4,
            parameters=[{"name": "Coolant Temp", "value": "91", "unit": "C"}],
        )
    )

    payload = buffer.get_delta_payload()

    assert payload["initial_state"] == [{"name": "Coolant Temp", "value": "90", "unit": "C"}]
    assert payload["timeline"] == []
    assert payload["sampling_quality"]["stale_ratio"] == 0.5


def test_append_snapshot_trims_window_but_keeps_last_known_value_for_initial_state() -> None:
    buffer = DiagnosticBuffer(window_seconds=2)

    buffer.append_snapshot(
        _snapshot(
            1.0,
            parameters=[{"name": "Engine Speed", "value": "800", "unit": "RPM"}],
        )
    )
    buffer.append_snapshot(
        _snapshot(
            2.0,
            parameters=[{"name": "Engine Speed", "value": "900", "unit": "RPM"}],
        )
    )
    buffer.append_snapshot(
        _snapshot(
            4.5,
            parameters=[{"name": "Engine Speed", "value": "1000", "unit": "RPM"}],
        )
    )

    payload = buffer.get_delta_payload()

    assert buffer.duration_seconds == 0.0
    assert payload["actual_duration"] == 0.0
    assert payload["initial_state"] == [{"name": "Engine Speed", "value": "1000", "unit": "RPM"}]
    assert payload["timeline"] == []


def test_get_raw_payload_returns_cached_payload_without_blocking() -> None:
    buffer = DiagnosticBuffer(window_seconds=30)
    buffer.append_snapshot(
        _snapshot(
            100.0,
            parameters=[{"name": "Engine Speed", "value": "800", "unit": "RPM"}],
        )
    )

    result_queue: queue.Queue[dict[str, object]] = queue.Queue()

    def _call() -> None:
        try:
            result_queue.put({"payload": buffer.get_raw_payload()})
        except Exception as exc:  # pragma: no cover - assertion will fail below
            result_queue.put({"error": exc})

    worker = threading.Thread(target=_call, daemon=True)
    worker.start()
    worker.join(timeout=0.5)

    assert worker.is_alive() is False
    result = result_queue.get_nowait()
    assert "error" not in result
    payload = result["payload"]
    assert payload["snapshot_count"] == 1
    assert payload["delta_payload"]["initial_state"] == [
        {"name": "Engine Speed", "value": "800", "unit": "RPM"}
    ]
    assert isinstance(payload["cached_at"], str)


def test_clear_resets_all_buffer_state() -> None:
    buffer = DiagnosticBuffer(window_seconds=30)
    buffer.append_snapshot(
        _snapshot(
            100.0,
            parameters=[{"name": "Engine Speed", "value": "800", "unit": "RPM"}],
        )
    )

    buffer.clear()

    assert buffer.snapshot_count == 0
    assert buffer.duration_seconds == 0.0
    assert buffer.get_delta_payload() == {
        "window_seconds": 30,
        "actual_duration": 0,
        "snapshot_count": 0,
        "initial_state": [],
        "timeline": [],
        "dtcs": [],
        "significant_changes": [],
    }


def test_format_sampling_quality_summary_renders_expected_one_line_output() -> None:
    summary = format_sampling_quality_summary(
        {
            "grade": "B",
            "status": "degraded",
            "snapshot_count": 200,
            "expected_snapshot_count": 300,
            "gap_ms": {"avg": 123.0, "p95": 456.0, "max": 789.0},
            "lag_ms": {"avg": 10.0, "p95": 20.0, "max": 30.0},
            "stale_ratio": 0.125,
        }
    )

    assert summary == (
        "[AI-Sampling] grade=B status=degraded | samples=200/300 | "
        "gap_ms(avg/p95/max)=123/456/789 | lag_ms(avg/p95/max)=10/20/30 | stale=12.5%"
    )
