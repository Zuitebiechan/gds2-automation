from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from diagnostic_platform.observability import JsonlWriter, emit_event
from diagnostic_platform.observability_artifacts import get_cloud_incidents_dir, get_cloud_session_traces_dir
from vci_proxy.observability_outbox import ObservabilityOutbox


def test_acceptance_success_session_emits_session_trace_only(tmp_path: Path, monkeypatch) -> None:
    programdata = tmp_path / "ProgramData"
    monkeypatch.setenv("PROGRAMDATA", str(programdata))
    writer = JsonlWriter(programdata / "RPA_Diagnostic" / "observability" / "cloud" / "raw" / "session.jsonl")

    emit_event(
        writer,
        component="session_runtime",
        event_type="session.lifecycle.completed",
        session_id="session-success-1",
        connection_epoch="epoch-success-1",
    )
    writer.close()

    trace_files = list(get_cloud_session_traces_dir(programdata).glob("*.json"))
    incident_files = list(get_cloud_incidents_dir(programdata).glob("*.json"))

    assert trace_files
    assert not incident_files


def test_acceptance_trigger_incident_emits_bundle(tmp_path: Path, monkeypatch) -> None:
    programdata = tmp_path / "ProgramData"
    monkeypatch.setenv("PROGRAMDATA", str(programdata))
    writer = JsonlWriter(programdata / "RPA_Diagnostic" / "observability" / "cloud" / "raw" / "incident.jsonl")

    emit_event(
        writer,
        component="reverse_server",
        event_type="proxy.request.timeout",
        session_id="session-incident-1",
        connection_epoch="epoch-incident-1",
        status="error",
        failure_code="timeout",
        operation_kind="j2534:PassThruReadMsgs",
    )
    writer.close()

    incident_files = list(get_cloud_incidents_dir(programdata).glob("*.json"))
    assert incident_files
    bundle = json.loads(incident_files[0].read_text(encoding="utf-8"))
    assert bundle["primary_failure_domain"] == "cloud_proxy_tunnel"


def test_acceptance_outbox_retries_after_upload_failure(tmp_path: Path) -> None:
    appdata = tmp_path / "AppData"
    local_root = appdata / "VCI_Proxy" / "observability"
    raw_dir = local_root / "raw"
    raw_dir.mkdir(parents=True)
    artifact = raw_dir / "local.jsonl"
    artifact.write_text(
        json.dumps(
            {
                "schema_version": "observability.v1",
                "ts": "2026-04-22T00:00:00Z",
                "component": "reverse_client",
                "component_instance_id": "reverse_client:pid:startup",
                "event_type": "proxy.request.client_received",
                "session_id": "session-retry-1",
                "connection_epoch": "epoch-retry-1",
                "dll_seq": None,
                "proxy_seq": 1,
                "worker_request_id": None,
                "operation_kind": "j2534:PassThruReadMsgs",
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
                "impact_scope": "proxy_request",
                "next_checks": [],
                "redaction_applied": [],
            }
        ),
        encoding="utf-8",
    )
    stale = os.path.getmtime(artifact) - 10
    os.utime(artifact, (stale, stale))

    outbox = ObservabilityOutbox(appdata=appdata)
    outbox.stage_default_artifacts(client_instance_id="client-retry-1", local_root=local_root, min_age_seconds=0)

    class _FailingResponse:
        status = 503

        def read(self):
            return b"{}"

    class _SuccessResponse:
        status = 201

        def read(self):
            return b"{}"

    assert outbox.upload_pending(
        api_base_url="https://diag.example:8080",
        opener=lambda request: _FailingResponse(),
    )["uploaded_count"] == 0
    assert len(outbox.list_pending()) == 1

    assert outbox.upload_pending(
        api_base_url="https://diag.example:8080",
        opener=lambda request: _SuccessResponse(),
    )["uploaded_count"] == 1
    assert len(outbox.list_pending()) == 0
    assert not any((appdata / "VCI_Proxy" / "observability" / "outbox" / "artifacts").glob("*"))
