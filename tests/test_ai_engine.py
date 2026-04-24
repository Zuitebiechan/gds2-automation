from __future__ import annotations

import json
import queue
from pathlib import Path

import pytest

from diagnostic_platform.observability import flush_product_log_writers
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


class _StableValue:
    def __str__(self) -> str:
        return "stable-value"


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


def _read_ai_engine_events(tmp_path: Path) -> list[dict[str, object]]:
    flush_product_log_writers()
    raw_dir = tmp_path / "RPA_Diagnostic" / "observability" / "cloud" / "raw"
    records: list[dict[str, object]] = []
    for path in sorted(raw_dir.glob("*.jsonl")):
        if not path.name.startswith("ai_engine-"):
            continue
        records.extend(
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    return records


def test_sse_event_preserves_unicode_json_payload() -> None:
    event = _sse_event("progress", {"message": "分析中", "ok": True})

    assert event == 'event: progress\ndata: {"message": "分析中", "ok": true}\n\n'


def test_sse_event_stringifies_non_json_payload_values() -> None:
    event = _sse_event(
        RuntimeError("progress"),
        {
            "detail": RuntimeError("boom"),
            "value": _StableValue(),
        },
    )

    assert event == 'event: progress\ndata: {"detail": "boom", "value": "stable-value"}\n\n'


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


def test_emit_keeps_essential_ai_events_when_queue_is_full(monkeypatch) -> None:
    monkeypatch.setattr(ai_engine, "LLMClient", lambda api_key: object())
    engine = AIEngine(api_key="test")
    engine._event_queues["session-1"] = queue.Queue(maxsize=1)
    engine._event_queues["session-1"].put_nowait(
        'event: progress\ndata: {"phase": "assembling"}\n\n'
    )

    engine._emit("session-1", "done", {"session_id": "session-1"})

    assert engine.get_event_queue("session-1").get_nowait() == (
        'event: done\ndata: {"session_id": "session-1"}\n\n'
    )


def test_start_session_from_payload_emits_error_and_observability_on_provider_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class _ProviderPermissionError(RuntimeError):
        def __init__(self) -> None:
            super().__init__(
                "Error code: 403 - {'error': {'message': 'This API key is not allowed to use any enabled OpenAI provider.', 'type': 'authentication_error'}, 'type': 'error'}"
            )
            self.status_code = 403
            self.body = {
                "error": {
                    "message": "This API key is not allowed to use any enabled OpenAI provider.",
                    "type": "authentication_error",
                },
                "type": "error",
            }

    class _FakeLLMClient:
        def __init__(self, api_key: str, model: str = "gpt-5.4", *, base_url: str | None = None):
            self.api_key = api_key
            self.model = model
            self.base_url = base_url

        def diagnose_stream(self, vehicle_context, delta_payload, brand: str, software: str):
            raise _ProviderPermissionError()

        def provider_metadata(self) -> dict[str, object]:
            return {
                "model": self.model,
                "base_url": self.base_url,
                "reasoning_effort": "none",
            }

        @staticmethod
        def parse_verdict(response_text: str) -> dict[str, object]:
            raise AssertionError("parse_verdict must not run on provider failure")

    class _NoopTimer:
        def __init__(self, _seconds: float, _callback):
            self.daemon = False

        def start(self) -> None:
            return None

    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    monkeypatch.setattr(ai_engine, "LLMClient", _FakeLLMClient)
    monkeypatch.setattr(ai_engine.threading, "Timer", _NoopTimer)
    monkeypatch.setattr(ai_engine.uuid, "uuid4", lambda: "session-403")
    _payload_cache.clear()

    engine = AIEngine(api_key="test", base_url="https://moacode.org/team/v1")
    session_id = "session-403"
    engine._active_session = session_id
    engine._event_queues[session_id] = queue.Queue(maxsize=500)
    engine._payload_worker(
        session_id,
        {
            "session_id": "business-session-1",
            "brand": "GM",
            "software": "GDS2",
            "module": "ECM",
            "data_category": "Engine Data",
        },
        {
            "snapshot_count": 2,
            "actual_duration": 1.5,
            "dtcs": [{"code": "P0101"}],
            "timeline": [{"param": "RPM"}],
            "significant_changes": [],
            "sampling_quality": {"grade": "A"},
            "quality_summary": "summary",
            "gaps": [],
        },
    )

    events = _drain_queue(engine.get_event_queue(session_id))

    assert [event["event"] for event in events] == [
        "progress",
        "progress",
        "error",
        "done",
    ]
    assert events[2]["data"] == {
        "error": "AI analysis failed: This API key is not allowed to use any enabled OpenAI provider. (HTTP 403)",
        "cached_payload_id": "session-403",
        "retryable": True,
    }
    assert events[3]["data"] == {"session_id": "session-403"}

    records = _read_ai_engine_events(tmp_path)
    provider_errors = [record for record in records if record.get("event_type") == "ai.provider.error"]

    assert len(provider_errors) == 1
    assert provider_errors[0]["component"] == "ai_engine"
    assert provider_errors[0]["session_id"] == "business-session-1"
    assert provider_errors[0]["failure_code"] == "ai_provider_http_403"
    assert provider_errors[0]["failure_domain"] == "session_runtime"
    assert provider_errors[0]["operation_kind"] == "ai.provider_request"
    assert provider_errors[0]["module"] == "ECM"
    assert provider_errors[0]["data_category"] == "Engine Data"
    assert provider_errors[0]["ai_session_id"] == "session-403"
    assert provider_errors[0]["ai_provider_model"] == "gpt-5.4"
    assert provider_errors[0]["ai_provider_base_url"] == "https://moacode.org/team/v1"
    assert provider_errors[0]["provider_status_code"] == 403
    assert provider_errors[0]["provider_error_type"] == "authentication_error"
    assert (
        provider_errors[0]["provider_message"]
        == "This API key is not allowed to use any enabled OpenAI provider."
    )


def test_ai_engine_verify_ready_runs_provider_preflight(monkeypatch) -> None:
    observed: dict[str, object] = {}

    class _FakeLLMClient:
        def __init__(self, api_key: str, model: str = "gpt-5.4", *, base_url: str | None = None):
            observed["api_key"] = api_key
            observed["model"] = model
            observed["base_url"] = base_url

        def verify_ready(self) -> dict[str, object]:
            observed["verified"] = True
            return {"model": observed["model"], "base_url": observed["base_url"]}

    monkeypatch.setattr(ai_engine, "LLMClient", _FakeLLMClient)

    engine = AIEngine(api_key="test", base_url="https://moacode.org/team/v1")
    engine.verify_ready()

    assert observed == {
        "api_key": "test",
        "model": "gpt-5.4",
        "base_url": "https://moacode.org/team/v1",
        "verified": True,
    }


def test_ai_engine_verify_ready_formats_provider_failure(monkeypatch) -> None:
    class _ProviderPermissionError(RuntimeError):
        def __init__(self) -> None:
            super().__init__("provider denied access")
            self.status_code = 403
            self.body = {
                "error": {
                    "message": "This API key is not allowed to use any enabled OpenAI provider.",
                    "type": "authentication_error",
                }
            }

    class _FakeLLMClient:
        def __init__(self, api_key: str, model: str = "gpt-5.4", *, base_url: str | None = None):
            self.api_key = api_key
            self.model = model
            self.base_url = base_url

        def verify_ready(self) -> None:
            raise _ProviderPermissionError()

    monkeypatch.setattr(ai_engine, "LLMClient", _FakeLLMClient)

    engine = AIEngine(api_key="test", base_url="https://moacode.org/team/v1")

    with pytest.raises(
        RuntimeError,
        match=r"AI readiness check failed: This API key is not allowed to use any enabled OpenAI provider\. \(HTTP 403\)",
    ):
        engine.verify_ready()
