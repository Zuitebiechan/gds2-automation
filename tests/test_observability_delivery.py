from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.error
from pathlib import Path

from diagnostic_platform.observability import JsonlWriter, emit_event
import diagnostic_platform.observability_artifacts as observability_artifacts
from diagnostic_platform.observability_artifacts import (
    cleanup_product_observability,
    get_cloud_incidents_dir,
    get_cloud_session_traces_dir,
    ingest_uploaded_artifact,
    materialize_session_artifacts,
    wait_for_observability_artifact_jobs,
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
    assert wait_for_observability_artifact_jobs(timeout_s=5.0)

    trace_files = list(get_cloud_session_traces_dir(tmp_path / "ProgramData").glob("*.json"))
    incident_files = list(get_cloud_incidents_dir(tmp_path / "ProgramData").glob("*.json"))
    assert trace_files
    assert incident_files


def test_emit_event_auto_materializes_aborted_trace_after_terminal_event(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "ProgramData"))
    writer = JsonlWriter(
        tmp_path / "ProgramData" / "RPA_Diagnostic" / "observability" / "cloud" / "raw" / "session.jsonl"
    )

    emit_event(
        writer,
        component="session_runtime",
        event_type="session.lifecycle.started",
        session_id="session-abort-1",
        connection_epoch="epoch-abort-1",
    )
    emit_event(
        writer,
        component="session_runtime",
        event_type="session.lifecycle.aborted",
        session_id="session-abort-1",
        connection_epoch="epoch-abort-1",
        status="error",
        failure_code="aborted",
        failure_domain="session_runtime",
        reason="Aborted by user",
    )
    writer.close()
    assert wait_for_observability_artifact_jobs(timeout_s=5.0)

    trace_path = get_cloud_session_traces_dir(tmp_path / "ProgramData") / "trace-session-abort-1.json"
    trace_payload = json.loads(trace_path.read_text(encoding="utf-8"))
    assert trace_payload["status"] == "aborted"
    assert trace_payload["timeline"][-1]["event_type"] == "session.lifecycle.aborted"


def test_emit_event_queues_materialization_without_blocking_request_thread(
    tmp_path: Path,
    monkeypatch,
) -> None:
    writer = JsonlWriter(
        tmp_path / "ProgramData" / "RPA_Diagnostic" / "observability" / "cloud" / "raw" / "server.jsonl"
    )
    started = threading.Event()
    release = threading.Event()

    def _blocked_materialize(*args, **kwargs):
        started.set()
        release.wait(timeout=2.0)
        return {"trace_path": None, "incident_paths": []}

    monkeypatch.setattr(
        observability_artifacts,
        "materialize_session_artifacts",
        _blocked_materialize,
    )

    before = time.perf_counter()
    emit_event(
        writer,
        component="session_runtime",
        event_type="session.lifecycle.aborted",
        session_id="session-async",
        connection_epoch="epoch-async",
    )
    elapsed_ms = (time.perf_counter() - before) * 1000.0

    try:
        assert elapsed_ms < 250.0
        assert started.wait(timeout=1.0)
    finally:
        release.set()
        assert wait_for_observability_artifact_jobs(timeout_s=5.0)
        writer.close()


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


def test_ingest_uploaded_artifact_discovers_context_after_initial_empty_event(tmp_path: Path) -> None:
    cloud_root = tmp_path / "ProgramData" / "RPA_Diagnostic" / "observability" / "cloud"
    payload_bytes = "\n".join(
        [
            json.dumps(_event("2026-04-22T00:00:00Z", "reverse_client", "tunnel.lifecycle.connected")),
            json.dumps(
                _event(
                    "2026-04-22T00:00:01Z",
                    "j2534_worker",
                    "worker.rpc.failed",
                    session_id="session-8",
                    connection_epoch="epoch-8",
                    worker_request_id="wrk-80",
                    status="error",
                    failure_code="RuntimeError",
                    failure_domain="local_j2534_driver",
                )
            ),
        ]
    ).encode("utf-8")
    payload = {
        "client_instance_id": "client-8",
        "connection_epoch": "no-epoch",
        "artifact_id": "artifact-8",
        "artifact_name": "local.jsonl",
        "artifact_type": "raw",
        "session_id": None,
        "content_base64": base64.b64encode(payload_bytes).decode("ascii"),
    }

    result = ingest_uploaded_artifact(payload, cloud_root=cloud_root)

    trace_payload = json.loads(result["trace_path"].read_text(encoding="utf-8"))
    assert trace_payload["session_id"] == "session-8"
    assert trace_payload["connection_epoch"] == "epoch-8"
    assert [event["event_type"] for event in trace_payload["timeline"]] == [
        "worker.rpc.failed",
    ]


