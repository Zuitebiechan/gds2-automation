from __future__ import annotations

import json
from pathlib import Path

from diagnostic_platform.observability_analysis import (
    assemble_session_trace,
    classify_incident,
    generate_incident_bundle,
)


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


def test_observability_classifier_maps_cloud_dll_local_proxy() -> None:
    events = [
        _event(
            "2026-04-22T00:00:01Z",
            "virtual_j2534",
            "dll.request.retry_exhausted",
            dll_seq=10,
            status="error",
            failure_code="retry_exhausted",
            reason="all retries exhausted",
        )
    ]

    classification = classify_incident(events)

    assert classification["primary_failure_domain"] == "cloud_dll_local_proxy"
    assert classification["triggering_event"]["event_type"] == "dll.request.retry_exhausted"


def test_observability_classifier_maps_cloud_proxy_tunnel() -> None:
    events = [
        _event("2026-04-22T00:00:01Z", "reverse_server", "proxy.request.received_from_dll", dll_seq=11, proxy_seq=21, connection_epoch="epoch-1"),
        _event("2026-04-22T00:00:02Z", "reverse_server", "proxy.request.forwarded_to_tunnel", dll_seq=11, proxy_seq=21, connection_epoch="epoch-1"),
        _event("2026-04-22T00:00:03Z", "reverse_server", "tunnel.probe.failure", connection_epoch="epoch-1", status="error", failure_code="probe_failure"),
    ]

    classification = classify_incident(events)

    assert classification["primary_failure_domain"] == "cloud_proxy_tunnel"
    assert classification["first_abnormal_event"]["event_type"] == "tunnel.probe.failure"


def test_observability_classifier_maps_local_worker_rpc() -> None:
    events = [
        _event("2026-04-22T00:00:01Z", "reverse_client", "proxy.request.client_received", proxy_seq=30, worker_request_id="wrk-1"),
        _event("2026-04-22T00:00:02Z", "j2534_worker_controller", "worker.lifecycle.crashed", status="error", failure_code="worker_fatal_error"),
    ]

    classification = classify_incident(events)

    assert classification["primary_failure_domain"] == "local_worker_rpc"


def test_observability_classifier_maps_local_j2534_driver() -> None:
    events = [
        _event("2026-04-22T00:00:01Z", "j2534_worker", "worker.rpc.received", worker_request_id="wrk-2"),
        _event(
            "2026-04-22T00:00:02Z",
            "j2534_worker",
            "worker.rpc.failed",
            worker_request_id="wrk-2",
            status="error",
            failure_code="RuntimeError",
            failure_domain="local_j2534_driver",
        ),
    ]

    classification = classify_incident(events)

    assert classification["primary_failure_domain"] == "local_j2534_driver"


def test_observability_classifier_maps_vehicle_or_vci() -> None:
    events = [
        _event("2026-04-22T00:00:01Z", "reverse_server", "tunnel.quality.changed", connection_epoch="epoch-1", status="ok"),
        _event(
            "2026-04-22T00:00:02Z",
            "reverse_client",
            "j2534.call.failed",
            worker_request_id="wrk-3",
            status="error",
            failure_code="7",
            failure_domain="local_j2534_driver",
            reason="driver_return_code",
            error_name="ERR_DEVICE_NOT_CONNECTED",
        ),
    ]

    classification = classify_incident(events)

    assert classification["primary_failure_domain"] == "vehicle_or_vci"


def test_observability_classifier_maps_gds2_ui_or_agent() -> None:
    events = [
        _event("2026-04-22T00:00:01Z", "gds2_ui_or_agent", "page_guard_triggered", page="j2534_disconnect", status="error"),
        _event("2026-04-22T00:00:02Z", "gds2_ui_or_agent", "recovery_failed", page="j2534_disconnect", status="error"),
    ]

    classification = classify_incident(events)

    assert classification["primary_failure_domain"] == "gds2_ui_or_agent"


