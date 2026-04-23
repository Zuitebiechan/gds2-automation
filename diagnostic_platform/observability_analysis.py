"""Observability analysis helpers.

Provides:
- deterministic incident classification
- raw-event/session-trace assembly
- incident bundle generation
"""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

TRIGGER_EVENT_TYPES = {
    "dll.request.retry_exhausted",
    "proxy.request.timeout",
    "tunnel.lifecycle.disconnected",
    "tunnel.probe.failure",
    "worker.lifecycle.crashed",
    "recovery_failed",
    "agent.collector.guard_failed",
    "session.network_gate.blocked",
    "ai.stream.error",
    "live_data.stream.error",
}

NEXT_CHECKS_BY_DOMAIN = {
    "cloud_dll_local_proxy": [
        "inspect virtual_j2534 socket connect/send/recv failures",
        "verify reverse_server received the same dll_seq",
    ],
    "cloud_proxy_tunnel": [
        "inspect reverse tunnel health and probe timeline",
        "verify reverse_client registration and receive path",
    ],
    "local_reverse_client": [
        "inspect reverse_client connection lifecycle and reconnect loop",
        "verify local tray/reverse client process health",
    ],
    "local_worker_rpc": [
        "inspect worker spawn/listener/parent-disconnect events",
        "verify worker_request_id continuity from reverse_client to worker",
    ],
    "local_j2534_driver": [
        "inspect worker.rpc.failed or j2534.call.failed details",
        "verify local J2534 driver behavior for the same method",
    ],
    "vehicle_or_vci": [
        "check local VCI/device power and cabling",
        "verify repeated ERR_DEVICE_NOT_CONNECTED or device timeout behavior",
    ],
    "gds2_ui_or_agent": [
        "inspect page_guard, recovery_failed, and collector events",
        "verify Java agent freshness and Data Display stability",
    ],
    "session_runtime": [
        "inspect network_gate/session lifecycle decisions and races",
        "verify session state transitions and overrides",
    ],
    "node_routing": [
        "inspect bootstrap routing inputs, selected zone, and route conflicts",
        "verify baseline network quality for the chosen node",
    ],
    "unknown": [
        "inspect raw event timeline for missing join keys",
        "verify event coverage across DLL/server/client/worker/session layers",
    ],
}


