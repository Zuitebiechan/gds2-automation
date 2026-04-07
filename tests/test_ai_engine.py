from __future__ import annotations

import json
import queue

from diagnostic_platform.contracts import (
    DTC,
    DiagnosticPayload,
    LiveDataPoint,
    SamplingQuality,
    VehicleContext,
)
from src.diagnosis import ai_engine
from src.diagnosis.ai_engine import (
    AIEngine,
    _cache_payload,
    _diagnostic_payload_to_delta_payload,
    _payload_cache,
    _sampling_quality_to_grade,
    _sampling_quality_to_status,
    _sse_event,
    get_cached_payload,
)


def _drain_queue(events: queue.Queue[str]) -> list[dict[str, object]]:
    drained: list[dict[str, object]] = []
    while not events.empty():
        raw = events.get_nowait()
        event_type, data = raw.strip().split("\n", 1)
        drained.append(
            {
                "event": event_type.removeprefix("event: "),
                "data": json.loads(data.removeprefix("data: ")),
            }
        )
    return drained


def test_sse_event_preserves_unicode_json_payload() -> None:
    event = _sse_event("progress", {"message": "分析中", "ok": True})

    assert event == 'event: progress\ndata: {"message": "分析中", "ok": true}\n\n'


def test_sampling_quality_grade_and_status_map_contract_enums() -> None:
    assert _sampling_quality_to_grade(SamplingQuality.EXCELLENT) == "A"
    assert _sampling_quality_to_grade(SamplingQuality.GOOD) == "A"
    assert _sampling_quality_to_grade(SamplingQuality.FAIR) == "B"
    assert _sampling_quality_to_grade(SamplingQuality.POOR) == "C"

    assert _sampling_quality_to_status(SamplingQuality.EXCELLENT) == "healthy"
    assert _sampling_quality_to_status(SamplingQuality.GOOD) == "healthy"
    assert _sampling_quality_to_status(SamplingQuality.FAIR) == "degraded"
    assert _sampling_quality_to_status(SamplingQuality.POOR) == "insufficient"


def test_cache_payload_evicts_expired_entries_and_honors_ttl(monkeypatch) -> None:
    _payload_cache.clear()
    _payload_cache["expired"] = {"_cached_at": 100.0, "delta_payload": {}}

    timestamps = iter(
        [
            ai_engine.CACHE_TTL_SECONDS + 200.0,
            ai_engine.CACHE_TTL_SECONDS + 200.0,
            ai_engine.CACHE_TTL_SECONDS * 2 + 201.0,
        ]
    )
    monkeypatch.setattr(ai_engine.time, "time", lambda: next(timestamps))
    monkeypatch.setattr(ai_engine.uuid, "uuid4", lambda: "payload-1")

    payload_id = _cache_payload({"delta_payload": {"snapshot_count": 3}})

    assert payload_id == "payload-1"
    assert "expired" not in _payload_cache
    assert get_cached_payload("payload-1")["delta_payload"]["snapshot_count"] == 3
    assert get_cached_payload("payload-1") is None
    assert "payload-1" not in _payload_cache


def test_diagnostic_payload_to_delta_payload_sorts_points_builds_timeline_and_quality() -> None:
    payload = DiagnosticPayload(
        vehicle_context=VehicleContext(brand="GM", model="Malibu"),
        dtcs=[
            DTC(
                code="P0101",
                module="ECM",
                status="Active",
                description="MAF performance",
                source_backend="gds2",
            )
        ],
        live_data=[
            LiveDataPoint(parameter="RPM", value=900.0, unit="rpm", timestamp=12.0),
            LiveDataPoint(parameter="Coolant Temp", value=88.0, unit="C", timestamp=10.0),
            LiveDataPoint(parameter="RPM", value=800.0, unit="rpm", timestamp=10.0),
            LiveDataPoint(parameter="RPM", value=900.0, unit="rpm", timestamp=11.0),
            LiveDataPoint(parameter="Coolant Temp", value=88.0, unit="C", timestamp=11.0),
        ],
        sampling_quality=SamplingQuality.FAIR,
        source_backend="gds2",
    )

    delta_payload = _diagnostic_payload_to_delta_payload(payload)

    assert delta_payload["window_seconds"] == 2
    assert delta_payload["actual_duration"] == 2.0
    assert delta_payload["snapshot_count"] == 3
    assert delta_payload["initial_state"] == [
        {"name": "Coolant Temp", "value": "88.0", "unit": "C"},
        {"name": "RPM", "value": "800.0", "unit": "rpm"},
    ]
    assert delta_payload["timeline"] == [
        {"t": "1.0s", "param": "RPM", "from": "800.0", "to": "900.0", "unit": "rpm"}
    ]
    assert delta_payload["dtcs"] == [
        {
            "code": "P0101",
            "description": "MAF performance",
            "status": "Active",
            "dtc_type": "ECM",
        }
    ]
    assert delta_payload["sampling_quality"] == {
        "grade": "B",
        "status": "degraded",
        "snapshot_count": 3,
        "expected_snapshot_count": 3,
        "completeness_ratio": 1.0,
        "observed_rate_hz": 1.5,
        "target_rate_hz": 1.5,
        "gap_ms": {"avg": 0.0, "p95": 0.0, "max": 0.0},
        "lag_ms": {"avg": 0.0, "p95": 0.0, "max": 0.0},
        "degradation_reasons": ["fair"],
    }
    assert delta_payload["quality_summary"] == "[AI-Sampling] grade=B status=degraded | samples=3/3"


