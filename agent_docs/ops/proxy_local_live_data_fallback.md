# Proxy Local Live Data Fallback

## Document Role

| Field | Content |
| --- | --- |
| Type | Product fallback design / implementation handoff |
| Status | Selected as the primary next lane for Engine Speed after 2026-05-19 real-vehicle shadow tests |
| Owner scope | Local-side decoded live values that are separate from native GDS2 Data Display replay |
| Primary code paths | `vci_proxy/reverse_client.py`, `vci_proxy/sweep_executor.py`, `vci_proxy/local_live_data.py` |
| Related docs | `agent_docs/ops/proxy_j2534_latency_optimization.md`, `agent_docs/ops/proxy_j2534_local_sweep_scheduler.md`, `agent_docs/ops/vci_proxy_and_tunnel.md`, `agent_docs/ops/product_observability.md` |

## Decision - 2026-05-19

Use `Proxy Local Live Data` as the primary next implementation lane for Engine
Speed freshness. Keep the current GDS2 replay/write-side crossing reduction
lane as a lower-priority research lane, not the next product delivery path.

The latest real-vehicle focused `UDS DID 0x000C` runs changed the decision:

- `READ_TIMEOUT_MS=5` proved the focused config was active, but did not produce
  stable `sweep.shadow.match` before session abort.
- `READ_TIMEOUT_MS=15` improved local capture substantially: the local sweep
  frequently captured `0x000C` response frames, and `sweep.shadow.match`
  appeared twice before abort.
- The same `15ms` run still failed the replay gate: `0x000C` had only
  `clean_match_streak=1`, repeated `sweep.shadow.mismatch`, multiple plan
  cancellations, and no `active_replay_armed` / `active_replay_served`.
- The mismatches were mostly dynamic-value drift, not a simple local read
  timeout failure. Engine Speed is changing fast enough that raw payload exact
  equality is a poor proof model for serving GDS2.
- Continuing replay safely would require semantic comparison, freshness
  windows, and value tolerance before even testing DLL-facing serving. That is
  a larger risk surface than the product needs for a small fast Engine Speed
  display.

Therefore, do not spend the next implementation pass tuning
`VCI_PROXY_LOCAL_SWEEP_READ_TIMEOUT_MS` or enabling `active_replay` for Engine
Speed. Build a separate decoded local live-data stream first.

## Product Boundary

This fallback does not make native GDS2 Data Display refresh faster.

It adds a second value source for a small allowlist of known safe signals:

```text
vehicle-side J2534 / VCI
  -> local VCI Proxy live-data collector or existing local sweep result
  -> allowlisted decoder
  -> local live sample file and structured observability
  -> later: reverse tunnel value event
  -> later: cloud/session latest-value cache and UI panel
```

The existing Java Agent / GDS2 Data Display path remains the source for complete
OEM page rows:

```text
GDS2 Data Display
  -> Java Agent latest.json
  -> cloud/session SSE
  -> product Live Data table
```

The proxy-local stream must expose its source, value age, and decoder id so the
operator can distinguish a complete GDS2-decoded row from a proxy-local decoded
fast signal.

## Safety Rules

- Never synthesize DLL-visible `WRITE_MSGS_RSP` or `READ_MSGS_RSP` for GDS2 in
  this fallback.
- Do not use GM `A9 81 xx` as an active polling or decoded product source.
  It remains observe-only / inventory-only under current evidence.
- Decode only allowlisted standard or explicitly validated signals.
- Foreground GDS2 traffic has priority. A local collector must pause, back off,
  or stop when foreground traffic, J2534 errors, Data Display recovery, filter
  changes, disconnects, or negative responses indicate instability.
- Keep the first implementation disabled by default and reversible through
  environment/config.

## MVP 0: Local Decoder And Local Output

The first implementation should not change the tunnel protocol or product UI.
It should prove that the local side can produce semantic Engine Speed samples
from local J2534 response frames.

Scope:

- Add a small decoder module, preferably `vci_proxy/local_live_data.py`.
- Decode Engine Speed from known response shapes:
  - standard OBD Mode 01 PID `0x0C`: `41 0C A B`
  - validated CAN-ID-prefixed UDS DID `0x000C`: `62 00 0C A B`
