# Proxy J2534 Local Sweep Scheduler Design

## Document Role

| Field | Content |
| --- | --- |
| Type | Long-term optimization design |
| Status | Guarded `observe_only` implemented; `shadow_local` transport is available but GM `A9 81 xx` shadow execution remains disabled by default; `active_replay` remains disabled |
| Owner scope | Cloud GDS2 Data Display freshness over the Proxy J2534 tunnel |
| Primary code paths | `vci_proxy/reverse_server.py`, `vci_proxy/reverse_client.py`, `vci_proxy/protocol.py`, `vci_proxy/j2534_worker.py` |
| Related docs | `agent_docs/ops/proxy_j2534_latency_optimization.md`, `agent_docs/ops/vci_proxy_and_tunnel.md`, `agent_docs/ops/product_observability.md` |

## One-Line Conclusion

The implemented first stage learns repeated read-only Data Display sweeps and can run a local shadow executor for comparison, while real GDS2 requests continue through the existing proxy/tunnel behavior. Current Engine Control Module / Engine Data evidence keeps strict GM `A9 81 xx` signatures in observe-only handling unless an explicit bounded experiment enables them. The long-term replay goal remains future work.

## Current Implementation Stage

The current code implements two disabled-by-default modes:

- `observe_only`: cloud-side `reverse_server` classifies exact allowlisted UDS `0x22` ReadDataByIdentifier, OBD Mode 01 PID, and strict CAN-ID-prefixed GM `A9 81 xx` packet write requests, correlates them with later `READ_MSGS_RSP(data)`, and emits candidate count, confidence, cadence, rejection, request-inventory, coverage, and projected RTT-cost observability. It does not change protocol, reverse-client behavior, local runtime behavior, or local J2534 call counts.
- `shadow_local`: after observe learning and capability negotiation, the cloud waits a short configurable delay window (`VCI_PROXY_LOCAL_SWEEP_PLAN_DELAY_MS`, default `300ms`) before sending an internal sweep plan to the local reverse client. The delay lets signatures learned milliseconds apart join the first plan instead of starting from a one-item plan. The local client executes the plan serially through the same J2534 driver-call path used by foreground requests, queues shadow read results, and returns them only through server-driven status/drain control frames. Real `WRITE_MSGS_REQ` and `READ_MSGS_REQ` from GDS2 continue through the existing normal proxy path; local sweep does not synthesize replies or skip forwarding.

The v1 shadow transport is server-driven and request/response shaped:

- `SWEEP_STATUS_REQ/RSP` checks local executor state.
- `SWEEP_DRAIN_RESULTS_REQ/RSP` returns immediately with queued shadow results or an empty result set.
- Unsolicited client-to-server result push and blocking long-poll drain are not implemented.

Shadow data is comparison-only. It is stored in `SweepShadowStore`, compared against normal GDS2-visible `READ_MSGS_RSP` bodies produced by the existing proxy path, and never consulted by `_try_serve_cached()`, never written to `PrefetchReadMsgsBuffer`, and never used to fulfill normal `READ_MSGS_REQ`.

Inventory data is also observability-only. `SweepInventoryTracker` records the
allowlisted request signatures seen during the foreground GDS2 stream, their
learned/replay-candidate status, counts by request kind, rejection reasons,
write/read network p50/p95/max, and the observed write/pair RTT cost that could
be removed by a future replay stage. It does not decode the meaning of GM data
packets, does not serve responses, and does not enable `active_replay`.

The observe gate artifact for this stage is `.omx/plans/local-sweep-scheduler-observe-gate-signoff.md`.

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
  `sweep.plan.skipped` with `reason=gm_a9_packet_shadow_disabled`;
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

Do not implement or enable `active_replay` from this evidence alone.

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

`active_replay` is intentionally rejected by configuration in this stage.

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
  and fresh result exists or is expected within a tiny grace window:
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

Modes:

| Mode | Behavior |
| --- | --- |
| `observe_only` | learn patterns and log would-have-hit decisions, no local extra polling, no replay |
| `shadow_local` | install local plan and collect results, but still forward GDS2 calls normally |

`active_replay` is documented as a future mode but is not accepted by runtime configuration in this stage.

Default remains disabled until real-vehicle validation is complete. GM `A9 81 xx`
signatures are learned and logged when
`VCI_PROXY_LOCAL_SWEEP_ALLOW_GM_A9_PACKET=1`, but they stay out of
`shadow_local` plans unless
`VCI_PROXY_LOCAL_SWEEP_SHADOW_ALLOW_GM_A9_PACKET=1` is explicitly set. The
runtime also floors `VCI_PROXY_LOCAL_SWEEP_MIN_ITEM_INTERVAL_MS` to `250ms` to
avoid high-rate shadow loops competing with the Data Display foreground stream.

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

Risk:

- this can add extra ECU traffic, so it must be short-duration and feature-flagged.
- observed ECU tests showed unthrottled GM A9 shadow loops can correlate with frozen or disconnected Data Display windows; keep GM A9 in `observe_only` unless a new baseline proves stability.
- shadow fidelity is not proven until real-vehicle logs show `sweep.shadow.match` or actionable `stale`/`mismatch` outcomes after results have drained.

Acceptance criteria:

- local executor remains stable
- shadow results match normal responses by signature and response shape
- no GDS2 communication errors increase during shadow windows

### Phase C: Active Replay For A Small Allowlist

Not implemented in this stage.

Scope:

- use inventory coverage and shadow-fidelity evidence to decide whether replay
  is worth implementing for the observed Data Display page
- enable synthetic write success and synthetic read response for allowlisted signatures
- start with one channel and one learned plan
- use strict result freshness and exact signature matching
- fall back immediately on mismatch

Acceptance criteria:

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

## Not Implemented In This Stage

These remain undone and disabled:

- `active_replay`
- synthetic `WRITE_MSGS_RSP`
- synthetic `READ_MSGS_RSP`
- skipped real tunnel forwarding
- serving shadow data to GDS2
- unsolicited client-to-server sweep result push
- blocking long-poll drain
- production rollout controls and tray UI controls
- allowlist expansion beyond exact UDS `0x22`, OBD Mode 01 one-identifier,
  and strict observed GM `A9 81 xx` request shapes
- adaptive sweep-rate tuning and priority scheduling
- default GM `A9 81 xx` shadow execution; it is observe-only unless explicitly opted in
- proof that shadow results match real GDS2-visible responses on Engine Control
  Module / Engine Data after the delayed-plan, read-only-IOCTL, and GM
  `A9 81 xx` allowlist changes

Any future replay implementation needs a separate ADR/spec and real-vehicle evidence from `observe_only` and `shadow_local`.

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
6. enable GM A9 shadow only with
   `VCI_PROXY_LOCAL_SWEEP_SHADOW_ALLOW_GM_A9_PACKET=1` after the baseline remains
   stable and only for a bounded experiment with immediate rollback available
7. write a separate ADR/spec before enabling `active_replay`
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
