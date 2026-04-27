from __future__ import annotations

import json
import queue
import types

from diagnostic_platform.runtime.session_streams import (
    iter_ai_events,
    iter_engine_events,
    iter_navigation_events,
    iter_scoped_agent_events,
    iter_session_events,
)
from diagnostic_platform.runtime.worker_runtime import WorkerRuntime
from src.gds2_orchestration.session_orchestrator import SessionStatus


class _SequencedQueue:
    def __init__(self, items: list[object]) -> None:
        self._items = list(items)

    def get(self, timeout=None):
        if not self._items:
            raise queue.Empty
        item = self._items.pop(0)
        if item is queue.Empty:
            raise queue.Empty
        return item


class _ResidualQueue:
    def __init__(self, residual_event: dict[str, object]) -> None:
        self._residual_event = residual_event
        self._drained = False

    def get(self, timeout=None):
        raise queue.Empty

    def empty(self) -> bool:
        return self._drained

    def get_nowait(self):
        if self._drained:
            raise queue.Empty
        self._drained = True
        return self._residual_event


class _StableValue:
    def __str__(self) -> str:
        return "stable-value"


def _event_payload(message: str) -> tuple[str, dict[str, object]]:
    lines = message.strip().splitlines()
    event_type = lines[0].removeprefix("event: ")
    payload = json.loads(lines[1].removeprefix("data: "))
    return event_type, payload


def _event_types(messages: list[str]) -> list[str]:
    event_types: list[str] = []
    for message in messages:
        if message.startswith(":"):
            event_types.append("keepalive")
            continue
        event_types.append(message.splitlines()[0].removeprefix("event: "))
    return event_types


def _assert_no_session_only_events(messages: list[str]) -> None:
    joined_events = "".join(messages)
    assert "event: network_quality_changed\n" not in joined_events
    assert "event: decision_timeout\n" not in joined_events
    assert "event: decision_resolved\n" not in joined_events


def test_iter_engine_events_emits_keepalive_then_stops_on_done_event() -> None:
    events = list(
        iter_engine_events(
            session_id="session-1",
            event_queue=_SequencedQueue(
                [
                    queue.Empty,
                    "event: done\ndata: {\"ok\": true}\n\n",
                ]
            ),
        )
    )

    assert events == [
        'event: connected\ndata: {"session_id": "session-1"}\n\n',
        ": keepalive\n\n",
        'event: done\ndata: {"ok": true}\n\n',
    ]


def test_iter_engine_events_runs_terminal_callback_before_yielding_terminal_message() -> None:
    state = {"cleared": False}
    stream = iter_engine_events(
        session_id="session-1",
        event_queue=_SequencedQueue(
            [
                'event: error\ndata: {"error": "boom"}\n\n',
            ]
        ),
        on_message=lambda message: state.__setitem__("cleared", True)
        or message.startswith("event: error\n"),
    )

    assert next(stream) == 'event: connected\ndata: {"session_id": "session-1"}\n\n'

    terminal_message = next(stream)

    assert terminal_message == 'event: error\ndata: {"error": "boom"}\n\n'
    assert state["cleared"] is True


def test_iter_engine_events_ignores_non_string_messages() -> None:
    events = list(
        iter_engine_events(
            session_id="session-1",
            event_queue=_SequencedQueue(
                [
                    ["bad-message"],
                    'event: done\ndata: {"ok": true}\n\n',
                ]
            ),
        )
    )

    assert events == [
        'event: connected\ndata: {"session_id": "session-1"}\n\n',
        'event: done\ndata: {"ok": true}\n\n',
    ]


def test_iter_session_events_filters_stale_decisions_and_emits_network_change(monkeypatch) -> None:
    session = types.SimpleNamespace(status=SessionStatus.RUNNING)
    event_queue = _SequencedQueue(
        [
            queue.Empty,
            'event: decision_required\ndata: {"decision_id": "d-1"}\n\n',
            'event: done\ndata: {"session_id": "session-1"}\n\n',
        ]
    )
    orchestrator = types.SimpleNamespace(
        get_event_queue=lambda session_id: event_queue,
        check_decision_timeout=lambda session_id: None,
        get_session=lambda session_id: session,
    )
    snapshots = iter(
        [
            {
                "network_quality": {
                    "grade": "warn",
                    "status": "degraded",
                    "reason": "high_latency",
                },
                "network_override": None,
                "connection_epoch": "epoch-1",
            },
            {
                "network_quality": {
                    "grade": "block",
                    "status": "blocked",
                    "reason": "timeout",
                },
                "network_override": None,
                "connection_epoch": "epoch-2",
            },
        ]
    )
    monkeypatch.setattr(
        "diagnostic_platform.runtime.session_streams.get_session_network_snapshot",
        lambda **kwargs: next(snapshots),
    )

    events = list(
        iter_session_events(
            orchestrator=orchestrator,
            backend=object(),
            session_id="session-1",
        )
    )

    assert len(events) == 3
    assert _event_types(events) == ["connected", "network_quality_changed", "done"]
    assert events[0] == 'event: connected\ndata: {"session_id": "session-1"}\n\n'
    event_type, payload = _event_payload(events[1])
    assert event_type == "network_quality_changed"
    assert payload == {
        "session_id": "session-1",
        "network_quality": {
            "grade": "block",
            "status": "blocked",
            "reason": "timeout",
        },
        "network_override": None,
        "connection_epoch": "epoch-2",
    }
    assert events[2] == 'event: done\ndata: {"session_id": "session-1"}\n\n'
    joined_events = "".join(events)
    assert "event: decision_required\n" not in joined_events
    assert "event: decision_resolved\n" not in joined_events
    assert "event: decision_timeout\n" not in joined_events


