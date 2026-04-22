"""Product-level observability primitives for cloud and local diagnostics.

Phase 1 scope:
- canonical `observability.v1` event envelope
- JSONL writers with per-component-instance files
- payload redaction helpers
- active-session snapshot storage
- lightweight component-writer registry for server/runtime usage
"""

from __future__ import annotations

import atexit
import gzip
import hashlib
import json
import os
import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

OBSERVABILITY_SCHEMA_VERSION = "observability.v1"
_PROCESS_STARTUP_TS = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
_JSON_DUMP_KWARGS = {"ensure_ascii": False, "separators": (",", ":")}
_SENTINEL = object()
_WRITER_REGISTRY: dict[tuple[str, str], "JsonlWriter"] = {}
_WRITER_LOCK = threading.RLock()


def utc_now_iso(ts: float | None = None) -> str:
    """Return one UTC timestamp formatted for event/snapshot payloads."""
    value = time.time() if ts is None else ts
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def build_component_instance_id(
    component: str,
    *,
    pid: int | None = None,
    startup_ts: str | None = None,
) -> str:
    """Return the canonical component-instance identifier."""
    return f"{component}:{pid or os.getpid()}:{startup_ts or _PROCESS_STARTUP_TS}"


def generate_request_id() -> str:
    """Return one short request correlation id for API events."""
    return uuid.uuid4().hex[:16]


def get_cloud_observability_root(programdata: str | Path | None = None) -> Path:
    base = Path(programdata) if programdata is not None else Path(
        os.environ.get("PROGRAMDATA", "C:/ProgramData")
    )
    return base / "RPA_Diagnostic" / "observability" / "cloud"


def get_local_observability_root(appdata: str | Path | None = None) -> Path:
    base = Path(appdata) if appdata is not None else Path(
        os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
    )
    return base / "VCI_Proxy" / "observability"


def get_active_session_snapshot_path(programdata: str | Path | None = None) -> Path:
    return get_cloud_observability_root(programdata) / "active_session_snapshot.json"


def _get_cloud_raw_dir(programdata: str | Path | None = None) -> Path:
    return get_cloud_observability_root(programdata) / "raw"


def _sanitize_component_for_filename(component: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(component or "").strip())
    return text or "component"


def _atomic_write_text(path: Path, text: str) -> Path:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temp_path.write_text(text, encoding="utf-8")
        os.replace(temp_path, path)
    except OSError:
        return path
    return path


def _sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _mask_vin(vin: str) -> str:
    text = str(vin or "").strip()
    if len(text) <= 6:
        return "*" * len(text)
    return f"{text[:3]}{'*' * (len(text) - 6)}{text[-3:]}"


def _summarize_binary_payload(value: bytes | bytearray) -> dict[str, Any]:
    raw = bytes(value)
    return {
        "type": "bytes",
        "length": len(raw),
        "digest": _sha256_hex(raw),
        "prefix_hex": raw[:16].hex(),
    }


def redact_payload(payload: Any) -> Any:
    """Best-effort redaction for structured observability payloads."""
    if isinstance(payload, Mapping):
        redacted: dict[str, Any] = {}
        for key, value in payload.items():
            lower_key = str(key).strip().lower()
            if lower_key == "vin":
                vin_text = str(value or "").strip()
                redacted["vin_masked"] = _mask_vin(vin_text)
                redacted["vin_hash"] = _sha256_hex(vin_text.encode("utf-8"))
                continue
            if lower_key in {"authorization", "x-api-token", "token", "psk"} or lower_key.endswith(
                "_token"
            ):
                redacted[key] = "<redacted>"
                continue
            if isinstance(value, (bytes, bytearray)):
                redacted[key] = _summarize_binary_payload(value)
                continue
            redacted[key] = redact_payload(value)
        return redacted
    if isinstance(payload, (list, tuple)):
        return [redact_payload(item) for item in payload]
    return payload


@dataclass(slots=True)
class LogContext:
    session_id: str | None = None
    connection_epoch: str | None = None
    dll_seq: int | None = None
    proxy_seq: int | None = None
    worker_request_id: str | None = None
    operation_kind: str | None = None
    page: str | None = None
    module: str | None = None
    data_category: str | None = None
    request_id: str | None = None


