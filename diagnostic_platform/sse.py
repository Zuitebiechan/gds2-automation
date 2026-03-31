"""Shared SSE infrastructure for agent-driven streaming callbacks."""

from __future__ import annotations

import json
import logging
import queue
import time
from collections.abc import Callable
from typing import Any

from diagnostic_platform.runtime.worker_runtime import get_worker_runtime

logger = logging.getLogger(__name__)

DEFAULT_AGENT_STREAM_SCOPE = "diagnostics"

_runtime = get_worker_runtime()
agent_clients = _runtime.agent_clients
agent_lock = _runtime.agent_lock


def session_agent_stream_scope(session_id: str) -> str:
    """Build the scoped stream key for a business session."""
    return f"session:{session_id}"


def _event_hub():
    return get_worker_runtime().agent_event_hub


def _format_sse_message(event_type: str, data: dict[str, Any]) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def subscribe_agent_stream(scope: str, *, maxsize: int = 200) -> queue.Queue:
    """Subscribe to one live-data stream scope."""
    return _event_hub().subscribe(scope, maxsize=maxsize)


def unsubscribe_agent_stream(scope: str, client_queue: queue.Queue) -> None:
    """Remove a live-data subscriber from one scope."""
    _event_hub().unsubscribe(scope, client_queue)


def agent_stream_client_count(scope: str) -> int:
    """Return the active subscriber count for a scope."""
    return _event_hub().client_count(scope)


def broadcast_agent_event(scope: str, event_type: str, data: dict[str, Any]) -> int:
    """Broadcast one SSE event to a scoped set of live-data clients."""
    message = _format_sse_message(event_type, data)
    return _event_hub().broadcast(scope, message)


def _snapshot_payload(snapshot: Any, param_changes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "type": "snapshot",
        "extraction_count": snapshot.extraction_count,
        "extraction_duration_ms": snapshot.extraction_duration_ms,
        "page_context": snapshot.page_context,
        "param_count": len(snapshot.parameters),
        "dtc_count": len(snapshot.dtcs),
        "parameters": snapshot.parameters,
        "dtcs": [d.to_dict() for d in snapshot.dtcs],
        "param_changes": param_changes or [],
        "timestamp": time.time(),
    }


def _dtc_change_payload(added: list[Any], removed: list[Any]) -> dict[str, Any]:
    return {
        "type": "dtc_changes",
        "added": [d.to_dict() for d in added],
        "removed": [d.to_dict() for d in removed],
        "timestamp": time.time(),
    }


def _error_payload(error: str) -> dict[str, Any]:
    return {
        "type": "error",
        "message": error,
        "timestamp": time.time(),
    }


def make_scoped_agent_event_callbacks(scope: str) -> dict[str, Callable[..., None]]:
    """Create collector callbacks bound to one stream scope."""

    def on_snapshot(snapshot: Any, param_changes: list[dict[str, Any]] | None = None) -> None:
        broadcast_agent_event(scope, "snapshot", _snapshot_payload(snapshot, param_changes))

    def on_param_change(changes: list[dict[str, Any]]) -> None:
        logger.info("Agent detected %s parameter change(s) for scope=%s", len(changes), scope)

    def on_dtc_change(added: list[Any], removed: list[Any]) -> None:
        broadcast_agent_event(scope, "dtc_changes", _dtc_change_payload(added, removed))

    def on_error(error: str) -> None:
        broadcast_agent_event(scope, "error", _error_payload(error))
        logger.error("Agent streaming error for scope=%s: %s", scope, error)

    return {
        "on_snapshot": on_snapshot,
        "on_param_change": on_param_change,
        "on_dtc_change": on_dtc_change,
        "on_error": on_error,
    }


def broadcast_to_agent_clients(event_type: str, data: dict[str, Any]) -> int:
    """Backward-compatible default-scope broadcast."""
    return broadcast_agent_event(DEFAULT_AGENT_STREAM_SCOPE, event_type, data)


def on_agent_snapshot(snapshot: Any, param_changes: list[dict[str, Any]] | None = None) -> None:
    """Callback when Agent produces a new snapshot."""
    broadcast_agent_event(
        DEFAULT_AGENT_STREAM_SCOPE,
        "snapshot",
        _snapshot_payload(snapshot, param_changes),
    )


def on_agent_param_change(changes: list[dict[str, Any]]) -> None:
    """Callback when Agent detects parameter changes."""
    logger.info("Agent detected %s parameter change(s)", len(changes))


def on_agent_dtc_change(added: list[Any], removed: list[Any]) -> None:
    """Callback when Agent detects DTC changes."""
    broadcast_agent_event(
        DEFAULT_AGENT_STREAM_SCOPE,
        "dtc_changes",
        _dtc_change_payload(added, removed),
    )


def on_agent_error(error: str) -> None:
    """Callback when Agent encounters an error."""
    broadcast_agent_event(DEFAULT_AGENT_STREAM_SCOPE, "error", _error_payload(error))
    logger.error("Agent streaming error: %s", error)