- Formula: `rpm = ((A * 256) + B) / 4`.
- Emit a structured local observability event:
  `proxy.local_live_data.sample`.
- Write a local latest-value snapshot, for example:
  `%APPDATA%\VCI_Proxy\live_data\latest.json`.
- Source may initially be existing local `shadow_local` sweep results for
  `UDS DID 0x000C`; this is acceptable for decoder proof, but the event must
  say `source=proxy_local_known_uds` and must not imply GDS2 was accelerated.

Recommended sample fields:

```text
signal_key=engine_speed
display_name=Engine Speed
value=...
unit=RPM
source=proxy_local_obd | proxy_local_known_uds
decoder_id=obd_mode01_pid_0c | uds_did_000c_engine_speed
sample_ts=...
sample_age_ms=...
raw_prefix_hex=...
return_code=...
message_count=...
poll_duration_ms=...
foreground_priority_state=...
backoff_reason=...
j2534_return_code_warning=...
j2534_return_code_warning_reason=...
```

If a J2534 read returns a timeout-like non-zero code but includes an
allowlisted, decodable Engine Speed payload, MVP 0 should still emit
`proxy.local_live_data.sample` and preserve the non-zero return code as a
warning field. Treat non-zero return codes as backoff only when no supported
payload was captured.

MVP 0 success means:

- the local log contains changing `proxy.local_live_data.sample` Engine Speed
  values during a real-vehicle run;
- the sample stream is continuous enough for analysis, with p95 sample age
  near the local-feel target (`<= 1000-2000ms`);
- GDS2 remains stable;
- no sample is sourced from active GM `A9 81 xx` polling.

MVP 0 failure means:

- no safe `0x000C` response shape appears;
- decoded RPM does not track real RPM changes;
- sample age is still multi-second;
- or the local source interferes with GDS2.

## MVP 1: Independent Local Collector

After MVP 0 proves the decoder and local output, add an independent local
collector that can poll a tiny allowlist without relying on the GDS2 Data
Display page to learn a shadow plan.

MVP 1 should still be local-first:

- use a disabled-by-default config flag such as
  `VCI_PROXY_LOCAL_LIVE_DATA=1`;
- start with one signal: Engine Speed;
- use the validated non-GM-A9 CAN-ID-prefixed UDS DID `0x000C` request shape
  first: `00 00 07 E0 22 00 0C`;
- keep OBD `01 0C` as a later source only when the vehicle/session proves it is
  supported and safe;
- enforce a conservative poll interval, for example `250-500ms`;
- pause on foreground J2534 activity rather than competing with GDS2;
- log unsupported, timeout, negative-response, and backoff reasons.

Only after MVP 1 works should the project add cloud push/SSE and UI display.

Current MVP 1 local config:

```text
VCI_PROXY_LOCAL_LIVE_DATA=1
VCI_PROXY_LOCAL_LIVE_DATA_INTERVAL_MS=500
VCI_PROXY_LOCAL_LIVE_DATA_READ_TIMEOUT_MS=15
VCI_PROXY_LOCAL_LIVE_DATA_MAX_CONSECUTIVE_ERRORS=3
VCI_PROXY_LOCAL_LIVE_DATA_SOURCE=uds_did_000c
```

This config is independent from `VCI_PROXY_LOCAL_SWEEP_*`. The collector starts
only after a successful local `PassThruConnect` gives the proxy an ISO15765
channel (`protocol_id=6`). Non-ISO15765 channels emit
`proxy.local_live_data.unsupported` with `reason=unsupported_protocol` and do
not start collection. The collector runs through the same serialized
driver-call path as foreground GDS2 traffic. Stop requests set a local stop
flag and let any in-flight J2534 call finish before teardown, then stop on
disconnect, close, connection cleanup, or shutdown. It does not require
`shadow_local` and does not arm/serve `active_replay`.

## MVP 2: Tunnel Push And Cloud Latest Endpoint

MVP 2 makes the local Engine Speed sample visible to the cloud/session side
without changing GDS2, the virtual DLL, or Java Agent Data Display semantics.

Scope:

- the local client advertises `local_live_data=1` only when
  `VCI_PROXY_LOCAL_LIVE_DATA=1`;
- the cloud reverse server echoes `local_live_data=1` only after the client
  advertised that capability;
- the client sends a best-effort internal `LOCAL_LIVE_DATA_SAMPLE` tunnel frame
  only after that auth ack;
- the cloud reverse server writes an atomic latest cache at
  `%PROGRAMDATA%\RPA_Diagnostic\observability\cloud\live_data\proxy_local_latest.json`;
- the session runtime also writes a small cloud-visible activity ledger at
  `%PROGRAMDATA%\RPA_Diagnostic\observability\cloud\live_data\proxy_local_session_state.json`
  from `session.live_data.started`, live-data stop/error, and terminal session
  events, so the reverse-server process can associate pushed samples with the
  active live-data session even when `active_session_snapshot.json` is missing
  or stale;
- Flask exposes
  `GET /api/session/live_data/proxy_local/latest?session_id=...&max_age_ms=5000`.

The sample frame is unsolicited and internal. It is routed by message type
before pending response-future sequence matching, so it cannot be mistaken for
a DLL-visible J2534 response even if its sequence number collides with an
in-flight request.

The endpoint returns a sample only when all gates pass:

- session exists and supports `LIVE_DATA`;
- the session live-data stream is active in worker runtime;
- latest cache `session_id` matches the requested session;
- latest cache `connection_epoch` matches the active worker epoch when present;
- freshness is within `max_age_ms`, computed from cloud `cloud_received_ts`.

When receiving a `LOCAL_LIVE_DATA_SAMPLE`, the reverse server first uses
`active_session_snapshot.json` if it says live data is active for the current
connection epoch. If that snapshot is absent or reports live data inactive, it
falls back to `proxy_local_session_state.json` only when the state says live
data is active and the connection epoch matches. Terminal session events and
live-data stop/error events mark the ledger inactive, so post-abort samples
remain unassociated instead of being served through the session endpoint.

MVP 2 remains latest-value only. It does not add GUI, cloud SSE fan-out,
additional signals, active replay, DLL response synthesis, Java Agent snapshot
mutation, or GM `A9 81 xx` polling/decoding.

## UI Debug Display

After MVP 2 real-vehicle validation proves the guarded latest endpoint, the
local diagnostics window may show Engine Speed as a separate read-only
`Proxy Local` signal in the Live Data output area. This UI display reads:

```text
GET /api/session/live_data/proxy_local/latest?session_id=...&max_age_ms=5000
```

It must remain visibly separate from the native GDS2 Data Display table:

- display only Engine Speed;
- label the source as `Proxy Local`;
- show freshness/source/decoder metadata when available;
- clear or mark the value unavailable on stale, inactive, wrong-session,
  wrong-epoch, malformed, or endpoint-error responses;
- do not insert proxy-local samples into the GDS2-native live-data row table.

This UI proof still does not add cloud SSE fan-out, additional signals,
active replay, DLL response synthesis, Java Agent snapshot mutation, or GM
`A9 81 xx` polling/decoding.

## Observability Requirements

Use local observability first. Cloud observability can be added later when the
value stream is pushed through the tunnel.

Local events:

```text
proxy.local_live_data.sample
proxy.local_live_data.summary
proxy.local_live_data.backoff
proxy.local_live_data.unsupported
proxy.local_live_data.collector.started
proxy.local_live_data.collector.stopped
proxy.local_live_data.tunnel_send_failed
```

Summary fields should include:

- sample count;
- p50/p95/max `sample_age_ms`;
- p50/p95/max `sample_gap_ms`;
- p50/p95/max poll duration;
- latest value and latest source;
- unsupported/negative-response/timeout counts;
- foreground-priority pause count;
- decoder id counts.

When cloud push is implemented, add the cloud receive timestamp and cloud-side
age so pasted logs can distinguish local collection freshness from tunnel/UI
delivery freshness.

MVP 2 cloud events:

```text
proxy.local_live_data.cloud_sample_received
proxy.local_live_data.cloud_sample_dropped
```

MVP 2 cache fields:

- `schema_version=proxy.local_live_data.cloud_latest.v1`;
- `cloud_received_ts`;
- `connection_epoch`;
- `session_id`;
- `live_data_active_at_receive`;
- `source=proxy_local_live_data`;
- `latest_sample`;
- `signals.engine_speed`.

## Implemented Local Passes

MVP 0:

1. Add pure decoder helpers and unit tests.
2. Hook the decoder into local sweep item completion for DID `0x000C`.
3. Emit `proxy.local_live_data.sample` to local raw observability.
4. Write `%APPDATA%\VCI_Proxy\live_data\latest.json`.

MVP 1:

1. Add independent `VCI_PROXY_LOCAL_LIVE_DATA` config.
2. Start a disabled-by-default local collector after successful channel connect.
3. Poll only Engine Speed DID `0x000C` through the guarded local driver-call
   path.
4. Pause on foreground traffic and emit reason-specific backoff/summary events.
5. Stop collection on disconnect, close, connection cleanup, and shutdown.

MVP 2:

1. Add internal tunnel frame `LOCAL_LIVE_DATA_SAMPLE`.
2. Add `local_live_data=1` auth capability negotiation.
3. Push local Engine Speed samples through the tunnel only after ack.
4. Write cloud latest cache using the server connection epoch.
5. Add guarded Flask latest endpoint.
6. Keep source labels separate from native GDS2 Data Display values.

Do not implement `active_replay`, semantic replay tolerance, cloud SSE, or GUI
in these passes.

## Validation Run

Use a real vehicle and change RPM deliberately. A useful run should provide:

- `VCI_PROXY_LOCAL_SWEEP=0` so the test proves the collector is independent of
  `shadow_local`;
- `VCI_PROXY_LOCAL_LIVE_DATA=1`;
- local raw observability containing `proxy.local_live_data.sample`;
- `proxy.local_live_data.collector.started` followed by
  `proxy.local_live_data.collector.stopped` at disconnect/shutdown;
- cloud/session logs proving GDS2 stayed stable;
- Java Agent samples if available, for rough comparison only;
- no `active_replay_*` requirement, because this fallback is not replay.

Fields to inspect:

- `request_origin=local_live_data_collector`;
- `source=proxy_local_known_uds`;
- `decoder_id=uds_did_000c_engine_speed`;
- `sample_gap_ms`, `sample_gap_ms_p95`, and `sample_gap_ms_max`;
- `poll_duration_ms`, `poll_duration_ms_p95`, and `poll_duration_ms_max`;
- `foreground_priority_pause_count`;
- `timeout_count`, `negative_response_count`, `unsupported_count`;
- `return_code_warning_count`;
- `backoff_reason`;
- absence of `active_replay_armed`, `active_replay_served`, and GM `A9 81 xx`
  sample sources.

Additional MVP 2 fields to inspect:

- local `reverse_client.lifecycle.auth_succeeded` reason includes
  `local_live_data=1`;
- cloud `tunnel.auth.accepted` includes
  `local_live_data_supported=true`;
- local sample events include `client_sample_seq`;
- absence of `proxy.local_live_data.tunnel_send_failed`;
- cloud `proxy.local_live_data.cloud_sample_received` includes
  `session_id`, `connection_epoch`, `cloud_received_ts`,
  `cloud_received_age_ms`, `local_to_cloud_clock_delta_ms`,
  `source=proxy_local_known_uds`, and
  `decoder_id=uds_did_000c_engine_speed`;
- latest endpoint returns `success=true`, `available=true`,
  `source=proxy_local_live_data`, `epoch_match_status=matched`, and a fresh
  `latest_sample.value`;
- stale, inactive, wrong-session, or wrong-epoch checks return `409` rather
  than showing a misleading value.

Go criteria:

- decoded Engine Speed follows the same direction and approximate value as the
  real vehicle/GDS2 display;
- p95 sample age is near `<= 1000-2000ms`;
- no new Data Display freeze, repeated recovery, or tunnel disconnect appears
  during the collection window.

No-go criteria:

- only GM `A9 81 xx` can provide the value;
- local polling causes foreground instability;
- decoded values are wrong or stale;
- sample age does not materially beat GDS2 Data Display page refresh.