@dataclass(slots=True)
class ProductLogEvent:
    component: str
    event_type: str
    schema_version: str = OBSERVABILITY_SCHEMA_VERSION
    ts: str = field(default_factory=utc_now_iso)
    component_instance_id: str | None = None
    session_id: str | None = None
    connection_epoch: str | None = None
    dll_seq: int | None = None
    proxy_seq: int | None = None
    worker_request_id: str | None = None
    operation_kind: str | None = None
    status: str = "info"
    failure_code: str | None = None
    failure_domain: str = "unknown"
    reason: str | None = None
    duration_ms: float | int | None = None
    hw_ms: float | int | None = None
    network_ms: float | int | None = None
    page: str | None = None
    module: str | None = None
    data_category: str | None = None
    symptom: str | None = None
    impact_scope: str | None = None
    next_checks: list[str] = field(default_factory=list)
    redaction_applied: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self.component:
            raise ValueError("component cannot be empty")
        if not self.event_type:
            raise ValueError("event_type cannot be empty")
        if not self.component_instance_id:
            self.component_instance_id = build_component_instance_id(self.component)
        self.next_checks = list(self.next_checks or [])
        self.redaction_applied = list(self.redaction_applied or [])
        self.extra = dict(self.extra or {})

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "ts": self.ts,
            "component": self.component,
            "component_instance_id": self.component_instance_id,
            "event_type": self.event_type,
            "session_id": self.session_id,
            "connection_epoch": self.connection_epoch,
            "dll_seq": self.dll_seq,
            "proxy_seq": self.proxy_seq,
            "worker_request_id": self.worker_request_id,
            "operation_kind": self.operation_kind,
            "status": self.status,
            "failure_code": self.failure_code,
            "failure_domain": self.failure_domain,
            "reason": self.reason,
            "duration_ms": self.duration_ms,
            "hw_ms": self.hw_ms,
            "network_ms": self.network_ms,
            "page": self.page,
            "module": self.module,
            "data_category": self.data_category,
            "symptom": self.symptom,
            "impact_scope": self.impact_scope,
            "next_checks": list(self.next_checks),
            "redaction_applied": list(self.redaction_applied),
        }
        payload.update(self.extra)
        return payload


_EVENT_FIELD_NAMES = {item.name for item in fields(ProductLogEvent)} - {"extra"}


