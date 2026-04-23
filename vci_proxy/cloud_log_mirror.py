"""Local mirror helpers for cloud-side log synchronization."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from diagnostic_platform.observability import get_local_observability_root


def get_cloud_mirror_root(appdata: str | Path | None = None) -> Path:
    return get_local_observability_root(appdata) / "cloud_mirror"


def build_cloud_mirror_source_id(api_base_url: str) -> str:
    parsed = urlparse(str(api_base_url or "").strip())
    host = (parsed.hostname or "unknown-source").strip().lower()
    safe_host = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in host) or "unknown-source"
    digest = hashlib.sha1(str(api_base_url or "").rstrip("/").encode("utf-8")).hexdigest()[:12]
    return f"{safe_host}-{digest}"


def get_cloud_mirror_source_root(
    api_base_url: str,
    appdata: str | Path | None = None,
) -> Path:
    return get_cloud_mirror_root(appdata) / "sources" / build_cloud_mirror_source_id(api_base_url)


def get_cloud_mirror_files_root(
    api_base_url: str,
    appdata: str | Path | None = None,
) -> Path:
    return get_cloud_mirror_source_root(api_base_url, appdata) / "files"


def get_cloud_mirror_state_path(
    api_base_url: str,
    appdata: str | Path | None = None,
) -> Path:
    return get_cloud_mirror_source_root(api_base_url, appdata) / "state.json"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, path)
    return path


def _atomic_write_bytes(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp_path.write_bytes(payload)
    os.replace(temp_path, path)
    return path


def _safe_relative_target(root: Path, relative_path: str) -> Path:
    parts = [part for part in str(relative_path or "").replace("\\", "/").split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise ValueError("relative_path must be a safe relative path")
    return root.joinpath(*parts)


def read_cloud_mirror_state(
    api_base_url: str,
    appdata: str | Path | None = None,
) -> dict[str, Any]:
    path = get_cloud_mirror_state_path(api_base_url, appdata)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {
            "cursor_mtime_ns": 0,
            "cursor_path": "",
        }
    if not isinstance(raw, dict):
        return {
            "cursor_mtime_ns": 0,
            "cursor_path": "",
        }
    return {
        "cursor_mtime_ns": int(raw.get("cursor_mtime_ns") or 0),
        "cursor_path": str(raw.get("cursor_path") or ""),
        "last_synced_at": raw.get("last_synced_at"),
    }


def apply_cloud_log_sync(
    payload: dict[str, Any],
    *,
    api_base_url: str,
    appdata: str | Path | None = None,
) -> dict[str, Any]:
    root = get_cloud_mirror_files_root(api_base_url, appdata)
    mirrored_count = 0
    skipped_count = 0

    for item in payload.get("files") or []:
        relative_path = str(item.get("relative_path") or "")
        content_base64 = str(item.get("content_base64") or "")
        expected_sha256 = str(item.get("sha256") or "")
        if not relative_path or not content_base64:
            skipped_count += 1
            continue

        target_path = _safe_relative_target(root, relative_path)
        content = base64.b64decode(content_base64.encode("ascii"))
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if expected_sha256 and actual_sha256 != expected_sha256:
            raise ValueError(f"sha256 mismatch for {relative_path}")

        if target_path.exists():
            try:
                current = target_path.read_bytes()
            except OSError:
                current = None
            if current == content:
                skipped_count += 1
                continue

        _atomic_write_bytes(target_path, content)
        mirrored_count += 1

    state = {
        "cursor_mtime_ns": int(payload.get("next_cursor_mtime_ns") or 0),
        "cursor_path": str(payload.get("next_cursor_path") or ""),
        "last_synced_at": payload.get("synced_at"),
        "source_id": build_cloud_mirror_source_id(api_base_url),
        "api_base_url": api_base_url.rstrip("/"),
    }
    _atomic_write_json(get_cloud_mirror_state_path(api_base_url, appdata), state)
    return {
        "mirrored_count": mirrored_count,
        "skipped_count": skipped_count,
        "cursor_mtime_ns": state["cursor_mtime_ns"],
        "cursor_path": state["cursor_path"],
        "has_more": bool(payload.get("has_more")),
    }


def sync_cloud_logs(
    *,
    api_base_url: str,
    api_token: str = "",
    appdata: str | Path | None = None,
    opener: Any | None = None,
    max_files: int = 20,
    max_batches: int = 5,
    max_batch_bytes: int = 5 * 1024 * 1024,
) -> dict[str, int]:
    open_request = opener or urllib.request.urlopen
    normalized_api_base_url = api_base_url.rstrip("/")
    state = read_cloud_mirror_state(normalized_api_base_url, appdata)
    mirrored_total = 0
    skipped_total = 0

    for _ in range(max(1, int(max_batches))):
        body = {
            "cursor_mtime_ns": int(state.get("cursor_mtime_ns") or 0),
            "cursor_path": str(state.get("cursor_path") or ""),
            "max_files": int(max_files),
            "max_batch_bytes": int(max_batch_bytes),
        }
        request = urllib.request.Request(
            normalized_api_base_url + "/api/session/logs/sync",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                **({"X-API-Token": api_token} if api_token else {}),
            },
            method="POST",
        )
        response = open_request(request)
        status = int(getattr(response, "status", 200) or 200)
        payload = json.loads(response.read().decode("utf-8") or "{}")
        if status >= 400 or not bool(payload.get("success", False)):
            raise RuntimeError(str(payload.get("error") or f"cloud log sync failed with status {status}"))

        payload["synced_at"] = payload.get("synced_at") or state.get("last_synced_at")
        applied = apply_cloud_log_sync(payload, api_base_url=normalized_api_base_url, appdata=appdata)
        mirrored_total += int(applied.get("mirrored_count") or 0)
        skipped_total += int(applied.get("skipped_count") or 0)
        state = read_cloud_mirror_state(normalized_api_base_url, appdata)

        if not bool(applied.get("has_more")):
            break

    return {
        "mirrored_count": mirrored_total,
        "skipped_count": skipped_total,
    }


__all__ = [
    "apply_cloud_log_sync",
    "build_cloud_mirror_source_id",
    "get_cloud_mirror_root",
    "get_cloud_mirror_source_root",
    "get_cloud_mirror_files_root",
    "get_cloud_mirror_state_path",
    "read_cloud_mirror_state",
    "sync_cloud_logs",
]
