from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

from diagnostic_platform.observability import JsonlWriter, emit_event
from diagnostic_platform.observability_artifacts import (
    cleanup_product_observability,
    export_cloud_log_artifacts,
    get_cloud_incidents_dir,
    get_cloud_session_traces_dir,
    ingest_uploaded_artifact,
    materialize_session_artifacts,
)
from vci_proxy.observability_outbox import ObservabilityOutbox


def _event(ts: str, component: str, event_type: str, **overrides):
    event = {
        "schema_version": "observability.v1",
        "ts": ts,
        "component": component,
        "component_instance_id": f"{component}:pid:startup",
        "event_type": event_type,
        "session_id": None,
        "connection_epoch": None,
        "dll_seq": None,
        "proxy_seq": None,
        "worker_request_id": None,
        "operation_kind": None,
        "status": "ok",
        "failure_code": None,
        "failure_domain": "unknown",
        "reason": None,
        "duration_ms": None,
        "hw_ms": None,
        "network_ms": None,
        "page": None,
        "module": None,
        "data_category": None,
        "symptom": None,
        "impact_scope": None,
        "next_checks": [],
        "redaction_applied": [],
    }
    event.update(overrides)
    return event


def test_materialize_session_artifacts_writes_trace_and_incident_bundle(tmp_path: Path) -> None:
    cloud_root = tmp_path / "ProgramData" / "RPA_Diagnostic" / "observability" / "cloud"
    raw_dir = cloud_root / "raw"
    raw_dir.mkdir(parents=True)
    events = [
        _event("2026-04-22T00:00:00Z", "session_runtime", "session.live_data.started", session_id="session-1", connection_epoch="epoch-1", page="data_display", module="Engine Control Module", data_category="Engine Data"),
        _event("2026-04-22T00:00:01Z", "reverse_server", "tunnel.probe.failure", session_id="session-1", connection_epoch="epoch-1", status="error", failure_code="probe_failure"),
        _event("2026-04-22T00:00:02Z", "reverse_server", "proxy.request.timeout", session_id="session-1", connection_epoch="epoch-1", status="error", failure_code="timeout", operation_kind="j2534:PassThruReadMsgs", impact_scope="live_data"),
        _event("2026-04-22T00:00:03Z", "session_runtime", "session.lifecycle.completed", session_id="session-1", connection_epoch="epoch-1"),
    ]
    (raw_dir / "cloud.jsonl").write_text("\n".join(json.dumps(item) for item in events), encoding="utf-8")

    result = materialize_session_artifacts(cloud_root=cloud_root, session_id="session-1")

    assert result["trace_path"].exists()
    assert result["incident_paths"]
    trace_payload = json.loads(result["trace_path"].read_text(encoding="utf-8"))
    bundle_payload = json.loads(result["incident_paths"][0].read_text(encoding="utf-8"))
    assert trace_payload["trace_id"] == "trace:session-1"
    assert bundle_payload["primary_failure_domain"] == "cloud_proxy_tunnel"


def test_emit_event_auto_materializes_cloud_trace_and_bundle(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "ProgramData"))
    writer = JsonlWriter(
        tmp_path / "ProgramData" / "RPA_Diagnostic" / "observability" / "cloud" / "raw" / "server.jsonl"
    )

    emit_event(
        writer,
        component="session_runtime",
        event_type="session.lifecycle.completed",
        session_id="session-2",
        connection_epoch="epoch-2",
    )
    emit_event(
        writer,
        component="reverse_server",
        event_type="proxy.request.timeout",
        session_id="session-2",
        connection_epoch="epoch-2",
        status="error",
        failure_code="timeout",
        operation_kind="j2534:PassThruReadMsgs",
    )
    writer.close()

    trace_files = list(get_cloud_session_traces_dir(tmp_path / "ProgramData").glob("*.json"))
    incident_files = list(get_cloud_incidents_dir(tmp_path / "ProgramData").glob("*.json"))
    assert trace_files
    assert incident_files


