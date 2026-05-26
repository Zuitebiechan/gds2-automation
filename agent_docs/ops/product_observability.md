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

Session trace status semantics:

- `completed`: session observed a `session.lifecycle.completed` terminal event
- `aborted`: session observed a `session.lifecycle.aborted` terminal event
- `failed`: session observed a `session.lifecycle.failed` terminal event
- `partial`: no terminal session event has been assembled yet

Local artifacts:

- `raw\*.jsonl`
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

Expected upload payload fields:

- `client_instance_id`
- `connection_epoch`
- `artifact_id`
- `artifact_name`
- `artifact_type`
- `session_id` optional
- `content_base64`

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
- cloud-side cleanup also recovers complete JSON artifacts left under atomic
  write `.tmp` names when a process exits after writing the temp file but
  before the final rename; incomplete temp files remain for retention cleanup
  instead of being promoted
- tray client startup runs best-effort cleanup for uploaded outbox entries
- tray client runs a background uploader loop that stages local artifacts into the outbox and uploads them to the cloud ingest API
- local outbox staging scans artifact contents for the first non-placeholder `session_id` and `connection_epoch`; leading lifecycle events without session context do not force the upload into `no-epoch`
- cloud materialization may backfill uploaded artifact manifests with a resolved `session_id` when the artifact initially arrived with only `connection_epoch` and its event window overlaps the assembled session trace
- trace assembly treats `no-session` and `no-epoch` as missing selectors and falls back to the active snapshot or raw-event context before materializing a trace
- runtime-triggered trace/incident materialization runs on a background queue; terminal session events and uploaded local artifacts must not block the API request path while large traces are assembled
- terminal session events also start a direct non-daemon materialization worker
  with the event's session/epoch selectors so session-named traces are refreshed
  even when an earlier epoch-only materialization already produced a partial
  trace
- trace/incident materialization tolerates raw-file rotation between discovery
  and read: if a discovered `*.jsonl` raw file has already been compressed to
  `*.jsonl.gz`, analysis falls back to the compressed sibling and records that
  resolved artifact path as the source
- trace/incident JSON artifacts are only promoted after the temporary file can
  be parsed as complete JSON; if an existing target artifact is incomplete, the
  writer quarantines it with a `.corrupt-*` suffix before publishing a fresh
  valid artifact
- transient upload/API failures leave pending manifests in place for the next uploader pass instead of crashing the tray background loop
- the cloud-side agent collector emits `agent.collector.focus_value_changed` for primary focused value transitions such as `battery_voltage`, with previous/current values plus collector timing context for direct freshness analysis; noisy numeric signals may apply a key-specific significance threshold before a change event is emitted

## Current VCI Latency Analysis Handoff - 2026-05-06

For the current Proxy J2534 Data Display latency workstream, raw observability is
the authoritative evidence. The latest inspected ECU run had cloud raw logs and
local raw logs, but no materialized `session_traces`, `incidents`, `uploads`, or
local outbox artifacts in the inspected mirror. That is an artifact-health note,
not by itself a Data Display failure.

Use these event checks to decide whether the latest optimization build was
actually active:

- cloud `reverse_server` `process.lifecycle.started`:
  `read_cache_enabled`, `read_cache_active_ttl_ms`,
  `read_cache_active_adaptive_ttl_max_ms`,
  `read_cache_active_adaptive_ttl_margin_ms`,
  `read_ahead_enabled`, `read_ahead_transaction_enabled`,
  `read_ahead_write_collect_max_reads`,
  `read_ahead_max_empty_reads`,
  `read_ahead_max_consecutive_empty_reads`,
  `read_ahead_min_drain_ms`,
  `read_ahead_transaction_max_network_ms`,
  `read_ahead_transaction_cooldown_ms`, `local_sweep_enabled`,
  `local_sweep_mode`, `local_sweep_read_timeout_ms`, and
  `local_sweep_shadow_allow_gm_a9_packet`;