def test_ingest_uploaded_artifact_infers_session_id_from_cloud_epoch_context(
    tmp_path: Path,
) -> None:
    cloud_root = tmp_path / "ProgramData" / "RPA_Diagnostic" / "observability" / "cloud"
    raw_dir = cloud_root / "raw"
    raw_dir.mkdir(parents=True)
    (raw_dir / "cloud.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    _event(
                        "2026-04-22T00:00:00Z",
                        "session_runtime",
                        "session.live_data.started",
                        session_id="session-epoch-only",
                        connection_epoch="epoch-ctx-1",
                        page="data_display",
                    )
                ),
                json.dumps(
                    _event(
                        "2026-04-22T00:00:01Z",
                        "session_runtime",
                        "session.lifecycle.aborted",
                        session_id="session-epoch-only",
                        connection_epoch="epoch-ctx-1",
                        status="error",
                        failure_code="aborted",
                    )
                ),
            ]
        ),
        encoding="utf-8",
    )
    payload_bytes = "\n".join(
        [
            json.dumps(_event("2026-04-22T00:00:02Z", "reverse_client", "proxy.request.client_received")),
            json.dumps(_event("2026-04-22T00:00:03Z", "j2534_worker", "worker.rpc.received")),
        ]
    ).encode("utf-8")
    payload = {
        "client_instance_id": "client-epoch",
        "connection_epoch": "epoch-ctx-1",
        "artifact_id": "artifact-epoch",
        "artifact_name": "local.jsonl",
        "artifact_type": "raw",
        "session_id": None,
        "content_base64": base64.b64encode(payload_bytes).decode("ascii"),
    }

    result = ingest_uploaded_artifact(payload, cloud_root=cloud_root)

    manifest_payload = json.loads(result["manifest_path"].read_text(encoding="utf-8"))
    trace_payload = json.loads(result["trace_path"].read_text(encoding="utf-8"))
    assert manifest_payload["session_id"] == "session-epoch-only"
    assert trace_payload["session_id"] == "session-epoch-only"
    assert trace_payload["connection_epoch"] == "epoch-ctx-1"


def test_materialize_session_artifacts_ignores_placeholder_epoch(tmp_path: Path) -> None:
    cloud_root = tmp_path / "ProgramData" / "RPA_Diagnostic" / "observability" / "cloud"
    raw_dir = cloud_root / "raw"
    raw_dir.mkdir(parents=True)
    (cloud_root / "active_session_snapshot.json").write_text(
        json.dumps(
            {
                "session_id": "session-9",
                "backend_name": "gds2",
                "operation_kind": "live_data.start",
                "selected_module": "Engine Control Module",
                "selected_data_category": "Engine Data",
                "current_page": "module_list",
                "navigation_session_id": None,
                "ai_session_id": None,
                "live_data_active": True,
                "connection_epoch": "epoch-9",
                "updated_at": "2026-04-22T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    (raw_dir / "cloud.jsonl").write_text(
        json.dumps(
            _event(
                "2026-04-22T00:00:01Z",
                "session_runtime",
                "session.live_data.started",
                session_id="session-9",
                connection_epoch="epoch-9",
                page="data_display",
            )
        ),
        encoding="utf-8",
    )

    result = materialize_session_artifacts(cloud_root=cloud_root, connection_epoch="no-epoch")

    trace_payload = json.loads(result["trace_path"].read_text(encoding="utf-8"))
    assert trace_payload["session_id"] == "session-9"
    assert trace_payload["connection_epoch"] == "epoch-9"
    assert trace_payload["key_metrics"]["event_count"] == 1
    assert trace_payload["page_context"]["page"] == "data_display"


def test_materialize_session_artifacts_backfills_epoch_only_upload_manifest_within_session_window(
    tmp_path: Path,
) -> None:
    cloud_root = tmp_path / "ProgramData" / "RPA_Diagnostic" / "observability" / "cloud"
    raw_dir = cloud_root / "raw"
    upload_dir = cloud_root / "uploads" / "client-backfill" / "epoch-backfill-1"
    raw_dir.mkdir(parents=True)
    upload_dir.mkdir(parents=True)

    (raw_dir / "cloud.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    _event(
                        "2026-04-22T00:00:05Z",
                        "session_runtime",
                        "session.lifecycle.started",
                        session_id="session-backfill-1",
                        connection_epoch="epoch-backfill-1",
                        page="data_display",
                    )
                ),
                json.dumps(
                    _event(
                        "2026-04-22T00:00:12Z",
                        "session_runtime",
                        "session.lifecycle.aborted",
                        session_id="session-backfill-1",
                        connection_epoch="epoch-backfill-1",
                        page="data_display",
                        status="error",
                        failure_code="aborted",
                        failure_domain="session_runtime",
                    )
                ),
            ]
        ),
        encoding="utf-8",
    )
    (upload_dir / "artifactbackfill-local.jsonl").write_text(
        json.dumps(
            _event(
                "2026-04-22T00:00:06Z",
                "reverse_client",
                "proxy.request.client_received",
                session_id=None,
                connection_epoch=None,
                proxy_seq=901,
            )
        ),
        encoding="utf-8",
    )
    manifest_path = upload_dir / "artifactbackfill.manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "client_instance_id": "client-backfill",
                "connection_epoch": "epoch-backfill-1",
                "artifact_id": "artifactbackfill",
                "artifact_name": "local.jsonl",
                "artifact_type": "raw",
                "session_id": None,
                "ingested_at": 1770000000.0,
            }
        ),
        encoding="utf-8",
    )

    result = materialize_session_artifacts(
        cloud_root=cloud_root,
        session_id="session-backfill-1",
        connection_epoch="epoch-backfill-1",
    )

    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    trace_payload = json.loads(result["trace_path"].read_text(encoding="utf-8"))
    assert manifest_payload["session_id"] == "session-backfill-1"
    assert trace_payload["status"] == "aborted"
    assert [event["event_type"] for event in trace_payload["timeline"]] == [
        "session.lifecycle.started",
        "proxy.request.client_received",
        "session.lifecycle.aborted",
    ]


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


