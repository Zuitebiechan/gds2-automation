# Proxy J2534 Local Sweep Scheduler Design

## Document Role

| Field | Content |
| --- | --- |
| Type | Long-term optimization design |
| Status | Guarded `observe_only` implemented; `shadow_local` transport is available; `active_replay` is now an explicit experimental mode with a narrow exact-signature replay path, but latest ECU evidence requires GM `A9 81 xx` to stay observe-only / inventory-only in the current implementation |
| Owner scope | Cloud GDS2 Data Display freshness over the Proxy J2534 tunnel |
| Primary code paths | `vci_proxy/reverse_server.py`, `vci_proxy/reverse_client.py`, `vci_proxy/protocol.py`, `vci_proxy/j2534_worker.py` |
| Related docs | `agent_docs/ops/proxy_j2534_latency_optimization.md`, `agent_docs/ops/vci_proxy_and_tunnel.md`, `agent_docs/ops/product_observability.md` |

## One-Line Conclusion

The implemented first stage learns repeated read-only Data Display sweeps and can run a local shadow executor for comparison, while real GDS2 requests continue through the existing proxy/tunnel behavior. That means `shadow_local` is not itself a latency optimization; it is the safety proof needed before any future serve/replay path can reduce write-side tunnel crossings. Recent Engine Speed evidence shows the read-side transport stack still leaves multi-second focus-DID cadence, so the next useful work is to prove a narrow non-GM-A9 read-only sweep can match foreground results before any DLL-facing replay is considered. GM `A9 81 xx` remains observe-only / inventory-only.

## Reassessment - 2026-05-18

The local sweep scheduler is still the right architectural direction for the
remaining bottleneck, but the current implemented stage has not yet delivered
visible latency reduction.

Evidence from recent runs:

- real-vehicle Engine Speed tests showed healthy tunnel RTT but slow focus DID
  cadence (`~25.4s` p50 in one verdict and `~9.1s` p50 after later plan
  expansion), so the problem is page-sweep amplification rather than a single
  slow call;
- read-ahead and transaction wrapping can reduce read-side tunnel trips, but
  foreground GDS2 still sends one write per repeated DID across the tunnel;
- `shadow_local` can negotiate, start, execute, and drain in ECU tests, but it
  does not answer GDS2 requests and therefore cannot improve user-visible
  Data Display freshness by itself;
- the current ECU voltage scene mostly exercises GM `A9 81 xx` foreground
  traffic, which remains excluded from active shadow execution and replay
  because earlier evidence showed it can disturb Data Display semantics.

Implications:

- do not spend more effort tuning read-cache/read-tail/read-collect parameters
  unless a log points to a concrete read-side defect;
- the useful next validation target is a repeated non-GM-A9 UDS/OBD read-only
  signature that can produce real `sweep.shadow.match` evidence;
- only after clean shadow matches, acceptable result age, and stable GDS2
  behavior should a narrow replay/serve step be evaluated;
- repeating GM A9-dominated ECU voltage tests is useful for startup and
  observability regression checks, but not for proving Engine Speed freshness
  improvement.

## Write-Side Crossing Reduction Workstream

This is the next active design lane. The objective is not to add another
read-side collection probe; it is to prove that repeated foreground
`WRITE_MSGS_REQ` / `READ_MSGS_REQ` pairs for safe Data Display items can be
answered from fresh local sweep results without crossing the tunnel for every
item.

The workstream has three gates.

1. Inventory gate:
   - run `observe_only` or `shadow_local` with GM `A9 81 xx` still
     observe-only / inventory-only;
   - identify repeated `uds_did` or `obd_pid` signatures with real
     `READ_MSGS_RSP(data)`;
   - require useful coverage in `sweep.inventory.summary`, especially
     non-GM-A9 `sweep_inventory_replay_candidate_request_count_by_kind`;
   - reject scenes where the only learned traffic is GM `A9 81 xx`.

2. Shadow-match gate:
   - run `shadow_local` for one or a small set of non-GM-A9 signatures, using
     `VCI_PROXY_LOCAL_SWEEP_INCLUDE_UDS_DIDS` when narrowing is needed;
   - require `sweep.plan.started`, `sweep.batch.drained`, and repeated
     `sweep.shadow.match` for the same signature after the startup/not-ready
     window;
   - require the latest shadow generation to be fresh, success-coded,
     non-empty, and to satisfy the clean-match streak gate
     (`max(2, VCI_PROXY_LOCAL_SWEEP_MIN_CYCLES)`);
   - treat `sweep.shadow.not_ready` during plan startup separately from real
     `sweep.shadow.missing`, but do not advance on persistent missing, stale,
     mismatch, or error events;
   - verify GDS2 foreground behavior remains stable: no communication errors,
     no Data Display freeze, no new disconnects, and no foreground cadence
     gaps caused by local shadow execution.

3. Serve/replay canary gate:
   - only after the shadow-match gate passes, allow `active_replay` for exact
     learned non-GM-A9 signatures whose latest result is replay-ready;
   - synthesize only the matched write success and the immediately following
     read response for that channel;
   - fall back to the existing transaction/read-ahead/normal tunnel path on
     any stale result, generation drift, read-shape mismatch, unexpected
     request order, negative/empty result, or active-plan degradation;
   - prove crossing reduction with `proxy.request.active_replay_armed` and
     `proxy.request.active_replay_served`, plus a lower forwarded
     `WRITE_MSGS_REQ` count for the same signature;
   - prove product value with faster DID cadence and, where collector samples
     exist, materially lower focused value freshness lag.

For Engine Speed, this lane should prefer standard non-GM-A9 UDS/OBD evidence,
for example UDS `0x22` DID `0x000C` or OBD Mode 01 PID `0x0C`, when the page
actually emits those requests. A CAN-ID-prefixed GM `A9 81 xx` packet that
contains RPM-like data is still not an eligible active execution or replay
path under the current design.

## Exit Criteria And Fallback If Replay Is Not Viable

The local sweep scheduler should have a clear stop condition. If the target
Data Display page does not expose useful non-GM-A9 replay candidates, if
`shadow_local` cannot produce clean `sweep.shadow.match` evidence, or if
`active_replay` serves too little traffic to improve DID cadence, this
workstream should be paused for that page rather than expanded into unsafe GM
A9 execution or more read-side tuning.