- cloud `reverse_server` `sweep.config.warning`:
  `failure_code=local_sweep_shadow_read_timeout_zero` means
  `shadow_local` / `active_replay` was enabled with
  `local_sweep_read_timeout_ms=0`, so focused shadow-tail validation is not
  active for that run;
- local `reverse_client.lifecycle.auth_succeeded` reason:
  `read_ahead=1`, `read_collect=1`, `write_collect=1`, and `sweep_shadow=1`;
- cloud `tunnel.auth.accepted`:
  parsed `client_capabilities`, returned `vci_capabilities`, and the booleans
  `read_ahead_enabled`, `read_collect_enabled`, `write_collect_enabled`, and
  `sweep_shadow_supported`; use this event to confirm the server actually
  accepted `sweep_shadow=1` instead of inferring from the client-side auth
  response string alone;
- transaction activity:
  `proxy.request.forwarded_to_tunnel` with
  `reason=write_collect_transaction` or, for non-blocking foreground reads with
  tail collection enabled, `reason=read_collect_transaction`;
- local read-ahead collection completion:
  `read_ahead.collection_finished` includes `attempted_reads`,
  `collected_messages`, `soft_max_reads_after_data`, and stop reasons such as
  `soft_max_reads_after_data`, which indicate that adaptive write-collect ended
  before the deeper hard cap after capturing data;
- slow-link guard activity:
  `read_ahead.transaction.guard_armed` plus forwarded writes with
  `reason=write_collect_guarded_no_collect`;
- local sweep observe/inventory:
  `sweep.pattern.observed`, `sweep.pattern.learned`,
  `sweep.inventory.signature`, and `sweep.inventory.summary`;
- skipped GM A9 shadow plans:
  `sweep.plan.skipped` with `reason=gm_a9_packet_observe_only`;
- other skipped shadow starts:
  `sweep.plan.skipped` with `reason=shadow_transport_disabled`,
  `vci_sweep_shadow_not_supported`, `shadow_plan_start_pending`, or
  `no_learned_items`;
- actual local shadow execution:
  `sweep.plan.started`, `sweep.batch.drained`, and `sweep.shadow.*`.

Latest known interpretation:

- absence of `read_ahead.transaction.guard_armed` is expected when no
  non-cache-hit tunnel response reaches the startup-configured threshold for
  that run; new builds default that guard to `400ms`, but older logs may show
  the previous `750ms` threshold;
- a GM A9-only plan skipped by `gm_a9_packet_observe_only` means local shadow
  execution did not run and therefore could not improve or harm the run through
  extra local polling;
- no `j2534_disconnect`, no `proxy.j2534.cadence_gap`, no
  `proxy.request.timeout`, and no `tunnel.probe.failure` during Data Display is
  evidence that the current guarded stack was stable for that test window;
- if focused `Engine Speed`, `Accelerator Pedal Position`, and
  `Battery Voltage` samples remain constant, the logs cannot prove
  value-level freshness improvement. A changing-value run is required
  before judging visible lag.

### Write-Side Crossing Reduction Evidence

For the next Proxy J2534 latency workstream, observability must prove the
write-side path in this order:

1. `observe_only` / inventory proof:
   - `sweep.inventory.summary` shows non-GM-A9 replay-candidate coverage under
     `sweep_inventory_replay_candidate_request_count_by_kind`;
   - `sweep.inventory.summary` also surfaces `sweep_inventory_verdict`,
     `sweep_inventory_next_step`, `sweep_inventory_non_gm_replay_candidate_*`,
     and the top non-GM candidate fields so pasted logs can be triaged quickly;
   - `sweep.inventory.signature` shows repeated `uds_did` or `obd_pid`
     signatures with `sweep_inventory_signature_verdict`,
     `sweep_inventory_signature_next_step`, successful write/read pairs, and
     projected write RTT savings;
   - GM `A9 81 xx` remains visible only as observe-only / inventory-only.

