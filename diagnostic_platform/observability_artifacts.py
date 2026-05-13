"""Materialized observability artifacts for traces, incidents, uploads, and cleanup."""

from __future__ import annotations

import base64
import atexit
import hashlib
import json
import logging
import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

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

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _MaterializationJob:
    cloud_root: Path
    session_id: str | None
    connection_epoch: str | None
    local_root: str | Path | None
    triggering_event_type: str | None
    flush_callback: Callable[[], Any] | None = None


_MATERIALIZATION_QUEUE: queue.Queue[_MaterializationJob] = queue.Queue(maxsize=256)
_MATERIALIZATION_WORKER_LOCK = threading.Lock()
_MATERIALIZATION_WORKER: threading.Thread | None = None
_DIRECT_MATERIALIZATION_THREADS: set[threading.Thread] = set()
_DIRECT_MATERIALIZATION_THREADS_LOCK = threading.Lock()
_ARTIFACT_WRITE_LOCK = threading.RLock()


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


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> Path:
    with _ARTIFACT_WRITE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        _recover_atomic_json_temp_siblings(path)
        if path.exists() and not _json_file_is_complete(path):
            corrupt_path = path.with_name(
                f"{path.name}.corrupt-{int(time.time())}-{uuid.uuid4().hex[:8]}"
            )
            try:
                os.replace(path, corrupt_path)
                logger.warning("quarantined incomplete observability JSON artifact: %s", corrupt_path)
            except OSError:
                logger.warning("failed to quarantine incomplete observability JSON artifact: %s", path)
        temp_path = path.with_name(
            f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            if not _json_file_is_complete(temp_path):
                raise ValueError(f"incomplete JSON artifact temp file: {temp_path}")
            os.replace(temp_path, path)
            _cleanup_atomic_json_temp_siblings(path)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
    return path


def _atomic_write_bytes(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(
        f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.artifact.tmp"
    )
    try:
        temp_path.write_bytes(payload)
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
    return path


def _atomic_json_temp_target_name(path: Path) -> str | None:
    target_name, pid, unique_id, suffix = (path.name.rsplit(".", 3) + [None] * 4)[:4]
    if suffix != "tmp" or not str(pid).isdigit():
        return None
    if not str(target_name or "").endswith(".json"):
        return None
    if len(str(unique_id)) != 32:
        return None
    try:
        int(str(unique_id), 16)
    except ValueError:
        return None
    return target_name or None


def _json_file_is_complete(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8") as handle:
            json.load(handle)
        return True
    except Exception:
        return False


def _cleanup_atomic_json_temp_siblings(path: Path) -> None:
    for candidate in path.parent.glob(f"{path.name}.*.tmp"):
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            continue


def _recover_atomic_json_temp_siblings(path: Path) -> int:
    candidates = sorted(
        path.parent.glob(f"{path.name}.*.tmp"),
        key=lambda candidate: candidate.stat().st_mtime if candidate.exists() else 0.0,
        reverse=True,
    )
    if not candidates:
        return 0
    if path.exists():
        _cleanup_atomic_json_temp_siblings(path)
        return 0

    recovered = 0
    for candidate in candidates:
        if _atomic_json_temp_target_name(candidate) != path.name:
            continue
        if not _json_file_is_complete(candidate):
            continue
        try:
            os.replace(candidate, path)
            recovered = 1
            break
        except OSError:
            continue
    if path.exists():
        _cleanup_atomic_json_temp_siblings(path)
    return recovered


def _recover_atomic_json_temp_files(directory: Path, *, recursive: bool = False) -> int:
    if not directory.exists():
        return 0
    recovered = 0
    pattern = "**/*.tmp" if recursive else "*.tmp"
    targets: set[Path] = set()
    for candidate in directory.glob(pattern):
        if not candidate.is_file():
            continue
        target_name = _atomic_json_temp_target_name(candidate)
        if target_name is None:
            continue
        targets.add(candidate.with_name(target_name))
    for target in targets:
        recovered += _recover_atomic_json_temp_siblings(target)
    return recovered


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
    if _backfill_uploaded_manifest_session_ids(
        cloud_root=cloud_root_path,
        session_id=trace.get("session_id"),
        connection_epoch=trace.get("connection_epoch"),
        timeline=list(trace.get("timeline") or []),
    ):
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


def _run_materialization_job(job: _MaterializationJob) -> None:
    if callable(job.flush_callback):
        try:
            job.flush_callback()
        except Exception:
            logger.debug("Failed to flush raw writer before artifact materialization", exc_info=True)
    materialize_session_artifacts(
        cloud_root=job.cloud_root,
        session_id=job.session_id,
        connection_epoch=job.connection_epoch,
        local_root=job.local_root,
        triggering_event_type=job.triggering_event_type,
    )


def _materialization_worker_loop() -> None:
    while True:
        job = _MATERIALIZATION_QUEUE.get()
        try:
            _run_materialization_job(job)
        except Exception:
            logger.exception("observability artifact materialization failed")
        finally:
            _MATERIALIZATION_QUEUE.task_done()


def _ensure_materialization_worker_started() -> None:
    global _MATERIALIZATION_WORKER
    with _MATERIALIZATION_WORKER_LOCK:
        if _MATERIALIZATION_WORKER is not None and _MATERIALIZATION_WORKER.is_alive():
            return
        _MATERIALIZATION_WORKER = threading.Thread(
            target=_materialization_worker_loop,
            name="observability-artifact-materializer",
            daemon=True,
        )
        _MATERIALIZATION_WORKER.start()


def queue_session_artifact_materialization(
    *,
    cloud_root: str | Path,
    session_id: str | None = None,
    connection_epoch: str | None = None,
    local_root: str | Path | None = None,
    triggering_event_type: str | None = None,
    flush_callback: Callable[[], Any] | None = None,
) -> bool:
    """Queue trace/incident refresh work without blocking the caller."""
    job = _MaterializationJob(
        cloud_root=_resolve_cloud_root(cloud_root),
        session_id=session_id,
        connection_epoch=connection_epoch,
        local_root=local_root,
        triggering_event_type=triggering_event_type,
        flush_callback=flush_callback,
    )
    _ensure_materialization_worker_started()
    try:
        _MATERIALIZATION_QUEUE.put_nowait(job)
        return True
    except queue.Full:
        logger.warning(
            "observability artifact materialization queue full; dropping job session_id=%s connection_epoch=%s",
            session_id,
            connection_epoch,
        )
        return False


def start_session_artifact_materialization(
    *,
    cloud_root: str | Path,
    session_id: str | None = None,
    connection_epoch: str | None = None,
    local_root: str | Path | None = None,
    triggering_event_type: str | None = None,
    flush_callback: Callable[[], Any] | None = None,
) -> bool:
    """Start one background materialization thread that is not tied to the queue worker."""
    job = _MaterializationJob(
        cloud_root=_resolve_cloud_root(cloud_root),
        session_id=session_id,
        connection_epoch=connection_epoch,
        local_root=local_root,
        triggering_event_type=triggering_event_type,
        flush_callback=flush_callback,
    )

    def _runner() -> None:
        current = threading.current_thread()
        try:
            _run_materialization_job(job)
        finally:
            with _DIRECT_MATERIALIZATION_THREADS_LOCK:
                _DIRECT_MATERIALIZATION_THREADS.discard(current)

    thread = threading.Thread(
        target=_runner,
        name=(
            "observability-artifact-direct-"
            f"{_safe_name(session_id or connection_epoch or 'unknown')}"
        ),
        daemon=False,
    )
    with _DIRECT_MATERIALIZATION_THREADS_LOCK:
        _DIRECT_MATERIALIZATION_THREADS.add(thread)
    try:
        thread.start()
        return True
    except Exception:
        with _DIRECT_MATERIALIZATION_THREADS_LOCK:
            _DIRECT_MATERIALIZATION_THREADS.discard(thread)
        logger.exception(
            "failed to start direct observability artifact materialization thread session_id=%s connection_epoch=%s",
            session_id,
            connection_epoch,
        )
        return False


def wait_for_observability_artifact_jobs(timeout_s: float = 5.0) -> bool:
    """Best-effort wait for queued artifact work, used by tests and shutdown."""
    deadline = time.time() + max(0.0, float(timeout_s))
    while time.time() <= deadline:
        with _DIRECT_MATERIALIZATION_THREADS_LOCK:
            active_threads = [thread for thread in _DIRECT_MATERIALIZATION_THREADS if thread.is_alive()]
            _DIRECT_MATERIALIZATION_THREADS.intersection_update(active_threads)
        if _MATERIALIZATION_QUEUE.unfinished_tasks == 0 and not active_threads:
            return True
        for thread in active_threads:
            thread.join(timeout=0.01)
        time.sleep(0.01)
    with _DIRECT_MATERIALIZATION_THREADS_LOCK:
        active_threads = [thread for thread in _DIRECT_MATERIALIZATION_THREADS if thread.is_alive()]
        _DIRECT_MATERIALIZATION_THREADS.intersection_update(active_threads)
    return _MATERIALIZATION_QUEUE.unfinished_tasks == 0 and not active_threads


def _normalize_context_value(value: Any, *, placeholder: str) -> str | None:
    text = str(value or "").strip()
    if not text or text.lower() == placeholder:
        return None
    return text


def _context_from_payload(payload: Any) -> tuple[str | None, str | None]:
    if not isinstance(payload, dict):
        return None, None
    return (
        _normalize_context_value(payload.get("session_id"), placeholder="no-session"),
        _normalize_context_value(payload.get("connection_epoch"), placeholder="no-epoch"),
    )


def _discover_connection_context_from_artifact(path: Path) -> tuple[str | None, str | None]:
    if path.suffix == ".gz":
        import gzip

        handle = gzip.open(path, "rt", encoding="utf-8")
    else:
        handle = path.open("rt", encoding="utf-8")

    with handle:
        session_id: str | None = None
        connection_epoch: str | None = None
        for line in handle:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except Exception:
                continue
            payload_session_id, payload_connection_epoch = _context_from_payload(payload)
            session_id = session_id or payload_session_id
            connection_epoch = connection_epoch or payload_connection_epoch
            if session_id and connection_epoch:
                break
    return session_id, connection_epoch


def _infer_session_id_from_cloud_context(
    *,
    cloud_root: Path,
    connection_epoch: str | None,
) -> str | None:
    normalized_epoch = _normalize_context_value(
        connection_epoch,
        placeholder="no-epoch",
    )
    if normalized_epoch is None:
        return None
    try:
        trace = assemble_session_trace(
            cloud_root=cloud_root,
            connection_epoch=normalized_epoch,
        )
    except Exception:
        return None
    return _normalize_context_value(
        trace.get("session_id"),
        placeholder="no-session",
    )


def _parse_event_ts(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _artifact_event_bounds(path: Path) -> tuple[datetime | None, datetime | None]:
    if path.suffix == ".gz":
        import gzip

        handle = gzip.open(path, "rt", encoding="utf-8")
    else:
        handle = path.open("rt", encoding="utf-8")

    with handle:
        start: datetime | None = None
        end: datetime | None = None
        for line in handle:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except Exception:
                continue
            ts = _parse_event_ts(payload.get("ts"))
            if ts is None:
                continue
            start = ts if start is None or ts < start else start
            end = ts if end is None or ts > end else end
    return start, end


def _timeline_event_bounds(events: list[dict[str, Any]]) -> tuple[datetime | None, datetime | None]:
    start: datetime | None = None
    end: datetime | None = None
    for event in events:
        ts = _parse_event_ts(event.get("ts"))
        if ts is None:
            continue
        start = ts if start is None or ts < start else start
        end = ts if end is None or ts > end else end
    return start, end


def _backfill_uploaded_manifest_session_ids(
    *,
    cloud_root: Path,
    session_id: str | None,
    connection_epoch: str | None,
    timeline: list[dict[str, Any]],
) -> int:
    normalized_session_id = _normalize_context_value(session_id, placeholder="no-session")
    normalized_epoch = _normalize_context_value(connection_epoch, placeholder="no-epoch")
    if normalized_session_id is None or normalized_epoch is None:
        return 0

    uploads_root = get_cloud_uploads_dir(cloud_root)
    if not uploads_root.exists():
        return 0

    window_start, window_end = _timeline_event_bounds(timeline)
    updated = 0
    for client_dir in uploads_root.iterdir():
        if not client_dir.is_dir():
            continue
        epoch_dir = client_dir / _safe_name(normalized_epoch)
        if not epoch_dir.exists():
            continue
        for manifest_path in epoch_dir.glob("*.manifest.json"):
            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            existing_session_id = _normalize_context_value(
                payload.get("session_id"),
                placeholder="no-session",
            )
            if existing_session_id is not None:
                continue

            artifact_id = str(payload.get("artifact_id") or "").strip()
            artifact_name = str(payload.get("artifact_name") or "").strip()
            if not artifact_id or not artifact_name:
                continue
            artifact_path = epoch_dir / f"{_safe_name(artifact_id)}-{_safe_name(artifact_name)}"
            if not artifact_path.exists():
                continue

            try:
                discovered_session_id, discovered_connection_epoch = _discover_connection_context_from_artifact(
                    artifact_path
                )
            except Exception:
                discovered_session_id, discovered_connection_epoch = None, None
            if discovered_session_id is not None and discovered_session_id != normalized_session_id:
                continue
            if discovered_connection_epoch is not None and discovered_connection_epoch != normalized_epoch:
                continue

            artifact_start, artifact_end = _artifact_event_bounds(artifact_path)
            if window_start is not None and artifact_end is not None and artifact_end < window_start:
                continue
            if window_end is not None and artifact_start is not None and artifact_start > window_end:
                continue

            payload["session_id"] = normalized_session_id
            _atomic_write_json(manifest_path, payload)
            updated += 1
    return updated


def ingest_uploaded_artifact(
    payload: dict[str, Any],
    *,
    cloud_root: str | Path | None = None,
    max_artifact_mb: int = 50,
    materialize_async: bool = False,
) -> dict[str, Any]:
    cloud_root_path = _resolve_cloud_root(cloud_root)
    client_instance_id = str(payload.get("client_instance_id") or "").strip()
    payload_connection_epoch = _normalize_context_value(
        payload.get("connection_epoch"),
        placeholder="no-epoch",
    )
    connection_epoch = payload_connection_epoch or "no-epoch"
    artifact_id = str(payload.get("artifact_id") or "").strip()
    artifact_name = str(payload.get("artifact_name") or "").strip()
    content_base64 = str(payload.get("content_base64") or "").strip()
    if not (client_instance_id and connection_epoch and artifact_id and artifact_name and content_base64):
        raise ValueError("client_instance_id, connection_epoch, artifact_id, artifact_name, and content_base64 are required")

    artifact_bytes = base64.b64decode(content_base64.encode("ascii"))
    if len(artifact_bytes) > max(1, int(max_artifact_mb)) * 1024 * 1024:
        raise ValueError("artifact exceeds PRODUCT_LOG_MAX_ARTIFACT_MB")
    artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()

    target_dir = get_cloud_uploads_dir(cloud_root_path) / _safe_name(client_instance_id) / _safe_name(connection_epoch)
    target_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = target_dir / f"{_safe_name(artifact_id)}-{_safe_name(artifact_name)}"
    manifest_path = target_dir / f"{_safe_name(artifact_id)}.manifest.json"
    existing_bytes_match = False
    if artifact_path.exists():
        try:
            existing_bytes_match = (
                artifact_path.stat().st_size == len(artifact_bytes)
                and hashlib.sha256(artifact_path.read_bytes()).hexdigest() == artifact_sha256
            )
        except OSError:
            existing_bytes_match = False
    manifest_valid = manifest_path.exists() and _json_file_is_complete(manifest_path)
    deduped = existing_bytes_match and manifest_valid
    if not deduped:
        _atomic_write_bytes(artifact_path, artifact_bytes)
    session_id = _normalize_context_value(payload.get("session_id"), placeholder="no-session")
    if session_id is None or payload_connection_epoch is None:
        try:
            discovered_session_id, discovered_connection_epoch = (
                _discover_connection_context_from_artifact(artifact_path)
            )
        except Exception:
            discovered_session_id, discovered_connection_epoch = None, None
        session_id = session_id or discovered_session_id
        if payload_connection_epoch is None and discovered_connection_epoch:
            connection_epoch = discovered_connection_epoch
    if session_id is None:
        session_id = _infer_session_id_from_cloud_context(
            cloud_root=cloud_root_path,
            connection_epoch=connection_epoch,
        )

    if not deduped:
        _atomic_write_json(
            manifest_path,
            {
                "client_instance_id": client_instance_id,
                "connection_epoch": connection_epoch,
                "artifact_id": artifact_id,
                "artifact_name": artifact_name,
                "artifact_type": payload.get("artifact_type"),
                "session_id": session_id,
                "artifact_size_bytes": len(artifact_bytes),
                "artifact_sha256": artifact_sha256,
                "ingested_at": time.time(),
            },
        )

    if materialize_async:
        materialization_queued = queue_session_artifact_materialization(
            cloud_root=cloud_root_path,
            session_id=session_id,
            connection_epoch=connection_epoch,
        )
        refresh = {"trace_path": None, "incident_paths": []}
        materialization = "queued" if materialization_queued else "dropped"
    else:
        refresh = materialize_session_artifacts(
            cloud_root=cloud_root_path,
            session_id=session_id,
            connection_epoch=connection_epoch,
        )
        materialization = "completed"

    return {
        "success": True,
        "deduped": deduped,
        "artifact_path": artifact_path,
        "manifest_path": manifest_path,
        "trace_path": refresh.get("trace_path"),
        "incident_paths": refresh.get("incident_paths", []),
        "materialization": materialization,
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

    _recover_atomic_json_temp_files(cloud_root / "session_traces")
    _recover_atomic_json_temp_files(cloud_root / "incidents")
    _recover_atomic_json_temp_files(cloud_root / "uploads", recursive=True)

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
    materialize_async: bool = True,
) -> None:
    if writer_path is None:
        return
    path = Path(writer_path)
    if path.parent.name != "raw":
        return
    event_type = str(event_payload.get("event_type") or "")
    if event_type not in SESSION_TERMINAL_EVENT_TYPES and event_type not in INCIDENT_TRIGGER_EVENT_TYPES:
        return
    cloud_root = path.parent.parent
    session_id = event_payload.get("session_id")
    connection_epoch = event_payload.get("connection_epoch")
    materialize_kwargs = {
        "cloud_root": cloud_root,
        "session_id": str(session_id) if session_id not in (None, "") else None,
        "connection_epoch": str(connection_epoch) if connection_epoch not in (None, "") else None,
        "triggering_event_type": event_type if event_type in INCIDENT_TRIGGER_EVENT_TYPES else None,
    }
    if materialize_async:
        if event_type in SESSION_TERMINAL_EVENT_TYPES:
            start_session_artifact_materialization(
                **materialize_kwargs,
                flush_callback=flush_callback if callable(flush_callback) else None,
            )
            return
        queue_session_artifact_materialization(
            **materialize_kwargs,
            flush_callback=flush_callback if callable(flush_callback) else None,
        )
        return
    if callable(flush_callback):
        try:
            flush_callback()
        except Exception:
            pass
    materialize_session_artifacts(**materialize_kwargs)


atexit.register(wait_for_observability_artifact_jobs, timeout_s=5.0)


__all__ = [
    "cleanup_product_observability",
    "get_cloud_incidents_dir",
    "get_cloud_session_traces_dir",
    "get_cloud_uploads_dir",
    "ingest_uploaded_artifact",
    "materialize_session_artifacts",
    "maybe_materialize_cloud_artifacts",
    "queue_session_artifact_materialization",
    "start_session_artifact_materialization",
    "wait_for_observability_artifact_jobs",
    "ProductLogSettings",
    "resolve_product_log_settings",
]
