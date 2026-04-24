"""Observability artifact upload handlers for session-scoped APIs."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from diagnostic_platform.observability import get_cloud_observability_root
from diagnostic_platform.observability_artifacts import (
    ingest_uploaded_artifact,
    resolve_product_log_settings,
)
from server.api.http_utils import internal_error_payload

logger = logging.getLogger(__name__)


def upload_session_logs(data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    try:
        settings = resolve_product_log_settings()
        result = ingest_uploaded_artifact(
            data,
            cloud_root=get_cloud_observability_root(),
            max_artifact_mb=settings.max_artifact_mb,
        )
        return {
            "success": True,
            "deduped": bool(result["deduped"]),
            "artifact_path": str(Path(result["artifact_path"])),
            "trace_path": str(result["trace_path"]) if result.get("trace_path") is not None else None,
            "incident_paths": [str(path) for path in result.get("incident_paths", [])],
        }, 201 if not result["deduped"] else 200
    except ValueError as exc:
        return {"success": False, "error": str(exc)}, 400
    except Exception:
        logger.exception("session_logs_upload failed")
        return internal_error_payload(), 500


__all__ = ["upload_session_logs"]
