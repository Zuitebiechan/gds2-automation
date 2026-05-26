from __future__ import annotations

import types

from diagnostic_platform.contracts import BackendCapability
from diagnostic_platform.proxy_local_live_data import write_proxy_local_live_data_latest
from diagnostic_platform.runtime.navigation_errors import (
    NavigationDecisionMismatchError,
    NavigationNotAwaitingDecisionError,
    NavigationSessionTerminatedError,
)
from diagnostic_platform.runtime.session_errors import SessionNotRunningError
from diagnostic_platform.runtime.worker_runtime import WorkerRuntime
from server.api.http_utils import (
    navigation_decision_error_payload,
    session_state_error_payload,
)
from server.api import session_ai_handlers, session_live_data_handlers, session_navigation_handlers
from src.gds2_orchestration.session_orchestrator import Session, SessionContext, SessionStatus


def _session(
    session_id: str,
    *,
    status: SessionStatus = SessionStatus.RUNNING,
    capabilities: list[BackendCapability],
) -> Session:
    session = Session(
        session_id=session_id,
        context=SessionContext(brand="GM", model="Malibu"),
        status=status,
    )
    session.backend_name = "gds2"
    session.capabilities = [capability.value for capability in capabilities]
    return session


class _FakeOrchestrator:
    def __init__(self, session: Session) -> None:
        self._session = session
        self.progress: list[tuple[str, str]] = []

    def get_session(self, session_id: str) -> Session:
        if session_id != self._session.session_id:
            raise KeyError(f"Session {session_id} not found")
        return self._session

    def emit_progress(self, session_id: str, message: str) -> None:
        self.progress.append((session_id, message))


def test_start_ai_diagnose_requires_data_category(monkeypatch) -> None:
    session = _session("session-ai", capabilities=[BackendCapability.AI_DATA_COLLECTION])
    orch = _FakeOrchestrator(session)

    monkeypatch.setattr(session_ai_handlers, "get_orchestrator", lambda: orch)
    monkeypatch.setattr(
        session_ai_handlers,
        "get_backend",
        lambda session_id=None: types.SimpleNamespace(
            collect_ai_payload=lambda **kwargs: (_ for _ in ()).throw(
                AssertionError("collect_ai_payload should not be called")
            )
        ),
    )
    monkeypatch.setattr(
        session_ai_handlers,
        "get_ai_engine",
        lambda: types.SimpleNamespace(collection_seconds=30),
    )
    monkeypatch.setattr(session_ai_handlers, "_runtime", lambda: WorkerRuntime())
    monkeypatch.setattr(
        session_ai_handlers,
        "resolve_session_vehicle_context",
        lambda session, data, backend: {"vin": "", "module": "ECM", "data_category": ""},
    )

    payload, status = session_ai_handlers.start_ai_diagnose({"session_id": "session-ai"})

    assert status == 400
    assert payload == {"success": False, "error": "data_category required"}


def test_http_error_mapping_prefers_typed_session_errors() -> None:
    payload, status = session_state_error_payload(
        SessionNotRunningError(SessionStatus.AWAITING_DECISION)
    )

    assert status == 409
    assert payload == {
        "success": False,
        "error": "Session not running (status=awaiting_decision)",
        "error_code": "session_not_running",
        "session_status": "awaiting_decision",
    }


def test_http_error_mapping_prefers_typed_navigation_errors() -> None:
    cases = [
        (
            NavigationNotAwaitingDecisionError("running"),
            409,
            "navigation_not_awaiting_decision",
            "Session is not awaiting a decision (status=running)",
        ),
        (
            NavigationSessionTerminatedError("completed"),
            409,
            "navigation_session_terminated",
            "Session already terminated (status=completed)",
        ),
        (
            NavigationDecisionMismatchError("decision-1", "decision-2"),
            400,
            "navigation_decision_mismatch",
            "Decision ID mismatch: expected 'decision-1', got 'decision-2'",
        ),
    ]

    for error, expected_status, expected_code, expected_message in cases:
        payload, status = navigation_decision_error_payload(error)

        assert status == expected_status
        assert payload == {
            "success": False,
            "error": expected_message,
            "error_code": expected_code,
        }