In that case, the product fallback is `Proxy Local Live Data`: a separate
local-side live-data collector that does not serve GDS2 and does not try to
accelerate the native GDS2 Data Display page. It polls only a small allowlist
of known read-only signals locally, decodes only signals with known standard or
validated formulas, and streams value-level samples to the product UI.

This fallback is intentionally narrower than GDS2:

- J2534 provides raw messages, not GDS2 parameter names, units, or OEM decode
  semantics.
- Standard OBD Mode 01 PIDs can be decoded when supported. Engine Speed is the
  initial target via OBD `01 0C` (`41 0C A B` -> `((A * 256) + B) / 4` RPM).
- UDS `0x22` DIDs are eligible only when the DID and scale are explicitly
  known or have been validated against GDS2/local evidence.
- GM `A9 81 xx` remains observe-only / inventory-only and is not an active
  polling or decoder source for this fallback.

The fallback data path should be separate from replay:

```text
local J2534 worker / local VCI Proxy
  -> Proxy Local Live Data collector
  -> allowlisted decoder
  -> reverse tunnel value event
  -> cloud live-data event stream
  -> client Live Data panel
```

The current Java Agent page-scan stream remains the source of complete GDS2
Data Display rows. The proxy-local stream is a second source for a small fast
signal set, and the UI should expose source and value age so operators can
distinguish `gds2_agent` rows from `proxy_local_obd` or
`proxy_local_known_uds` rows.

Validation should prove product value directly:

- decoded Engine Speed follows real RPM changes;
- local collector sample-age p95 is near `<= 1000-2000ms`;
- GDS2 stays connected and Data Display does not freeze;
- active requests are read-only, non-GM-A9, and from the explicit allowlist;
- no DLL-visible GDS2 responses are synthesized.

## Current Implementation Stage

The current code implements two disabled-by-default modes:

- `observe_only`: cloud-side `reverse_server` classifies exact allowlisted UDS `0x22` ReadDataByIdentifier, OBD Mode 01 PID, and strict CAN-ID-prefixed GM `A9 81 xx` packet write requests, correlates them with later `READ_MSGS_RSP(data)`, and emits candidate count, confidence, cadence, rejection, request-inventory, coverage, and projected RTT-cost observability. It does not change protocol, reverse-client behavior, local runtime behavior, or local J2534 call counts.
- `shadow_local`: after observe learning and capability negotiation, the cloud waits a short configurable delay window (`VCI_PROXY_LOCAL_SWEEP_PLAN_DELAY_MS`, default `300ms`) before sending an internal sweep plan to the local reverse client. The delay lets signatures learned milliseconds apart join the first plan instead of starting from a one-item plan. The local client executes the plan serially through the same J2534 driver-call path used by foreground requests, queues shadow read results, and returns them only through server-driven status/drain control frames. Real `WRITE_MSGS_REQ` and `READ_MSGS_REQ` from GDS2 continue through the existing normal proxy path; local sweep does not synthesize replies or skip forwarding.
- if later observations learn additional safe signatures while a shadow plan is
  already active, the cloud emits `sweep.plan.superseded` and starts a larger
  replacement plan with `reason=shadow_plan_expanded` instead of leaving the
  active plan frozen at the first learned DID. The replacement still uses the
  same include/exclude filters and GM A9 observe-only guardrails.

The v1 shadow transport is server-driven and request/response shaped:

- `SWEEP_STATUS_REQ/RSP` checks local executor state.
- `SWEEP_DRAIN_RESULTS_REQ/RSP` returns immediately with queued shadow results or an empty result set.
- Unsolicited client-to-server result push and blocking long-poll drain are not implemented.
- When the cloud serves a DLL-visible write/read pair from `active_replay`, it must still schedule the next status/drain poll so the cloud-side `SweepShadowStore` keeps receiving fresh local results instead of aging out after one replay hit.

Shadow data is comparison-only. It is stored in `SweepShadowStore`, compared against normal GDS2-visible `READ_MSGS_RSP` bodies produced by the existing proxy path, and never consulted by `_try_serve_cached()`, never written to `PrefetchReadMsgsBuffer`, and never used to fulfill normal `READ_MSGS_REQ`.

For pasted-log triage, shadow comparison events now also emit:

- `sweep_shadow_replay_gate`
- `sweep_shadow_replay_ready`
- `sweep_shadow_replay_next_step`

Inventory data is also observability-only. `SweepInventoryTracker` records the
allowlisted request signatures seen during the foreground GDS2 stream, their
learned/replay-candidate status, shadow eligibility, counts by request kind,
rejection reasons, write/read network p50/p95/max, and the observed write/pair
RTT cost that could be removed by a future replay stage. Under the current GM
A9 rollback, `GM A9 81 xx` signatures remain visible in inventory but are no
longer counted as replay candidates. The tracker still does not decode the
meaning of GM data packets, does not serve responses, and does not enable
`active_replay`.

For pasted-log triage, inventory events now also emit:

- `sweep_inventory_verdict` and `sweep_inventory_next_step`
- `sweep_inventory_signature_verdict` and `sweep_inventory_signature_next_step`
- `sweep_inventory_non_gm_replay_candidate_request_count`
- `sweep_inventory_top_candidate_*`

The observe gate artifact for this stage is `.omx/plans/local-sweep-scheduler-observe-gate-signoff.md`.

## Validation Status - 2026-05-08

Latest ECU Data Display validation showed the current `active_replay` experiment
did not actually serve replayed DLL-facing responses, but local GM A9 shadow
execution still started and correlated with a visible page-behavior regression.

Observed evidence:

- cloud startup configuration logged `local_sweep_enabled=true`,
  `local_sweep_mode=active_replay`,
  `local_sweep_allow_gm_a9_packet=true`, and
  `local_sweep_shadow_allow_gm_a9_packet=false`;
- the run used session `4ba7b8ee031c4c1f`, connection epoch
  `epoch-1778208278843-001`, and entered Data Display at
  `2026-05-08T02:46:33Z`;
