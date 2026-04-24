# Product Observability

This document is the authoritative contract for the product-level diagnostics observability system.

Schema version: `observability.v1`

## Purpose

The platform must capture structured evidence across the cloud-to-local diagnostics path so engineers and downstream LLM tooling can answer:

- which hop failed first
- which failure domain owns the incident
- which user-visible operation was impacted
- what should be checked next

This system is default-on, internal-only, and does not require user export steps.

## Scope

Current chain:

- cloud: GDS2, Flask API, session/runtime, `virtual_j2534.dll`
- local: tray client, reverse client, J2534 worker, real J2534 driver, VCI, ECU

Key business actions:

- session start / abort / complete
- start diagnostics
- navigation
- live data
- AI diagnosis
- network gate
- bootstrap routing

## Artifact Layout

Cloud root:

- `%PROGRAMDATA%\RPA_Diagnostic\observability\cloud\`
- override with `PRODUCT_LOG_CLOUD_ROOT`, for example `D:\RPA_Diagnostic\observability\cloud`

Local root:

- `%APPDATA%\VCI_Proxy\observability\`

Phase 1 cloud artifacts:

- `raw\*.jsonl`: one file per component instance
- `active_session_snapshot.json`: best-effort current business-session context

Materialized cloud artifacts:

- `session_traces\*.json`
- `incidents\*.json`
- `uploads\<client_instance_id>\<connection_epoch>\*`

Local artifacts:

- `raw\*.jsonl`
- `cloud_mirror\sources\<source_id>\state.json`
- `cloud_mirror\sources\<source_id>\files\**\*`
- `outbox\pending\*.json`
- `outbox\uploaded\*.json`

Compatibility logs retained but non-authoritative:

- `gds2_web.log`
- `client.log`
- `worker_x86.log`
- `worker_x64.log`
- `vci_proxy_dll.log`

## Event Contract

Every raw event uses `observability.v1` and includes these required fields:

- `schema_version`
- `ts`
- `component`
- `component_instance_id`
- `event_type`
- `session_id`
- `connection_epoch`
- `dll_seq`
- `proxy_seq`
- `worker_request_id`
- `operation_kind`
- `status`
- `failure_code`
- `failure_domain`
- `reason`
- `duration_ms`
- `hw_ms`
- `network_ms`
- `page`
- `module`
- `data_category`
- `symptom`
- `impact_scope`
- `next_checks`
- `redaction_applied`

Join keys:

- business: `session_id`
- tunnel: `connection_epoch`
- DLL -> server: `dll_seq`
- server -> client/worker: `proxy_seq`
- device side: `worker_request_id`

Trace id convention:

- assembled trace id: `trace:<session_id>`
- raw-event `trace_id` may be absent and filled during assembly

## Failure Domains

The allowed values are fixed:

- `cloud_dll_local_proxy`
- `cloud_proxy_tunnel`
- `local_reverse_client`
- `local_worker_rpc`
- `local_j2534_driver`
- `vehicle_or_vci`
- `gds2_ui_or_agent`
- `session_runtime`
- `node_routing`
- `unknown`

## Redaction Rules

- Never store tokens, PSKs, private keys, or full authorization headers.
- VIN is stored as `vin_masked` plus `vin_hash`, never full plaintext VIN.
- J2534 / IOCTL / CAN payloads default to type, length, digest, and optional 16-byte hex prefix.
- `ReadMsgs(BUFFER_EMPTY)` must be aggregated during steady state; detailed records belong only to incident windows.

## Active Session Snapshot

`active_session_snapshot.json` is a best-effort view of the current business session on the worker and currently assumes:

- `1 worker process = 1 active business session`

Required snapshot fields:

- `session_id`
- `backend_name`
- `operation_kind`
- `selected_module`
- `selected_data_category`
- `current_page`
- `navigation_session_id`
- `ai_session_id`
- `live_data_active`
- `connection_epoch`
- `updated_at`

When no active session exists, the snapshot may be absent.

## Upload And Retention

Upload API:

- `POST /api/session/logs/upload`
- `POST /api/session/logs/sync`
  - `POST /api/session/logs/sync` requires `DIAGNOSTIC_API_TOKEN` to be configured on the cloud API and supplied by the caller

Expected upload payload fields:

- `client_instance_id`
- `connection_epoch`
- `artifact_id`
- `artifact_name`
- `artifact_type`
- `session_id` optional
- `content_base64`

Expected cloud-sync request payload fields:

- `cursor_mtime_ns`
- `cursor_path`
- `max_files` optional
- `max_batch_bytes` optional

Expected cloud-sync response fields:

- `files`
- `skipped_files`
- `has_more`
- `next_cursor_mtime_ns`
- `next_cursor_path`

Current cloud-sync skip reasons include:

- `file_exceeds_max_artifact_mb`
- `file_exceeds_max_batch_bytes`
- `file_unreadable`

Default env-backed settings:

- `PRODUCT_LOGS_ENABLED=true`
- `PRODUCT_LOG_CLOUD_ROOT=` optional explicit cloud artifact root
- `PRODUCT_LOG_RETENTION_DAYS_RAW=30`
- `PRODUCT_LOG_RETENTION_DAYS_SESSION_TRACE=30`
- `PRODUCT_LOG_RETENTION_DAYS_INCIDENT=90`
- `PRODUCT_LOG_UPLOAD_ENABLED=true`
- `PRODUCT_LOG_MAX_ARTIFACT_MB=50`

Current runtime behavior:

- cloud-side startup runs best-effort retention cleanup for raw/session-trace/incident/uploaded artifacts
- tray client startup runs best-effort cleanup for uploaded outbox entries
- tray client runs a background uploader loop that stages local artifacts into the outbox and uploads them to the cloud ingest API
- tray client also polls the cloud sync API and mirrors changed cloud-side log files into `%APPDATA%\VCI_Proxy\observability\cloud_mirror\`

## Current Component Usage

- `server/app.py`
  - generates per-request `request_id`
  - writes structured API request completion events
- `server/api/session_log_handlers.py`
  - ingests local observability artifacts uploaded from the tray client
  - exports changed cloud-side log artifacts for local mirroring
- `diagnostic_platform/session_orchestrator.py`
  - emits session lifecycle completion/failure terminal events
- `diagnostic_platform/runtime/*`
  - writes and maintains `active_session_snapshot.json`
  - emits session, start-diagnostics, network-gate, navigation, live-data, and AI business events
- `src/diagnosis/ai_engine.py`
  - emits AI provider failure events with provider status, type, model, base URL, and ai session id
- `diagnostic_platform/observability_analysis.py`
  - assembles `session_trace`, classifies incidents deterministically, and generates `incident_bundle`
- `diagnostic_platform/observability.py`
  - provides the Python logging bridge that mirrors terminal/runtime logger output into structured `runtime.log` events
- `src/navigation/controller.py`
  - emits page detection and vehicle-selection UI action events
- `backends/gds2/backend.py`
  - emits Data Display guard and page-drift trigger events
- `backends/gds2/registry_navigation_runtime.py`
  - emits recovery attempted / succeeded / failed events
- `src/streaming/agent_data_collector.py`
  - emits availability, snapshot, guard-failed, started, and stopped collector events
- `vci_proxy/reverse_server.py`
  - emits tunnel lifecycle, probe, tunnel-quality, proxy-request staged events, and reverse-server process lifecycle events
- `vci_proxy/reverse_client.py`
  - emits reverse tunnel connection lifecycle, request receipt, and J2534 call events
- `vci_proxy/j2534_worker.py`
  - emits worker lifecycle, spawn status, RPC receipt/return/failure, and propagates `worker_request_id`
- `vci_proxy/tunnel_quality.py`
  - reuses shared UTC timestamp formatting from the observability module
- `vci_proxy/client_gui.py`
  - uploads local observability artifacts to the cloud
  - mirrors changed cloud-side log artifacts onto the local machine
- `vci_proxy/virtual_dll/virtual_j2534.c`
  - emits DLL-side JSONL call lifecycle and transport retry/socket events
- `server.runtime` / `reverse_server.runtime`
  - mirror Python runtime logger output into structured `runtime.log` events so terminal-visible messages are searchable in raw observability artifacts

## Examples

### Success Session Trace Fragment

```json
{
  "trace_id": "trace:session-1",
  "session_id": "session-1",
  "status": "completed",
  "timeline": [
    {
      "ts": "2026-04-22T00:10:00Z",
      "component": "server.api",
      "event_type": "api.request.completed",
      "operation_kind": "http:POST /api/session/start",
      "status": "ok"
    },
    {
      "ts": "2026-04-22T00:10:02Z",
      "component": "session.runtime",
      "event_type": "session.start_diagnostics.completed",
      "page": "module_list",
      "status": "ok"
    }
  ]
}
```

### Proxy Timeout Raw Event

```json
{
  "schema_version": "observability.v1",
  "ts": "2026-04-22T00:12:05Z",
  "component": "reverse_server",
  "component_instance_id": "reverse_server:4120:20260422T000000Z",
  "event_type": "proxy.request.timeout",
  "session_id": "session-1",
  "connection_epoch": "epoch-7",
  "dll_seq": 1882,
  "proxy_seq": 991,
  "worker_request_id": null,
  "operation_kind": "j2534:PassThruReadMsgs",
  "status": "error",
  "failure_code": "timeout",
  "failure_domain": "cloud_proxy_tunnel",
  "reason": "timed out while waiting for tunnel response",
  "duration_ms": 5000,
  "hw_ms": null,
  "network_ms": 4970,
  "page": "data_display",
  "module": "Engine Control Module",
  "data_category": "Engine Data",
  "symptom": "live_data_stream_stalled",
  "impact_scope": "live_data",
  "next_checks": ["check tunnel probe events", "check reverse_client receive path"],
  "redaction_applied": []
}
```

### Incident Bundle

```json
{
  "incident_id": "incident-20260422-0001",
  "session_id": "session-1",
  "connection_epoch": "epoch-7",
  "primary_failure_domain": "cloud_proxy_tunnel",
  "triggering_event": {
    "ts": "2026-04-22T00:12:05Z",
    "component": "reverse_server",
    "event_type": "proxy.request.timeout",
    "failure_code": "timeout"
  },
  "first_abnormal_event": {
    "ts": "2026-04-22T00:12:01Z",
    "component": "reverse_server",
    "event_type": "tunnel.probe.failure",
    "failure_code": "probe_failure"
  },
  "affected_operations": ["live_data.start", "live_data.stream"],
  "page_context": {
    "page": "data_display",
    "module": "Engine Control Module",
    "data_category": "Engine Data"
  },
  "network_context": {
    "grade": "block",
    "reason": "probe_failures"
  },
  "route_context": {
    "selected_zone": "us-west-2-lax-1a"
  },
  "timeline": [
    {
      "ts": "2026-04-22T00:12:01Z",
      "event_type": "tunnel.probe.failure"
    },
    {
      "ts": "2026-04-22T00:12:05Z",
      "event_type": "proxy.request.timeout"
    },
    {
      "ts": "2026-04-22T00:12:06Z",
      "event_type": "live_data.stream.error"
    }
  ],
  "key_metrics": {
    "event_count": 3,
    "error_count": 3,
    "max_network_ms": 4100
  },
  "next_checks": [
    "inspect reverse tunnel health",
    "verify local reverse client heartbeat continuity"
  ],
  "source_artifacts": [
    "cloud/raw/reverse_server-*.jsonl",
    "cloud/active_session_snapshot.json"
  ]
}
```