def test_start_ai_diagnose_rejects_non_string_session_id() -> None:
    payload, status = session_ai_handlers.start_ai_diagnose({"session_id": 42})

    assert status == 400
    assert payload == {"success": False, "error": "session_id must be a string"}


def test_start_ai_diagnose_rejects_non_string_data_category(monkeypatch) -> None:
    session = _session("session-ai", capabilities=[BackendCapability.AI_DATA_COLLECTION])
    orch = _FakeOrchestrator(session)

    monkeypatch.setattr(session_ai_handlers, "get_orchestrator", lambda: orch)
    monkeypatch.setattr(session_ai_handlers, "get_backend", lambda session_id=None: object())
    monkeypatch.setattr(session_ai_handlers, "get_ai_engine", lambda: object())
    monkeypatch.setattr(session_ai_handlers, "_runtime", lambda: WorkerRuntime())
    monkeypatch.setattr(
        session_ai_handlers,
        "resolve_session_vehicle_context",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("resolve_session_vehicle_context should not be called")
        ),
    )

    payload, status = session_ai_handlers.start_ai_diagnose(
        {"session_id": "session-ai", "data_category": {"name": "Live Data"}}
    )

    assert status == 400
    assert payload == {"success": False, "error": "data_category must be a string"}


def test_start_ai_diagnose_verifies_ai_ready_before_collecting(monkeypatch) -> None:
    session = _session("session-ai", capabilities=[BackendCapability.AI_DATA_COLLECTION])
    orch = _FakeOrchestrator(session)
    order: list[str] = []

    class _FakeEngine:
        is_active = False
        collection_seconds = 30

        def verify_ready(self) -> None:
            order.append("verify")

        def start_session_from_payload(self, vehicle_context, diagnostic_payload):
            order.append("start")
            assert vehicle_context["session_id"] == "session-ai"
            assert diagnostic_payload == {"payload": "ok"}
            return "ai-1"

    monkeypatch.setattr(session_ai_handlers, "get_orchestrator", lambda: orch)
    monkeypatch.setattr(
        session_ai_handlers,
        "get_backend",
        lambda session_id=None: types.SimpleNamespace(
            collect_ai_payload=lambda **kwargs: order.append("collect") or {"payload": "ok"}
        ),
    )
    monkeypatch.setattr(session_ai_handlers, "get_ai_engine", lambda: _FakeEngine())
    monkeypatch.setattr(session_ai_handlers, "_runtime", lambda: WorkerRuntime())
    monkeypatch.setattr(
        session_ai_handlers,
        "resolve_session_vehicle_context",
        lambda session, data, backend: {
            "vin": "VIN-1",
            "module": "ECM",
            "data_category": "Engine Data",
        },
    )

    payload, status = session_ai_handlers.start_ai_diagnose({"session_id": "session-ai"})

    assert status == 200
    assert payload["ai_session_id"] == "ai-1"
    assert order == ["verify", "collect", "start"]


def test_start_ai_diagnose_stops_before_collection_when_ai_not_ready(monkeypatch) -> None:
    session = _session("session-ai", capabilities=[BackendCapability.AI_DATA_COLLECTION])
    orch = _FakeOrchestrator(session)

    class _FakeEngine:
        is_active = False
        collection_seconds = 30

        def verify_ready(self) -> None:
            raise RuntimeError("AI readiness check failed: provider denied access")

    monkeypatch.setattr(session_ai_handlers, "get_orchestrator", lambda: orch)
    monkeypatch.setattr(
        session_ai_handlers,
        "get_backend",
        lambda session_id=None: types.SimpleNamespace(
            collect_ai_payload=lambda **kwargs: (_ for _ in ()).throw(
                AssertionError("collect_ai_payload should not be called")
            )
        ),
    )
    monkeypatch.setattr(session_ai_handlers, "get_ai_engine", lambda: _FakeEngine())
    monkeypatch.setattr(session_ai_handlers, "_runtime", lambda: WorkerRuntime())
    monkeypatch.setattr(
        session_ai_handlers,
        "resolve_session_vehicle_context",
        lambda session, data, backend: {
            "vin": "VIN-1",
            "module": "ECM",
            "data_category": "Engine Data",
        },
    )

    payload, status = session_ai_handlers.start_ai_diagnose({"session_id": "session-ai"})

    assert status == 409
    assert payload == {
        "success": False,
        "error": "AI readiness check failed: provider denied access",
    }