2. `shadow_local` fidelity proof:
   - `sweep.plan.started` and `sweep.batch.drained` appear for the target
     non-GM-A9 signature;
   - repeated `sweep.shadow.match` appears after startup, with
     `sweep_shadow_clean_match_streak >= sweep_replay_min_clean_matches` and
     `sweep_shadow_replay_gate=ready_for_active_replay_canary`;
   - the latest compared shadow result is fresh, success-coded, and non-empty;
   - persistent `sweep.shadow.missing`, `sweep.shadow.stale`,
     `sweep.shadow.mismatch`, or `sweep.shadow.error` blocks replay, with the
     gate field explaining the next step.

3. `active_replay` canary proof:
   - `proxy.request.active_replay_armed` and
     `proxy.request.active_replay_served` appear only for the same proven
     non-GM-A9 signature, and include `replay_gate=ready_for_active_replay_canary`;
   - forwarded `WRITE_MSGS_REQ` count for that signature decreases compared
     with the shadow-only baseline;
   - `sweep.did.cadence` for the target DID improves, and focused collector
     lag improves when value-level samples exist.

Do not treat absence of `active_replay_*` as a bug when the page is dominated
by GM `A9 81 xx`, when no non-GM-A9 shadow plan started, or when the latest
shadow generation has not passed the clean-match gate.

## Proxy Local Live Data Fallback Observability

If the write-side replay lane fails for a GDS2 Data Display page, the next
candidate product lane is a separate proxy-local live-data stream. Observability
for that lane must make the source boundary explicit: these values are not
GDS2 Data Display rows and are not proof that the native GDS2 page refreshed
faster.

As of the 2026-05-19 Engine Speed decision, this lane is the primary next
implementation target for Engine Speed. The first implementation pass stayed
local-only. MVP 2 adds tunnel push, a cloud latest cache, and a guarded latest
endpoint while still deferring cloud SSE fan-out and UI display.
See `agent_docs/ops/proxy_local_live_data_fallback.md`.

Required source labels:

- `gds2_agent`: value came from the Java Agent scanning GDS2 Data Display and
  therefore inherits GDS2's parameter names, units, and OEM decode semantics;
- `proxy_local_obd`: value came from a standard OBD Mode 01 PID decoded by the
  local proxy allowlist;
- `proxy_local_known_uds`: value came from an explicitly validated non-GM-A9
  UDS DID decoder;
- `proxy_local_observe_only`: raw or correlated payload evidence only; not a
  product decoded value.

Current signal boundary as of 2026-05-25:

- Proxy Local product decoding is implemented only for
  `signal_key=engine_speed`.
- Latest local raw logs show `proxy.local_live_data.sample` /
  `proxy.local_live_data.summary` for `Engine Speed` with
  `source=proxy_local_known_uds` and
  `decoder_id=uds_did_000c_engine_speed`.
- GDS2/Java Agent focused value events may monitor `Engine Speed`,
  `Accelerator Pedal Position`, and `Battery Voltage`, but those are
  `gds2_agent` path values, not Proxy Local raw decoder outputs.
- Proxy payload candidate fields and sweep inventory hints remain correlation
  evidence only until a decoder is allowlisted and covered by tests.

The proxy-local stream should emit value-level events that are easy to paste
and classify, for example `proxy.local_live_data.sample` and
`proxy.local_live_data.summary`. Each sample should include:

- `signal_key`, display name, numeric value, unit, and source label;
- local sample timestamp, cloud receive timestamp, and `sample_age_ms`;
- request signature (`obd_pid` or `uds_did`), decoder id, return code, and
  poll duration;
- foreground-priority/backoff fields showing whether GDS2 traffic was allowed
  to run first;
- negative-response, timeout, or unsupported-signal reasons when no value is
  emitted.

For Engine Speed MVP validation, logs must answer these questions without
needing raw payload reconstruction:

- did the vehicle support a safe source such as OBD `01 0C`, or a validated
  non-GM-A9 UDS DID;