def test_iter_session_events_ignores_non_string_messages() -> None:
    session = types.SimpleNamespace(status=SessionStatus.RUNNING)
    event_queue = _SequencedQueue(
        [
            ["bad-message"],
            'event: done\ndata: {"session_id": "session-1"}\n\n',
        ]
    )
    orchestrator = types.SimpleNamespace(
        get_event_queue=lambda session_id: event_queue,
        check_decision_timeout=lambda session_id: None,
        get_session=lambda session_id: session,
    )

    events = list(
        iter_session_events(
            orchestrator=orchestrator,
            backend=object(),
            session_id="session-1",
        )
    )

    assert events == [
        'event: connected\ndata: {"session_id": "session-1"}\n\n',
        'event: done\ndata: {"session_id": "session-1"}\n\n',
    ]


def test_iter_session_events_stringifies_non_json_network_payloads(monkeypatch) -> None:
    session = types.SimpleNamespace(status=SessionStatus.RUNNING)
    event_queue = _SequencedQueue(
        [
            queue.Empty,
            'event: done\ndata: {"session_id": "session-1"}\n\n',
        ]
    )
    orchestrator = types.SimpleNamespace(
        get_event_queue=lambda session_id: event_queue,
        check_decision_timeout=lambda session_id: None,
        get_session=lambda session_id: session,
    )
    snapshots = iter(
        [
            {
                "network_quality": {
                    "grade": "warn",
                    "status": "degraded",
                    "reason": "high_latency",
                },
                "network_override": None,
                "connection_epoch": "epoch-1",
            },
            {
                "network_quality": {
                    "grade": "block",
                    "status": "blocked",
                    "reason": RuntimeError("boom"),
                },
                "network_override": None,
                "connection_epoch": "epoch-2",
            },
        ]
    )
    monkeypatch.setattr(
        "diagnostic_platform.runtime.session_streams.get_session_network_snapshot",
        lambda **kwargs: next(snapshots),
    )

    events = list(
        iter_session_events(
            orchestrator=orchestrator,
            backend=object(),
            session_id="session-1",
        )
    )

    event_type, payload = _event_payload(events[1])
    assert event_type == "network_quality_changed"
    assert payload["network_quality"]["reason"] == "boom"


def test_iter_ai_events_clears_bound_ai_session_on_terminal_event() -> None:
    runtime = WorkerRuntime()
    runtime.bind_business_session("session-1")
    runtime.bind_ai_session("session-1", "ai-1")
    session = types.SimpleNamespace(session_id="session-1", updated_at=0.0)
    event_queue = _SequencedQueue(
        [
            'event: progress\ndata: {"phase": "analyzing"}\n\n',
            'event: error\ndata: {"error": "boom"}\n\n',
        ]
    )

    events = list(
        iter_ai_events(
            runtime=runtime,
            session=session,
            session_id="session-1",
            event_queue=event_queue,
        )
    )

    assert events == [
        'event: connected\ndata: {"session_id": "session-1"}\n\n',
        'event: progress\ndata: {"phase": "analyzing"}\n\n',
        'event: error\ndata: {"error": "boom"}\n\n',
    ]
    assert runtime.get_ai_session_id("session-1") is None


def test_iter_scoped_agent_events_emits_keepalive_and_unsubscribes_on_close(monkeypatch) -> None:
    subscribed_queue = _SequencedQueue([queue.Empty])
    unsubscribed: list[tuple[str, object]] = []

    monkeypatch.setattr(
        "diagnostic_platform.sse.subscribe_agent_stream",
        lambda scope, maxsize=200: subscribed_queue,
    )
    monkeypatch.setattr(
        "diagnostic_platform.sse.unsubscribe_agent_stream",
        lambda scope, client_queue: unsubscribed.append((scope, client_queue)),
    )

    stream = iter_scoped_agent_events(scope="session:1", session_id="session-1", timeout_sec=1)

    assert next(stream) == (
        'event: connected\ndata: {"session_id": "session-1", "message": "Connected to stream"}\n\n'
    )
    assert next(stream) == ": keepalive\n\n"

    stream.close()

    assert unsubscribed == [("session:1", subscribed_queue)]