def test_stream_ai_diagnose_events_binds_ai_iterator(monkeypatch) -> None:
    session = _session("session-ai", capabilities=[BackendCapability.AI_DATA_COLLECTION])
    orch = _FakeOrchestrator(session)
    runtime = WorkerRuntime()

    monkeypatch.setattr(session_ai_handlers, "get_orchestrator", lambda: orch)
    monkeypatch.setattr(session_ai_handlers, "get_ai_engine", lambda: object())
    monkeypatch.setattr(session_ai_handlers, "_runtime", lambda: runtime)
    monkeypatch.setattr(
        session_ai_handlers,
        "resolve_ai_event_stream",
        lambda runtime, session, engine: ("ai-1", object()),
    )
    monkeypatch.setattr(
        session_ai_handlers,
        "iter_ai_events",
        lambda runtime, session, session_id, event_queue: iter(
            ['event: connected\ndata: {"session_id": "session-ai"}\n\n']
        ),
    )

    response = session_ai_handlers.stream_ai_diagnose_events(
        "session-ai",
        sse_response=lambda iterator: {"events": list(iterator)},
    )

    assert response == {
        "events": ['event: connected\ndata: {"session_id": "session-ai"}\n\n']
    }


def test_retry_ai_diagnose_requires_cached_payload_id() -> None:
    payload, status = session_ai_handlers.retry_ai_diagnose({"session_id": "session-ai"})

    assert status == 400
    assert payload == {"success": False, "error": "cached_payload_id required"}


def test_start_live_data_session_returns_backend_payload_and_scope(monkeypatch) -> None:
    session = _session("session-live", capabilities=[BackendCapability.LIVE_DATA])
    orch = _FakeOrchestrator(session)
    runtime = WorkerRuntime()

    monkeypatch.setattr(session_live_data_handlers, "get_orchestrator", lambda: orch)
    monkeypatch.setattr(session_live_data_handlers, "get_backend", lambda session_id=None: object())
    monkeypatch.setattr(session_live_data_handlers, "_runtime", lambda: runtime)
    monkeypatch.setattr(
        session_live_data_handlers,
        "start_live_data",
        lambda runtime, session, data, interval_ms, backend, stream_scope, emit_progress: (
            "Engine Data",
            {
                "stream_scope": stream_scope,
                "interval_ms": interval_ms,
                "stream_active": True,
            },
        ),
    )

    payload, status = session_live_data_handlers.start_live_data_session(
        {"session_id": "session-live", "interval_ms": 250}
    )

    assert status == 200
    assert payload == {
        "success": True,
        "session_id": "session-live",
        "stream_scope": "session:session-live",
        "interval_ms": 250,
        "stream_active": True,
    }


def test_start_live_data_session_rejects_invalid_interval_ms(monkeypatch) -> None:
    session = _session("session-live", capabilities=[BackendCapability.LIVE_DATA])
    orch = _FakeOrchestrator(session)

    monkeypatch.setattr(session_live_data_handlers, "get_orchestrator", lambda: orch)
    monkeypatch.setattr(session_live_data_handlers, "get_backend", lambda session_id=None: object())
    monkeypatch.setattr(session_live_data_handlers, "_runtime", lambda: WorkerRuntime())
    monkeypatch.setattr(
        session_live_data_handlers,
        "start_live_data",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("start_live_data should not be called")
        ),
    )

    payload, status = session_live_data_handlers.start_live_data_session(
        {"session_id": "session-live", "interval_ms": "fast"}
    )

    assert status == 400
    assert payload == {"success": False, "error": "interval_ms must be an integer"}