- did decoded RPM change when the real vehicle RPM changed;
- did `sample_age_ms` stay near the local-feel target (`<= 1000-2000ms` p95);
- did GDS2 stay connected and avoid Data Display freeze/disconnect events;
- were any samples sourced from GM `A9 81 xx` active polling. The expected
  answer is no under the current design.

MVP 0 observability acceptance:

- local raw logs contain `proxy.local_live_data.sample` with
  `signal_key=engine_speed`, `value`, `unit=RPM`, `source`, `decoder_id`,
  `sample_age_ms`, `return_code`, and redacted `raw_prefix_hex`;
- `%APPDATA%\VCI_Proxy\live_data\latest.json` contains the latest local Engine
  Speed sample for quick operator-side inspection;
- `proxy.local_live_data.summary` reports sample count, p50/p95/max sample age,
  poll duration, unsupported/timeout/negative-response counts, and foreground
  priority pause counts;
- cloud/session logs still show GDS2 stability separately from the proxy-local
  stream.

MVP 2 observability acceptance:

- local auth success includes `local_live_data=1` only after server ack;
- cloud `tunnel.auth.accepted` includes `local_live_data_supported=true`;
- local `proxy.local_live_data.sample` includes `client_sample_seq`;
- local `proxy.local_live_data.tunnel_sample_sent` appears after the tunnel
  frame write succeeds and records `client_sample_seq`, `signal_key`, `source`,
  `decoder_id`, `value`, `unit`, and `local_send_ts`;
- local `proxy.local_live_data.tunnel_send_failed` is absent during healthy
  runs;
- cloud `proxy.local_live_data.cloud_sample_received` records
  `cloud_received_ts`, `cloud_received_age_ms`,
  `local_to_cloud_clock_delta_ms`, `session_id`, `connection_epoch`,
  `live_data_active_at_receive`, `source`, `decoder_id`, `value`, `unit`, and
  `proxy_local_latest_path`;
- cloud `proxy.local_live_data.cloud_sample_dropped` records invalid payloads
  or missing capability negotiation;
- cloud latest cache is stored under
  `live_data\proxy_local_latest.json` below the cloud observability root and
  uses schema `proxy.local_live_data.cloud_latest.v1`.
- reverse-server `process.lifecycle.started` records `product_log_cloud_root`,
  `programdata`, `proxy_local_latest_path`, and
  `proxy_local_session_state_path` so cloud/API root mismatches can be
  diagnosed from pasted logs;
- the guarded latest endpoint returns `checked_paths` with
  `reason=no_sample_cache`, including the configured `PRODUCT_LOG_CLOUD_ROOT`,
  current `%PROGRAMDATA%` cloud root, and Windows fallback cloud root when
  applicable.

## Real-Vehicle Engine Speed Freshness Handoff - 2026-05-13

For the next real-vehicle test, `Engine Speed` is the primary freshness signal.
The battery-voltage rows remain useful for ECU bench validation, but they are
not the primary real-vehicle proof for this run.

Required analysis posture:

- prefer raw observability and assembled session traces over legacy text logs;
- confirm the build was running with read-ahead / read-collect / write-collect
  enabled before interpreting latency;
- treat GM `A9 81 xx` as observe-only / inventory-only. Do not infer that
  `active_replay` helped unless raw logs contain
  `proxy.request.active_replay_armed` and
  `proxy.request.active_replay_served` for a safe, non-GM-A9 path;
- if `Engine Speed` does not change by a meaningful amount, report the run as
  inconclusive for value freshness rather than claiming success or failure.

Focused report command:

```powershell
python scripts/analyze_battery_voltage_freshness.py `
  --cloud-root "C:\Users\shsww\projects\RPA_demo\vci_proxy\cloud_mirror" `
  --focus-key engine_speed `
  --min-delta 100 `
  --json reports/engine_speed_freshness.json `
  --report reports/engine_speed_freshness.md
