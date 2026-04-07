from __future__ import annotations

import types

from diagnostic_platform.contracts import BackendCapability
from diagnostic_platform.runtime.worker_runtime import WorkerRuntime
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
        lambda: types.SimpleNamespace(
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
    monkeypatch.setattr(session_live_data_handlers, "get_backend", lambda: object())
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


def test_stop_live_data_session_returns_conflict_when_session_not_running(monkeypatch) -> None:
    session = _session(
        "session-live",
        status=SessionStatus.AWAITING_DECISION,
        capabilities=[BackendCapability.LIVE_DATA],
    )
    orch = _FakeOrchestrator(session)

    monkeypatch.setattr(session_live_data_handlers, "get_orchestrator", lambda: orch)
    monkeypatch.setattr(session_live_data_handlers, "_runtime", lambda: WorkerRuntime())
    monkeypatch.setattr(session_live_data_handlers, "get_backend", lambda: object())

    payload, status = session_live_data_handlers.stop_live_data_session(
        {"session_id": "session-live"}
    )

    assert status == 409
    assert payload == {
        "success": False,
        "error": "Session not running (status=awaiting_decision)",
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
    }