def test_ingest_uploaded_artifact_stores_file_dedupes_and_refreshes_trace(tmp_path: Path) -> None:
    cloud_root = tmp_path / "ProgramData" / "RPA_Diagnostic" / "observability" / "cloud"
    payload_bytes = "\n".join(
        [
            json.dumps(_event("2026-04-22T00:00:00Z", "reverse_client", "proxy.request.client_received", proxy_seq=20, connection_epoch="epoch-3")),
            json.dumps(_event("2026-04-22T00:00:01Z", "j2534_worker", "worker.rpc.failed", worker_request_id="wrk-20", proxy_seq=20, connection_epoch="epoch-3", status="error", failure_code="RuntimeError", failure_domain="local_j2534_driver")),
        ]
    ).encode("utf-8")
    payload = {
        "client_instance_id": "client-1",
        "connection_epoch": "epoch-3",
        "artifact_id": "artifact-1",
        "artifact_name": "local.jsonl",
        "artifact_type": "raw",
        "session_id": "session-3",
        "content_base64": base64.b64encode(payload_bytes).decode("ascii"),
    }

    first = ingest_uploaded_artifact(payload, cloud_root=cloud_root)
    second = ingest_uploaded_artifact(payload, cloud_root=cloud_root)

    assert first["deduped"] is False
    assert first["artifact_path"].exists()
    assert second["deduped"] is True
    assert list(get_cloud_session_traces_dir(cloud_root).glob("*.json"))