class RotatingGzipWriter:
    """Synchronous line writer with size-based rotation and optional gzip."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_bytes: int = 5 * 1024 * 1024,
        gzip_rotated: bool = True,
    ) -> None:
        self.path = Path(path)
        self.max_bytes = max(1, int(max_bytes))
        self.gzip_rotated = bool(gzip_rotated)
        self._lock = threading.RLock()
        self._handle = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._open()

    def _open(self) -> None:
        self._handle = self.path.open("a", encoding="utf-8")

    def _rotation_target(self) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        candidate = self.path.with_name(f"{self.path.stem}-{stamp}{self.path.suffix}")
        index = 1
        while candidate.exists() or candidate.with_suffix(candidate.suffix + ".gz").exists():
            candidate = self.path.with_name(
                f"{self.path.stem}-{stamp}-{index}{self.path.suffix}"
            )
            index += 1
        return candidate

    def _rotate(self) -> None:
        if self._handle is not None:
            self._handle.flush()
            self._handle.close()
            self._handle = None
        if not self.path.exists() or self.path.stat().st_size <= 0:
            self._open()
            return

        rotated_path = self._rotation_target()
        os.replace(self.path, rotated_path)
        if self.gzip_rotated:
            gz_path = rotated_path.with_suffix(rotated_path.suffix + ".gz")
            with rotated_path.open("rb") as source, gzip.open(gz_path, "wb") as target:
                target.write(source.read())
            rotated_path.unlink(missing_ok=True)
        self._open()

    def write_line(self, line: str) -> None:
        payload = line if line.endswith("\n") else f"{line}\n"
        encoded = payload.encode("utf-8")
        with self._lock:
            if self._handle is None:
                self._open()
            current_size = self._handle.tell() if self._handle is not None else 0
            if current_size > 0 and current_size + len(encoded) > self.max_bytes:
                self._rotate()
            self._handle.write(payload)

    def flush(self) -> None:
        with self._lock:
            if self._handle is not None:
                self._handle.flush()

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                self._handle.flush()
                self._handle.close()
                self._handle = None


class JsonlWriter:
    """Queue-backed JSONL writer for one component instance."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_queue_size: int = 1024,
        rotate_bytes: int = 5 * 1024 * 1024,
        gzip_rotated: bool = True,
    ) -> None:
        self.path = Path(path)
        self._sink = RotatingGzipWriter(
            self.path,
            max_bytes=rotate_bytes,
            gzip_rotated=gzip_rotated,
        )
        self._queue: queue.Queue[str | object] = queue.Queue(maxsize=max(1, int(max_queue_size)))
        self._closed = False
        self._worker = threading.Thread(
            target=self._run,
            name=f"jsonl-writer-{self.path.stem}",
            daemon=True,
        )
        self._worker.start()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _SENTINEL:
                    return
                self._sink.write_line(str(item))
            finally:
                self._queue.task_done()

    def write(self, record: ProductLogEvent | Mapping[str, Any]) -> None:
        if self._closed:
            raise RuntimeError(f"Writer already closed for {self.path}")
        payload = record.to_dict() if isinstance(record, ProductLogEvent) else dict(record)
        line = json.dumps(payload, **_JSON_DUMP_KWARGS)
        self._queue.put_nowait(line)

    def flush(self) -> None:
        self._queue.join()
        self._sink.flush()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._queue.put(_SENTINEL)
        self._queue.join()
        self._worker.join(timeout=2.0)
        self._sink.close()


class NullJsonlWriter:
    """Fallback writer used when the configured product-log root is unavailable."""

    path = Path("<disabled>")

    def write(self, record: ProductLogEvent | Mapping[str, Any]) -> None:
        return None

    def flush(self) -> None:
        return None

    def close(self) -> None:
        return None


def _resolve_writer_root(root: str | Path | None = None) -> Path:
    return Path(root) if root is not None else _get_cloud_raw_dir()


def get_product_log_writer(
    component: str,
    *,
    root: str | Path | None = None,
) -> JsonlWriter | NullJsonlWriter:
    raw_root = _resolve_writer_root(root)
    component_instance_id = build_component_instance_id(component)
    writer_key = (component, str(raw_root))
    with _WRITER_LOCK:
        writer = _WRITER_REGISTRY.get(writer_key)
        if writer is None:
            try:
                raw_root.mkdir(parents=True, exist_ok=True)
                file_name = (
                    f"{_sanitize_component_for_filename(component)}-"
                    f"{_sanitize_component_for_filename(component_instance_id)}.jsonl"
                )
                writer = JsonlWriter(raw_root / file_name)
            except OSError:
                writer = NullJsonlWriter()
            _WRITER_REGISTRY[writer_key] = writer
        return writer


def flush_product_log_writers() -> None:
    with _WRITER_LOCK:
        writers = list(_WRITER_REGISTRY.values())
    for writer in writers:
        writer.flush()


def close_product_log_writers() -> None:
    with _WRITER_LOCK:
        writers = list(_WRITER_REGISTRY.values())
        _WRITER_REGISTRY.clear()
    for writer in writers:
        writer.close()


atexit.register(close_product_log_writers)


def _build_product_log_event(
    *,
    component: str,
    event_type: str,
    context: LogContext | None = None,
    **fields_override: Any,
) -> ProductLogEvent:
    base_fields: dict[str, Any] = {}
    extra_fields: dict[str, Any] = {}
    if context is not None:
        for item in fields(LogContext):
            key = item.name
            value = getattr(context, key)
            if key in _EVENT_FIELD_NAMES:
                base_fields[key] = value
            elif value is not None:
                extra_fields[key] = value
    for key, value in fields_override.items():
        if key in _EVENT_FIELD_NAMES:
            base_fields[key] = value
        else:
            extra_fields[key] = value
    return ProductLogEvent(
        component=component,
        event_type=event_type,
        extra=extra_fields,
        **base_fields,
    )