def test_observability_classifier_maps_session_runtime() -> None:
    events = [
        _event(
            "2026-04-22T00:00:01Z",
            "session_runtime",
            "session.network_gate.blocked",
            session_id="session-1",
            status="error",
            failure_code="network_gate_blocked",
        )
    ]

    classification = classify_incident(events)

    assert classification["primary_failure_domain"] == "session_runtime"


def test_observability_classifier_maps_node_routing() -> None:
    events = [
        _event(
            "2026-04-22T00:00:01Z",
            "session_runtime",
            "session.bootstrap.route_conflict",
            session_id="session-1",
            status="error",
            failure_code="route_conflict",
            failure_domain="node_routing",
        )
    ]

    classification = classify_incident(events)

    assert classification["primary_failure_domain"] == "node_routing"


def test_observability_analysis_assembles_session_trace_from_cloud_and_local_events(tmp_path: Path) -> None:
    cloud_root = tmp_path / "ProgramData" / "RPA_Diagnostic" / "observability" / "cloud"
    local_root = tmp_path / "AppData" / "VCI_Proxy" / "observability"
    (cloud_root / "raw").mkdir(parents=True)
    (local_root / "raw").mkdir(parents=True)

    (cloud_root / "active_session_snapshot.json").write_text(
        json.dumps(
            {
                "session_id": "session-1",
                "backend_name": "gds2",
                "operation_kind": "live_data.start",
                "selected_module": "Engine Control Module",
                "selected_data_category": "Engine Data",
                "current_page": "data_display",
                "navigation_session_id": None,
                "ai_session_id": None,
                "live_data_active": True,
                "connection_epoch": "epoch-1",
                "updated_at": "2026-04-22T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    cloud_events = [
        _event("2026-04-22T00:00:03Z", "reverse_server", "proxy.request.response_received", session_id="session-1", connection_epoch="epoch-1", proxy_seq=20),
        _event("2026-04-22T00:00:01Z", "session_runtime", "session.live_data.started", session_id="session-1", connection_epoch="epoch-1", page="data_display", module="Engine Control Module", data_category="Engine Data"),
    ]
    local_events = [
        _event("2026-04-22T00:00:02Z", "reverse_client", "proxy.request.client_received", proxy_seq=20, worker_request_id="wrk-20"),
    ]
    (cloud_root / "raw" / "cloud.jsonl").write_text("\n".join(json.dumps(item) for item in cloud_events), encoding="utf-8")
    (local_root / "raw" / "local.jsonl").write_text("\n".join(json.dumps(item) for item in local_events), encoding="utf-8")

    trace = assemble_session_trace(
        cloud_root=cloud_root,
        local_root=local_root,
        session_id="session-1",
    )

    assert trace["trace_id"] == "trace:session-1"
    assert trace["session_id"] == "session-1"
    assert [event["event_type"] for event in trace["timeline"]] == [
        "session.live_data.started",
        "proxy.request.client_received",
        "proxy.request.response_received",
    ]
    assert trace["page_context"]["page"] == "data_display"
    assert len(trace["source_artifacts"]) == 3


def test_observability_analysis_treats_placeholder_epoch_as_missing(tmp_path: Path) -> None:
    cloud_root = tmp_path / "ProgramData" / "RPA_Diagnostic" / "observability" / "cloud"
    raw_dir = cloud_root / "raw"
    raw_dir.mkdir(parents=True)
    (cloud_root / "active_session_snapshot.json").write_text(
        json.dumps(
            {
                "session_id": "session-5",
                "backend_name": "gds2",
                "operation_kind": "live_data.start",
                "selected_module": "Engine Control Module",
                "selected_data_category": "Engine Data",
                "current_page": "module_list",
                "navigation_session_id": None,
                "ai_session_id": None,
                "live_data_active": True,
                "connection_epoch": "epoch-5",
                "updated_at": "2026-04-22T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    event = _event(
        "2026-04-22T00:00:01Z",
        "session_runtime",
        "session.live_data.started",
        session_id="session-5",
        connection_epoch="epoch-5",
        page="data_display",
        module="Engine Control Module",
        data_category="Engine Data",
    )
    (raw_dir / "cloud.jsonl").write_text(json.dumps(event), encoding="utf-8")

    trace = assemble_session_trace(cloud_root=cloud_root, connection_epoch="no-epoch")

    assert trace["session_id"] == "session-5"
    assert trace["connection_epoch"] == "epoch-5"
    assert [event["event_type"] for event in trace["timeline"]] == ["session.live_data.started"]
    assert trace["page_context"]["page"] == "data_display"


def test_observability_analysis_uses_upload_manifest_context_for_epoch_only_local_events(
    tmp_path: Path,
) -> None:
    cloud_root = tmp_path / "ProgramData" / "RPA_Diagnostic" / "observability" / "cloud"
    raw_dir = cloud_root / "raw"
    upload_dir = cloud_root / "uploads" / "client-ctx" / "epoch-ctx-2"
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
                        session_id="session-ctx-2",
                        connection_epoch="epoch-ctx-2",
                    )
                ),
                json.dumps(
                    _event(
                        "2026-04-22T00:00:10Z",
                        "session_runtime",
                        "session.lifecycle.aborted",
                        session_id="session-ctx-2",
                        connection_epoch="epoch-ctx-2",
                        status="error",
                        failure_code="aborted",
                    )
                ),
            ]
        ),
        encoding="utf-8",
    )
    (upload_dir / "artifactctx-local.jsonl").write_text(
        json.dumps(
            _event(
                "2026-04-22T00:00:06Z",
                "reverse_client",
                "proxy.request.client_received",
                session_id=None,
                connection_epoch=None,
                proxy_seq=602,
            )
        ),
        encoding="utf-8",
    )
    (upload_dir / "artifactctx.manifest.json").write_text(
        json.dumps(
            {
                "client_instance_id": "client-ctx",
                "connection_epoch": "epoch-ctx-2",
                "artifact_id": "artifactctx",
                "artifact_name": "local.jsonl",
                "artifact_type": "raw",
                "session_id": "session-ctx-2",
                "ingested_at": 1770000000.0,
            }
        ),
        encoding="utf-8",
    )

    trace = assemble_session_trace(
        cloud_root=cloud_root,
        session_id="session-ctx-2",
        connection_epoch="epoch-ctx-2",
    )

    assert trace["status"] == "aborted"
    assert [event["event_type"] for event in trace["timeline"]] == [
        "session.lifecycle.started",
        "proxy.request.client_received",
        "session.lifecycle.aborted",
    ]


