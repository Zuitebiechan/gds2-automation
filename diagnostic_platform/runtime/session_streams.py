"""SSE stream helpers for session-scoped APIs."""

from __future__ import annotations

import json
import logging
import queue
from collections.abc import Iterator
from typing import Any, Callable

from src.gds2_orchestration.session_orchestrator import SessionStatus

from .session_actions import (
    apply_navigation_event,
    handle_ai_stream_terminal_event,
    navigation_terminal_payload,
)
from .session_preflight import (
    get_session_network_snapshot,
    network_quality_summary,
    network_signature,
)
from .worker_runtime import WorkerRuntime

logger = logging.getLogger(__name__)


def iter_engine_events(
    *,
    session_id: str,
    event_queue: Any,
    on_message: Callable[[str], bool] | None = None,
) -> Iterator[str]:
    """Yield SSE events from one engine-owned queue."""
    yield f"event: connected\ndata: {json.dumps({'session_id': session_id})}\n\n"
    while True:
        try:
            message = event_queue.get(timeout=60)
            handled_terminal = False
            if on_message is not None:
                handled_terminal = on_message(message)
            yield message
            if on_message is not None:
                if handled_terminal:
                    break
                continue
            if message.startswith("event: done\n") or message.startswith("event: error\n"):
                break
        except queue.Empty:
            yield ": keepalive\n\n"


def iter_session_events(
    *,
    orchestrator: Any,
    backend: Any,
    session_id: str,
) -> Iterator[str]:
    """Yield business-session SSE events."""
    event_queue = orchestrator.get_event_queue(session_id)
    if event_queue is None:
        raise LookupError(f"Session {session_id} not found")

    yield f"event: connected\ndata: {json.dumps({'session_id': session_id})}\n\n"

    last_network_sig: tuple[Any, ...] | None = None
    try:
        last_network_sig = network_signature(
            get_session_network_snapshot(
                orchestrator=orchestrator,
                backend=backend,
                session_id=session_id,
            )
        )
    except Exception:
        last_network_sig = None

    while True:
        try:
            try:
                orchestrator.check_decision_timeout(session_id)
            except Exception:
                pass

            message = event_queue.get(timeout=1)
            if message.startswith("event: decision_required\n"):
                try:
                    session = orchestrator.get_session(session_id)
                except KeyError:
                    break
                if session.status != SessionStatus.AWAITING_DECISION:
                    continue

            yield message
            if message.startswith("event: done\n"):
                break
        except queue.Empty:
            try:
                network_snapshot = get_session_network_snapshot(
                    orchestrator=orchestrator,
                    backend=backend,
                    session_id=session_id,
                )
                signature = network_signature(network_snapshot)
                if last_network_sig is None:
                    last_network_sig = signature
                elif signature != last_network_sig:
                    last_network_sig = signature
                    logger.info(
                        "[NETWORK_GATE] session=%s sse=network_quality_changed override=%s %s",
                        session_id,
                        bool(network_snapshot.get("network_override")),
                        network_quality_summary(network_snapshot.get("network_quality")),
                    )
                    yield (
                        "event: network_quality_changed\n"
                        f"data: {json.dumps({'session_id': session_id, **network_snapshot})}\n\n"
                    )
                    continue
            except KeyError:
                break
            except Exception:
                pass
            yield ": keepalive\n\n"


def iter_ai_events(
    *,
    runtime: WorkerRuntime,
    session: Any,
    session_id: str,
    event_queue: Any,
) -> Iterator[str]:
    """Yield AI diagnosis SSE events."""
    yield from iter_engine_events(
        session_id=session_id,
        event_queue=event_queue,
        on_message=lambda message: handle_ai_stream_terminal_event(runtime, session, message),
    )


def iter_scoped_agent_events(
    *,
    scope: str,
    session_id: str,
    connected_message: str = "Connected to stream",
    timeout_sec: int = 30,
    on_message: Callable[[str], bool] | None = None,
) -> Iterator[str]:
    """Yield SSE messages for one scoped agent stream."""
    from diagnostic_platform.sse import subscribe_agent_stream, unsubscribe_agent_stream

    client_queue = subscribe_agent_stream(scope, maxsize=200)
    try:
        yield (
            "event: connected\n"
            f"data: {json.dumps({'session_id': session_id, 'message': connected_message})}\n\n"
        )
        while True:
            try:
                message = client_queue.get(timeout=timeout_sec)
                handled_terminal = False
                if on_message is not None:
                    handled_terminal = on_message(message)
                yield message
                if on_message is not None:
                    if handled_terminal:
                        break
                    continue
            except queue.Empty:
                yield ": keepalive\n\n"
    except GeneratorExit:
        pass
    finally:
        unsubscribe_agent_stream(scope, client_queue)


def iter_navigation_events(
    *,
    runtime: WorkerRuntime,
    session: Any,
    session_id: str,
    nav_session: Any,
) -> Iterator[str]:
    """Yield navigation SSE events."""
    yield f"event: connected\ndata: {json.dumps({'session_id': session_id})}\n\n"
    while True:
        try:
            event = nav_session.event_queue.get(timeout=1)
        except queue.Empty:
            yield ": keepalive\n\n"
            if nav_session.thread and not nav_session.thread.is_alive():
                while not nav_session.event_queue.empty():
                    try:
                        event = nav_session.event_queue.get_nowait()
                        event_type = event.get("type", "progress")
                        yield f"event: {event_type}\ndata: {json.dumps(event)}\n\n"
                    except queue.Empty:
                        break
                terminal_payload = navigation_terminal_payload(runtime, session, nav_session)
                if terminal_payload is not None:
                    yield f"event: done\ndata: {json.dumps(terminal_payload)}\n\n"
                    return
            continue

        event_type = apply_navigation_event(runtime, session, nav_session, event)
        yield f"event: {event_type}\ndata: {json.dumps(event)}\n\n"
        if event_type in ("done", "error"):
            return