- the cloud started a local shadow plan before the main Data Display window and
  continued draining shadow batches during Data Display;
- cloud raw observability recorded `proxy.request.active_replay_armed=0` and
  `proxy.request.active_replay_served=0` for the run;
- cloud collector samples formed two plateaus instead of live in-page refresh:
  the first Data Display entry stayed at roughly `12.9V`, a later re-entry
  stayed at roughly `13.5V`, and there were no
  `agent.collector.focus_value_changed` events;
- `Engine Speed` and `Accelerator Pedal Position` stayed `0` throughout the
  sampled Data Display window, even while foreground proxy reads continued;
- foreground `READ_MSGS_REQ(data)` responses still arrived and their payload
  digests changed, so the tunnel and local worker were not simply idle;
- local shadow execution completed items with `return_code=18`; this maps to
  `ERR_NOT_UNIQUE` in the current J2534 error table and should be treated as an
  invalid shadow result, not replay-ready evidence;
- the visible page regression happened before the later tunnel EOF at
  `2026-05-08T02:49:37Z`; the disconnect was not the first failure during the
  frozen-value window.

Current interpretation:

- this run does not prove that `active_replay` itself served stale data, because
  replay never armed or served;
- it does show that local GM A9 shadow execution can disturb Data Display
  semantics even when foreground reads are still flowing and even before any
  DLL-facing replay occurs;
- for the current implementation, GM `A9 81 xx` must remain
  observe-only / inventory-only. Do not start GM A9 shadow execution or GM A9
  replay from this evidence alone;
- before any broader replay rollout, add hard quality gates so only
  `return_code == 0`, non-empty, comparison-clean shadow results can ever
  become replay-ready inputs.
- the current runtime now enforces those gates: GM A9 is excluded from both
  shadow-plan execution and replay candidacy, and replay-ready requires the
  latest shadow generation to satisfy a clean-match streak of
  `max(2, min_cycles)`.

## Validation Status - 2026-05-06

Latest ECU Data Display logs confirm that the observe/inventory path is active
and visible in observability, but the local shadow executor did not run because
the only learned signature in that run was a guarded GM `A9 81 xx` packet.

Observed evidence:

- cloud startup configuration: `local_sweep_enabled=true`,
  `local_sweep_mode=shadow_local`,
  `local_sweep_shadow_allow_gm_a9_packet=false`;
- local client auth capability: `sweep_shadow=1`;
- Data Display stayed connected for the tested window, with no
  `j2534_disconnect`, no `proxy.request.timeout`, no `proxy.j2534.cadence_gap`,
  and no `tunnel.probe.failure`;
- `sweep.pattern.observed=90`, `sweep.pattern.learned=1`,
  `sweep.inventory.signature=90`, and `sweep.inventory.summary=262`;
- the learned candidate was a strict GM `A9 81 xx` packet and emitted
  `sweep.plan.skipped` with `reason=gm_a9_packet_observe_only`;
- `sweep.plan.started=0` and `sweep.batch.drained=0`, so no shadow result
  fidelity conclusions can be drawn from this run.

Current interpretation:

- `shadow_local` mode is safe to leave configured only because GM A9 shadow
  execution is still blocked by default; in this state it behaves like
  observe/inventory for GM A9-only plans.
- The latest stable run should not be treated as proof that high-rate GM A9
  local shadow execution is safe. Earlier ECU runs correlated unguarded or
  aggressive GM A9 shadow behavior with frozen or disconnected Data Display
  windows.
- The next useful evidence is either a non-GM-A9 allowlisted signature that can
  run in `shadow_local`, or a deliberately bounded GM A9 shadow experiment after
  another stable observe-only baseline.

Do not implement or enable GM A9 shadow execution or replay from this evidence alone.

## Real-Vehicle Engine Speed Validation Handoff - 2026-05-13

The next planned run is a real-vehicle Engine Data / Data Display test using
`Engine Speed` changes as the value-level freshness proof. This run should not
be interpreted as a reason to re-open GM A9 shadow execution.

Validation posture:

- local sweep may remain configured only in the safe observe/inventory lane;
- GM `A9 81 xx` plans should be skipped or inventoried, not executed locally;
- `VCI_PROXY_LOCAL_SWEEP_SHADOW_ALLOW_GM_A9_PACKET=0` must remain in effect;
- a successful run should show meaningful `Engine Speed` changes, preferably
  filtered with `--focus-key engine_speed --min-delta 100`, and correlate those
  changes with read-ahead/read-collect/FIFO behavior;
- if `Engine Speed` remains constant, the run is inconclusive for freshness
  even if the tunnel appears stable.

Use this focused analysis command after collecting the logs:

```powershell
python scripts/analyze_battery_voltage_freshness.py `
  --cloud-root "C:\Users\shsww\projects\RPA_demo\vci_proxy\cloud_mirror" `
  --focus-key engine_speed `
  --min-delta 100 `
  --json reports/engine_speed_freshness.json `
  --report reports/engine_speed_freshness.md
```

When analyzing a new conversation, preserve the current conclusion: GM A9
active replay is not the current optimization lane; the working lane is
transport-layer reduction of serial `ReadMsgs` tunnel cost.

## Background

GDS2 Data Display does not let this project choose a tiny parameter subset from outside the application. Once the operator enters a Data Display page, GDS2 polls the parameters on that page. For Engine Data this can mean many DIDs in a stable loop.

Local GDS2 can still feel responsive because each J2534 call is near the VCI:

```text
GDS2 -> local J2534 driver -> VCI -> ECU
```

Cloud GDS2 through Proxy J2534 adds a tunnel to each synchronous call:

```text
Cloud GDS2
  -> virtual J2534 DLL
  -> cloud reverse_server local proxy
  -> reverse tunnel
  -> local reverse_client
  -> local J2534 worker / driver
  -> VCI -> ECU