def test_observability_outbox_stages_context_after_initial_empty_event(tmp_path: Path) -> None:
    appdata = tmp_path / "AppData"
    local_root = appdata / "VCI_Proxy" / "observability"
    raw_dir = local_root / "raw"
    raw_dir.mkdir(parents=True)
    artifact = raw_dir / "local.jsonl"
    artifact.write_text(
        "\n".join(
            [
                json.dumps(_event("2026-04-22T00:00:00Z", "reverse_client", "tunnel.lifecycle.connected")),
                json.dumps(
                    _event(
                        "2026-04-22T00:00:01Z",
                        "reverse_client",
                        "proxy.request.client_received",
                        session_id="session-6",
                        connection_epoch="epoch-6",
                        proxy_seq=60,
                    )
                ),
            ]
        ),
        encoding="utf-8",
    )
    stale = time.time() - 10
    os.utime(artifact, (stale, stale))

    outbox = ObservabilityOutbox(appdata=appdata)
    staged = outbox.stage_default_artifacts(
        client_instance_id="client-6",
        local_root=local_root,
        min_age_seconds=0,
    )

    assert staged["queued_count"] == 1
    pending = outbox.list_pending()
    assert pending[0]["session_id"] == "session-6"
    assert pending[0]["connection_epoch"] == "epoch-6"


def test_observability_outbox_uses_default_context_when_raw_artifact_lacks_it(
    tmp_path: Path,
) -> None:
    appdata = tmp_path / "AppData"
    local_root = appdata / "VCI_Proxy" / "observability"
    raw_dir = local_root / "raw"
    raw_dir.mkdir(parents=True)
    artifact = raw_dir / "local.jsonl"
    artifact.write_text(
        json.dumps(_event("2026-04-22T00:00:00Z", "reverse_client", "tunnel.lifecycle.connected")),
        encoding="utf-8",
    )
    stale = time.time() - 10
    os.utime(artifact, (stale, stale))

    outbox = ObservabilityOutbox(appdata=appdata)
    staged = outbox.stage_default_artifacts(
        client_instance_id="client-ctx",
        local_root=local_root,
        min_age_seconds=0,
        default_session_id="session-default",
        default_connection_epoch="epoch-default",
    )

    assert staged["queued_count"] == 1
    pending = outbox.list_pending()
    assert pending[0]["session_id"] == "session-default"
    assert pending[0]["connection_epoch"] == "epoch-default"


def test_observability_outbox_upload_failure_keeps_pending_manifest(tmp_path: Path) -> None:
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
                session_id="session-10",
                connection_epoch="epoch-10",
            )
        ),
        encoding="utf-8",
    )
    stale = time.time() - 10
    os.utime(artifact, (stale, stale))

    outbox = ObservabilityOutbox(appdata=appdata)
    outbox.stage_default_artifacts(
        client_instance_id="client-10",
        local_root=local_root,
        min_age_seconds=0,
    )
    pending = outbox.list_pending()
    staged_artifact_path = Path(pending[0]["artifact_path"])

    def _failing_opener(request):
        raise urllib.error.HTTPError(request.full_url, 502, "Bad Gateway", hdrs=None, fp=None)

    uploaded = outbox.upload_pending(
        api_base_url="https://diag.example:8080",
        opener=_failing_opener,
    )

    assert uploaded["uploaded_count"] == 0
    assert uploaded["failed_count"] == 1
    assert len(outbox.list_pending()) == 1
    assert staged_artifact_path.exists()


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