def emit_event(
    writer: JsonlWriter | None,
    *,
    component: str,
    event_type: str,
    context: LogContext | None = None,
    **fields_override: Any,
) -> dict[str, Any]:
    event = _build_product_log_event(
        component=component,
        event_type=event_type,
        context=context,
        **fields_override,
    )
    payload = event.to_dict()
    if writer is not None:
        writer.write(payload)
        writer_path = getattr(writer, "path", None)
        if writer_path is not None:
            try:
                from diagnostic_platform.observability_artifacts import maybe_materialize_cloud_artifacts

                maybe_materialize_cloud_artifacts(
                    payload,
                    writer_path=writer_path,
                    flush_callback=getattr(writer, "flush", None),
                )
            except Exception:
                pass
    return payload


def emit_span_start(
    writer: JsonlWriter | None,
    *,
    component: str,
    event_type: str,
    context: LogContext | None = None,
    **fields_override: Any,
) -> dict[str, Any]:
    return emit_event(
        writer,
        component=component,
        event_type=event_type,
        context=context,
        status=fields_override.pop("status", "started"),
        **fields_override,
    )


def emit_span_finish(
    writer: JsonlWriter | None,
    *,
    component: str,
    event_type: str,
    context: LogContext | None = None,
    **fields_override: Any,
) -> dict[str, Any]:
    return emit_event(
        writer,
        component=component,
        event_type=event_type,
        context=context,
        status=fields_override.pop("status", "finished"),
        **fields_override,
    )


def normalize_active_session_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(snapshot)
    normalized = {
        "session_id": str(data.get("session_id") or ""),
        "backend_name": str(data.get("backend_name") or ""),
        "operation_kind": str(data.get("operation_kind") or ""),
        "selected_module": str(data.get("selected_module") or ""),
        "selected_data_category": str(data.get("selected_data_category") or ""),
        "current_page": str(data.get("current_page") or ""),
        "navigation_session_id": data.get("navigation_session_id"),
        "ai_session_id": data.get("ai_session_id"),
        "live_data_active": bool(data.get("live_data_active", False)),
        "connection_epoch": data.get("connection_epoch"),
        "updated_at": str(data.get("updated_at") or utc_now_iso()),
    }
    return normalized


def read_active_session_snapshot(path: str | Path | None = None) -> dict[str, Any] | None:
    snapshot_path = Path(path) if path is not None else get_active_session_snapshot_path()
    try:
        raw = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    return normalize_active_session_snapshot(raw)


class ActiveSessionSnapshotStore:
    """Atomic store for the currently active business-session snapshot."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else get_active_session_snapshot_path()

    def write(self, snapshot: Mapping[str, Any]) -> Path:
        normalized = normalize_active_session_snapshot(
            {
                **dict(snapshot),
                "updated_at": utc_now_iso(),
            }
        )
        return _atomic_write_text(
            self.path,
            json.dumps(normalized, ensure_ascii=False, indent=2),
        )

    def read(self) -> dict[str, Any] | None:
        return read_active_session_snapshot(self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


__all__ = [
    "ActiveSessionSnapshotStore",
    "JsonlWriter",
    "LogContext",
    "NullJsonlWriter",
    "OBSERVABILITY_SCHEMA_VERSION",
    "ProductLogEvent",
    "RotatingGzipWriter",
    "build_component_instance_id",
    "close_product_log_writers",
    "emit_event",
    "emit_span_finish",
    "emit_span_start",
    "flush_product_log_writers",
    "generate_request_id",
    "get_active_session_snapshot_path",
    "get_cloud_observability_root",
    "get_local_observability_root",
    "get_product_log_writer",
    "normalize_active_session_snapshot",
    "read_active_session_snapshot",
    "redact_payload",
    "utc_now_iso",
]