def test_iter_scoped_agent_events_ignores_non_string_messages(monkeypatch) -> None:
    subscribed_queue = _SequencedQueue(
        [
            ["bad-message"],
            'event: done\ndata: {"ok": true}\n\n',
        ]
    )

    monkeypatch.setattr(
        "diagnostic_platform.sse.subscribe_agent_stream",
        lambda scope, maxsize=200: subscribed_queue,
    )
    monkeypatch.setattr(
        "diagnostic_platform.sse.unsubscribe_agent_stream",
        lambda scope, client_queue: None,
    )

    events = list(
        iter_scoped_agent_events(
            scope="session:1",
            session_id="session-1",
            timeout_sec=1,
            on_message=lambda message: message.startswith("event: done\n"),
        )
    )

    assert events == [
        'event: connected\ndata: {"session_id": "session-1", "message": "Connected to stream"}\n\n',
        'event: done\ndata: {"ok": true}\n\n',
    ]


def test_iter_navigation_events_flushes_residual_events_after_thread_exit(monkeypatch) -> None:
    monkeypatch.setattr(
        "diagnostic_platform.runtime.session_streams.navigation_terminal_payload",
        lambda runtime, session, nav_session: {
            "type": "done",
            "status": "completed",
            "error": None,
        },
    )
    nav_session = types.SimpleNamespace(
        event_queue=_ResidualQueue({"type": "progress", "page": "module_list"}),
        thread=types.SimpleNamespace(is_alive=lambda: False),
    )

    events = list(
        iter_navigation_events(
            runtime=object(),
            session=object(),
            session_id="session-1",
            nav_session=nav_session,
        )
    )

    assert events == [
        'event: connected\ndata: {"session_id": "session-1"}\n\n',
        ": keepalive\n\n",
        'event: progress\ndata: {"type": "progress", "page": "module_list"}\n\n',
        'event: done\ndata: {"type": "done", "status": "completed", "error": null}\n\n',
    ]
    assert _event_types(events) == ["connected", "keepalive", "progress", "done"]
    _assert_no_session_only_events(events)


def test_iter_navigation_events_ignores_malformed_residual_events(monkeypatch) -> None:
    monkeypatch.setattr(
        "diagnostic_platform.runtime.session_streams.navigation_terminal_payload",
        lambda runtime, session, nav_session: {
            "type": "done",
            "status": "completed",
            "error": None,
        },
    )
    nav_session = types.SimpleNamespace(
        event_queue=_ResidualQueue(["bad-event"]),
        thread=types.SimpleNamespace(is_alive=lambda: False),
    )

    events = list(
        iter_navigation_events(
            runtime=object(),
            session=object(),
            session_id="session-1",
            nav_session=nav_session,
        )
    )

    assert events == [
        'event: connected\ndata: {"session_id": "session-1"}\n\n',
        ": keepalive\n\n",
        'event: progress\ndata: {}\n\n',
        'event: done\ndata: {"type": "done", "status": "completed", "error": null}\n\n',
    ]


def test_iter_navigation_events_stringifies_non_json_event_values() -> None:
    runtime = WorkerRuntime()
    session = types.SimpleNamespace(session_id="session-1")
    nav_session = types.SimpleNamespace(
        current_page="",
        status="running",
        pending_decision_id=None,
        pending_items=[],
        error=None,
        thread=None,
        event_queue=_SequencedQueue(
            [
                {"type": "progress", "page": RuntimeError("boom")},
                {"type": "error", "error": RuntimeError("bad")},
            ]
        ),
    )

    events = list(
        iter_navigation_events(
            runtime=runtime,
            session=session,
            session_id="session-1",
            nav_session=nav_session,
        )
    )

    assert events == [
        'event: connected\ndata: {"session_id": "session-1"}\n\n',
        'event: progress\ndata: {"type": "progress", "page": "boom"}\n\n',
        'event: error\ndata: {"type": "error", "error": "bad"}\n\n',
    ]
    assert _event_types(events) == ["connected", "progress", "error"]
    _assert_no_session_only_events(events)


def test_iter_navigation_events_serializes_non_json_values(monkeypatch) -> None:
    monkeypatch.setattr(
        "diagnostic_platform.runtime.session_streams.navigation_terminal_payload",
        lambda runtime, session, nav_session: {
            "type": "done",
            "status": "completed",
            "error": None,
        },
    )
    nav_session = types.SimpleNamespace(
        event_queue=_ResidualQueue({"type": "progress", "page": _StableValue()}),
        thread=types.SimpleNamespace(is_alive=lambda: False),
    )

    events = list(
        iter_navigation_events(
            runtime=object(),
            session=object(),
            session_id="session-1",
            nav_session=nav_session,
        )
    )

    assert events == [
        'event: connected\ndata: {"session_id": "session-1"}\n\n',
        ": keepalive\n\n",
        'event: progress\ndata: {"type": "progress", "page": "stable-value"}\n\n',
        'event: done\ndata: {"type": "done", "status": "completed", "error": null}\n\n',
    ]
    assert _event_types(events) == ["connected", "keepalive", "progress", "done"]
    _assert_no_session_only_events(events)