def test_start_live_data_session_rejects_non_string_data_category(monkeypatch) -> None:
    session = _session("session-live", capabilities=[BackendCapability.LIVE_DATA])
    orch = _FakeOrchestrator(session)

    monkeypatch.setattr(session_live_data_handlers, "get_orchestrator", lambda: orch)
    monkeypatch.setattr(session_live_data_handlers, "get_backend", lambda session_id=None: object())
    monkeypatch.setattr(session_live_data_handlers, "_runtime", lambda: WorkerRuntime())
    monkeypatch.setattr(
        session_live_data_handlers,
        "start_live_data",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("start_live_data should not be called")
        ),
    )

    payload, status = session_live_data_handlers.start_live_data_session(
        {"session_id": "session-live", "data_category": ["Live Data"]}
    )

    assert status == 400
    assert payload == {"success": False, "error": "data_category must be a string"}


def test_stream_live_data_events_returns_404_when_stream_is_inactive(monkeypatch) -> None:
    session = _session("session-live", capabilities=[BackendCapability.LIVE_DATA])
    orch = _FakeOrchestrator(session)

    monkeypatch.setattr(session_live_data_handlers, "get_orchestrator", lambda: orch)
    monkeypatch.setattr(session_live_data_handlers, "_runtime", lambda: WorkerRuntime())
    monkeypatch.setattr(session_live_data_handlers, "is_live_data_active", lambda runtime, session_id: False)

    payload, status = session_live_data_handlers.stream_live_data_events(
        "session-live",
        sse_response=lambda iterator: {"events": list(iterator)},
    )

    assert status == 404
    assert payload == {
        "success": False,
        "error": "No active live data stream for session-live",
    }


def test_proxy_local_latest_returns_404_when_session_missing(monkeypatch) -> None:
    session = _session("session-live", capabilities=[BackendCapability.LIVE_DATA])
    orch = _FakeOrchestrator(session)

    monkeypatch.setattr(session_live_data_handlers, "get_orchestrator", lambda: orch)

    payload, status = session_live_data_handlers.get_proxy_local_live_data_latest(
        "missing-session"
    )

    assert status == 404
    assert "missing-session" in payload["error"]


def test_proxy_local_latest_returns_501_when_live_data_unsupported(monkeypatch) -> None:
    session = _session("session-live", capabilities=[BackendCapability.READ_DTCS])
    orch = _FakeOrchestrator(session)

    monkeypatch.setattr(session_live_data_handlers, "get_orchestrator", lambda: orch)

    payload, status = session_live_data_handlers.get_proxy_local_live_data_latest(
        "session-live"
    )

    assert status == 501
    assert payload["success"] is False


def test_proxy_local_latest_returns_404_when_cache_missing(monkeypatch) -> None:
    session = _session("session-live", capabilities=[BackendCapability.LIVE_DATA])
    orch = _FakeOrchestrator(session)
    runtime = WorkerRuntime()
    runtime.bind_business_session("session-live")
    runtime.set_live_data_active("session-live", True)
    runtime.set_connection_epoch("session-live", "epoch-live-1")

    monkeypatch.setattr(session_live_data_handlers, "get_orchestrator", lambda: orch)
    monkeypatch.setattr(session_live_data_handlers, "_runtime", lambda: runtime)
    monkeypatch.setattr(
        session_live_data_handlers,
        "read_proxy_local_live_data_latest",
        lambda: None,
    )

    payload, status = session_live_data_handlers.get_proxy_local_live_data_latest(
        "session-live"
    )

    assert status == 404
    assert payload["reason"] == "no_sample_cache"
    assert payload["checked_paths"]


def test_proxy_local_latest_returns_409_when_stream_inactive(monkeypatch, tmp_path) -> None:
    session = _session("session-live", capabilities=[BackendCapability.LIVE_DATA])
    orch = _FakeOrchestrator(session)
    runtime = WorkerRuntime()
    runtime.bind_business_session("session-live")
    runtime.set_connection_epoch("session-live", "epoch-live-1")
    latest = write_proxy_local_live_data_latest(
        sample={
            "schema_version": "proxy.local_live_data.sample.v1",
            "signal_key": "engine_speed",
            "display_name": "Engine Speed",
            "unit": "RPM",
            "value": 900.0,
            "source": "proxy_local_known_uds",
            "decoder_id": "uds_did_000c_engine_speed",
        },
        connection_epoch="epoch-live-1",
        session_snapshot={"session_id": "session-live", "live_data_active": True},
        path=tmp_path / "unused.json",
        received_at_s=1_800_000_000.0,
    )

    monkeypatch.setattr(session_live_data_handlers, "get_orchestrator", lambda: orch)
    monkeypatch.setattr(session_live_data_handlers, "_runtime", lambda: runtime)
    monkeypatch.setattr(
        session_live_data_handlers,
        "read_proxy_local_live_data_latest",
        lambda: latest,
    )

    payload, status = session_live_data_handlers.get_proxy_local_live_data_latest(
        "session-live"
    )

    assert status == 409
    assert payload["reason"] == "live_data_inactive"