```

Recent real-vehicle manual GDS2 logs showed this shape:

- the tunnel was not stalled; `proxy.j2534.cadence_gap` was `0`
- live J2534 inter-request gaps on the cloud side stayed below `500ms`
- `WRITE_MSGS_REQ` and `READ_MSGS_REQ` tunnel p95 latency stayed in the tens of milliseconds
- read-ahead was working, with most post-write reads served from the cloud FIFO
- the payload matching `CAN ID + UDS positive response + DID 0x000C` appeared roughly every `8-10s`

That evidence does not point to one broken `ReadMsgs` call. It points to the product of:

```text
GDS2 page-wide serial request count x added tunnel round-trip cost per request
```

The DID that likely corresponds to Engine Speed is not special-cased by the tunnel. It appears infrequently because it is one item in a wider polling loop. The cloud path makes each item in that loop more expensive, so the full loop gets longer and a single value is refreshed less often.

## Current Optimizations And Their Limit

The existing roadmap already implemented or planned these layers:

- post-write empty-read cache bypass
- adaptive empty-read caching
- local read-ahead after a write
- internal `WRITE_AND_COLLECT_READS_REQ` transaction

Those optimizations are useful and should remain. They mainly reduce the cost of:

```text
WRITE RTT + READ RTT
```

into:

```text
WRITE RTT + local read-ahead + cloud READ hit
```

The remaining cost is the cloud-to-local write transaction for every DID query. If a page has dozens of read-only DID queries, a single tunnel RTT per item can still produce multi-second page-refresh cycles.

The long-term scheduler addresses the remaining bottleneck by reducing how often GDS2's repeated read-only DID writes need to cross the tunnel at all.

Current validation has confirmed this limit in practice. The later Engine Speed
run still showed `~9.1s` p50 focus-DID cadence while tunnel RTT stayed in the
tens of milliseconds and replay never armed or served. This means read-side
optimization alone is not enough for local-feeling Data Display freshness.
Future work should therefore be judged by whether it removes or synthesizes
foreground write/read pairs for proven-safe repeated read-only signatures, not
by whether it adds another local read-tail probe.

## Goal

The goal is to preserve the synchronous J2534 API surface visible to GDS2 while moving the repeated read-only polling work to the local side.

Target behavior:

1. The cloud reverse server learns that GDS2 is running a stable Data Display sweep.
2. The cloud sends a guarded sweep plan to the local reverse client.
3. The local reverse client executes that read-only sweep near the VCI.
4. The local side streams compact result batches back to the cloud.
5. When GDS2 later issues a matching `WRITE_MSGS_REQ` / `READ_MSGS_REQ` pair, the cloud replies from fresh local sweep results instead of making a synchronous tunnel round trip.

This changes the tunnel cost model from:

```text
N parameters x (cloud-to-local WRITE + cloud-to-local READ)
```

or, after read-ahead:

```text
N parameters x cloud-to-local WRITE
```

to:

```text
background local sweep batches + cloud-side synchronous replay for matching GDS2 calls
```

## Non-Goals

This design must not:

- change the public J2534 API exposed to GDS2
- require controlling which parameters GDS2 displays
- cache arbitrary `PassThruReadMsgs` responses as reusable old data
- replay data for mutating diagnostic operations
- parallelize unknown J2534 driver calls
- hide communication errors from GDS2
- run by default before real-vehicle validation proves correctness

## Core Safety Rule

Only read-only, side-effect-free diagnostic requests may be locally scheduled ahead of GDS2.

Initial allowlist candidates:

- UDS `ReadDataByIdentifier` request: service `0x22`, for example `22 00 0C`
- OBD Mode 01 current-data PID request, for example `01 0C`
- Strict observed GM packet request: CAN ID prefix `0x7E0..0x7EF` plus
  `A9 81 xx`, for example `00 00 07 E0 A9 81 1A`. This is comparison-only
  until shadow logs prove response fidelity on the target vehicle/module.
- read-only equivalents that are proven by logs and protocol review

Initial denylist examples:

- `ClearDiagnosticInformation` / clear DTC
- `DiagnosticSessionControl`
- `ECUReset`
- `SecurityAccess`
- `CommunicationControl`
- `RoutineControl`
- `RequestDownload`, `TransferData`, `RequestTransferExit`
- `WriteDataByIdentifier`
- any request near filter mutation, channel reset, connection reset, or mutating IOCTL

If a request cannot be classified as read-only, it is forwarded through the current normal path.

## Architecture Overview

```text
Cloud reverse_server
  - observes GDS2's normal J2534 call stream
  - learns stable read-only sweep patterns
  - installs a sweep plan on the local reverse_client
  - receives local sweep result batches
  - matches later GDS2 calls to fresh local results
  - falls back to normal tunnel forwarding on mismatch or stale data

Local reverse_client
  - owns the local sweep executor
  - runs read-only request sequences through the existing J2534 worker
  - serializes scheduled calls with any normal J2534 calls
  - streams result batches to the cloud
  - stops immediately on plan cancel, driver error, or state mutation
