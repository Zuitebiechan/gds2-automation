from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from vci_proxy.cloud_log_mirror import (
    apply_cloud_log_sync,
    build_cloud_mirror_source_id,
    get_cloud_mirror_files_root,
    get_cloud_mirror_root,
    read_cloud_mirror_state,
    sync_cloud_logs,
)


def test_apply_cloud_log_sync_writes_files_and_state(tmp_path: Path) -> None:
    appdata = tmp_path / "AppData"
    api_base_url = "https://node-a.example.com:443"
    content = b'{"cloud":"log"}\n'
    payload = {
        "success": True,
        "files": [
            {
                "relative_path": "observability/cloud/raw/server.jsonl",
                "mtime_ns": 123,
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "content_base64": base64.b64encode(content).decode("ascii"),
            }
        ],
        "next_cursor_mtime_ns": 123,
        "next_cursor_path": "observability/cloud/raw/server.jsonl",
        "has_more": False,
        "synced_at": "2026-04-22T12:00:00Z",
    }

    result = apply_cloud_log_sync(payload, api_base_url=api_base_url, appdata=appdata)

    target = get_cloud_mirror_files_root(api_base_url, appdata) / "observability" / "cloud" / "raw" / "server.jsonl"
    assert result["mirrored_count"] == 1
    assert target.read_bytes() == content
    assert read_cloud_mirror_state(api_base_url, appdata)["cursor_mtime_ns"] == 123


def test_sync_cloud_logs_posts_cursor_and_applies_response(tmp_path: Path) -> None:
    appdata = tmp_path / "AppData"
    observed: dict[str, object] = {}
    api_base_url = "https://diag.example:8080"
    content = b"compat-log\n"

    class _FakeResponse:
        def __init__(self, payload: dict[str, object]) -> None:
            self.status = 200
            self._payload = json.dumps(payload).encode("utf-8")

        def read(self) -> bytes:
            return self._payload

    def _fake_opener(request):
        observed["url"] = request.full_url
        observed["headers"] = dict(request.header_items())
        observed["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(
            {
                "success": True,
                "files": [
                    {
                        "relative_path": "compat/logs/flask_api.log",
                        "mtime_ns": 456,
                        "size_bytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "content_base64": base64.b64encode(content).decode("ascii"),
                    }
                ],
                "skipped_files": [],
                "has_more": False,
                "next_cursor_mtime_ns": 456,
                "next_cursor_path": "compat/logs/flask_api.log",
                "synced_at": "2026-04-22T12:10:00Z",
            }
        )

    result = sync_cloud_logs(
        api_base_url=api_base_url,
        api_token="api-secret",
        appdata=appdata,
        opener=_fake_opener,
    )

    assert result == {"mirrored_count": 1, "skipped_count": 0}
    assert observed["url"].endswith("/api/session/logs/sync")
    assert observed["headers"]["X-api-token"] == "api-secret"
    assert observed["body"]["cursor_mtime_ns"] == 0
    assert observed["body"]["max_batch_bytes"] == 5 * 1024 * 1024
    target = get_cloud_mirror_files_root(api_base_url, appdata) / "compat" / "logs" / "flask_api.log"
    assert target.read_bytes() == content


def test_cloud_log_mirror_is_scoped_per_api_base_url(tmp_path: Path) -> None:
    appdata = tmp_path / "AppData"
    node_a = "https://node-a.example.com:443"
    node_b = "https://node-b.example.com:443"
    payload = {
        "success": True,
        "files": [],
        "next_cursor_mtime_ns": 100,
        "next_cursor_path": "a",
        "has_more": False,
        "synced_at": "2026-04-22T12:20:00Z",
    }

    apply_cloud_log_sync(payload, api_base_url=node_a, appdata=appdata)
    apply_cloud_log_sync(
        {
            **payload,
            "next_cursor_mtime_ns": 200,
            "next_cursor_path": "b",
        },
        api_base_url=node_b,
        appdata=appdata,
    )

    assert read_cloud_mirror_state(node_a, appdata)["cursor_mtime_ns"] == 100
    assert read_cloud_mirror_state(node_b, appdata)["cursor_mtime_ns"] == 200
    source_root = get_cloud_mirror_root(appdata) / "sources"
    assert (source_root / build_cloud_mirror_source_id(node_a)).exists()
    assert (source_root / build_cloud_mirror_source_id(node_b)).exists()