def test_proxy_local_latest_returns_200_for_fresh_matching_sample(monkeypatch, tmp_path) -> None:
    session = _session("session-live", capabilities=[BackendCapability.LIVE_DATA])
    orch = _FakeOrchestrator(session)
    runtime = WorkerRuntime()
    runtime.bind_business_session("session-live")
    runtime.set_live_data_active("session-live", True)
    runtime.set_connection_epoch("session-live", "epoch-live-1")
    latest = write_proxy_local_live_data_latest(
        sample={
            "schema_version": "proxy.local_live_data.sample.v1",
            "signal_key": "engine_speed",
            "display_name": "Engine Speed",
            "unit": "RPM",
            "value": 900.0,
            "source": "proxy_local_known_uds",
            "decoder_id": "uds_did_000c_engine_speed",
        },
        connection_epoch="epoch-live-1",
        session_snapshot={"session_id": "session-live", "live_data_active": True},
        path=tmp_path / "unused.json",
        received_at_s=1_800_000_000.0,
    )

    monkeypatch.setattr(session_live_data_handlers, "get_orchestrator", lambda: orch)
    monkeypatch.setattr(session_live_data_handlers, "_runtime", lambda: runtime)
    monkeypatch.setattr(
        session_live_data_handlers,
        "read_proxy_local_live_data_latest",
        lambda: latest,
    )
    monkeypatch.setattr("diagnostic_platform.proxy_local_live_data.time.time", lambda: 1_800_000_001.0)

    payload, status = session_live_data_handlers.get_proxy_local_live_data_latest(
        "session-live",
        max_age_ms=5000,
    )

    assert status == 200
    assert payload["success"] is True
    assert payload["available"] is True
    assert payload["session_id"] == "session-live"
    assert payload["source"] == "proxy_local_live_data"
    assert payload["latest_sample"]["value"] == 900.0
    assert payload["cloud_received_age_ms"] == 1000.0
    assert payload["epoch_match_status"] == "matched"


def test_read_session_dtcs_rejects_non_string_session_id() -> None:
    payload, status = session_live_data_handlers.read_session_dtcs({"session_id": ["bad"]})

    assert status == 400
    assert payload == {"success": False, "error": "session_id must be a string"}


def test_read_session_dtcs_rejects_non_string_module(monkeypatch) -> None:
    session = _session("session-live", capabilities=[BackendCapability.READ_DTCS])
    orch = _FakeOrchestrator(session)

    monkeypatch.setattr(session_live_data_handlers, "get_orchestrator", lambda: orch)
    monkeypatch.setattr(session_live_data_handlers, "get_backend", lambda session_id=None: object())
    monkeypatch.setattr(
        session_live_data_handlers,
        "read_dtcs",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("read_dtcs should not be called")
        ),
    )

    payload, status = session_live_data_handlers.read_session_dtcs(
        {"session_id": "session-live", "module": {"name": "ECM"}}
    )

    assert status == 400
    assert payload == {"success": False, "error": "module must be a string"}


def test_clear_session_dtcs_rejects_non_string_data_category(monkeypatch) -> None:
    session = _session("session-live", capabilities=[BackendCapability.CLEAR_DTCS])
    orch = _FakeOrchestrator(session)

    monkeypatch.setattr(session_live_data_handlers, "get_orchestrator", lambda: orch)
    monkeypatch.setattr(session_live_data_handlers, "get_backend", lambda session_id=None: object())
    monkeypatch.setattr(session_live_data_handlers, "_runtime", lambda: WorkerRuntime())
    monkeypatch.setattr(
        session_live_data_handlers,
        "clear_dtcs",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("clear_dtcs should not be called")
        ),
    )

    payload, status = session_live_data_handlers.clear_session_dtcs(
        {"session_id": "session-live", "data_category": ["Engine Data"]}
    )

    assert status == 400
    assert payload == {"success": False, "error": "data_category must be a string"}


