"""Shared cloud latest cache for proxy-local live-data samples."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from diagnostic_platform.observability import get_cloud_observability_root, utc_now_iso


CLOUD_LATEST_SCHEMA_VERSION = "proxy.local_live_data.cloud_latest.v1"
SESSION_STATE_SCHEMA_VERSION = "proxy.local_live_data.session_state.v1"
LOCAL_SAMPLE_SCHEMA_VERSION = "proxy.local_live_data.sample.v1"
PRODUCT_SOURCE = "proxy_local_live_data"
ENGINE_SPEED_SIGNAL_KEY = "engine_speed"
DEFAULT_MAX_AGE_MS = 5000
_WINDOWS_DEFAULT_CLOUD_ROOT = Path("D:/RPA_Diagnostic/observability/cloud")
_ATOMIC_REPLACE_MAX_ATTEMPTS = 3
_ATOMIC_REPLACE_RETRY_DELAY_S = 0.05


@dataclass(frozen=True)
class ProxyLocalLatestValidation:
    ok: bool
    reason: str
    status: int
    payload: dict[str, Any]


def get_proxy_local_live_data_latest_path(
    programdata: str | Path | None = None,
) -> Path:
    return (
        get_cloud_observability_root(programdata)
        / "live_data"
        / "proxy_local_latest.json"
    )


def get_proxy_local_live_data_latest_candidate_paths() -> list[Path]:
    return _candidate_live_data_paths("proxy_local_latest.json")


def get_proxy_local_live_data_session_state_path(
    programdata: str | Path | None = None,
) -> Path:
    return (
        get_cloud_observability_root(programdata)
        / "live_data"
        / "proxy_local_session_state.json"
    )


def get_proxy_local_live_data_session_state_candidate_paths() -> list[Path]:
    return _candidate_live_data_paths("proxy_local_session_state.json")


def write_proxy_local_live_data_latest(
    *,
    sample: Mapping[str, Any],
    connection_epoch: str | None,
    session_snapshot: Mapping[str, Any] | None = None,
    path: str | Path | None = None,
    received_at_s: float | None = None,
) -> dict[str, Any]:
    received_at_s = time.time() if received_at_s is None else float(received_at_s)
    snapshot = dict(session_snapshot or {})
    latest_sample = _sanitize_latest_sample(sample)
    payload = {
        "schema_version": CLOUD_LATEST_SCHEMA_VERSION,
        "updated_at": utc_now_iso(received_at_s),
        "cloud_received_ts": utc_now_iso(received_at_s),
        "connection_epoch": connection_epoch,
        "session_id": str(snapshot.get("session_id") or "") or None,
        "live_data_active_at_receive": bool(snapshot.get("live_data_active", False)),
        "source": PRODUCT_SOURCE,
        "latest_sample": latest_sample,
        "signals": {ENGINE_SPEED_SIGNAL_KEY: latest_sample},
    }
    _atomic_write_json(
        Path(path) if path is not None else get_proxy_local_live_data_latest_path(),
        payload,
    )
    return payload


def write_proxy_local_live_data_session_state(
    *,
    session_id: str,
    connection_epoch: str | None,
    live_data_active: bool,
    source_event_type: str,
    operation_kind: str | None = None,
    current_page: str | None = None,
    selected_module: str | None = None,
    selected_data_category: str | None = None,
    path: str | Path | None = None,
    updated_at_s: float | None = None,
) -> dict[str, Any]:
    updated_at_s = time.time() if updated_at_s is None else float(updated_at_s)
    session_id = str(session_id or "").strip()
    if not session_id:
        raise ValueError("session_id required")
    payload = {
        "schema_version": SESSION_STATE_SCHEMA_VERSION,
        "updated_at": utc_now_iso(updated_at_s),
        "session_id": session_id,
        "connection_epoch": _clean_optional_text(connection_epoch),
        "live_data_active": bool(live_data_active),
        "source_event_type": str(source_event_type or "").strip(),
        "operation_kind": _clean_optional_text(operation_kind),
        "current_page": _clean_optional_text(current_page),
        "selected_module": _clean_optional_text(selected_module),
        "selected_data_category": _clean_optional_text(selected_data_category),
    }
    _atomic_write_json(
        Path(path)
        if path is not None
        else get_proxy_local_live_data_session_state_path(),
        payload,
    )
    return payload


def read_proxy_local_live_data_latest(
    path: str | Path | None = None,
) -> dict[str, Any] | None:
    latest_paths = (
        [Path(path)]
        if path is not None
        else get_proxy_local_live_data_latest_candidate_paths()
    )
    for latest_path in latest_paths:
        try:
            raw = json.loads(latest_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
        if not isinstance(raw, dict):
            continue
        try:
            return normalize_proxy_local_live_data_latest(raw)
        except ValueError:
            continue
    return None


def read_proxy_local_live_data_session_state(
    path: str | Path | None = None,
) -> dict[str, Any] | None:
    state_paths = (
        [Path(path)]
        if path is not None
        else get_proxy_local_live_data_session_state_candidate_paths()
    )
    for state_path in state_paths:
        try:
            raw = json.loads(state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
        if not isinstance(raw, dict):
            continue
        try:
            return normalize_proxy_local_live_data_session_state(raw)
        except ValueError:
            continue
    return None


def normalize_proxy_local_live_data_latest(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    data = dict(payload)
    if str(data.get("schema_version") or "") != CLOUD_LATEST_SCHEMA_VERSION:
        raise ValueError("unsupported proxy-local latest cache schema")
    sample = data.get("latest_sample")
    sample = _sanitize_latest_sample(sample if isinstance(sample, Mapping) else {})
    return {
        "schema_version": str(
            data.get("schema_version") or CLOUD_LATEST_SCHEMA_VERSION
        ),
        "updated_at": str(data.get("updated_at") or ""),
        "cloud_received_ts": str(data.get("cloud_received_ts") or ""),
        "connection_epoch": data.get("connection_epoch"),
        "session_id": str(data.get("session_id") or "") or None,
        "live_data_active_at_receive": bool(
            data.get("live_data_active_at_receive", False)
        ),
        "source": str(data.get("source") or PRODUCT_SOURCE),
        "latest_sample": sample,
        "signals": {ENGINE_SPEED_SIGNAL_KEY: sample},
    }


def normalize_proxy_local_live_data_session_state(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    data = dict(payload)
    if str(data.get("schema_version") or "") != SESSION_STATE_SCHEMA_VERSION:
        raise ValueError("unsupported proxy-local session state schema")
    return {
        "schema_version": SESSION_STATE_SCHEMA_VERSION,
        "updated_at": str(data.get("updated_at") or ""),
        "session_id": _clean_optional_text(data.get("session_id")),
        "connection_epoch": _clean_optional_text(data.get("connection_epoch")),
        "live_data_active": bool(data.get("live_data_active", False)),
        "source_event_type": str(data.get("source_event_type") or ""),
        "operation_kind": _clean_optional_text(data.get("operation_kind")),
        "current_page": _clean_optional_text(data.get("current_page")),
        "selected_module": _clean_optional_text(data.get("selected_module")),
        "selected_data_category": _clean_optional_text(
            data.get("selected_data_category")
        ),
    }


def resolve_proxy_local_live_data_session_snapshot(
    *,
    active_snapshot: Mapping[str, Any] | None,
    connection_epoch: str | None,
    session_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    snapshot = dict(active_snapshot or {})
    if bool(snapshot.get("live_data_active", False)) and _epoch_matches(
        snapshot.get("connection_epoch"),
        connection_epoch,
    ):
        return snapshot

    state = (
        dict(session_state)
        if session_state is not None
        else read_proxy_local_live_data_session_state()
    )
    if not state:
        return snapshot
    try:
        normalized = normalize_proxy_local_live_data_session_state(state)
    except ValueError:
        return snapshot
    if not normalized.get("live_data_active"):
        return snapshot
    if not normalized.get("session_id"):
        return snapshot
    if not _epoch_matches(normalized.get("connection_epoch"), connection_epoch):
        return snapshot

    resolved = dict(snapshot)
    resolved["session_id"] = normalized["session_id"]
    resolved["connection_epoch"] = normalized.get("connection_epoch") or connection_epoch
    resolved["live_data_active"] = True
    for source_key, snapshot_key in (
        ("operation_kind", "operation_kind"),
        ("current_page", "current_page"),
        ("selected_module", "selected_module"),
        ("selected_data_category", "selected_data_category"),
    ):
        if not resolved.get(snapshot_key) and normalized.get(source_key):
            resolved[snapshot_key] = normalized[source_key]
    return resolved


def validate_proxy_local_latest_for_session(
    latest: Mapping[str, Any] | None,
    *,
    session_id: str,
    active_connection_epoch: str | None,
    live_data_active: bool,
    max_age_ms: int = DEFAULT_MAX_AGE_MS,
    now_s: float | None = None,
) -> ProxyLocalLatestValidation:
    if not live_data_active:
        return _validation(False, "live_data_inactive", 409)
    if latest is None:
        return _validation(
            False,
            "no_sample_cache",
            404,
            checked_paths=[
                str(path) for path in get_proxy_local_live_data_latest_candidate_paths()
            ],
        )

    try:
        normalized = normalize_proxy_local_live_data_latest(latest)
    except ValueError:
        return _validation(False, "invalid_sample_cache", 409)
    latest_session_id = normalized.get("session_id")
    if latest_session_id != session_id:
        return _validation(
            False,
            "session_mismatch",
            409,
            cache_session_id=latest_session_id,
            requested_session_id=session_id,
        )

    cache_epoch = normalized.get("connection_epoch")
    epoch_match_status = "matched"
    if active_connection_epoch:
        if cache_epoch != active_connection_epoch:
            return _validation(
                False,
                "epoch_mismatch",
                409,
                connection_epoch=cache_epoch,
                active_connection_epoch=active_connection_epoch,
            )
    else:
        epoch_match_status = "active_epoch_missing_legacy"

    received_s = _parse_iso_ts(str(normalized.get("cloud_received_ts") or ""))
    if received_s is None:
        return _validation(False, "invalid_cloud_received_ts", 409)
    now_s = time.time() if now_s is None else float(now_s)
    age_ms = max(0.0, (now_s - received_s) * 1000.0)
    if age_ms > max(0, int(max_age_ms)):
        return _validation(
            False,
            "stale_sample",
            409,
            cloud_received_age_ms=round(age_ms, 3),
            max_age_ms=max(0, int(max_age_ms)),
        )

    return ProxyLocalLatestValidation(
        ok=True,
        reason="sample_available",
        status=200,
        payload={
            "success": True,
            "available": True,
            "source": PRODUCT_SOURCE,
            "latest_sample": normalized["latest_sample"],
            "cloud_received_age_ms": round(age_ms, 3),
            "connection_epoch": cache_epoch,
            "epoch_match_status": epoch_match_status,
        },
    )


def _validation(
    ok: bool,
    reason: str,
    status: int,
    **extra: Any,
) -> ProxyLocalLatestValidation:
    return ProxyLocalLatestValidation(
        ok=ok,
        reason=reason,
        status=status,
        payload={
            "success": False,
            "available": False,
            "source": PRODUCT_SOURCE,
            "reason": reason,
            **extra,
        },
    )


def _candidate_live_data_paths(file_name: str) -> list[Path]:
    candidates = [get_cloud_observability_root()]
    programdata_root = get_cloud_observability_root(
        os.environ.get("PROGRAMDATA", "C:/ProgramData")
    )
    candidates.append(programdata_root)
    if os.name == "nt":
        candidates.append(_WINDOWS_DEFAULT_CLOUD_ROOT)

    paths: list[Path] = []
    seen: set[str] = set()
    for root in candidates:
        path = Path(root) / "live_data" / file_name
        key = os.path.normcase(os.path.abspath(os.fspath(path)))
        if key in seen:
            continue
        seen.add(key)
        paths.append(path)
    return paths


def _sanitize_latest_sample(sample: Mapping[str, Any]) -> dict[str, Any]:
    if str(sample.get("schema_version") or "") != LOCAL_SAMPLE_SCHEMA_VERSION:
        raise ValueError("unsupported proxy-local sample schema")
    if str(sample.get("signal_key") or "") != ENGINE_SPEED_SIGNAL_KEY:
        raise ValueError("unsupported proxy-local signal")

    allowed = {
        "schema_version",
        "signal_key",
        "display_name",
        "unit",
        "value",
        "source",
        "decoder_id",
        "sample_ts",
        "local_send_ts",
        "local_reported_sample_age_ms",
        "poll_id",
        "channel_id",
        "request_kind",
        "request_origin",
        "return_code",
        "j2534_return_code_warning",
        "raw_prefix_hex",
        "collector_interval_ms",
        "read_timeout_ms",
        "client_sample_seq",
    }
    sanitized = {key: sample.get(key) for key in allowed if key in sample}
    sanitized["signal_key"] = ENGINE_SPEED_SIGNAL_KEY
    sanitized["schema_version"] = LOCAL_SAMPLE_SCHEMA_VERSION
    return sanitized


def _clean_optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _epoch_matches(candidate: Any, expected: str | None) -> bool:
    expected_text = _clean_optional_text(expected)
    if expected_text is None:
        return True
    return _clean_optional_text(candidate) == expected_text


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(
            json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        for attempt in range(_ATOMIC_REPLACE_MAX_ATTEMPTS):
            try:
                os.replace(temp_path, path)
                break
            except OSError as exc:
                if (
                    attempt >= _ATOMIC_REPLACE_MAX_ATTEMPTS - 1
                    or not _should_retry_atomic_replace(exc)
                ):
                    raise
                time.sleep(_ATOMIC_REPLACE_RETRY_DELAY_S * (attempt + 1))
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
    return path


def _should_retry_atomic_replace(exc: OSError) -> bool:
    if isinstance(exc, PermissionError):
        return True
    return getattr(exc, "winerror", None) in {5, 32}


def _parse_iso_ts(value: str) -> float | None:
    text = value.strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).timestamp()
    except (TypeError, ValueError):
        return None
