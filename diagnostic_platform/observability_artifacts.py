"""Materialized observability artifacts for traces, incidents, uploads, cleanup, and export."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from diagnostic_platform.observability import get_cloud_observability_root
from diagnostic_platform.observability_analysis import assemble_session_trace, generate_incident_bundle

SESSION_TERMINAL_EVENT_TYPES = {
    "session.lifecycle.completed",
    "session.lifecycle.aborted",
    "session.lifecycle.failed",
}

INCIDENT_TRIGGER_EVENT_TYPES = {
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


@dataclass(frozen=True)
class ProductLogSettings:
    enabled: bool = True
    retention_days_raw: int = 30
    retention_days_session_trace: int = 30
    retention_days_incident: int = 90
    upload_enabled: bool = True
    max_artifact_mb: int = 50


def _is_truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def resolve_product_log_settings(environ: dict[str, str] | None = None) -> ProductLogSettings:
    env = os.environ if environ is None else environ
    return ProductLogSettings(
        enabled=_is_truthy(env.get("PRODUCT_LOGS_ENABLED", "true")),
        retention_days_raw=int(str(env.get("PRODUCT_LOG_RETENTION_DAYS_RAW", "30")).strip() or 30),
        retention_days_session_trace=int(
            str(env.get("PRODUCT_LOG_RETENTION_DAYS_SESSION_TRACE", "30")).strip() or 30
        ),
        retention_days_incident=int(str(env.get("PRODUCT_LOG_RETENTION_DAYS_INCIDENT", "90")).strip() or 90),
        upload_enabled=_is_truthy(env.get("PRODUCT_LOG_UPLOAD_ENABLED", "true")),
        max_artifact_mb=int(str(env.get("PRODUCT_LOG_MAX_ARTIFACT_MB", "50")).strip() or 50),
    )


def _resolve_cloud_root(path_like: str | Path | None) -> Path:
    if path_like is None:
        return get_cloud_observability_root()
    path = Path(path_like)
    if path.name == "cloud":
        return path
    return get_cloud_observability_root(path)


def get_cloud_session_traces_dir(programdata: str | Path | None = None) -> Path:
    return _resolve_cloud_root(programdata) / "session_traces"


def get_cloud_incidents_dir(programdata: str | Path | None = None) -> Path:
    return _resolve_cloud_root(programdata) / "incidents"


def get_cloud_uploads_dir(programdata: str | Path | None = None) -> Path:
    return _resolve_cloud_root(programdata) / "uploads"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _read_bytes_sha256(path: Path) -> tuple[bytes, str]:
    payload = path.read_bytes()
    return payload, hashlib.sha256(payload).hexdigest()


def _discover_cloud_export_candidates(
    *,
    cloud_root: Path,
    project_root: Path,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []

    def _add_path(path: Path, relative_path: str) -> None:
        if not path.exists() or not path.is_file():
            return
        stat = path.stat()
        candidates.append(
            {
                "path": path,
                "relative_path": relative_path,
                "mtime_ns": int(stat.st_mtime_ns),
                "size_bytes": int(stat.st_size),
            }
        )

    for directory_name in ("raw", "session_traces", "incidents"):
        directory = cloud_root / directory_name
        if not directory.exists():
            continue
        for path in sorted(item for item in directory.rglob("*") if item.is_file()):
            relative = "observability/cloud/" + path.relative_to(cloud_root).as_posix()
            _add_path(path, relative)

    _add_path(
        cloud_root / "active_session_snapshot.json",
        "observability/cloud/active_session_snapshot.json",
    )

    compatibility_files = (
        (project_root / "gds2_web.log", "compat/gds2_web.log"),
        (project_root / "logs" / "vci_proxy.log", "compat/logs/vci_proxy.log"),
        (project_root / "logs" / "flask_api.log", "compat/logs/flask_api.log"),
        (Path.home() / "gds2-data" / "vci_proxy_dll.log", "compat/gds2-data/vci_proxy_dll.log"),
        (
            Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "VCI_Proxy" / "tunnel_quality.json",
            "compat/programdata/VCI_Proxy/tunnel_quality.json",
        ),
    )
    for path, relative_path in compatibility_files:
        _add_path(path, relative_path)

    return sorted(candidates, key=lambda item: (item["mtime_ns"], item["relative_path"]))


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, path)
    return path


def _safe_name(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in str(text or "").strip()) or "artifact"


def _load_event_types(trace: dict[str, Any]) -> set[str]:
    return {str(event.get("event_type") or "") for event in trace.get("timeline") or []}


def materialize_session_artifacts(
    *,
    cloud_root: str | Path | None = None,
    session_id: str | None = None,
    connection_epoch: str | None = None,
    local_root: str | Path | None = None,
    triggering_event_type: str | None = None,
) -> dict[str, Any]:
    cloud_root_path = _resolve_cloud_root(cloud_root)
    trace = assemble_session_trace(
        cloud_root=cloud_root_path,
        local_root=local_root,
        session_id=session_id,
        connection_epoch=connection_epoch,
    )
    if trace.get("session_id") is None and trace.get("connection_epoch") is None:
        return {"trace": trace, "trace_path": None, "incident_paths": []}

    trace_name = _safe_name(trace.get("trace_id") or trace.get("connection_epoch") or "trace")
    trace_path = _atomic_write_json(get_cloud_session_traces_dir(cloud_root_path) / f"{trace_name}.json", trace)

    incident_paths: list[Path] = []
    event_types = _load_event_types(trace)
    should_generate_bundle = bool(triggering_event_type and triggering_event_type in event_types)
    if not should_generate_bundle:
        should_generate_bundle = bool(event_types & INCIDENT_TRIGGER_EVENT_TYPES)
    if should_generate_bundle:
        bundle = generate_incident_bundle(trace, triggering_event_type=triggering_event_type)
        incident_path = _atomic_write_json(
            get_cloud_incidents_dir(cloud_root_path) / f"{_safe_name(bundle['incident_id'])}.json",
            bundle,
        )
        incident_paths.append(incident_path)

    return {"trace": trace, "trace_path": trace_path, "incident_paths": incident_paths}


def _discover_connection_context_from_artifact(path: Path) -> tuple[str | None, str | None]:
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        session_id = payload.get("session_id")
        connection_epoch = payload.get("connection_epoch")
        return (
            str(session_id) if session_id not in (None, "") else None,
            str(connection_epoch) if connection_epoch not in (None, "") else None,
        )
    return None, None


def ingest_uploaded_artifact(
    payload: dict[str, Any],
    *,
    cloud_root: str | Path | None = None,
    max_artifact_mb: int = 50,
) -> dict[str, Any]:
    cloud_root_path = _resolve_cloud_root(cloud_root)
    client_instance_id = str(payload.get("client_instance_id") or "").strip()
    connection_epoch = str(payload.get("connection_epoch") or "").strip()
    artifact_id = str(payload.get("artifact_id") or "").strip()
    artifact_name = str(payload.get("artifact_name") or "").strip()
    content_base64 = str(payload.get("content_base64") or "").strip()
    if not (client_instance_id and connection_epoch and artifact_id and artifact_name and content_base64):
        raise ValueError("client_instance_id, connection_epoch, artifact_id, artifact_name, and content_base64 are required")

    target_dir = get_cloud_uploads_dir(cloud_root_path) / _safe_name(client_instance_id) / _safe_name(connection_epoch)
    target_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = target_dir / f"{_safe_name(artifact_id)}-{_safe_name(artifact_name)}"
    manifest_path = target_dir / f"{_safe_name(artifact_id)}.manifest.json"
    deduped = artifact_path.exists()
    if not deduped:
        artifact_bytes = base64.b64decode(content_base64.encode("ascii"))
        if len(artifact_bytes) > max(1, int(max_artifact_mb)) * 1024 * 1024:
            raise ValueError("artifact exceeds PRODUCT_LOG_MAX_ARTIFACT_MB")
        artifact_path.write_bytes(artifact_bytes)
        _atomic_write_json(
            manifest_path,
            {
                "client_instance_id": client_instance_id,
                "connection_epoch": connection_epoch,
                "artifact_id": artifact_id,
                "artifact_name": artifact_name,
                "artifact_type": payload.get("artifact_type"),
                "session_id": payload.get("session_id"),
                "ingested_at": time.time(),
            },
        )

    session_id = str(payload.get("session_id") or "").strip() or None
    if session_id is None:
        try:
            session_id, _ = _discover_connection_context_from_artifact(artifact_path)
        except Exception:
            session_id = None

    refresh = materialize_session_artifacts(
        cloud_root=cloud_root_path,
        session_id=session_id,
        connection_epoch=connection_epoch,
    )

    return {
        "success": True,
        "deduped": deduped,
        "artifact_path": artifact_path,
        "manifest_path": manifest_path,
        "trace_path": refresh.get("trace_path"),
        "incident_paths": refresh.get("incident_paths", []),
    }


def export_cloud_log_artifacts(
    payload: dict[str, Any] | None = None,
    *,
    cloud_root: str | Path | None = None,
    project_root: str | Path | None = None,
    max_artifact_mb: int = 50,
) -> dict[str, Any]:
    request = dict(payload or {})
    cursor_mtime_ns = int(request.get("cursor_mtime_ns") or 0)
    cursor_path = str(request.get("cursor_path") or "")
    max_files = int(request.get("max_files") or 20)
    max_batch_bytes = int(request.get("max_batch_bytes") or (5 * 1024 * 1024))
    if max_files < 1:
        raise ValueError("max_files must be >= 1")
    if max_files > 100:
        max_files = 100
    if max_batch_bytes < 1:
        raise ValueError("max_batch_bytes must be >= 1")

    resolved_cloud_root = _resolve_cloud_root(cloud_root)
    resolved_project_root = Path(project_root) if project_root is not None else _repo_root()
    max_bytes = max(1, int(max_artifact_mb)) * 1024 * 1024

    candidates = _discover_cloud_export_candidates(
        cloud_root=resolved_cloud_root,
        project_root=resolved_project_root,
    )
    changed = [
        item
        for item in candidates
        if (item["mtime_ns"], item["relative_path"]) > (cursor_mtime_ns, cursor_path)
    ]

    files: list[dict[str, Any]] = []
    skipped_files: list[dict[str, Any]] = []
    last_cursor_mtime_ns = cursor_mtime_ns
    last_cursor_path = cursor_path
    processed_count = 0
    batch_bytes = 0

    for item in changed:
        if processed_count >= max_files:
            break
        relative_path = str(item["relative_path"])
        size_bytes = int(item["size_bytes"])
        mtime_ns = int(item["mtime_ns"])

        if size_bytes > max_bytes:
            last_cursor_mtime_ns = mtime_ns
            last_cursor_path = relative_path
            skipped_files.append(
                {
                    "relative_path": relative_path,
                    "mtime_ns": mtime_ns,
                    "size_bytes": size_bytes,
                    "reason": "file_exceeds_max_artifact_mb",
                }
            )
            processed_count += 1
            continue
        if size_bytes > max_batch_bytes:
            last_cursor_mtime_ns = mtime_ns
            last_cursor_path = relative_path
            skipped_files.append(
                {
                    "relative_path": relative_path,
                    "mtime_ns": mtime_ns,
                    "size_bytes": size_bytes,
                    "reason": "file_exceeds_max_batch_bytes",
                }
            )
            processed_count += 1
            continue
        if batch_bytes + size_bytes > max_batch_bytes:
            break

        last_cursor_mtime_ns = mtime_ns
        last_cursor_path = relative_path
        try:
            content, sha256 = _read_bytes_sha256(Path(item["path"]))
        except OSError:
            skipped_files.append(
                {
                    "relative_path": relative_path,
                    "mtime_ns": mtime_ns,
                    "size_bytes": size_bytes,
                    "reason": "file_unreadable",
                }
            )
            processed_count += 1
            continue
        files.append(
            {
                "relative_path": relative_path,
                "mtime_ns": mtime_ns,
                "size_bytes": size_bytes,
                "sha256": sha256,
                "content_base64": base64.b64encode(content).decode("ascii"),
            }
        )
        batch_bytes += size_bytes
        processed_count += 1

    has_more = processed_count < len(changed)
    if processed_count == 0:
        last_cursor_mtime_ns = cursor_mtime_ns
        last_cursor_path = cursor_path

    return {
        "success": True,
        "files": files,
        "skipped_files": skipped_files,
        "has_more": has_more,
        "next_cursor_mtime_ns": last_cursor_mtime_ns,
        "next_cursor_path": last_cursor_path,
        "synced_at": time.time(),
        "max_batch_bytes": max_batch_bytes,
    }


def _delete_older_than(directory: Path, *, max_age_days: int, now: float) -> None:
    if not directory.exists():
        return
    cutoff = now - (max_age_days * 24 * 3600)
    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except OSError:
            continue


def _pending_outbox_artifact_paths(local_root: Path) -> set[Path]:
    pending_dir = local_root / "outbox" / "pending"
    artifact_paths: set[Path] = set()
    if not pending_dir.exists():
        return artifact_paths
    for manifest_path in pending_dir.glob("*.json"):
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        artifact_path = payload.get("artifact_path")
        if artifact_path:
            artifact_paths.add(Path(artifact_path))
    return artifact_paths


def cleanup_product_observability(
    *,
    programdata: str | Path | None = None,
    appdata: str | Path | None = None,
    now: float | None = None,
    retention_days_raw: int = 30,
    retention_days_session_trace: int = 30,
    retention_days_incident: int = 90,
) -> None:
    current_time = time.time() if now is None else float(now)
    cloud_root = _resolve_cloud_root(programdata)
    local_root = Path(appdata) / "VCI_Proxy" / "observability" if appdata is not None else None

    _delete_older_than(cloud_root / "raw", max_age_days=retention_days_raw, now=current_time)
    _delete_older_than(cloud_root / "session_traces", max_age_days=retention_days_session_trace, now=current_time)
    _delete_older_than(cloud_root / "incidents", max_age_days=retention_days_incident, now=current_time)
    _delete_older_than(cloud_root / "uploads", max_age_days=retention_days_incident, now=current_time)
    if local_root is not None:
        _delete_older_than(local_root / "outbox" / "uploaded", max_age_days=retention_days_incident, now=current_time)
        keep_artifacts = _pending_outbox_artifact_paths(local_root)
        artifacts_dir = local_root / "outbox" / "artifacts"
        if artifacts_dir.exists():
            cutoff = current_time - (retention_days_incident * 24 * 3600)
            for path in artifacts_dir.glob("*"):
                if not path.is_file() or path in keep_artifacts:
                    continue
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink(missing_ok=True)
                except OSError:
                    continue


def maybe_materialize_cloud_artifacts(
    event_payload: dict[str, Any],
    *,
    writer_path: str | Path | None,
    flush_callback: Any | None = None,
) -> None:
    if writer_path is None:
        return
    path = Path(writer_path)
    if path.parent.name != "raw":
        return
    event_type = str(event_payload.get("event_type") or "")
    if event_type not in SESSION_TERMINAL_EVENT_TYPES and event_type not in INCIDENT_TRIGGER_EVENT_TYPES:
        return
    if callable(flush_callback):
        try:
            flush_callback()
        except Exception:
            pass
    cloud_root = path.parent.parent
    session_id = event_payload.get("session_id")
    connection_epoch = event_payload.get("connection_epoch")
    materialize_session_artifacts(
        cloud_root=cloud_root,
        session_id=str(session_id) if session_id not in (None, "") else None,
        connection_epoch=str(connection_epoch) if connection_epoch not in (None, "") else None,
        triggering_event_type=event_type if event_type in INCIDENT_TRIGGER_EVENT_TYPES else None,
    )


__all__ = [
    "cleanup_product_observability",
    "export_cloud_log_artifacts",
    "get_cloud_incidents_dir",
    "get_cloud_session_traces_dir",
    "get_cloud_uploads_dir",
    "ingest_uploaded_artifact",
    "materialize_session_artifacts",
    "maybe_materialize_cloud_artifacts",
    "ProductLogSettings",
    "resolve_product_log_settings",
]