```

GDS2 still sees:

```text
PassThruWriteMsgs -> PassThruReadMsgs -> PassThruWriteMsgs -> PassThruReadMsgs
```

Internally in the current stage, matching read-only pairs are compared with data that the local side already collected. They are not satisfied from that data yet.

## Key Concepts

### Request Signature

A request signature identifies one read-only diagnostic query.

Recommended fields:

```text
channel_id
protocol_id
tx_flags
payload bytes after normalizing J2534 wrapper fields
logical ECU target when visible in the payload
diagnostic service id
DID, PID, or GM packet id
filter_generation
connection_epoch
```

The matching rule must be exact for raw payload bytes after normalization. Observability may log only redacted digests and short prefixes, but runtime matching can use the full in-memory payload.

### Sweep Plan

A sweep plan is an ordered list of request signatures learned from GDS2's Data Display loop.

Recommended fields:

```text
plan_id
connection_epoch
channel_id
protocol_id
filter_generation
session_generation
request_signatures[]
min_item_interval_ms
max_cycle_interval_ms
max_result_age_ms
max_negative_response_age_ms
max_consecutive_errors
enabled_mode
```

`enabled_mode` currently accepts:

- `observe_only`
- `shadow_local`

`active_replay` is now accepted only as an explicit experimental mode. It uses
the same local sweep transport as `shadow_local`, but DLL-facing replay is
limited to exact learned signatures whose latest shadow generation is already
present, fresh, `return_code == 0`, non-empty when decoded as
`READ_MSGS_RSP`, and validated by the replay clean-match streak. Fallback to
the normal tunnel path remains the default when any replay precondition is
missing.

### Sweep Result

A sweep result is a local execution result for one request signature.

Recommended fields:

```text
plan_id
cycle_seq
item_index
request_signature_digest
started_at
finished_at
duration_ms
return_code
message_count
messages
error_name
negative_response_code
result_generation
```

Messages must preserve the J2534 response shape expected by existing `READ_MSGS_RSP` packing.

### Result Store

The cloud result store keeps the latest safe result per request signature plus a small bounded history.

Long-term recommended behavior:

- keep only fresh results within `max_result_age_ms`
- track `last_served_generation` per signature
- prefer a new generation over serving the same data twice
- allow a stale-but-safe fallback only if the configured mode explicitly permits it
- flush on channel/filter/session mutation

Current behavior:

- store only comparison records
- enforce freshness during shadow-vs-real comparison
- never return a `READ_MSGS_RSP` for normal serving
- flush on disconnect, close, filter mutation, non-cacheable/mutating IOCTL, failed write, connection epoch change, mismatch threshold, error threshold, max-seconds expiry, and communication-error escalation
- distinguish `sweep.shadow.not_ready` (plan pending/not started/no drained results yet) from true `sweep.shadow.missing` (comparison window exists but no matching result is available)

## Protocol Extension

Add internal message types that are never exposed to GDS2:

```text
SWEEP_PLAN_START_REQ
SWEEP_PLAN_START_RSP
SWEEP_PLAN_STOP_REQ
SWEEP_PLAN_STOP_RSP
SWEEP_STATUS_REQ
SWEEP_STATUS_RSP
SWEEP_DRAIN_RESULTS_REQ
SWEEP_DRAIN_RESULTS_RSP
```

### `SWEEP_PLAN_START_REQ`

Sent by the cloud reverse server to the local reverse client.

Payload:

```text
plan_id
connection_epoch
channel_id
protocol_id
filter_generation
session_generation
request_specs[]
min_item_interval_ms
max_cycle_interval_ms
max_result_age_ms
max_cycles_before_refresh
shadow_only
```

### `SWEEP_DRAIN_RESULTS_REQ`

Sent by the cloud reverse server to the local reverse client. It returns immediately and never waits for new data.

Payload:

```text
empty request body
response results[]
```

The current protocol is mostly server-request/client-response oriented. The implemented v1 choice is server-issued immediate result draining:

1. rejected for v1: true unsolicited client-to-server internal frames
2. implemented for v1: non-blocking `SWEEP_STATUS_REQ` plus immediate `SWEEP_DRAIN_RESULTS_REQ`
3. rejected for v1: blocking long-poll drain, because it can occupy the current reverse-client request loop

## Cloud-Side Components

### `SweepPatternLearner`

Responsibilities:

- observe `WRITE_MSGS_REQ` and subsequent `READ_MSGS_RSP(data)` pairs
- classify requests as allowlisted read-only candidates
- group candidates by channel and filter generation
- detect stable ordered loops across multiple cycles
- reject loops with mutating calls, unknown requests, frequent errors, or unstable order

Initial learning rule:

- require at least `2` complete matching cycles before proposing a plan
- require a minimum number of unique read-only items, for example `8`
- allow small missing windows only in `observe_only`; active replay should require high confidence

### `SweepPlanManager`

Responsibilities:

- own active plan lifecycle
- send start/stop messages to the local side
- defer first plan start briefly after the first learned item so newly learned same-burst signatures can join
- cancel plans on state mutation
- expose feature flags and runtime config
- keep one active plan per channel at first

Initial cancellation triggers:

- `DISCONNECT_REQ`
- `CLOSE_REQ`
- `START_FILTER_REQ`
- `STOP_FILTER_REQ`
- mutating or non-cacheable `IOCTL_REQ`
- pending plan start invalidation before the delay elapses
- unknown `WRITE_MSGS_REQ` inside an active replay window
- repeated signature mismatch
- result batch timeout
- local executor error

### `SweepResultStore`

Responsibilities:

- store fresh local results keyed by request signature
- enforce `max_result_age_ms`
- enforce generation checks
- keep comparison-only shadow results
- record match, mismatch, stale, missing, and error outcomes

Serving results to synthetic GDS2 read responses is not implemented in this stage.

### `SyntheticReplayGate`

Responsibilities:

- decide whether a GDS2 `WRITE_MSGS_REQ` can be synthetic-acknowledged
- decide whether a following `READ_MSGS_REQ` can be answered from the result store
- fall back to Phase 4 transaction or normal tunnel forwarding on any doubt

Write handling:

```text
if active_replay enabled
  and request signature matches expected plan item
  and the latest shadow generation is fresh, success-coded, non-empty, and
      already comparison-clean:
      do not forward WRITE_MSGS_REQ over the tunnel
      reply with a normal WRITE_MSGS_RSP success
      mark pending synthetic read context for this channel
else:
      use existing Phase 4 / Phase 3 / normal path
```

Read handling:

```text
if pending synthetic read context exists
  and a fresh matching sweep result exists
  and result generation is acceptable:
      reply with normal READ_MSGS_RSP carrying the local sweep messages
      clear pending synthetic read context
else:
      clear pending synthetic context
      use existing prefetch FIFO or normal tunnel read
