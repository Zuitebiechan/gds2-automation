from __future__ import annotations

import types

from src.diagnosis.llm_client import LLMClient, _build_user_message


def test_build_user_message_includes_sampling_quality_and_gaps() -> None:
    vehicle_context = {"vin": "VIN123", "module": "ECM"}
    delta_payload = {
        "dtcs": [
            {
                "code": "P0101",
                "description": "Mass Air Flow Sensor Performance",
                "status": "Active",
                "dtc_type": "Current",
            }
        ],
        "actual_duration": 12.5,
        "sampling_quality": {
            "grade": "B",
            "status": "degraded",
            "snapshot_count": 24,
            "expected_snapshot_count": 30,
            "completeness_ratio": 0.8,
            "observed_rate_hz": 8.0,
            "target_rate_hz": 10.0,
            "gap_ms": {"avg": 110.0, "p95": 220.0, "max": 300.0},
            "lag_ms": {"avg": 12.0, "p95": 18.0, "max": 25.0},
            "degradation_reasons": ["sparse samples", "probe jitter"],
        },
        "gaps": [
            {
                "start_t": 3.0,
                "end_t": 4.5,
                "duration_ms": 1500,
                "reason": "collector busy",
            }
        ],
        "initial_state": [{"name": "RPM", "value": "800", "unit": "rpm"}],
        "timeline": [{"t": "4.0s", "param": "RPM", "from": "800", "to": "1600", "unit": "rpm"}],
        "significant_changes": [
            {
                "parameter": "RPM",
                "from": "800",
                "to": "1600",
                "unit": "rpm",
                "change_percent": 100,
                "duration": "1.0s",
            }
        ],
    }

    message = _build_user_message(vehicle_context, delta_payload)

    assert "VIN: VIN123" in message
    assert "Module: ECM" in message
    assert "P0101" in message
    assert "Grade: B (degraded)" in message
    assert "Degradation Reasons: sparse samples, probe jitter" in message
    assert "Observation Gaps (1 found)" in message
    assert "Gap from 3.0s to 4.5s" in message
    assert "Timeline (1 changes)" in message
    assert "Interpretation Guardrails" in message


def test_parse_verdict_accepts_markdown_wrapped_json() -> None:
    llm_response = """
Diagnosis result:

```json
{"verdict":"monitor","confidence":68}
```
""".strip()

    parsed = LLMClient.parse_verdict(llm_response)

    assert parsed == {"verdict": "monitor", "confidence": 68}


def test_parse_verdict_accepts_python_style_dict_fallback() -> None:
    llm_response = "Summary first {'verdict': 'action_needed', 'confidence': 35}"

    parsed = LLMClient.parse_verdict(llm_response)

    assert parsed == {"verdict": "action_needed", "confidence": 35}


def test_diagnose_stream_falls_back_to_non_stream_when_stream_is_empty(monkeypatch) -> None:
    class _FakeChunk:
        def __init__(self, content):
            self.choices = [types.SimpleNamespace(delta=types.SimpleNamespace(content=content))]

    class _FakeResponse:
        def __init__(self, content: str):
            self.choices = [types.SimpleNamespace(message=types.SimpleNamespace(content=content))]

    class _FakeCompletions:
        def __init__(self):
            self.calls: list[dict] = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs["stream"]:
                return iter([_FakeChunk(None), _FakeChunk(None)])
            return _FakeResponse('{"verdict":"monitor","confidence":55}')

    fake_completions = _FakeCompletions()
    fake_client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=fake_completions))
    client = LLMClient(api_key="test-key", model="glm-test")
    monkeypatch.setattr(client, "_get_client", lambda: fake_client)

    chunks = list(
        client.diagnose_stream(
            vehicle_context={"vin": "VIN123", "module": "ECM"},
            delta_payload={"dtcs": [], "timeline": [], "actual_duration": 1.0},
            brand="Honda",
            software="HDS",
        )
    )

    assert chunks == ['{"verdict":"monitor","confidence":55}']
    assert fake_completions.calls[0]["stream"] is True
    assert fake_completions.calls[1]["stream"] is False
    assert "Honda" in fake_completions.calls[0]["messages"][0]["content"]
    assert "HDS" in fake_completions.calls[0]["messages"][0]["content"]