def _parse_ts(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        return datetime.min.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _sort_events(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted((dict(event) for event in events), key=lambda item: _parse_ts(item.get("ts")))


def _has_value(value: Any) -> bool:
    return value not in (None, "", [], {})


def _is_abnormal(event: dict[str, Any]) -> bool:
    return bool(
        str(event.get("status") or "").lower() == "error"
        or event.get("failure_code") is not None
        or str(event.get("event_type") or "") in TRIGGER_EVENT_TYPES
    )


def _latest_non_empty(events: list[dict[str, Any]], key: str) -> Any:
    for event in reversed(events):
        value = event.get(key)
        if _has_value(value):
            return value
    return None


def _discover_artifacts(cloud_root: Path | None, local_root: Path | None) -> tuple[list[Path], list[Path]]:
    raw_paths: list[Path] = []
    aux_paths: list[Path] = []
    for root in (cloud_root, local_root):
        if root is None:
            continue
        raw_dir = root / "raw"
        if raw_dir.exists():
            raw_paths.extend(sorted(raw_dir.glob("*.jsonl")))
            raw_paths.extend(sorted(raw_dir.glob("*.jsonl.gz")))
            raw_paths.extend(sorted(raw_dir.glob("*.gz")))
    if cloud_root is not None:
        uploads_dir = cloud_root / "uploads"
        if uploads_dir.exists():
            raw_paths.extend(sorted(uploads_dir.rglob("*.jsonl")))
            raw_paths.extend(sorted(uploads_dir.rglob("*.jsonl.gz")))
            raw_paths.extend(sorted(uploads_dir.rglob("*.gz")))
    if cloud_root is not None:
        snapshot_path = cloud_root / "active_session_snapshot.json"
        if snapshot_path.exists():
            aux_paths.append(snapshot_path)
    return raw_paths, aux_paths


def _iter_text_lines(path: Path) -> Iterable[tuple[int, str]]:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                yield line_number, line
        return

    with path.open("rt", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            yield line_number, line


def _iter_event_file(path: Path) -> Iterable[dict[str, Any]]:
    resolved_path = str(path.resolve())
    for line_number, line in _iter_text_lines(path):
        if not line.strip():
            continue
        payload = json.loads(line)
        payload["source_artifact"] = resolved_path
        payload["source_line"] = line_number
        yield payload


def _read_event_file(path: Path) -> list[dict[str, Any]]:
    return list(_iter_event_file(path))


def _read_snapshot(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _expand_related_events(
    events: list[dict[str, Any]],
    *,
    session_id: str | None,
    connection_epoch: str | None,
) -> list[dict[str, Any]]:
    if not session_id and not connection_epoch:
        return _sort_events(events)

    selected = [
        event for event in events
        if (session_id and event.get("session_id") == session_id)
        or (connection_epoch and event.get("connection_epoch") == connection_epoch)
    ]

    if not selected and session_id:
        selected = [event for event in events if event.get("session_id") is None]

    changed = True
    while changed:
        changed = False
        dll_seqs = {event.get("dll_seq") for event in selected if event.get("dll_seq") is not None}
        proxy_seqs = {event.get("proxy_seq") for event in selected if event.get("proxy_seq") is not None}
        worker_ids = {
            event.get("worker_request_id")
            for event in selected
            if event.get("worker_request_id") not in (None, "")
        }
        epochs = {
            event.get("connection_epoch")
            for event in selected
            if event.get("connection_epoch") not in (None, "")
        }
        for event in events:
            if event in selected:
                continue
            if (
                (session_id and event.get("session_id") == session_id)
                or (event.get("dll_seq") in dll_seqs if event.get("dll_seq") is not None else False)
                or (event.get("proxy_seq") in proxy_seqs if event.get("proxy_seq") is not None else False)
                or (event.get("worker_request_id") in worker_ids if event.get("worker_request_id") else False)
                or (event.get("connection_epoch") in epochs if event.get("connection_epoch") else False)
            ):
                selected.append(event)
                changed = True

    return _sort_events(selected)


def _stream_related_events(
    raw_paths: list[Path],
    *,
    session_id: str | None,
    connection_epoch: str | None,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    selected_keys: set[tuple[str, int]] = set()
    changed = True

    while changed:
        changed = False
        dll_seqs = {event.get("dll_seq") for event in selected if event.get("dll_seq") is not None}
        proxy_seqs = {event.get("proxy_seq") for event in selected if event.get("proxy_seq") is not None}
        worker_ids = {
            event.get("worker_request_id")
            for event in selected
            if event.get("worker_request_id") not in (None, "")
        }
        epochs = {
            event.get("connection_epoch")
            for event in selected
            if event.get("connection_epoch") not in (None, "")
        }

        for raw_path in raw_paths:
            for event in _iter_event_file(raw_path):
                event_key = (
                    str(event.get("source_artifact") or ""),
                    int(event.get("source_line") or 0),
                )
                if event_key in selected_keys:
                    continue

                if (
                    (session_id and event.get("session_id") == session_id)
                    or (connection_epoch and event.get("connection_epoch") == connection_epoch)
                    or (event.get("dll_seq") in dll_seqs if event.get("dll_seq") is not None else False)
                    or (event.get("proxy_seq") in proxy_seqs if event.get("proxy_seq") is not None else False)
                    or (event.get("worker_request_id") in worker_ids if event.get("worker_request_id") else False)
                    or (event.get("connection_epoch") in epochs if event.get("connection_epoch") else False)
                ):
                    selected.append(event)
                    selected_keys.add(event_key)
                    changed = True

    return _sort_events(selected)


def _derive_page_context(events: list[dict[str, Any]], snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "page": _latest_non_empty(events, "page") or snapshot.get("current_page"),
        "module": _latest_non_empty(events, "module") or snapshot.get("selected_module"),
        "data_category": _latest_non_empty(events, "data_category") or snapshot.get("selected_data_category"),
    }


def _derive_network_context(events: list[dict[str, Any]]) -> dict[str, Any]:
    for event in reversed(events):
        context: dict[str, Any] = {}
        network_quality = event.get("network_quality")
        if isinstance(network_quality, dict):
            if _has_value(network_quality.get("grade")):
                context["grade"] = network_quality.get("grade")
            if _has_value(network_quality.get("status")):
                context["status"] = network_quality.get("status")
            if _has_value(network_quality.get("reason")):
                context["reason"] = network_quality.get("reason")
            if _has_value(network_quality.get("connection_epoch")):
                context["connection_epoch"] = network_quality.get("connection_epoch")
        for key in ("network_quality", "network_grade", "tunnel_status", "probe_failures"):
            if _has_value(event.get(key)):
                context[key] = event.get(key)
        if _has_value(event.get("connection_epoch")):
            context["connection_epoch"] = event.get("connection_epoch")
        if context.get("grade") is None and _has_value(event.get("network_grade")):
            context["grade"] = event.get("network_grade")
        if context.get("status") is None and _has_value(event.get("tunnel_status")):
            context["status"] = event.get("tunnel_status")
        if any(key in context for key in ("network_quality", "network_grade", "tunnel_status", "probe_failures", "grade", "status")):
            if _has_value(event.get("reason")):
                context["reason"] = event.get("reason")
            return context
    return {}


def _derive_route_context(events: list[dict[str, Any]]) -> dict[str, Any]:
    for event in reversed(events):
        context: dict[str, Any] = {}
        for key in ("selected_zone", "selected_metro", "preferred_zone", "preferred_metro", "route_source"):
            if _has_value(event.get(key)):
                context[key] = event.get(key)
        if context:
            return context
    return {}


def _derive_key_metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
    duration_values = [float(event["duration_ms"]) for event in events if event.get("duration_ms") not in (None, "")]
    network_values = [float(event["network_ms"]) for event in events if event.get("network_ms") not in (None, "")]
    hw_values = [float(event["hw_ms"]) for event in events if event.get("hw_ms") not in (None, "")]
    return {
        "event_count": len(events),
        "error_count": sum(1 for event in events if _is_abnormal(event)),
        "max_duration_ms": max(duration_values) if duration_values else None,
        "max_network_ms": max(network_values) if network_values else None,
        "max_hw_ms": max(hw_values) if hw_values else None,
    }


def assemble_session_trace(
    *,
    events: list[dict[str, Any]] | None = None,
    cloud_root: str | Path | None = None,
    local_root: str | Path | None = None,
    session_id: str | None = None,
    connection_epoch: str | None = None,
) -> dict[str, Any]:
    cloud_root_path = Path(cloud_root) if cloud_root is not None else None
    local_root_path = Path(local_root) if local_root is not None else None

    source_artifacts: list[str] = []
    snapshot: dict[str, Any] = {}
    loaded_events = list(events or [])
    if events is None:
        raw_paths, aux_paths = _discover_artifacts(cloud_root_path, local_root_path)
        if session_id or connection_epoch:
            loaded_events.extend(
                _stream_related_events(
                    raw_paths,
                    session_id=session_id,
                    connection_epoch=connection_epoch,
                )
            )
            source_artifacts.extend(
                str(Path(event["source_artifact"]).resolve())
                for event in loaded_events
                if event.get("source_artifact")
            )
        else:
            for raw_path in raw_paths:
                loaded_events.extend(_read_event_file(raw_path))
                source_artifacts.append(str(raw_path.resolve()))
        snapshot_path = aux_paths[0] if aux_paths else None
        snapshot = _read_snapshot(snapshot_path)
        if snapshot_path is not None:
            source_artifacts.append(str(snapshot_path.resolve()))

    resolved_session_id = session_id or str(snapshot.get("session_id") or "") or None
    resolved_connection_epoch = connection_epoch or str(snapshot.get("connection_epoch") or "") or None
    timeline = _expand_related_events(
        loaded_events,
        session_id=resolved_session_id,
        connection_epoch=resolved_connection_epoch,
    )

    if resolved_session_id is None:
        resolved_session_id = _latest_non_empty(timeline, "session_id")
    if resolved_connection_epoch is None:
        resolved_connection_epoch = _latest_non_empty(timeline, "connection_epoch")

    source_artifacts.extend(
        str(Path(event["source_artifact"]).resolve())
        for event in timeline
        if event.get("source_artifact")
    )
    unique_sources = list(dict.fromkeys(source_artifacts))

    return {
        "trace_id": f"trace:{resolved_session_id}" if resolved_session_id else None,
        "session_id": resolved_session_id,
        "connection_epoch": resolved_connection_epoch,
        "status": (
            "completed"
            if any(event.get("event_type") == "session.lifecycle.completed" for event in timeline)
            else "partial"
        ),
        "page_context": _derive_page_context(timeline, snapshot),
        "network_context": _derive_network_context(timeline),
        "route_context": _derive_route_context(timeline),
        "key_metrics": _derive_key_metrics(timeline),
        "timeline": timeline,
        "source_artifacts": unique_sources,
    }


def _find_triggering_event(events: list[dict[str, Any]], triggering_event_type: str | None = None) -> dict[str, Any] | None:
    if not events:
        return None
    if triggering_event_type:
        matches = [event for event in events if event.get("event_type") == triggering_event_type]
        if matches:
            return matches[-1]
    matches = [event for event in events if str(event.get("event_type") or "") in TRIGGER_EVENT_TYPES]
    if matches:
        return matches[-1]
    abnormal = [event for event in events if _is_abnormal(event)]
    return abnormal[-1] if abnormal else events[-1]


def _find_first_abnormal_event(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in events:
        if _is_abnormal(event):
            return event
    return events[0] if events else None


def _has_reverse_server_dll_seq(events: list[dict[str, Any]], dll_seq: Any) -> bool:
    return any(
        event.get("component") == "reverse_server"
        and event.get("dll_seq") == dll_seq
        and str(event.get("event_type") or "").startswith("proxy.request.")
        for event in events
    )


def _domain_from_events(events: list[dict[str, Any]]) -> str:
    if any(
        event.get("failure_domain") == "node_routing"
        or str(event.get("event_type") or "") in {"session.bootstrap.route_conflict", "session.node_routing.route_conflict"}
        for event in events
    ):
        return "node_routing"

    for event in events:
        if str(event.get("event_type") or "") in {"dll.request.retry_exhausted", "dll.socket.connect_failed", "dll.socket.send_failed", "dll.socket.recv_failed"}:
            dll_seq = event.get("dll_seq")
            if dll_seq is None or not _has_reverse_server_dll_seq(events, dll_seq):
                return "cloud_dll_local_proxy"

    if any(
        event.get("failure_domain") == "cloud_proxy_tunnel"
        or str(event.get("event_type") or "") in {"proxy.request.timeout", "tunnel.lifecycle.disconnected", "tunnel.probe.failure"}
        for event in events
    ):
        return "cloud_proxy_tunnel"

    if any(
        event.get("failure_domain") == "local_reverse_client"
        for event in events
    ):
        return "local_reverse_client"

    if any(
        event.get("failure_domain") == "local_worker_rpc"
        or str(event.get("event_type") or "") in {"worker.lifecycle.crashed", "worker.lifecycle.spawn_failed", "worker.lifecycle.parent_disconnected"}
        for event in events
    ):
        return "local_worker_rpc"

    if any(
        str(event.get("error_name") or "").upper() == "ERR_DEVICE_NOT_CONNECTED"
        or "device_not_connected" in str(event.get("reason") or "").lower()
        for event in events
    ):
        return "vehicle_or_vci"

    if any(
        event.get("failure_domain") == "local_j2534_driver"
        or str(event.get("event_type") or "") in {"worker.rpc.failed", "j2534.call.failed"}
        for event in events
    ):
        return "local_j2534_driver"

    if any(
        str(event.get("event_type") or "") in {"page_guard_triggered", "recovery_failed", "agent.collector.guard_failed", "agent.collector.error"}
        or event.get("failure_domain") == "gds2_ui_or_agent"
        for event in events
    ):
        return "gds2_ui_or_agent"

    if any(
        str(event.get("event_type") or "").startswith("session.network_gate.")
        or event.get("failure_domain") == "session_runtime"
        for event in events
    ):
        return "session_runtime"

    return "unknown"


def classify_incident(
    events: list[dict[str, Any]],
    *,
    triggering_event_type: str | None = None,
) -> dict[str, Any]:
    ordered = _sort_events(events)
    triggering_event = _find_triggering_event(ordered, triggering_event_type=triggering_event_type)
    first_abnormal_event = _find_first_abnormal_event(ordered)
    domain = _domain_from_events(ordered)
    return {
        "primary_failure_domain": domain,
        "triggering_event": triggering_event,
        "first_abnormal_event": first_abnormal_event,
        "next_checks": list(NEXT_CHECKS_BY_DOMAIN[domain]),
    }


def _derive_affected_operations(events: list[dict[str, Any]]) -> list[str]:
    generic_scopes = {
        "reverse_client",
        "reverse_server",
        "proxy_request",
        "j2534_worker",
        "j2534_worker_controller",
        "session_runtime",
        "gds2_ui_or_agent",
        "virtual_j2534",
    }
    operations: list[str] = []
    for event in events:
        if not _is_abnormal(event):
            continue
        impact_scope = event.get("impact_scope")
        operation_kind = event.get("operation_kind")
        if impact_scope and str(impact_scope) not in generic_scopes:
            candidate = impact_scope
        else:
            candidate = operation_kind or impact_scope
        if not candidate:
            continue
        text = str(candidate)
        if text not in operations:
            operations.append(text)
    return operations


def _stable_incident_id(session_id: str | None, triggering_event: dict[str, Any] | None) -> str:
    seed = f"{session_id or 'no-session'}|{(triggering_event or {}).get('ts')}|{(triggering_event or {}).get('event_type')}"
    return "incident-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]


def generate_incident_bundle(
    trace: dict[str, Any],
    *,
    triggering_event_type: str | None = None,
) -> dict[str, Any]:
    timeline = _sort_events(trace.get("timeline") or [])
    classification = classify_incident(timeline, triggering_event_type=triggering_event_type)
    triggering_event = classification["triggering_event"]
    first_abnormal_event = classification["first_abnormal_event"]
    page_context = {
        "page": (triggering_event or {}).get("page") or trace.get("page_context", {}).get("page"),
        "module": (triggering_event or {}).get("module") or trace.get("page_context", {}).get("module"),
        "data_category": (triggering_event or {}).get("data_category") or trace.get("page_context", {}).get("data_category"),
    }
    return {
        "incident_id": _stable_incident_id(trace.get("session_id"), triggering_event),
        "session_id": trace.get("session_id"),
        "connection_epoch": trace.get("connection_epoch") or (triggering_event or {}).get("connection_epoch"),
        "primary_failure_domain": classification["primary_failure_domain"],
        "triggering_event": triggering_event,
        "first_abnormal_event": first_abnormal_event,
        "affected_operations": _derive_affected_operations(timeline),
        "page_context": page_context,
        "network_context": trace.get("network_context") or {},
        "route_context": trace.get("route_context") or {},
        "timeline": timeline,
        "key_metrics": trace.get("key_metrics") or {},
        "next_checks": classification["next_checks"],
        "source_artifacts": list(trace.get("source_artifacts") or []),
    }


__all__ = [
    "assemble_session_trace",
    "classify_incident",
    "generate_incident_bundle",
]