```

This keeps the optimization local to known write/read pairs and avoids changing unrelated J2534 calls.

## Local-Side Components

### `LocalSweepExecutor`

Responsibilities:

- execute one plan sequentially through the existing J2534 worker
- use the same J2534 serialization lock as normal requests
- pause when normal foreground requests must be executed
- cancel on state-changing foreground requests; cacheable/read-only IOCTLs pause but do not cancel
- enforce rate limits
- collect result batches

The executor must not run concurrent driver calls with normal J2534 work unless that driver behavior is explicitly proven safe.

### `SweepScheduler`

Responsibilities:

- pace item execution
- avoid over-polling the ECU
- adapt to local worker duration and ECU response time
- stop on consecutive errors

Initial safety defaults:

```text
VCI_PROXY_LOCAL_SWEEP_MIN_ITEM_INTERVAL_MS=250
VCI_PROXY_LOCAL_SWEEP_MAX_CYCLE_HZ=2
VCI_PROXY_LOCAL_SWEEP_MAX_CONSECUTIVE_ERRORS=3
VCI_PROXY_LOCAL_SWEEP_MAX_RESULT_AGE_MS=1000
```

These values must be tuned with real-vehicle logs. The first active rollout should be conservative.

### `SweepResultPublisher`

Responsibilities:

- batch results for the cloud
- apply backpressure
- drop old superseded results when the cloud is behind
- expose queue depth and result age

For live data, the latest result per signature is usually more valuable than a long backlog.

## Matching And Fallback Rules

The system should prefer false negatives over false positives. Missing an optimization opportunity is acceptable; replaying the wrong response is not.

Use normal tunnel forwarding when:

- no active plan exists
- request signature is not allowlisted
- request signature does not match the current plan
- result is stale
- result belongs to an older filter or session generation
- result is a negative response not explicitly allowed
- message count or protocol flags look unexpected
- GDS2 issues a read without a matching synthetic write context
- local executor reports degraded state

On fallback, clear the pending synthetic context for that channel.

## State Invalidation

Flush sweep plans and result stores on:

- tunnel reconnect
- J2534 `CONNECT_REQ` / `DISCONNECT_REQ`
- `OPEN_REQ` / close-equivalent flow
- filter start/stop
- mutating IOCTL
- protocol/session control requests
- security access requests
- DTC clear or routine control
- repeated negative responses
- channel errors
- GDS2 request order drift beyond tolerance

The first implementation should use broad invalidation. Narrower invalidation can come later after logs show it is safe.

## Observability Requirements

Add structured events under `reverse_server` and `reverse_client`.

Cloud events:

```text
sweep.pattern.observed
sweep.pattern.learned
sweep.plan.started
sweep.plan.deferred
sweep.plan.cancelled
sweep.batch.drained
sweep.shadow.match
sweep.shadow.mismatch
sweep.shadow.stale
sweep.shadow.not_ready
sweep.shadow.missing
sweep.did.cadence
sweep.inventory.signature
sweep.inventory.summary
```

Local events:

```text
sweep.executor.started
sweep.executor.stopped
sweep.item.started
sweep.item.finished
sweep.executor.error
```

Important metrics:

| Metric | Purpose |
| --- | --- |
| per-DID observed cadence | prove whether Engine Speed refresh cycle changed |
| sweep inventory signature/request counts by kind | prove whether the Data Display page is mostly learnable without decoding every payload field |
| replay-candidate coverage percentage | estimate how much of the foreground GDS2 write stream a future replay stage could cover |
| projected write/pair RTT savings | estimate whether `J2534 serial request count x tunnel RTT` is still the dominant delay source |
| shadow match rate | prove local sweep fidelity before replay |
| shadow not-ready/missing/stale/mismatch counts | separate startup timing gaps from true freshness or correctness gaps |
| result age at serve time | prove data is fresh enough |
| fallback reason counts | identify unsafe or ineffective cases |
| local sweep cycle duration | compare to cloud GDS2 page cycle |
| local executor error rate | protect vehicle/driver behavior |
| Data Display value lag when collector is active | user-visible validation |

For focused `shadow_local` fidelity runs, `sweep.plan.started` must be checked
before interpreting results. It should show the expected DID filter and the
effective `sweep_plan_read_timeout_ms`. A run with
`VCI_PROXY_LOCAL_SWEEP_READ_TIMEOUT_MS=1` is not valid unless the plan reports
`sweep_plan_read_timeout_ms=[1]` for the narrowed item. Local
`sweep.item.finished` then shows whether the executor needed bounded tail reads
after an initial 4-byte `0x7E0..0x7EF` echo-like frame through
`tail_read_triggered`, `tail_read_attempts`, `tail_read_data_reads`,
`tail_read_timeout_ms`, and `read_attempts`.

The focused freshness analyzer now emits a per-session verdict. For Engine
Speed runs, `status=config_not_applied` with
`local_sweep_shadow_read_timeout_zero` means the logs show shadow transport but
not the intended positive shadow read timeout; do not use such a run to judge
the bounded tail-read fix. `status=freshness_still_slow` means meaningful
Engine Speed changes were present but the observed `0x000C` sweep cadence still
exceeded the local-feel target, so the next optimization remains write-side
tunnel crossing reduction rather than another read-cache/read-ahead tweak.

Before the next real-vehicle run, verify this minimum evidence chain:

- cloud `process.lifecycle.started` shows `local_sweep_mode=shadow_local` and
  `local_sweep_read_timeout_ms=1`;
- cloud `tunnel.auth.accepted` shows `client_capabilities` including
  `sweep_shadow=1` and `sweep_shadow_supported=true`;
- if no local shadow execution appears, check `sweep.plan.skipped` before
  concluding the optimization failed;
- a valid shadow-local execution run should show at least one
  `sweep.plan.started`, and an expanded learned set should later show
  `sweep.plan.superseded`;
- keep GM A9 in observe-only / inventory-only mode throughout these checks.

Manual GDS2 tests must keep relying on proxy-layer payload evidence because Flask live-data collector samples may not exist.

The Engine Speed detector should be expanded to recognize CAN-ID-prefixed UDS responses, for example:

```text
000007e8 62 00 0c aa bb
```

where `aa bb / 4` is the common RPM scaling for this observed response shape.

## Configuration

Suggested environment variables:

```text
VCI_PROXY_LOCAL_SWEEP=0
VCI_PROXY_LOCAL_SWEEP_MODE=observe_only
VCI_PROXY_LOCAL_SWEEP_MIN_CYCLES=2
VCI_PROXY_LOCAL_SWEEP_MAX_RESULT_AGE_MS=1000
VCI_PROXY_LOCAL_SWEEP_MIN_ITEM_INTERVAL_MS=250
VCI_PROXY_LOCAL_SWEEP_READ_TIMEOUT_MS=0
VCI_PROXY_LOCAL_SWEEP_MAX_ITEMS=128
VCI_PROXY_LOCAL_SWEEP_ALLOW_UDS_RDBI=1
VCI_PROXY_LOCAL_SWEEP_ALLOW_OBD_MODE01=1
VCI_PROXY_LOCAL_SWEEP_ALLOW_GM_A9_PACKET=1
VCI_PROXY_LOCAL_SWEEP_SHADOW_ALLOW_GM_A9_PACKET=0
VCI_PROXY_LOCAL_SWEEP_SHADOW_MAX_SECONDS=120
VCI_PROXY_LOCAL_SWEEP_PLAN_DELAY_MS=300
VCI_PROXY_LOCAL_SWEEP_MISMATCH_THRESHOLD=3
VCI_PROXY_LOCAL_SWEEP_ERROR_THRESHOLD=3
```

For narrow fidelity runs, the current implementation also accepts:

- `VCI_PROXY_LOCAL_SWEEP_INCLUDE_UDS_DIDS`
- `VCI_PROXY_LOCAL_SWEEP_EXCLUDE_UDS_DIDS`

These are comma-separated `UDS 0x22` DID filters applied only to the local
shadow plan, not to the foreground GDS2 page. Use them to prove one safe
signature at a time, for example keeping only `0x000C` in `shadow_local`, or
excluding a known-bad signature such as `0x0031` while investigating a mismatch.
For `shadow_local`, `VCI_PROXY_LOCAL_SWEEP_READ_TIMEOUT_MS` is a shadow-only
minimum read timeout layered on top of the foreground read shape. The foreground
GDS2 `ReadMsgs(timeout=0)` still passes through unchanged, while the local
shadow plan can use `timeout=1ms` to test whether delayed positive UDS response
frames are captured locally.

Modes:

| Mode | Behavior |
| --- | --- |
| `observe_only` | learn patterns and log would-have-hit decisions, no local extra polling, no replay |
| `shadow_local` | install local plan and collect results, but still forward GDS2 calls normally |
| `active_replay` | install the same local plan, but allow DLL-facing replay only for exact learned signatures whose latest shadow generation is fresh, success-coded, non-empty, and validated by the clean-match streak; all other requests still fall back to the normal tunnel path |

Default remains disabled until real-vehicle validation is complete. GM `A9 81 xx`
signatures are learned and logged when
`VCI_PROXY_LOCAL_SWEEP_ALLOW_GM_A9_PACKET=1`, but in the current runtime they
stay out of both `shadow_local` plans and `active_replay` candidacy. The legacy
`VCI_PROXY_LOCAL_SWEEP_SHADOW_ALLOW_GM_A9_PACKET` flag remains visible in
startup observability for compatibility and rollback analysis, but it does not
re-open GM A9 execution. The runtime also floors
`VCI_PROXY_LOCAL_SWEEP_MIN_ITEM_INTERVAL_MS` to `250ms` to avoid high-rate
shadow loops competing with the Data Display foreground stream.

## Implementation Phases

### Phase A: Per-DID Observability And Pattern Learning

Implemented.

Scope:

- parse CAN-ID-prefixed UDS positive responses
- emit per-DID cadence summaries
- identify candidate read-only request signatures from `WRITE_MSGS_REQ`
- include strict observed GM `A9 81 xx` packet request signatures when the
  CAN-ID prefix targets `0x7E0..0x7EF`
- learn stable loops in `observe_only`
- emit `sweep.inventory.signature` and `sweep.inventory.summary` with learned
  coverage, replay-candidate coverage, rejected write counts, per-kind counts,
  and projected write/pair RTT savings

No behavior change.

Acceptance criteria:

- logs can prove how often `DID 0x000C` is sampled
- logs can list the stable Data Display sweep order
- logs can estimate potential synthetic hit rate
- logs can show whether GM `A9 81 xx` traffic is learned but excluded from replay
  candidacy by the default guard

### Phase B: Protocol And Local Shadow Executor

Implemented scope:

- add internal sweep plan/result protocol messages
- implement local sequential sweep executor
- run only in `shadow_local`
- keep forwarding all GDS2 calls normally
- compare shadow results against normal GDS2-visible responses
- use server-driven non-blocking `STATUS` and immediate `DRAIN`
- defer plan start for the configured delay window so first plans can include multiple same-burst learned signatures
- keep read-only/cacheable IOCTL foreground calls from cancelling local shadow execution; they still serialize through the shared driver lock
- skip GM `A9 81 xx` signatures from local shadow execution by default and emit `sweep.plan.skipped` when that guard prevents a plan
- allow optional `uds_did` include/exclude filtering so `shadow_local` can be narrowed to a diagnostically useful subset without changing the GDS2 page

Risk:

- this can add extra ECU traffic, so it must be short-duration and feature-flagged.
- observed ECU tests showed unthrottled GM A9 shadow loops can correlate with frozen or disconnected Data Display windows; keep GM A9 in `observe_only` unless a new baseline proves stability.
- shadow fidelity is not proven until real-vehicle logs show `sweep.shadow.match` or actionable `stale`/`mismatch` outcomes after results have drained.

Acceptance criteria:

- local executor remains stable
- shadow results match normal responses by signature and response shape
- no GDS2 communication errors increase during shadow windows

### Phase C: Active Replay For A Small Allowlist

Implemented only as a narrow exact-signature experimental canary. It is not
validated for production rollout and must not be enabled for GM `A9 81 xx`.

Scope:

- use inventory coverage and shadow-fidelity evidence to decide whether the
  canary is worth enabling for the observed Data Display page
- enable synthetic write success and synthetic read response only for
  allowlisted signatures that passed the shadow-match gate
- start with one channel and one learned plan
- use strict result freshness and exact signature matching
- fall back immediately on mismatch

Acceptance criteria:

- `sweep.shadow.match` exists first for the same non-GM-A9 signature
- synthetic write hit rate is high during Data Display
- `DID 0x000C` cadence improves materially
- GDS2 displays values without communication errors
- result age at serve time stays within the configured threshold

### Phase D: Adaptive Scheduler

Scope:

- tune local sweep rate based on ECU response time and result freshness
- prioritize changing/high-value DIDs only if the plan safely supports it
- prevent local queue growth
- adjust pacing when tunnel result delivery is delayed

Acceptance criteria:

- local sweep cycle stays stable
- no ECU overload symptoms appear
- Engine Speed freshness approaches local GDS2 behavior

### Phase E: Production Hardening

Scope:

- add canary rollout controls
- add automatic disable triggers
- persist feature state in tray config
- add operational runbook
- add A/B report templates

Acceptance criteria:

- rollback requires only environment/config change
- failure reason is visible in observability
- disabled mode is byte-for-byte equivalent to current behavior where practical

## Test Strategy

Unit tests:

- request classifier allowlist/denylist
- signature normalization
- stable loop learner
- result-store freshness and generation rules
- synthetic replay gate fallback rules
- invalidation on channel/filter/session mutation

Protocol tests:

- encode/decode sweep plan messages
- encode/decode sweep result batches
- unknown message compatibility
- async or long-poll result handling

Integration tests:

- learned loop enters `observe_only`
- shadow executor runs without changing GDS2 responses
- delayed plan start includes signatures learned during the short delay window
- cacheable foreground IOCTL pauses shadow without cancelling the plan
- future active replay serves a matching write/read pair
- future active replay mismatch falls back to normal tunnel path
- future active replay stale result falls back
- plan cancellation clears pending synthetic context

Real-vehicle validation:

- local GDS2 baseline
- cloud current baseline with read-ahead/transaction
- cloud `observe_only`
- cloud `shadow_local`
- cloud `active_replay` for a small allowlist

Required evidence:

- `DID 0x000C` cadence before and after
- Engine Speed visible lag before and after
- synthetic hit rates
- result age at serve time
- J2534 error rate
- GDS2 native communication errors
- tunnel p95 and reconnect events

## Remaining Unsupported Or Unproven Work

These remain undone, disabled, or not validated for rollout:

- broad `active_replay` outside the exact-signature canary path
- production use of synthetic `WRITE_MSGS_RSP`
- production use of synthetic `READ_MSGS_RSP`
- skipped real tunnel forwarding for signatures that have not passed the
  shadow-match gate
- serving shadow data to GDS2 through generic cache/FIFO paths or outside the
  exact-signature canary gate
- unsolicited client-to-server sweep result push
- blocking long-poll drain
- production rollout controls and tray UI controls
- allowlist expansion beyond exact UDS `0x22`, OBD Mode 01 one-identifier,
  and strict observed GM `A9 81 xx` request shapes
- adaptive sweep-rate tuning and priority scheduling
- supported GM `A9 81 xx` shadow execution or replay; it remains observe-only /
  inventory-only under the current design
- proof that non-GM-A9 shadow results match real GDS2-visible responses on the
  target Data Display page and improve visible freshness after canary replay

Any broader replay implementation, and any GM A9 execution path, needs a
separate ADR/spec plus real-vehicle evidence from `observe_only` and safe
non-GM-A9 `shadow_local` runs.

## Rollout And Rollback

Rollout order:

1. merge observability only
2. run at least one `observe_only` Engine Control Module / Engine Data Data Display baseline
3. enable `shadow_local` without GM A9 shadow execution for a short test window;
   GM A9-only plans should produce `sweep.plan.skipped`, not local ECU polling
4. compare shadow results with normal responses only when a non-skipped plan
   actually starts and drains results
5. collect a changing-value run, preferably Engine Speed on real vehicle or a
   controllable ECU parameter, before judging user-visible freshness
6. enable `active_replay` only for non-GM-A9 signatures whose shadow results are
   proven stable, `return_code == 0`, non-empty, and repeatedly comparison-clean
7. do not enable GM A9 shadow execution or GM A9 replay under the current
   design; it requires a separate isolation strategy and ADR/spec first
8. expand cautiously after repeated successful tests

Rollback triggers:

- GDS2 communication error appears
- Data Display disconnect frequency increases
- Data Display values freeze while foreground J2534 traffic continues
- DTC/session/security traffic is detected inside an active plan
- synthetic mismatch count exceeds threshold
- result age exceeds threshold repeatedly
- local executor error count exceeds threshold
- Engine Speed lag gets worse
- operator reports wrong or frozen values

Rollback action:

```text
VCI_PROXY_LOCAL_SWEEP=0
```

Then restart both cloud reverse server and local reverse client/tray process.

For the current Engine Control Module / Engine Data workstream, rollback also
means keeping `VCI_PROXY_LOCAL_SWEEP_SHADOW_ALLOW_GM_A9_PACKET=0` after restart.

## Why This Addresses The Current Problem

The current tunnel optimizations mostly reduce read-side cost after a write. The remaining page-refresh delay comes from many repeated read-only DID writes still crossing the tunnel one by one.

The local sweep scheduler reduces the number of synchronous cross-tunnel operations on the critical path. It does not need GDS2 to show fewer parameters. Instead, it accepts that GDS2 will ask for the full page and prepares fresh local results for those same requests before GDS2 asks.

In the best case, a Data Display cycle becomes:

```text
local side continuously polls ECU at local speed
cloud receives fresh result batches
GDS2 synchronous calls are answered from fresh cloud-side results
```

This targets the real multiplier:

```text
page parameter count x tunnel RTT
```

and replaces it with:

```text
local sweep cycle time + batched result streaming
```

## Open Questions

- Which exact request signatures in Engine Data are safe to allowlist across vehicles?
- Should active replay use latest-by-signature results, strict plan-order results, or a hybrid?
- What result age threshold feels equivalent to local GDS2 for Engine Speed?
- Does the current tunnel reader support unsolicited client-to-server result frames cleanly, or should the first version use server long-poll drain requests?
- How much extra ECU traffic is acceptable during `shadow_local` validation?
- Are there vehicle-specific DIDs that look read-only but have hidden side effects?
- Will a run with changing Engine Speed show that read-ahead/transaction already
  improves visible freshness enough, or is `active_replay` required to remove
  the remaining page-wide serial tunnel cost?
- Can the transaction slow-link guard be validated under a degraded tunnel
  without introducing Data Display disconnects?

## Decision Guidance

Start this design only after the existing read-ahead and transaction path are confirmed enabled and measured. If per-DID cadence remains slow while tunnel p95 is healthy, this design is the next appropriate optimization because it attacks request-count amplification instead of single-call latency.