def test_observability_session_trace_does_not_expand_to_other_sessions_in_same_epoch() -> None:
    events = [
        _event(
            "2026-04-22T00:00:00Z",
            "session_runtime",
            "session.lifecycle.started",
            session_id="session-1",
            connection_epoch="epoch-shared",
        ),
        _event(
            "2026-04-22T00:00:01Z",
            "reverse_server",
            "proxy.request.forwarded_to_tunnel",
            session_id="session-1",
            connection_epoch="epoch-shared",
            proxy_seq=100,
        ),
        _event(
            "2026-04-22T00:00:02Z",
            "reverse_client",
            "proxy.request.client_received",
            connection_epoch="epoch-shared",
            proxy_seq=100,
            worker_request_id="wrk-100",
        ),
        _event(
            "2026-04-22T00:00:03Z",
            "session_runtime",
            "session.lifecycle.aborted",
            session_id="session-1",
            connection_epoch="epoch-shared",
        ),
        _event(
            "2026-04-22T00:00:04Z",
            "session_runtime",
            "session.lifecycle.started",
            session_id="session-2",
            connection_epoch="epoch-shared",
        ),
        _event(
            "2026-04-22T00:00:05Z",
            "reverse_server",
            "proxy.request.forwarded_to_tunnel",
            session_id="session-2",
            connection_epoch="epoch-shared",
            proxy_seq=200,
        ),
        _event(
            "2026-04-22T00:00:06Z",
            "reverse_client",
            "proxy.request.client_received",
            connection_epoch="epoch-shared",
            proxy_seq=200,
            worker_request_id="wrk-200",
        ),
    ]

    trace = assemble_session_trace(
        events=events,
        session_id="session-1",
        connection_epoch="epoch-shared",
    )

    assert [event["event_type"] for event in trace["timeline"]] == [
        "session.lifecycle.started",
        "proxy.request.forwarded_to_tunnel",
        "proxy.request.client_received",
        "session.lifecycle.aborted",
    ]
    assert {event.get("proxy_seq") for event in trace["timeline"] if event.get("proxy_seq")} == {100}


