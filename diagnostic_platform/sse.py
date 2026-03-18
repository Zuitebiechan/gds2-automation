"""Shared SSE infrastructure for agent-driven streaming callbacks."""

import json
import logging
import queue
import threading
import time
from typing import List

logger = logging.getLogger(__name__)

agent_clients: List[queue.Queue] = []
agent_lock = threading.Lock()


def broadcast_to_agent_clients(event_type: str, data: dict):
    """Broadcast data to all connected Agent SSE clients."""
    message = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    with agent_lock:
        dead_clients = []
        for client_queue in agent_clients:
            try:
                client_queue.put_nowait(message)
            except queue.Full:
                dead_clients.append(client_queue)
        for dead in dead_clients:
            agent_clients.remove(dead)


def on_agent_snapshot(snapshot, param_changes=None):
    """Callback when Agent produces a new snapshot."""
    broadcast_to_agent_clients("snapshot", {
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
    })


def on_agent_param_change(changes):
    """Callback when Agent detects parameter changes."""
    logger.info(f"Agent detected {len(changes)} parameter change(s)")


def on_agent_dtc_change(added, removed):
    """Callback when Agent detects DTC changes."""
    broadcast_to_agent_clients("dtc_changes", {
        "type": "dtc_changes",
        "added": [d.to_dict() for d in added],
        "removed": [d.to_dict() for d in removed],
        "timestamp": time.time(),
    })


def on_agent_error(error):
    """Callback when Agent encounters an error."""
    broadcast_to_agent_clients("error", {
        "type": "error",
        "message": error,
        "timestamp": time.time(),
    })
    logger.error(f"Agent streaming error: {error}")
