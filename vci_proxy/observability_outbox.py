"""Local observability outbox and upload helpers."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import time
import urllib.request
from pathlib import Path
from typing import Any

from diagnostic_platform.observability import get_local_observability_root


def get_local_outbox_root(appdata: str | Path | None = None) -> Path:
    return get_local_observability_root(appdata) / "outbox"


def _safe_name(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in str(text or "").strip()) or "artifact"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, path)
    return path


def _read_jsonl_first_context(path: Path) -> tuple[str | None, str | None]:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None, None
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


class ObservabilityOutbox:
    def __init__(self, *, appdata: str | Path | None = None) -> None:
        self.root = get_local_outbox_root(appdata)
        self.pending_dir = self.root / "pending"
        self.uploaded_dir = self.root / "uploaded"
        self.artifacts_dir = self.root / "artifacts"

    def _manifest_name(self, *, client_instance_id: str, connection_epoch: str, artifact_id: str) -> str:
        return f"{_safe_name(client_instance_id)}-{_safe_name(connection_epoch)}-{_safe_name(artifact_id)}.json"

    def queue_artifact(
        self,
        artifact_path: str | Path,
        *,
        client_instance_id: str,
        connection_epoch: str,
        artifact_id: str,
        artifact_type: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        source_path = Path(artifact_path)
        manifest_name = self._manifest_name(
            client_instance_id=client_instance_id,
            connection_epoch=connection_epoch,
            artifact_id=artifact_id,
        )
        pending_manifest = self.pending_dir / manifest_name
        uploaded_manifest = self.uploaded_dir / manifest_name
        if pending_manifest.exists() or uploaded_manifest.exists():
            return {"queued": False, "manifest_path": pending_manifest if pending_manifest.exists() else uploaded_manifest}

        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        copied_path = self.artifacts_dir / f"{_safe_name(artifact_id)}-{source_path.name}"
        shutil.copy2(source_path, copied_path)
        manifest = {
            "client_instance_id": client_instance_id,
            "connection_epoch": connection_epoch,
            "artifact_id": artifact_id,
            "artifact_name": source_path.name,
            "artifact_type": artifact_type,
            "session_id": session_id,
            "artifact_path": str(copied_path),
        }
        _atomic_write_json(pending_manifest, manifest)
        return {"queued": True, "manifest_path": pending_manifest}

    def stage_default_artifacts(
        self,
        *,
        client_instance_id: str,
        local_root: str | Path | None = None,
        min_age_seconds: float = 5.0,
    ) -> dict[str, Any]:
        root = Path(local_root) if local_root is not None else get_local_observability_root()
        queued_count = 0
        for category, directory in (
            ("raw", root / "raw"),
            ("session_trace", root / "session_traces"),
            ("incident_bundle", root / "incidents"),
        ):
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*")):
                if not path.is_file():
                    continue
                age = time.time() - path.stat().st_mtime
                if age < min_age_seconds:
                    continue
                session_id, connection_epoch = _read_jsonl_first_context(path)
                resolved_epoch = connection_epoch or "no-epoch"
                artifact_id = hashlib.sha1(
                    f"{path.resolve()}|{path.stat().st_size}|{path.stat().st_mtime}".encode("utf-8")
                ).hexdigest()[:16]
                result = self.queue_artifact(
                    path,
                    client_instance_id=client_instance_id,
                    connection_epoch=resolved_epoch,
                    artifact_id=artifact_id,
                    artifact_type=category,
                    session_id=session_id,
                )
                if result["queued"]:
                    queued_count += 1
        return {"queued_count": queued_count}

    def list_pending(self) -> list[dict[str, Any]]:
        manifests: list[dict[str, Any]] = []
        for path in sorted(self.pending_dir.glob("*.json")):
            manifests.append(json.loads(path.read_text(encoding="utf-8")))
        return manifests

    def upload_pending(
        self,
        *,
        api_base_url: str,
        api_token: str = "",
        max_artifact_mb: int = 50,
        opener: Any | None = None,
    ) -> dict[str, Any]:
        uploaded_count = 0
        open_request = opener or urllib.request.urlopen
        for manifest_path in sorted(self.pending_dir.glob("*.json")):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            artifact_path = Path(manifest["artifact_path"])
            if not artifact_path.exists():
                continue
            artifact_bytes = artifact_path.read_bytes()
            if len(artifact_bytes) > max(1, int(max_artifact_mb)) * 1024 * 1024:
                continue
            body = {
                "client_instance_id": manifest["client_instance_id"],
                "connection_epoch": manifest["connection_epoch"],
                "artifact_id": manifest["artifact_id"],
                "artifact_name": manifest["artifact_name"],
                "artifact_type": manifest["artifact_type"],
                "session_id": manifest.get("session_id"),
                "content_base64": base64.b64encode(artifact_bytes).decode("ascii"),
            }
            request = urllib.request.Request(
                api_base_url.rstrip("/") + "/api/session/logs/upload",
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    **({"X-API-Token": api_token} if api_token else {}),
                },
                method="POST",
            )
            response = open_request(request)
            status = int(getattr(response, "status", 200) or 200)
            if status >= 400:
                continue
            self.uploaded_dir.mkdir(parents=True, exist_ok=True)
            try:
                artifact_path.unlink(missing_ok=True)
            except OSError:
                pass
            shutil.move(str(manifest_path), str(self.uploaded_dir / manifest_path.name))
            uploaded_count += 1
        return {"uploaded_count": uploaded_count}


__all__ = [
    "ObservabilityOutbox",
    "get_local_outbox_root",
]
