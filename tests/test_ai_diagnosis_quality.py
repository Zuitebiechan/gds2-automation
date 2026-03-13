from typing import cast
from unittest.mock import patch

from src.diagnosis.ai_engine import AIEngine
from src.diagnosis.llm_client import _build_user_message
from src.streaming.diagnostic_buffer import DiagnosticBuffer


def test_build_user_message_includes_sampling_quality_and_gaps() -> None:
    message = _build_user_message(
        {'vin': 'TESTVIN', 'module': 'ECM'},
        {
            'actual_duration': 29.9,
            'dtcs': [],
            'initial_state': [],
            'timeline': [],
            'significant_changes': [],
            'sampling_quality': {
                'grade': 'B',
                'status': 'degraded',
                'snapshot_count': 191,
                'expected_snapshot_count': 300,
                'completeness_ratio': 0.637,
                'observed_rate_hz': 6.38,
                'target_rate_hz': 10.0,
                'gap_ms': {'avg': 157.6, 'p95': 336.2, 'max': 1506.0},
                'lag_ms': {'avg': 63.8, 'p95': 106.8, 'max': 174.7},
                'degradation_reasons': ['low_snapshot_count'],
            },
            'gaps': [
                {
                    'start_t': 12.3,
                    'end_t': 13.8,
                    'duration_ms': 1506.0,
                    'reason': 'no_new_snapshot',
                }
            ],
        },
    )

    assert '### Sampling Quality' in message
    assert 'Grade: B (degraded)' in message
    assert 'Observation Gaps (1 found)' in message
    assert 'Treat explicit observation gaps as missing visibility' in message


def test_apply_sampling_quality_confidence_caps_grade_b() -> None:
    engine = AIEngine(api_key='test-key')
    verdict = {'verdict': 'action_needed', 'confidence': 99}
    delta_payload = {'sampling_quality': {'grade': 'B'}}

    adjusted = engine._apply_sampling_quality_confidence(verdict, delta_payload)

    assert adjusted is not None
    assert adjusted['confidence'] == 70
    assert 'grade B' in adjusted['confidence_note']


def test_apply_sampling_quality_confidence_caps_grade_c() -> None:
    engine = AIEngine(api_key='test-key')
    verdict = {'verdict': 'monitor', 'confidence': 88}
    delta_payload = {'sampling_quality': {'grade': 'C'}}

    adjusted = engine._apply_sampling_quality_confidence(verdict, delta_payload)

    assert adjusted is not None
    assert adjusted['confidence'] == 40
    assert 'grade C' in adjusted['confidence_note']


def test_handle_collection_guard_event_restart_resets_collection_window() -> None:
    engine = AIEngine(api_key='test-key')

    class _FakeBuffer:
        def __init__(self):
            self.cleared = False

        def clear(self) -> None:
            self.cleared = True

    buffer = _FakeBuffer()

    with patch('src.diagnosis.ai_engine.time.time', return_value=1234.5):
        new_start = engine._handle_collection_guard_event(
            session_id='session-1',
            guard_event={'message': 'Recovered', 'restart_collection': True},
            buffer=cast(DiagnosticBuffer, buffer),
            start_time=1000.0,
            elapsed=12,
        )

    assert buffer.cleared is True
    assert new_start == 1234.5
