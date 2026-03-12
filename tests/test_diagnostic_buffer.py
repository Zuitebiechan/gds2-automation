from src.streaming.agent_data_collector import AgentSnapshot
from src.streaming.diagnostic_buffer import (
    DiagnosticBuffer,
    format_sampling_quality_summary,
)


def _make_snapshot(ts_seconds: float, value: str, lag_ms: float = 50.0) -> AgentSnapshot:
    snapshot = AgentSnapshot(
        timestamp=int(ts_seconds * 1000),
        extraction_count=1,
        extraction_duration_ms=25,
        page_context={},
        parameters=[
            {
                'module': 'ECM',
                'name': 'Engine Speed',
                'value': value,
                'unit': 'RPM',
            }
        ],
        dtcs=[],
        table_count=1,
        agent_timestamp_s=ts_seconds,
        collected_at_s=ts_seconds + (lag_ms / 1000.0),
        collector_lag_ms=lag_ms,
    )
    return snapshot


def test_sampling_quality_ready_for_stable_stream() -> None:
    buffer = DiagnosticBuffer(window_seconds=30)

    for index in range(240):
        timestamp_s = 1000.0 + (index * 0.1)
        value = str(800 + (index % 3))
        buffer.append_snapshot(_make_snapshot(timestamp_s, value))

    payload = buffer.get_delta_payload()
    quality = payload['sampling_quality']

    assert quality['grade'] == 'A'
    assert quality['status'] == 'ready'
    assert quality['snapshot_count'] == 240
    assert quality['gap_ms']['max'] <= 800.0
    assert payload['gaps'] == []
    assert 'grade=A' in payload['quality_summary']


def test_sampling_quality_marks_large_gaps_as_degraded() -> None:
    buffer = DiagnosticBuffer(window_seconds=30)

    timestamps = [
        1000.0,
        1000.1,
        1000.2,
        1003.5,
        1003.6,
        1003.7,
    ]

    for index, timestamp_s in enumerate(timestamps):
        buffer.append_snapshot(_make_snapshot(timestamp_s, str(900 + index), lag_ms=120.0))

    payload = buffer.get_delta_payload()
    quality = payload['sampling_quality']

    assert quality['grade'] == 'C'
    assert quality['status'] == 'insufficient'
    assert 'large_gap' in quality['degradation_reasons']
    assert quality['gap_count'] == 1
    assert payload['gaps'][0]['duration_ms'] > 2000.0


def test_quality_summary_formatter_is_human_readable() -> None:
    summary = format_sampling_quality_summary(
        {
            'grade': 'B',
            'status': 'degraded',
            'snapshot_count': 210,
            'expected_snapshot_count': 300,
            'gap_ms': {'avg': 110.0, 'p95': 420.0, 'max': 1400.0},
            'lag_ms': {'avg': 35.0, 'p95': 90.0, 'max': 180.0},
            'stale_ratio': 0.05,
        }
    )

    assert '[AI-Sampling]' in summary
    assert 'grade=B' in summary
    assert 'samples=210/300' in summary
    assert 'stale=5.0%' in summary