```

Expected evidence in a useful run:

- `agent.collector.focus_value_changed` events where
  `focus_key=engine_speed`;
- nearby tunnel and FIFO events showing whether RPM changes followed FIFO hits,
  underfill merges, tunnel reads, read-collect, write-collect, or guard activity;
- complete or at least raw-backed session trace artifacts under the cloud
  observability root.

## Current Component Usage

- `server/app.py`
  - generates per-request `request_id`
  - writes structured API request completion events
- `server/api/session_log_handlers.py`
  - ingests local observability artifacts uploaded from the tray client
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
  - emits focused value-level Data Display samples for `Engine Speed`,
    `Accelerator Pedal Position`, and `Battery Voltage`; when
    `Battery Voltage` is present, same-module voltage rows with the same
    value/unit are emitted under the same focus key for correlation
  - focused freshness analysis can target any emitted focus key; use
    `scripts/analyze_battery_voltage_freshness.py --focus-key engine_speed
    --min-delta 100` for real-vehicle Engine Speed validation and the default
    `battery_voltage` mode for ECU bench voltage validation
  - current explicit `battery_voltage` aliases include OEM-specific names such
    as `Ignition 1 Signal` and `Engine Controls Ignition Relay Feedback 2 Signal`
  - `parameter_value_sources` identifies which parameter name supplied the
    primary value for each focused key
- `diagnostic_platform/observability_artifacts.py`
  - uploaded local artifacts are written as bytes separately from their JSON
    manifests; manifests include `artifact_size_bytes` and `artifact_sha256`.
    Cleanup only recovers atomic temp files for JSON targets when the temp file
    parses as complete JSON, so compressed raw artifacts cannot be promoted into
    `*.manifest.json`.
- `vci_proxy/reverse_server.py`
  - emits tunnel lifecycle, probe, tunnel-quality, proxy-request staged events, and reverse-server process lifecycle events
  - reverse-server process lifecycle events include read-cache/read-ahead settings plus local sweep mode, `local_sweep_allow_gm_a9_packet`, `local_sweep_shadow_allow_gm_a9_packet`, and `local_sweep_min_item_interval_ms` when reporting startup configuration
  - when shadow transport is enabled with `local_sweep_read_timeout_ms=0`, reverse server emits `sweep.config.warning` with `failure_code=local_sweep_shadow_read_timeout_zero` and `local_sweep_validation_blocked=true`; treat that run as unable to validate bounded shadow tail reads until the cloud reverse server is restarted with a positive `VCI_PROXY_LOCAL_SWEEP_READ_TIMEOUT_MS`
  - proxy-request events for `READ_MSGS_REQ` include decoded request metadata (`channel_id`, `num_msgs`, `timeout`) and, when applicable, `last_write_seq` plus `post_write_age_ms`
  - read-ahead FIFO decisions add `prefetch_fifo_pending_before`, `prefetch_fifo_pending_after`, `prefetch_requested_count`, `prefetch_served_count`, and `prefetch_underfill_count`; when FIFO data is served they also include `prefetch_source_counts` plus `prefetch_age_min_ms`, `prefetch_age_avg_ms`, and `prefetch_age_max_ms` so local-simulated-cloud runs can show whether hits came from `read_collect` or `write_collect` and how long frames waited before DLL consumption; `prefetch_miss` decisions include `prefetch_miss_detail`, read-collect eligibility/block reason, empty-cache state, and last prefetch record/drain/real-read age fields so miss clusters can be separated into no prior prefetch, exhausted FIFO, recent confirmed empty, or unavailable read-collect cases; partial FIFO fallback uses `reason=prefetch_underfill_forwarded`, then response/reply events use `prefetch_merge_tunnel_data`, `prefetch_merge_tunnel_empty`, or `prefetch_underfill_tunnel_error`
  - empty-cache state includes adaptive active-TTL fields such as `empty_cache_recent_empty_gap_ms`, `empty_cache_empty_gap_sample_count`, and `empty_cache_adaptive_ttl_applied` when repeated real `ReadMsgs(BUFFER_EMPTY)` replies have raised the effective active TTL above the configured base TTL
  - oversized non-blocking FIFO partial hits are merged with a reduced tunnel
    read when read-collect is available, using
    `reason=prefetch_underfill_read_collect_transaction` on the forwarded
    request and `prefetch_merge_tunnel_*` on the response. The legacy
    `reason=prefetch_partial_hit` plus `prefetch_partial_direct=true` remains a
    fallback only when the local client cannot run read-collect.
  - transaction-wrapped non-blocking read collection uses `reason=read_collect_transaction` on the forwarded `READ_MSGS_REQ`, strips the internal prefetch bundle before replying to the DLL, and records any extra local tail frames into the same consume-once FIFO; this tail probe also runs after a foreground `BUFFER_EMPTY`, so the DLL still receives the real empty response while immediately-following frames can be consumed once from FIFO by the next serial read; when a tail probe confirms a local `ReadMsgs(BUFFER_EMPTY)` boundary, response events include `prefetch_empty_confirmation_count` and `prefetch_empty_cache_recorded` if that boundary was converted into a short-lived empty-cache entry instead of FIFO data; when `VCI_PROXY_READ_AHEAD_MIN_DRAIN_MS` is configured, read-tail collection reports a capped `min_drain_ms` of at most `8` so logs can separate short foreground read-tail probing from the deeper post-write drain window
  - read-collect transaction budget fields include `read_collect_budget_reason`,
    `read_collect_budget_deepened`, `read_collect_collect_window_ms`,
    `read_collect_max_reads`, `read_collect_read_timeout_ms`, and
    `read_collect_max_messages`; the server keeps the standard foreground
    read-tail path light, but can temporarily deepen it after a recent FIFO data
    record or FIFO exhaustion so the next serial oversized non-blocking
    `ReadMsgs` is less likely to immediately miss after a partial FIFO hit.
  - proxy-request response events for `READ_MSGS_RSP` include `return_code`, `message_count`, `payload_bytes`, `read_result` (`empty` or `data`), redacted payload digest/prefix samples, and read-payload change markers
  - `WRITE_MSGS_REQ` request events include `channel_id`, `write_message_count`, `timeout`, `write_payload_bytes`, and redacted payload digest/prefix samples without logging full raw payload data
  - read-ahead transaction guard events include `read_ahead.transaction.guard_armed`; while active, forwarded write events record `reason=write_collect_guarded_no_collect` plus guard fields such as `read_ahead_transaction_guard_active`, `read_ahead_transaction_guard_reason`, `read_ahead_transaction_guard_remaining_ms`, `read_ahead_transaction_max_network_ms`, and `read_ahead_transaction_cooldown_ms`
  - manual GDS2 proxy events for `READ_MSGS_REQ` and `WRITE_MSGS_REQ` include per-channel `live_inter_request_gap_ms` / `live_inter_request_gap_bucket`; gaps at or above `1000ms` also emit `proxy.j2534.cadence_gap`
  - observed 11-bit GM CAN-ID-prefixed payload samples in the `0x500..0x7FF` range add `can_id`, `can_id_hex`, `can_payload_length`, and `can_payload_prefix_hex`; strict GM `A9 81 xx` request samples also add `gm_request_service_id`, `gm_request_subfunction`, `gm_request_packet_id`, and `gm_request_packet_id_hex`, while `0x500..0x5FF` response samples add `gm_data_packet_id` and `gm_data_packet_id_hex`
  - payload samples include a best-effort `*_engine_speed_candidate_rpm` only when standard `41 0C` or `62 F4 0C` Engine Speed response patterns are visible; treat this as a correlation hint, not a protocol guarantee
  - local sweep observe/shadow events include `sweep.pattern.*`, `sweep.inventory.signature`, `sweep.inventory.summary`, `sweep.plan.deferred`, `sweep.plan.started`, `sweep.plan.superseded`, `sweep.plan.skipped`, `sweep.batch.drained`, and `sweep.shadow.*`; `sweep.plan.superseded` records safe learned-signature set expansion while a shadow plan is already active, `sweep.plan.skipped` records guardrails such as default GM `A9 81 xx` observe-only handling, `sweep.shadow.not_ready` marks plan-pending/startup/no-drained-result windows, while `sweep.shadow.missing` is reserved for comparable windows where a matching shadow result is absent
- `sweep.inventory.signature` records per-signature observed write count, learned/replay-candidate status, shadow eligibility, eligibility reasons, read data/empty/error counts, cache-hit/forwarded read counts, write/read/pair duration and network p50/p95/max, and projected write/pair RTT savings; `sweep.inventory.summary` records aggregate signature/request counts by kind, rejected write counts by reason, learned and replay-candidate coverage percentages, and total projected RTT savings. Under the current GM A9 rollback, GM `A9 81 xx` stays observe-only / inventory-only and is no longer counted as a replay candidate. These events are observability-only and do not enable active replay.
  - shadow comparison events include plan state fields such as `sweep_plan_active`, `sweep_plan_pending`, `sweep_store_pending_count`, `sweep_signature_in_active_plan`, and `sweep_shadow_missing_reason` / `sweep_shadow_not_ready_reason` when applicable
  - `sweep.shadow.*` comparison events now include `sweep_shadow_clean_match_streak` and `sweep_replay_min_clean_matches`; `proxy.request.active_replay_armed` / `proxy.request.active_replay_served` include the same replay-quality gate fields when replay is actually permitted
- `vci_proxy/reverse_client.py`
  - emits reverse tunnel connection lifecycle, request receipt, and J2534 call events
  - local read-ahead collection emits `read_ahead.collection_finished` with attempted/data/empty read counts, collected message counts, effective budgets including `local_max_reads`, whether one-empty tail stop was enabled, whether the after-data empty-read grace was used, whether it consumed the one extra local read attempt (`empty_after_data_grace_extra_read_used`), whether that grace read then used a one-probe data boundary confirmation (`empty_after_data_grace_data_extra_read_used`, `empty_after_data_grace_data_extra_read_attempts`, `empty_after_data_grace_data_extra_read_limit`), whether data-at-budget continuation probes were used (`extra_read_after_data_at_max_used`), their `extra_read_after_data_at_max_attempts` / `extra_read_after_data_at_max_limit`, `empty_after_data_grace_sleep_ms`, and the stop reason
  - after-data empty-read stop reasons distinguish a real grace retry
    (`empty_after_data_grace_empty`) from ordinary budget/window termination;
    when normal read-tail `max_reads` is exhausted after tail data was already
    collected, the client can spend one extra local read attempt before
    stopping. If that extra grace read returns data, one narrower boundary
    confirmation can stop with `empty_after_data_grace_data_extra_empty` or
    `empty_after_data_grace_data_extra_limit`.
  - data-at-budget stop reasons distinguish the bounded extra
    data-continuation probes, currently capped at `2`, that still found data
    (`extra_read_after_data_at_max_limit`) from one that found the burst
    boundary (`extra_read_after_data_at_max_empty`)
  - local sweep executor logs distinguish plan start/stop, shadow item execution, foreground invalidation, configured/effective shadow item interval, shadow read timeout, bounded echo-only tail-read attempts, and error count; cacheable/read-only IOCTL foreground calls pause through the shared driver lock without emitting a shadow stop
- `vci_proxy/j2534_worker.py`
  - emits worker lifecycle, spawn status, RPC receipt/return/failure, and propagates `worker_request_id`
- `vci_proxy/tunnel_quality.py`
  - reuses shared UTC timestamp formatting from the observability module
- `vci_proxy/client_gui.py`
  - uploads local observability artifacts to the cloud
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