def test_stop_live_data_session_returns_conflict_when_session_not_running(monkeypatch) -> None:
    session = _session(
        "session-live",
        status=SessionStatus.AWAITING_DECISION,
        capabilities=[BackendCapability.LIVE_DATA],
    )
    orch = _FakeOrchestrator(session)

    monkeypatch.setattr(session_live_data_handlers, "get_orchestrator", lambda: orch)
    monkeypatch.setattr(session_live_data_handlers, "_runtime", lambda: WorkerRuntime())
    monkeypatch.setattr(session_live_data_handlers, "get_backend", lambda session_id=None: object())

    payload, status = session_live_data_handlers.stop_live_data_session(
        {"session_id": "session-live"}
    )

    assert status == 409
    assert payload == {
        "success": False,
        "error": "Session not running (status=awaiting_decision)",
        "error_code": "session_not_running",
        "session_status": "awaiting_decision",
    }


def test_submit_navigation_decision_conflict_maps_to_409(monkeypatch) -> None:
    session = _session("session-nav", capabilities=[BackendCapability.NAVIGATION])
    orch = _FakeOrchestrator(session)

    monkeypatch.setattr(session_navigation_handlers, "get_orchestrator", lambda: orch)
    monkeypatch.setattr(session_navigation_handlers, "_runtime", lambda: WorkerRuntime())
    monkeypatch.setattr(
        session_navigation_handlers,
        "submit_navigation_decision",
        lambda runtime, session, decision_id, selected_item: (_ for _ in ()).throw(
            ValueError("Session is not awaiting a decision (status=running)")
        ),
    )

    payload, status = session_navigation_handlers.submit_navigation_decision_for_business(
        {
            "session_id": "session-nav",
            "decision_id": "decision-1",
            "selected_item": "ECM",
        }
    )

    assert status == 409
    assert payload == {
        "success": False,
        "error": "Session is not awaiting a decision (status=running)",
        "error_code": "navigation_not_awaiting_decision",
    }


def test_start_navigation_session_rejects_non_string_goal(monkeypatch) -> None:
    session = _session("session-nav", capabilities=[BackendCapability.NAVIGATION])
    orch = _FakeOrchestrator(session)

    monkeypatch.setattr(session_navigation_handlers, "get_orchestrator", lambda: orch)
    monkeypatch.setattr(session_navigation_handlers, "_runtime", lambda: WorkerRuntime())
    monkeypatch.setattr(
        session_navigation_handlers,
        "start_navigation",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("start_navigation should not be called")
        ),
    )

    payload, status = session_navigation_handlers.start_navigation_session_for_business(
        {
            "session_id": "session-nav",
            "goal": {"screen": "data_display"},
        }
    )

    assert status == 400
    assert payload == {"success": False, "error": "goal must be a string"}


def test_start_navigation_session_returns_409_when_backend_navigation_runtime_missing(monkeypatch) -> None:
    session = _session("session-nav", capabilities=[BackendCapability.NAVIGATION])
    orch = _FakeOrchestrator(session)

    monkeypatch.setattr(session_navigation_handlers, "get_orchestrator", lambda: orch)
    monkeypatch.setattr(session_navigation_handlers, "_runtime", lambda: WorkerRuntime())
    monkeypatch.setattr(
        session_navigation_handlers,
        "get_backend",
        lambda session_id=None: types.SimpleNamespace(name="broken-backend"),
    )

    payload, status = session_navigation_handlers.start_navigation_session_for_business(
        {
            "session_id": "session-nav",
            "goal": "Navigate to Data Display",
        }
    )

    assert status == 409
    assert payload == {
        "success": False,
        "error": "Backend 'broken-backend' does not expose a navigation runtime",
    }