def test_apply_sampling_quality_confidence_caps_only_when_needed(monkeypatch) -> None:
    monkeypatch.setattr(ai_engine, "LLMClient", lambda api_key: object())
    engine = AIEngine(api_key="test")

    assert engine._apply_sampling_quality_confidence(
        {"confidence": 88, "verdict": "replace_sensor"},
        {"sampling_quality": {"grade": "B"}},
    ) == {
        "confidence": 70,
        "verdict": "replace_sensor",
        "confidence_note": "Capped from 88 due to grade B sampling quality.",
    }
    assert engine._apply_sampling_quality_confidence(
        {"confidence": 88, "verdict": "replace_sensor"},
        {"sampling_quality": {"grade": "C"}},
    ) == {
        "confidence": 40,
        "verdict": "replace_sensor",
        "confidence_note": "Capped from 88 due to grade C sampling quality.",
    }
    assert engine._apply_sampling_quality_confidence(
        {"confidence": 35, "verdict": "replace_sensor"},
        {"sampling_quality": {"grade": "C"}},
    ) == {"confidence": 35, "verdict": "replace_sensor"}
    assert engine._apply_sampling_quality_confidence(
        {"confidence": 88, "verdict": "replace_sensor"},
        {"sampling_quality": {"grade": "A"}},
    ) == {"confidence": 88, "verdict": "replace_sensor"}


def test_start_session_from_payload_emits_progress_chunks_result_and_done(monkeypatch) -> None:
    class _FakeLLMClient:
        def __init__(self, api_key: str) -> None:
            self.api_key = api_key

        def diagnose_stream(self, vehicle_context, delta_payload, brand: str, software: str):
            assert vehicle_context["brand"] == "GM"
            assert delta_payload["snapshot_count"] == 2
            assert brand == "GM"
            assert software == "GDS2"
            yield "chunk-1"
            yield "chunk-2"

        @staticmethod
        def parse_verdict(response_text: str) -> dict[str, object]:
            assert response_text == "chunk-1chunk-2"
            return {"verdict": "inspect_wiring", "confidence": 92}

    class _ImmediateThread:
        def __init__(self, target, args=(), daemon=None):
            self._target = target
            self._args = args
            self.daemon = daemon

        def start(self) -> None:
            self._target(*self._args)

    class _NoopTimer:
        def __init__(self, _seconds: float, _callback):
            self.daemon = False

        def start(self) -> None:
            return None

    monkeypatch.setattr(ai_engine, "LLMClient", _FakeLLMClient)
    monkeypatch.setattr(ai_engine.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(ai_engine.threading, "Timer", _NoopTimer)
    monkeypatch.setattr(ai_engine.uuid, "uuid4", lambda: "session-1")
    monkeypatch.setattr(ai_engine.time, "monotonic", lambda: 10.0)
    _payload_cache.clear()

    engine = AIEngine(api_key="test")
    session_id = engine.start_session_from_payload(
        {"brand": "GM", "software": "GDS2", "module": "ECM", "data_category": "Engine Data"},
        {
            "snapshot_count": 2,
            "actual_duration": 1.5,
            "dtcs": [{"code": "P0101"}],
            "timeline": [{"param": "RPM"}],
            "significant_changes": [],
            "sampling_quality": {"grade": "B"},
            "quality_summary": "summary",
            "gaps": [{"duration_ms": 2500.0}],
        },
    )

    events = _drain_queue(engine.get_event_queue(session_id))

    assert session_id == "session-1"
    assert engine.active_session_id is None
    assert [event["event"] for event in events] == [
        "progress",
        "progress",
        "llm_chunk",
        "llm_chunk",
        "result",
        "done",
    ]
    assert events[0]["data"] == {
        "phase": "assembling",
        "message": "Backend payload collected. Preparing AI analysis...",
        "sampling_quality": {"grade": "B"},
    }
    assert events[1]["data"] == {
        "phase": "analyzing",
        "message": "Sending to AI for analysis...",
    }
    assert events[2]["data"] == {"text": "chunk-1"}
    assert events[3]["data"] == {"text": "chunk-2"}
    assert events[4]["data"] == {
        "raw_response": "chunk-1chunk-2",
        "verdict": {
            "verdict": "inspect_wiring",
            "confidence": 70,
            "confidence_note": "Capped from 92 due to grade B sampling quality.",
        },
        "cached_payload_id": "session-1",
        "quality_summary": "summary",
        "data_summary": {
            "collection_duration": 1.5,
            "snapshot_count": 2,
            "dtc_count": 1,
            "timeline_events": 1,
            "significant_changes": 0,
            "sampling_quality": {"grade": "B"},
            "gaps": [{"duration_ms": 2500.0}],
        },
    }
    assert events[5]["data"] == {"session_id": "session-1"}


def test_retry_with_cached_raises_when_payload_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(ai_engine, "LLMClient", lambda api_key: object())
    engine = AIEngine(api_key="test")
    _payload_cache.clear()

    try:
        engine.retry_with_cached("missing", {"brand": "GM"})
    except RuntimeError as exc:
        assert "missing" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("retry_with_cached should fail when cache entry is missing")