def test_observability_analysis_generates_incident_bundle_for_proxy_timeout() -> None:
    events = [
        _event("2026-04-22T00:00:00Z", "reverse_server", "tunnel.quality.changed", session_id="session-1", connection_epoch="epoch-7", network_grade="block", tunnel_status="blocked", reason="probe_failures"),
        _event("2026-04-22T00:00:01Z", "reverse_server", "tunnel.probe.failure", session_id="session-1", connection_epoch="epoch-7", status="error", failure_code="probe_failure", page="data_display", module="Engine Control Module", data_category="Engine Data"),
        _event("2026-04-22T00:00:02Z", "reverse_server", "proxy.request.timeout", session_id="session-1", connection_epoch="epoch-7", dll_seq=1882, proxy_seq=991, operation_kind="j2534:PassThruReadMsgs", status="error", failure_code="timeout", page="data_display", module="Engine Control Module", data_category="Engine Data", impact_scope="live_data"),
    ]
    trace = assemble_session_trace(events=events, session_id="session-1")

    bundle = generate_incident_bundle(trace)

    assert bundle["session_id"] == "session-1"
    assert bundle["connection_epoch"] == "epoch-7"
    assert bundle["primary_failure_domain"] == "cloud_proxy_tunnel"
    assert bundle["triggering_event"]["event_type"] == "proxy.request.timeout"
    assert bundle["first_abnormal_event"]["event_type"] == "tunnel.probe.failure"
    assert bundle["network_context"]["grade"] == "block"
    assert "live_data" in bundle["affected_operations"]


def test_observability_analysis_generates_incident_bundle_for_recovery_failure() -> None:
    events = [
        _event("2026-04-22T00:00:01Z", "gds2_ui_or_agent", "page_guard_triggered", session_id="session-2", page="j2534_disconnect", module="Engine Control Module", data_category="Engine Data", status="error"),
        _event("2026-04-22T00:00:02Z", "gds2_ui_or_agent", "recovery_failed", session_id="session-2", page="j2534_disconnect", module="Engine Control Module", data_category="Engine Data", status="error", impact_scope="agent_data_collector"),
    ]
    trace = assemble_session_trace(events=events, session_id="session-2")

    bundle = generate_incident_bundle(trace)

    assert bundle["primary_failure_domain"] == "gds2_ui_or_agent"
    assert bundle["triggering_event"]["event_type"] == "recovery_failed"
    assert bundle["page_context"]["page"] == "j2534_disconnect"
    assert bundle["affected_operations"] == ["agent_data_collector"]


def test_observability_analysis_prefers_operation_kind_over_generic_component_scope() -> None:
    events = [
        _event(
            "2026-04-22T00:00:01Z",
            "reverse_client",
            "j2534.call.failed",
            session_id="session-3",
            operation_kind="j2534:PassThruDisconnect",
            impact_scope="reverse_client",
            status="error",
            failure_code="7",
            failure_domain="local_j2534_driver",
            error_name="ERR_DEVICE_NOT_CONNECTED",
        )
    ]
    trace = assemble_session_trace(events=events, session_id="session-3")

    bundle = generate_incident_bundle(trace)

    assert bundle["affected_operations"] == ["j2534:PassThruDisconnect"]