def test_observability_outbox_queues_and_uploads_pending_artifacts(tmp_path: Path) -> None:
    appdata = tmp_path / "AppData"
    local_root = appdata / "VCI_Proxy" / "observability"
    raw_dir = local_root / "raw"
    raw_dir.mkdir(parents=True)
    artifact = raw_dir / "local.jsonl"
    artifact.write_text(
        json.dumps(
            _event(
                "2026-04-22T00:00:00Z",
                "reverse_client",
                "proxy.request.client_received",
                session_id="session-4",
                connection_epoch="epoch-4",
                proxy_seq=40,
            )
        ),
        encoding="utf-8",
    )
    now = time.time() - 10
    os.utime(artifact, (now, now))

    outbox = ObservabilityOutbox(appdata=appdata)
    staged = outbox.stage_default_artifacts(
        client_instance_id="client-4",
        local_root=local_root,
        min_age_seconds=0,
    )
    assert staged["queued_count"] == 1
    pending = outbox.list_pending()
    assert len(pending) == 1
    staged_artifact_path = Path(pending[0]["artifact_path"])
    assert staged_artifact_path.exists()

    observed = {}

    class _FakeResponse:
        def __init__(self, status: int = 201, payload: bytes = b"{}") -> None:
            self.status = status
            self._payload = payload

        def read(self):
            return self._payload

    def _fake_opener(request):
        observed["url"] = request.full_url
        observed["headers"] = dict(request.header_items())
        observed["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse()

    uploaded = outbox.upload_pending(
        api_base_url="https://diag.example:8080",
        api_token="api-secret",
        opener=_fake_opener,
    )

    assert uploaded["uploaded_count"] == 1
    assert len(outbox.list_pending()) == 0
    assert not staged_artifact_path.exists()
    assert observed["url"].endswith("/api/session/logs/upload")
    assert observed["headers"]["X-api-token"] == "api-secret"


def test_observability_outbox_stages_pretty_printed_json_artifacts(tmp_path: Path) -> None:
    appdata = tmp_path / "AppData"
    local_root = appdata / "VCI_Proxy" / "observability"
    trace_dir = local_root / "session_traces"
    incident_dir = local_root / "incidents"
    trace_dir.mkdir(parents=True)
    incident_dir.mkdir(parents=True)

    (trace_dir / "trace.json").write_text(
        json.dumps({"trace_id": "trace:session-7", "session_id": "session-7", "connection_epoch": "epoch-7"}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (incident_dir / "incident.json").write_text(
        json.dumps({"incident_id": "incident-7", "session_id": "session-7", "connection_epoch": "epoch-7"}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    stale = time.time() - 10
    os.utime(trace_dir / "trace.json", (stale, stale))
    os.utime(incident_dir / "incident.json", (stale, stale))

    outbox = ObservabilityOutbox(appdata=appdata)
    staged = outbox.stage_default_artifacts(
        client_instance_id="client-7",
        local_root=local_root,
        min_age_seconds=0,
    )

    assert staged["queued_count"] == 2
    manifests = outbox.list_pending()
    assert {manifest["session_id"] for manifest in manifests} == {"session-7"}


def test_cleanup_product_observability_preserves_pending_outbox(tmp_path: Path) -> None:
    programdata = tmp_path / "ProgramData"
    appdata = tmp_path / "AppData"
    cloud_root = programdata / "RPA_Diagnostic" / "observability" / "cloud"
    local_root = appdata / "VCI_Proxy" / "observability"

    old_raw = cloud_root / "raw" / "old.jsonl"
    old_trace = cloud_root / "session_traces" / "old.json"
    old_incident = cloud_root / "incidents" / "old.json"
    old_upload = cloud_root / "uploads" / "client" / "epoch" / "old.jsonl"
    pending_manifest = local_root / "outbox" / "pending" / "keep.json"
    pending_artifact = local_root / "outbox" / "artifacts" / "keep.jsonl"
    uploaded_manifest = local_root / "outbox" / "uploaded" / "drop.json"
    orphan_artifact = local_root / "outbox" / "artifacts" / "drop.jsonl"

    for path in (old_raw, old_trace, old_incident, old_upload, pending_manifest, pending_artifact, uploaded_manifest, orphan_artifact):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        stale = time.time() - (91 * 24 * 3600)
        os.utime(path, (stale, stale))
    pending_manifest.write_text(json.dumps({"artifact_path": str(pending_artifact)}), encoding="utf-8")

    cleanup_product_observability(
        programdata=programdata,
        appdata=appdata,
        now=time.time(),
        retention_days_raw=30,
        retention_days_session_trace=30,
        retention_days_incident=90,
    )

    assert not old_raw.exists()
    assert not old_trace.exists()
    assert not old_incident.exists()
    assert not old_upload.exists()
    assert pending_manifest.exists()
    assert pending_artifact.exists()
    assert not uploaded_manifest.exists()
    assert not orphan_artifact.exists()


def test_export_cloud_log_artifacts_returns_changed_files_and_cursor(tmp_path: Path) -> None:
    cloud_root = tmp_path / "ProgramData" / "RPA_Diagnostic" / "observability" / "cloud"
    project_root = tmp_path / "repo"
    raw_file = cloud_root / "raw" / "server.jsonl"
    compat_file = project_root / "logs" / "flask_api.log"
    raw_file.parent.mkdir(parents=True, exist_ok=True)
    compat_file.parent.mkdir(parents=True, exist_ok=True)
    raw_file.write_text('{"event":"raw"}\n', encoding="utf-8")
    compat_file.write_text("compat-log\n", encoding="utf-8")

    result = export_cloud_log_artifacts(
        {"cursor_mtime_ns": 0, "cursor_path": "", "max_files": 10},
        cloud_root=cloud_root,
        project_root=project_root,
    )

    relative_paths = {item["relative_path"] for item in result["files"]}
    assert result["success"] is True
    assert "observability/cloud/raw/server.jsonl" in relative_paths
    assert "compat/logs/flask_api.log" in relative_paths
    assert result["next_cursor_mtime_ns"] >= 0
    assert isinstance(result["next_cursor_path"], str)


def test_export_cloud_log_artifacts_respects_cursor_and_max_files(tmp_path: Path, monkeypatch) -> None:
    cloud_root = tmp_path / "ProgramData" / "RPA_Diagnostic" / "observability" / "cloud"
    raw_dir = cloud_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    first = raw_dir / "a.jsonl"
    second = raw_dir / "b.jsonl"
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "ProgramData"))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
    first.write_text("a\n", encoding="utf-8")
    second.write_text("b\n", encoding="utf-8")
    now_ns = time.time_ns()
    os.utime(first, ns=(now_ns, now_ns))
    os.utime(second, ns=(now_ns + 100, now_ns + 100))

    first_page = export_cloud_log_artifacts(
        {"cursor_mtime_ns": 0, "cursor_path": "", "max_files": 1},
        cloud_root=cloud_root,
        project_root=tmp_path / "repo",
    )
    second_page = export_cloud_log_artifacts(
        {
            "cursor_mtime_ns": first_page["next_cursor_mtime_ns"],
            "cursor_path": first_page["next_cursor_path"],
            "max_files": 10,
        },
        cloud_root=cloud_root,
        project_root=tmp_path / "repo",
    )

    assert len(first_page["files"]) == 1
    assert first_page["has_more"] is True
    assert [item["relative_path"] for item in second_page["files"]] == [
        "observability/cloud/raw/b.jsonl"
    ]


def test_export_cloud_log_artifacts_respects_max_batch_bytes(tmp_path: Path, monkeypatch) -> None:
    cloud_root = tmp_path / "ProgramData" / "RPA_Diagnostic" / "observability" / "cloud"
    raw_dir = cloud_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "ProgramData"))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

    first = raw_dir / "a.jsonl"
    second = raw_dir / "b.jsonl"
    first.write_text("a" * 1024, encoding="utf-8")
    second.write_text("b" * 1024, encoding="utf-8")
    now_ns = time.time_ns()
    os.utime(first, ns=(now_ns, now_ns))
    os.utime(second, ns=(now_ns + 100, now_ns + 100))

    first_page = export_cloud_log_artifacts(
        {
            "cursor_mtime_ns": 0,
            "cursor_path": "",
            "max_files": 10,
            "max_batch_bytes": 1500,
        },
        cloud_root=cloud_root,
        project_root=tmp_path / "repo",
    )
    second_page = export_cloud_log_artifacts(
        {
            "cursor_mtime_ns": first_page["next_cursor_mtime_ns"],
            "cursor_path": first_page["next_cursor_path"],
            "max_files": 10,
            "max_batch_bytes": 1500,
        },
        cloud_root=cloud_root,
        project_root=tmp_path / "repo",
    )

    assert [item["relative_path"] for item in first_page["files"]] == [
        "observability/cloud/raw/a.jsonl"
    ]
    assert first_page["has_more"] is True
    assert [item["relative_path"] for item in second_page["files"]] == [
        "observability/cloud/raw/b.jsonl"
    ]


def test_export_cloud_log_artifacts_skips_single_file_over_batch_cap(tmp_path: Path, monkeypatch) -> None:
    cloud_root = tmp_path / "ProgramData" / "RPA_Diagnostic" / "observability" / "cloud"
    raw_dir = cloud_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "ProgramData"))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))

    oversized = raw_dir / "oversized.jsonl"
    oversized.write_text("x" * 2048, encoding="utf-8")

    page = export_cloud_log_artifacts(
        {
            "cursor_mtime_ns": 0,
            "cursor_path": "",
            "max_files": 10,
            "max_batch_bytes": 1024,
        },
        cloud_root=cloud_root,
        project_root=tmp_path / "repo",
    )

    assert page["files"] == []
    assert page["skipped_files"] == [
        {
            "relative_path": "observability/cloud/raw/oversized.jsonl",
            "mtime_ns": page["next_cursor_mtime_ns"],
            "size_bytes": 2048,
            "reason": "file_exceeds_max_batch_bytes",
        }
    ]
