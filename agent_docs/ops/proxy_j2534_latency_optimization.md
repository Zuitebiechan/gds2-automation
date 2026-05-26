# Proxy J2534 Latency Optimization Plan

## Document Role

| Field | Content |
| --- | --- |
| Type | Optimization plan / implementation guide |
| Status | Phases 1-4 plus first Phase 5 observe/inventory implemented; guarded `shadow_local` transport exists and `active_replay` remains an exact-signature experiment, but 2026-05-19 Engine Speed evidence moves the next product implementation lane to `Proxy Local Live Data` |
| Owner scope | Proxy J2534 reverse tunnel latency, especially cloud GDS2 live data freshness |
| Primary code paths | `vci_proxy/reverse_server.py`, `vci_proxy/reverse_client.py`, `vci_proxy/protocol.py`, `vci_proxy/cache_read_msgs.py` |
| Related docs | `agent_docs/ops/vci_proxy_and_tunnel.md`, `agent_docs/reports/network_ms.md`, `agent_docs/ops/product_observability.md`, `agent_docs/ops/proxy_j2534_local_sweep_scheduler.md`, `agent_docs/ops/proxy_local_live_data_fallback.md` |

## One-Line Conclusion

Cloud GDS2 live data lag is mainly amplified by `J2534 serial request count x tunnel round-trip cost`. The current transport stack improves safety, observability, and some read-side costs, but recent Engine Speed evidence shows it has not delivered the core product outcome: visible Data Display freshness close to local GDS2. The GDS2 replay/write-side crossing lane remains useful research for stable signatures, but the next Engine Speed product implementation should be `Proxy Local Live Data`: decode a tiny safe local signal allowlist and display it as a separate source rather than trying to make native GDS2 Data Display refresh faster.

## Direction Decision - 2026-05-19

Move Engine Speed work to the `Proxy Local Live Data` fallback as the primary
next implementation lane. Do not continue timeout-only tuning or enable
`active_replay` for Engine Speed from the current evidence.

Latest focused real-vehicle tests showed:

- `READ_TIMEOUT_MS=5` proved the focused `UDS DID 0x000C` shadow config was
  active, but did not produce a usable pre-abort match streak.
- `READ_TIMEOUT_MS=15` captured many local `0x000C` response frames and did
  produce two pre-abort `sweep.shadow.match` events.
- The same `15ms` run still produced repeated `sweep.shadow.mismatch`, only
  `clean_match_streak=1`, multiple plan cancellations, and no
  `active_replay_armed` / `active_replay_served`.
- The remaining mismatch pattern is dynamic-value drift and occasional
  echo/empty shape mismatch, not simply a local read timeout.

Implication:

- exact raw replay is not a good first product path for dynamic Engine Speed;
- semantic replay would require tolerance/freshness design before it can be
  considered safe;
- the lower-risk product path is to decode local Engine Speed as a separate
  `proxy_local_obd` or `proxy_local_known_uds` value stream.

Implementation handoff is now in
`agent_docs/ops/proxy_local_live_data_fallback.md`.

## Reassessment - 2026-05-18

Treat the current safe transport lane as reaching its practical limit unless new
logs identify a specific read-side correctness bug. It should no longer be
described as having made Engine Speed "close to local 1-2s" behavior.

Recent real-vehicle Engine Speed runs changed the interpretation:

- tunnel RTT was generally healthy; nearby `READ_MSGS_REQ` /
  `WRITE_MSGS_REQ` p95 values were mostly around `38-40ms`, so the observed
  lag is not explained by one slow network call;
- meaningful `Engine Speed` changes were present, but the observed focus DID
  cadence remained slow: one verdict saw p50 around `25.4s`, and the later
  shadow-plan-expanded run still saw p50 around `9.1s` with max around
  `11.6s`;
- each significant Engine Speed change window still contained roughly
  `42-45` forwarded tunnel requests, and `active_replay_armed` /
  `active_replay_served` stayed at `0`;
- `shadow_local` either did not start a useful plan for the focus signature or
  produced comparison-only evidence. By design it does not serve GDS2 and
  therefore cannot directly improve visible freshness;
- ECU voltage tests proved capability negotiation, plan startup, local
  execution, and result drain for `shadow_local`, but their dominant foreground
  evidence remained GM `A9 81 xx`, which is intentionally observe-only /
  inventory-only in the current architecture.

Current conclusion:

- `ReadMsgs` empty-cache changes, adaptive TTL, read-ahead FIFO, read-tail,
  underfill merge, and read/write collect transactions are still useful
  guardrails and read-side optimizations;
- they mainly reduce `WRITE RTT + READ RTT` toward `WRITE RTT + local read
  collection`, but do not remove the remaining one foreground write crossing
  per page item;
- for a Data Display page with many serial read-only requests, that still leaves
  `N parameters x one tunnel crossing` on the critical path, which is enough to
  produce multi-second Engine Speed freshness;
- the next optimization decision must be about reducing write-side crossings,
  not further tuning read-cache/read-ahead parameters.

Practical handoff for the next conversation:

- do not claim value-level freshness improvement unless the log shows changing
  focus values and a materially faster focus DID cadence;
- do not treat absence of `active_replay_*` events as a bug for the current GM
  A9 guarded lane;
- do not re-enable GM A9 shadow execution or replay without a separate
  isolation design and real-vehicle evidence;
- if continuing implementation, focus on proof tooling and a narrow
  non-GM-A9, read-only DID path that can produce `sweep.shadow.match` before
  any replay/serve decision;
- if continuing validation, choose a page or ECU scene with repeated non-GM-A9
  UDS DID foreground traffic. Repeating the current ECU voltage scene mainly
  validates mechanism startup, not latency improvement.

## Fallback Product Lane: Proxy Local Live Data

If the write-side crossing-reduction lane proves ineffective for the target
GDS2 Data Display page, do not keep tuning read-ahead/read-collect/read-tail
settings as a substitute for product proof. The next product lane is a separate
`Proxy Local Live Data` panel: a local-side collector reads a tiny allowlist of
known, safe, read-only signals through the vehicle-side J2534 path, decodes
only those known signals, and streams value events to the cloud/UI.

This lane is not a way to make the native GDS2 Data Display page refresh
faster. It deliberately bypasses the GDS2 page-level sweep for a small set of
high-value values such as Engine Speed, while GDS2 can continue to display its
full OEM Data Display at its existing cadence.

Important boundary:

- J2534 does not decode Data Display parameters. It only transports raw
  messages. GDS2 currently provides most parameter names, units, and OEM decode
  semantics through the Java Agent page snapshot path.
- The local collector may decode only signals with explicit known semantics,
  for example standard OBD Mode 01 PID `0x0C` Engine Speed
  (`rpm = ((A * 256) + B) / 4`) or an explicitly validated UDS DID with known
  byte layout and scale.
- OEM/private payloads, including GM `A9 81 xx`, are not eligible active poll
  sources under the current evidence. They may be observed for correlation, but
  not actively executed or treated as a decoded product source without a
  separate safety design.

Target architecture:

```text
vehicle-side VCI / J2534
  -> local VCI Proxy live-data collector
  -> small read-only allowlist decoder
  -> reverse tunnel value event
  -> cloud session latest-value cache and SSE
  -> product Live Data panel / client GUI
```

The current Java Agent path remains valid for complete GDS2 page data:

```text
GDS2 Data Display
  -> Java Agent latest.json
  -> cloud/session SSE
  -> product Live Data table
```

The fallback product lane adds a second source rather than pretending to own
GDS2's full decoder catalog. A UI should show source and age, for example
`source=gds2_agent` for complete but page-limited data and
`source=proxy_local_obd` for a small fast signal set.

Initial MVP scope:

- one signal first: Engine Speed;
- allowed source only if the vehicle supports standard OBD `01 0C`, or if a
  non-GM-A9 UDS DID has been explicitly validated against GDS2/local evidence;
- default disabled, foreground GDS2 traffic gets priority, and collector polls
  back off on errors, negative responses, stale results, or J2534/Data Display
  instability;
- stream decoded value events only to the product UI; never synthesize
  DLL-visible `WRITE_MSGS_RSP` / `READ_MSGS_RSP` for GDS2.

Go/no-go evidence for this fallback:

- go: local collector value follows real Engine Speed changes, sample age p95
  is near the local-feel target (`<= 1000-2000ms`), and GDS2 remains stable;
- no-go: no safe standard OBD/known-UDS Engine Speed source exists, the
  collector interferes with GDS2, sample age remains multi-second, or the only
  apparent source is GM `A9 81 xx`.

## Handoff For Real-Vehicle Engine Speed Validation - 2026-05-13

The next validation run is a real-vehicle Engine Data / Data Display test where
the operator will use `Engine Speed` changes, not bench voltage, as the primary
freshness signal. Do not assume `active_replay` has succeeded or is safe.

Run interpretation should start from these assumptions:

- the optimization lane under test is the safe transport layer:
  `ReadMsgs` empty-cache behavior, read-ahead FIFO, read-collect / write-collect
  transactions, FIFO underfill merge, and slow-link guard behavior;
- GM `A9 81 xx` remains observe-only / inventory-only. It must not be treated
  as shadow-execution or replay evidence unless raw logs explicitly show a
  non-GM-A9 safe signature with fresh, success-coded, comparison-clean shadow
  results;
- a useful real-vehicle run must contain actual `Engine Speed` value changes.
  Constant `0 RPM` or near-idle-only samples cannot prove user-visible
  freshness improvement;
- after the run, analyze focused value freshness with:

```powershell
python scripts/analyze_battery_voltage_freshness.py `
  --cloud-root "C:\Users\shsww\projects\RPA_demo\vci_proxy\cloud_mirror" `
  --focus-key engine_speed `
  --min-delta 100 `
  --json reports/engine_speed_freshness.json `
  --report reports/engine_speed_freshness.md
```

Primary evidence to correlate:

- `agent.collector.focus_value_changed` with `focus_key=engine_speed`;
- `collector_lag_ms`, `previous_value_number`, `current_value_number`, and
  `delta_value_number` for meaningful RPM changes;
- nearby `proxy.request.cache_decision`, `proxy.request.forwarded_to_tunnel`,
  `proxy.request.response_received`, `read_collect_transaction`,
  `write_collect_transaction`, and `read_ahead.transaction.guard_armed`;
- absence of `proxy.request.active_replay_armed` /
  `proxy.request.active_replay_served` is acceptable and expected for GM A9
  current-state validation.

Use the user's standard log paths for cross-run analysis:

- cloud: `C:\Users\shsww\projects\RPA_demo\vci_proxy\cloud_mirror`
- local: `C:\Users\shsww\AppData\Roaming\VCI_Proxy`

## Current Progress Snapshot - 2026-05-08

Latest ECU Data Display validation showed a regression mode that is more
specific than a tunnel stall: visible Data Display values latched at page-entry
plateaus even though foreground proxy and local worker traffic kept moving.

Validated active configuration in raw observability:

- cloud reverse server startup logged `read_ahead_enabled=true`,
  `read_ahead_transaction_enabled=true`,
  `local_sweep_enabled=true`,
  `local_sweep_mode=active_replay`,
  `local_sweep_allow_gm_a9_packet=true`, and
  `local_sweep_shadow_allow_gm_a9_packet=false`;
- the run used session `4ba7b8ee031c4c1f`, connection epoch
  `epoch-1778208278843-001`, with Data Display entered at
  `2026-05-08T02:46:33Z`;
- the server started a local shadow plan and drained shadow batches repeatedly
  during the Data Display window;
- cloud raw observability recorded no
  `proxy.request.active_replay_armed` and no
  `proxy.request.active_replay_served`;
- cloud collector samples plateaued at page-entry values rather than updating in
  page: the first entry stayed near `12.9V`, a later re-entry stayed near
  `13.5V`, and `Engine Speed` / `Accelerator Pedal Position` stayed `0`;
- foreground `READ_MSGS_REQ(data)` responses still arrived and their payload
  digests changed throughout the same window, so the system was not simply
  disconnected or idle;
- local shadow items finished with `return_code=18`, which maps to
  `ERR_NOT_UNIQUE` in the current J2534 error table and should not be treated as
  replay-ready evidence;
- the later `vci_disconnected` / tunnel EOF happened after the user-aborted
  session and was not the first failure in the frozen-value window.

Interpretation:

- this run does not show replay serving stale shadow data, because replay never
  armed or served;
- it does show that GM A9 local shadow execution is already disruptive in the
  current architecture, likely because it shares a consumptive `ReadMsgs` path
  with the foreground Data Display loop;
- the immediate optimization step is not "tune replay harder", but to push GM
  A9 back to observe-only / inventory-only and continue optimizing only on
  safer signatures whose latest shadow results are `return_code == 0`,
  non-empty, and comparison-stable.

## Current Progress Snapshot - 2026-05-06

Latest ECU Data Display validation showed the current guarded stack can run
without increasing disconnects when the tunnel is healthy, but it did not prove
the slow-link guard or value-level freshness improvement.

Validated active configuration in raw observability:

- cloud reverse server startup logged `read_ahead_enabled=true`,
  `read_ahead_transaction_enabled=true`,
  `read_ahead_transaction_max_network_ms=750`,
  `read_ahead_transaction_cooldown_ms=10000`,
  `local_sweep_enabled=true`, `local_sweep_mode=shadow_local`, and
  `local_sweep_shadow_allow_gm_a9_packet=false`.
- local reverse client authentication succeeded with
  `ok;read_ahead=1;read_collect=1;write_collect=1;sweep_shadow=1`.
- session `1edb8e1caf7f4def`, connection epoch
  `epoch-1778052452268-001`, Data Display window
  `2026-05-06T07:28:23Z` to `2026-05-06T07:36:23Z`.
- no Data Display `j2534_disconnect`, no `proxy.j2534.cadence_gap`, no
  `proxy.request.timeout`, and no `tunnel.probe.failure`.
- Data Display tunnel RTT stayed low: `READ_MSGS_REQ` p95 about `29.6ms`,
  p99 about `45.4ms`, max about `248.3ms`; `WRITE_MSGS_REQ` p95 about
  `41.1ms`, max about `119.7ms`; no sample reached the `750ms` transaction
  guard threshold.
- transaction wrapping was active (`write_collect_transaction` observed), but
  `read_ahead.transaction.guard_armed=0` and
  `write_collect_guarded_no_collect=0` because the tunnel never became slow
  enough to exercise the guard.
- local sweep observe/inventory was active and learned one strict GM
  `A9 81 xx` signature. The plan was skipped with
  `reason=gm_a9_packet_observe_only`, so no local shadow executor plan
  contributed data in this run.
- focused value samples for `Engine Speed`, `Accelerator Pedal Position`,
  and `Battery Voltage`
  were present, but both values remained `0` for all samples. This run cannot
  prove whether visible Engine Speed lag improved; a changing-value run is still
  required.

Interpretation:

- the latest fix reduced the observed disconnect risk compared with the earlier
  unstable GM A9 shadow experiments by keeping GM A9 in observe-only handling;
- the transaction/read-ahead path appears stable under a healthy tunnel;
- the current implementation still cannot remove the remaining
  `N requests x tunnel RTT` cost for a full Data Display page because
  `active_replay` is now available only as an explicit experimental mode. The
  current implementation can serve exact learned signatures only from a fresh,
  validated local shadow result whose latest generation has already passed the
  clean-compare gate; it still falls back to the normal tunnel path whenever a
  replay precondition is missing.

## Background

During real-vehicle testing, the local GDS2 Engine Data page showed `Engine Speed` changes with about `1-2s` delay, while cloud GDS2 showed about `5-6s` delay. Existing log analysis indicates that the local J2534 worker and hardware calls are usually only a few milliseconds, and the tunnel is not consistently bad enough to explain the full delay by a single slow request.

The observed shape is many synchronous J2534 calls, especially `READ_MSGS_REQ`, accumulating one tunnel round trip at a time.

Representative cloud reverse-server log window from `2026-04-28T07:39:14Z` to `2026-04-28T07:44:13Z`:

| Metric | Observed value |
| --- | ---: |
| Total proxy requests received | `20101` |
| `READ_MSGS_REQ` received | `18565` |
| `WRITE_MSGS_REQ` received | `1073` |
| `IOCTL_REQ` received | `300` |
| `READ_MSGS_REQ` cache hits | `16484` |
| `READ_MSGS_REQ` tunnel misses | `2081` |
| `IOCTL_REQ` cache hits | `243` |
| `IOCTL_REQ` tunnel misses | `57` |
| Non-cached `READ_MSGS_REQ` avg `network_ms` | about `31.5ms` |
| Non-cached `READ_MSGS_REQ` p95 `network_ms` | about `43.4ms` |
| Non-cached `READ_MSGS_REQ` p99 `network_ms` | about `89.1ms` |
| Non-cached `READ_MSGS_REQ` max `network_ms` | about `1181.5ms` |
| Non-cached `WRITE_MSGS_REQ` avg `network_ms` | about `30.1ms` |
| Non-cached `WRITE_MSGS_REQ` p95 `network_ms` | about `43.9ms` |

These numbers mean the tunnel is usually in the acceptable range, but the cloud path still pays tens of milliseconds repeatedly. In a high-frequency live-data loop, that repeated synchronous cost becomes visible as stale values on the GDS2 data display.

## Current Architecture

Current data path:

```text
Cloud GDS2
  -> virtual J2534 DLL
  -> cloud-side ReverseProxyServer local proxy listener
  -> reverse tunnel
  -> local-side ReverseProxyClient
  -> local J2534 driver / worker
  -> real VCI / ECU
```

Important current behavior:

- `vci_proxy/reverse_server.py` handles a DLL-side proxy connection as a sequential loop: read one request, check cache, forward one request over the tunnel, wait for one response, reply to the DLL, then read the next request.
- Tunnel forwarding is protected by `vci_lock`, so arbitrary request reordering or concurrent writes are intentionally avoided.
- `vci_proxy/reverse_client.py` receives one request, executes the matching J2534 driver method, appends `hw_ms`, and returns one response.
- `ReadMsgsCache` only short-circuits recent `BUFFER_EMPTY` results. It does not cache real message data.
- `FilterDeduplicationCache` deduplicates identical `START_FILTER_REQ` setup calls.
- `IoctlCache` caches selected read-only IOCTLs such as `GET_CONFIG`, `READ_VBATT`, and `READ_PROG_VOLTAGE`.
- The reverse server already sets `TCP_NODELAY` and tracks tunnel quality with probe samples.

## Constraints

These constraints are non-negotiable unless a future design explicitly replaces the current J2534 proxy model:

- GDS2 sees a synchronous J2534 API. The virtual DLL must return responses in the order and shape expected by GDS2.
- `PassThruReadMsgs` is consumptive. Real message data must not be replayed from a normal cache, because that can duplicate frames, hide frames, or change ordering.
- Arbitrary J2534 calls must not be reordered. Some calls mutate channel state, filter state, buffers, or protocol timing.
- J2534 driver calls should not be parallelized unless the exact operation is proven thread-safe and order-independent for the target driver.
- Every protocol-level optimization must be behind a feature flag and A/B tested against local GDS2 behavior on real vehicle flows.

## Optimization Goals

1. Reduce the freshness delay of cloud GDS2 live data values, starting with `Engine Speed` on the Engine Data page.
2. Reduce real tunnel round trips per live-data refresh cycle.
3. Preserve J2534 semantics visible to GDS2.
4. Avoid hiding new ECU data with overly aggressive empty-read caching.
5. Add enough observability to prove whether each optimization helps or hurts.

## Measurement Plan

Use `network_ms` only on non-cache-hit samples for tunnel quality. Cache-hit samples are useful for optimization effectiveness, but they should not be treated as tunnel latency.

Track these metrics per live-data session:

| Metric | Purpose |
| --- | --- |
| `Engine Speed` value age / collector lag | User-visible freshness |
| `Accelerator Pedal Position` value age / collector lag | Compare input event to engine response freshness |
| `Battery Voltage` value age / collector lag | Use a controllable changing parameter when Engine Speed is not practical |
| `READ_MSGS_REQ(data)` count and p95 `network_ms` | Real data delivery cost |
| `READ_MSGS_REQ(empty)` count and p95 `network_ms` | Polling noise cost |
| `READ_MSGS_REQ` cache hit rate | Empty-poll suppression effectiveness |
| `WRITE_MSGS_REQ -> first READ_MSGS_REQ(data)` elapsed time | Query-response freshness |
| Post-write `READ_MSGS_REQ` cache hits | Detect empty cache hiding possible ECU responses |
| Real tunnel round trips per display refresh window | Main optimization target |
| Tunnel-quality p95 | Deployment/network gate |

Existing focused value observability should be used when analyzing real vehicle runs:

- `agent.collector.focus_parameters_sampled`
- focused parameters: `Engine Speed`, `Accelerator Pedal Position`,
  `Battery Voltage`, plus known OEM-specific voltage aliases
- use `parameter_value_sources` to verify which OEM parameter name supplied the
  primary focused voltage value during a run
- session/page context: `data_display`, selected data category, live-data active state
- for real-vehicle `Engine Speed` validation, run the focused freshness script
  with `--focus-key engine_speed --min-delta 100` so idle jitter is filtered and
  each meaningful RPM change is correlated with nearby tunnel RTT, FIFO, and
  read-collect behavior; the same script defaults to battery voltage for ECU
  bench runs

## Optimization Roadmap

### Phase 0: Observability Hardening

Goal: make the request chain measurable before changing protocol behavior.

Recommended additions:

- Add decoded `READ_MSGS_REQ` fields to runtime observability events: `channel_id`, `num_msgs`, `timeout`.
- Add decoded `READ_MSGS_RSP` fields to runtime observability events: `return_code`, `message_count`, `payload_bytes`.
- Add `post_write_age_ms` to `READ_MSGS_REQ` events when the same channel recently had `WRITE_MSGS_REQ`.
- Add a per-channel rolling marker for `last_write_seq` and `last_write_ts`.
- Split runtime summaries into `READ_MSGS_REQ(empty)` and `READ_MSGS_REQ(data)`, matching the benchmark report model.

Acceptance criteria:

- A real Engine Data run can answer how many tunnel round trips happen between one visible Engine Speed update and the next.
- Logs can identify whether `READ_MSGS_REQ` cache hits occur immediately after `WRITE_MSGS_REQ`.
- Logs can compare value-level collector lag with proxy-level write/read timing.

### Phase 1: Post-Write Empty-Read Cache Bypass

Goal: stop the empty `ReadMsgs` cache from hiding ECU responses right after GDS2 sends a request.

Current risk:

- `ReadMsgsCache` serves recent `BUFFER_EMPTY` responses for a fixed TTL.
- If GDS2 writes a request to the ECU and then immediately reads, a recent empty cache entry can return `BUFFER_EMPTY` without touching the VCI.
- This saves one tunnel RTT, but it may delay the first real ECU response and make live data look stale.

Recommended behavior:

- On `WRITE_MSGS_REQ`, invalidate the `ReadMsgsCache` entry for the same channel.
- Add a short post-write bypass window, for example `100-250ms`, where `READ_MSGS_REQ` for that channel must go to the local side instead of empty-cache hit.
- Keep this feature configurable:

```text
read_cache_post_write_bypass_ms = 150
```

Implementation note:

- The reverse server exposes this as `--read-cache-post-write-bypass-ms`.
- The default is `150`.
- Set it to `0` to disable the post-write invalidation/forced-bypass behavior.
  With Phase 2 adaptive TTL enabled, writes may still enter active TTL mode. To
  approximate the previous fixed empty-cache behavior, also set
  `--read-cache-active-window-ms 0` and `--read-cache-max-timeout-ms -1`.

Expected effect:

- Slightly more real tunnel reads after writes.
- Better freshness for values that depend on immediate ECU response.
- Lower risk than protocol-level batching because J2534 call order stays unchanged.

Risks:

- Empty polling traffic can increase if GDS2 writes very frequently.
- If the tunnel is already degraded, bypassing cache can make the UI slower instead of fresher.

Validation:

- Compare cloud Engine Speed lag before/after.
- Ensure `READ_MSGS_REQ(data)` arrives earlier after `WRITE_MSGS_REQ`.
- Ensure total request error rate does not increase.

### Phase 2: Adaptive Empty-Read Cache TTL

Goal: keep suppressing idle empty polls without delaying active live-data responses.

Recommended behavior:

- Use a shorter empty-read TTL immediately after write or after recent data, for example `25-50ms`.
- Use the current or slightly tuned idle TTL only when the channel is quiet, for example `150ms`.
- Consider applying empty-cache hits only for `READ_MSGS_REQ` with `timeout == 0` or very small timeout.
- Reset to active mode after `WRITE_MSGS_REQ`, `START_FILTER_REQ`, `STOP_FILTER_REQ`, mutating `IOCTL_REQ`, or any successful data read.

Example configuration:

```text
read_cache_idle_ttl_ms = 150
read_cache_active_ttl_ms = 25
read_cache_active_adaptive_ttl_max_ms = 70
read_cache_active_adaptive_ttl_margin_ms = 8
read_cache_active_window_ms = 500
```

Implementation note:

- The reverse server keeps the existing `--read-cache-ttl` flag as the idle TTL.
- Active TTL is exposed as `--read-cache-active-ttl-ms`.
- Active TTL can adapt upward after repeated real `BUFFER_EMPTY` confirmations;
  `--read-cache-active-adaptive-ttl-max-ms` caps that learned TTL and
  `--read-cache-active-adaptive-ttl-margin-ms` controls the margin over the
  observed empty polling cadence.
- Active mode duration is exposed as `--read-cache-active-window-ms`.
- `--read-cache-max-timeout-ms` limits empty-cache hits to zero or very small
  `ReadMsgs` polls; the default is `25`, and `-1` allows all timeouts.
- Active mode is entered after `WRITE_MSGS_REQ`, successful data reads,
  `START_FILTER_REQ`, `STOP_FILTER_REQ`, and non-cacheable/mutating `IOCTL_REQ`.
- Real `ReadMsgs` data remains consume-once and is never replayed from cache.

Expected effect:

- Preserve most empty-poll reduction during idle periods.
- Reduce stale-value risk during active request/response periods.

Risks:

- More complexity in cache state.
- Requires good per-channel state cleanup on disconnect and close.

Validation:

- `READ_MSGS_REQ` cache hit rate should remain high during idle periods.
- Post-write cache hits should drop.
- `Engine Speed` value freshness should improve without increasing tunnel p95 beyond gating thresholds.

### Phase 3: Local-Side Read-Ahead

Goal: reduce tunnel round trips by moving likely follow-up reads to the local side.

This is the most promising medium-impact optimization for live data.

Design idea:

1. Cloud GDS2 sends normal `WRITE_MSGS_REQ`.
2. Reverse server forwards the write to the local reverse client.
3. Local reverse client executes `PassThruWriteMsgs`.
4. After write success, local reverse client immediately performs a bounded number of local `PassThruReadMsgs` calls on the same channel.
5. Any messages found are pushed back to the reverse server as sideband prefetched read data.
6. When GDS2 later sends `READ_MSGS_REQ`, the reverse server can answer from a per-channel prefetch FIFO instead of crossing the tunnel.

The key distinction from unsafe caching:

- Do not replay old `ReadMsgs` responses.
- Only serve messages that were actually consumed once by the local side and stored in FIFO order for the next matching cloud-side read.
- Each prefetched message must be consumed at most once.

Protocol changes likely needed:

- Add an internal sideband message type for prefetched `READ_MSGS` data, or
- Add an internal transaction response that carries `WRITE_MSGS_RSP` plus a bounded prefetch bundle.

Implementation note:

- The first Phase 3 implementation uses the internal transaction-response option.
- The local reverse client keeps replying with a normal `WRITE_MSGS_RSP`, but when
  read-ahead is enabled, the server advertised `read_ahead=1` during tunnel
  authentication, and the write succeeds, it appends an internal `PRF0` prefetch
  bundle before the timing trailer.
- The cloud reverse server always strips that internal bundle before replying to
  the virtual DLL. It records bundled read data only when its own read-ahead flag
  is enabled.
- Prefetched frames are stored in a per-channel consume-once FIFO. The FIFO is
  checked before the empty `ReadMsgs` cache so real prefetched data cannot be
  masked by a recent `BUFFER_EMPTY` cache entry.
- A later ordinary `WRITE_MSGS_REQ` does not clear already-prefetched frames.
  Those frames were already consumed from the local driver, so dropping them
  would create missing data and break queue ordering. They remain ahead of any
  newer read-ahead frames and are served in FIFO order.
- This first cut returns up to the number of prefetched messages requested by
  GDS2. It does not yet merge a partial prefetched response with an additional
  tunnel read or wait for a separate grace window; those behaviors remain
  hardening items after real-vehicle validation.

Unified runtime config:

```text
VCI_PROXY_READ_AHEAD=0
VCI_PROXY_READ_AHEAD_WINDOW_MS=200
VCI_PROXY_READ_AHEAD_MAX_READS=3
VCI_PROXY_READ_AHEAD_READ_TIMEOUT_MS=0
VCI_PROXY_READ_AHEAD_MAX_MESSAGES=16
VCI_PROXY_READ_AHEAD_MAX_EMPTY_READS=0
VCI_PROXY_READ_AHEAD_MAX_CONSECUTIVE_EMPTY_READS=0
VCI_PROXY_READ_AHEAD_MIN_DRAIN_MS=0
VCI_PROXY_READ_AHEAD_TRANSACTION=0
VCI_PROXY_READ_AHEAD_TRANSACTION_MAX_NETWORK_MS=400
VCI_PROXY_READ_AHEAD_TRANSACTION_COOLDOWN_MS=10000
```

Set `VCI_PROXY_READ_AHEAD=1` in both the cloud reverse server environment and
the local reverse client/tray environment to enable the feature without adding
long command-line arguments. The reverse server can read these values from the
repo `.env` file when `python-dotenv` is installed; the tray client applies the
same names as runtime overrides on top of `%APPDATA%/VCI_Proxy/config.json`.
Set `VCI_PROXY_READ_AHEAD_WINDOW_MS` to `0` to keep the feature configured but
prevent local read collection. CLI flags still exist for one-off tests:
`--read-ahead`, `--no-read-ahead`, `--read-ahead-window-ms`,
`--read-ahead-max-reads`, `--read-ahead-read-timeout-ms`,
`--read-ahead-max-messages`, `--read-ahead-max-empty-reads`,
`--read-ahead-max-consecutive-empty-reads`, `--read-ahead-min-drain-ms`,
`--read-ahead-transaction`, and `--no-read-ahead-transaction`.
`VCI_PROXY_READ_AHEAD_MIN_DRAIN_MS` is default-off; when set, the local client
keeps trying through early empty reads until the minimum drain window elapses,
which helps capture frames that arrive shortly after a successful foreground
write instead of exiting immediately on two zero-timeout empty polls. The
reverse-server-only slow-link guard is
controlled by `--read-ahead-transaction-max-network-ms` and
`--read-ahead-transaction-cooldown-ms`.

Important behavior rules:

- If write fails, do not read ahead; the cloud server also clears any pending
  prefetched frames for that channel on write error, forward failure, or response
  timeout because channel ordering is no longer confidently tied to a successful
  write chain.
- If the reverse server does not advertise read-ahead capability during tunnel
  authentication, the local reverse client does not perform read-ahead even if
  its local config flag is set.
- If channel state changes, clear prefetch FIFO.
- If `STOP_FILTER_REQ`, mutating `IOCTL_REQ`, `DISCONNECT_REQ`, or `CLOSE_REQ` occurs, clear affected FIFO.
- Transaction-wrapped non-blocking `READ_MSGS_REQ` calls keep the foreground
  response unchanged, including `BUFFER_EMPTY`, but still run the bounded local
  tail probe so immediately-following frames can be queued for the next serial
  read. If `VCI_PROXY_READ_AHEAD_MIN_DRAIN_MS` is configured, this read-tail
  probe clamps the drain window to `8ms` rather than using the full post-write
  drain setting. If tail data has already been collected and a following tail
  read is empty, the one empty-read grace retry may also wait briefly within the
  same `8ms` cap before trying once more. When the normal read-tail `max_reads`
  budget is exhausted at that point, the local client may spend one extra
  after-data grace read attempt, still bounded by the collection deadline and
  message limit. If that extra grace read returns data, the local client may
  spend one more boundary-confirmation probe; this captures the next short burst
  segment without granting the full data-at-budget continuation budget to the
  empty-grace path.
- The cloud server keeps ordinary read-tail collection light, but can deepen the
  next `READ_AND_COLLECT_READS_REQ` up to the existing write-collect read budget
  when recent same-channel FIFO data was just recorded or just exhausted. This
  targets the observed `prefetch_partial_hit` followed immediately by
  `fifo_empty_after_prefetch_exhausted` misses while avoiding extra work after a
  confirmed empty foreground read.
- If the read-tail probe reaches its normal `max_reads` budget while the latest
  local read still returned data, it may also spend a small fixed number
  (currently `2`) of data-continuation probes to capture the next short burst
  segment. These probes are still bounded by the collection deadline and message
  limit. The full two-probe budget is not stacked on top of an already
  budget-extending empty-grace retry.
- If GDS2 requests more messages than prefetched, the server now forwards one
  reduced `READ_MSGS_REQ` for the remaining count and merges prefetched frames
  first, then tunnel frames. If the reduced tunnel read returns `BUFFER_EMPTY`,
  the prefetched frames are returned with success. If it returns another error,
  the drained FIFO frames are restored and the tunnel error is returned.
- If a `READ_MSGS_REQ` arrives while read-ahead is in progress, a future
  hardening pass may wait for a very small grace window, for example `20-40ms`,
  then fall back to a real tunnel read.
- If recent tunnel forwarding reaches the transaction guard threshold, the cloud
  server temporarily sends a zero-budget `WRITE_AND_COLLECT_READS_REQ` instead
  of a collecting transaction. This preserves the synchronous write path while
  avoiding extra local read-ahead work during degraded windows.

Expected effect:

- Convert some `WRITE -> READ -> READ` tunnel chains into `WRITE -> local read-ahead -> cloud cache hit`.
- Reduce real tunnel round trips per live-data refresh.
- Improve cloud GDS2 freshness while preserving the synchronous DLL API.

Risks:

- Requires protocol extension and careful state management.
- Incorrect FIFO handling can drop, duplicate, or reorder messages.
- Some ECUs or protocols may require nonzero read timeout; tuning must be vehicle/protocol aware.

Validation:

- Unit tests for FIFO consume-once behavior.
- Integration tests for write/read order preservation.
- A/B real-vehicle run comparing local GDS2, cloud baseline, and cloud read-ahead.
- Confirm no duplicate frames and no missing expected responses in logs.

### Phase 4: Pattern-Specific Transaction RPC

Goal: collapse a known GDS2 live-data request pattern into one internal tunnel operation while preserving the external J2534 API.

Design idea:

- Reverse server detects a stable GDS2 pattern such as `WRITE_MSGS_REQ` followed by repeated `READ_MSGS_REQ` on the same channel.
- Instead of forwarding only a raw write, it sends an internal transaction RPC to the reverse client:

```text
WRITE_AND_COLLECT_READS_REQ
  channel_id
  write_messages
  write_timeout
  collect_window_ms
  max_reads
  max_messages
```

- Reverse client performs the write and bounded local reads near the VCI.
- Reverse server still replies to the virtual DLL with standard `WRITE_MSGS_RSP` and later standard `READ_MSGS_RSP` frames.

Implementation note:

- The guarded first cut adds an internal `WRITE_AND_COLLECT_READS_REQ` frame.
- The external DLL/GDS2 request remains a normal `WRITE_MSGS_REQ`; the cloud
  reverse server wraps it only when `VCI_PROXY_READ_AHEAD=1`,
  `VCI_PROXY_READ_AHEAD_TRANSACTION=1`, and the authenticated local client has
  advertised `write_collect=1`.
- The local client advertises `write_collect=1` only when its own read-ahead and
  transaction flags are enabled. Older clients or clients without that flag
  automatically fall back to Phase 3/normal write behavior.
- The transaction request carries the bounded collection parameters
  `collect_window_ms`, `max_reads`, `read_timeout_ms`, and `max_messages`.
  Post-write collection uses `VCI_PROXY_READ_AHEAD_WRITE_COLLECT_MAX_READS`
  (default `6`) so it can drain deeper than the stricter read-tail collection
  cap without widening every non-blocking `READ_MSGS_REQ`.
- `VCI_PROXY_READ_AHEAD_WRITE_COLLECT_MAX_READS` is only the hard cap. Once a
  post-write collection has captured data, the local client stops at the softer
  `VCI_PROXY_READ_AHEAD_MAX_READS` budget when that value is lower, so the write
  path does not always pay all six local reads during continuous GM A9 response
  streams. The soft stop is checked before minimum-drain empty retry behavior
  after data has already been collected.
- The local client still returns a standard `WRITE_MSGS_RSP` plus the same
  internal consume-once `PRF0` prefetch bundle used by Phase 3. The server strips
  the bundle before replying to GDS2 and records the frames in the existing FIFO.
- The same transaction transport also supports `READ_AND_COLLECT_READS_REQ` for
  non-blocking foreground reads. The local client returns the exact foreground
  `READ_MSGS_RSP` to GDS2 and attaches extra tail data in the `PRF0` bundle;
  this includes the case where the foreground response is `BUFFER_EMPTY`. Tail
  collection can also attach a `BUFFER_EMPTY` boundary confirmation. The cloud
  server strips that confirmation, records it only as a short-lived empty-cache
  entry after the foreground response is recorded, and never stores it in the
  consume-once data FIFO. When `VCI_PROXY_READ_AHEAD_MIN_DRAIN_MS` is
  configured, read-tail collection uses a hard cap of `8ms` so a `40ms`
  write-collect drain setting does not stall every foreground read.
- The cloud server now has a transaction slow-link guard. When any forwarded
  request response reaches `VCI_PROXY_READ_AHEAD_TRANSACTION_MAX_NETWORK_MS`
  (default `400ms`), the server arms a cooldown
  (`VCI_PROXY_READ_AHEAD_TRANSACTION_COOLDOWN_MS`, default `10000ms`). While the
  guard is active, transaction-capable clients receive
  `WRITE_AND_COLLECT_READS_REQ` with `collect_window_ms=0`, `max_reads=0`, and
  `max_messages=0`; this prevents ordinary local read-ahead collection from
  adding more synchronous work to a degraded Data Display path.

Expected effect:

- Stronger RTT reduction than opportunistic read-ahead.
- Better control over collection window and message limits.

Risks:

- More invasive than Phase 3.
- Pattern detection must be conservative.
- Must be disabled automatically if behavior diverges from expected GDS2 flow.

Validation:

- Feature-flagged rollout only.
- Per-vehicle/protocol allowlist until enough evidence exists.
- Compare live-data correctness against local GDS2.
- Verify `proxy.request.forwarded_to_tunnel` includes
  `forwarded_msg_name=WRITE_AND_COLLECT_READS_REQ` only when the transaction
  flag and client capability are both present.
- Verify `read_ahead.transaction.guard_armed` and
  `write_collect_guarded_no_collect` appear during slow-link tests and disappear
  when tunnel latency returns below the guard threshold.

### Phase 5: Local-Side Polling Subscription

Goal: move high-frequency J2534 polling near the VCI and use fresh local
results to reduce repeated foreground write/read crossings.

The first guarded implementation stage is available as `observe_only` and
`shadow_local`. A narrow exact-signature `active_replay` path also exists, but
it is not a proven latency solution yet. Treat it as an experimental canary
behind the shadow-match gate described in
`agent_docs/ops/proxy_j2534_local_sweep_scheduler.md`.

The detailed long-term design is maintained in
`agent_docs/ops/proxy_j2534_local_sweep_scheduler.md`. That document supersedes
this short roadmap section when implementing Phase 5.

Implemented first-stage behavior:

- `observe_only` learns exact allowlisted UDS `0x22`, OBD Mode 01 one-PID,
  and strict observed GM `A9 81 xx` request signatures from normal GDS2
  traffic and emits candidate/confidence, cadence, rejection, request-inventory,
  coverage, and projected RTT-cost observability.
- `sweep.inventory.signature` and `sweep.inventory.summary` record unique
  request signatures, counts by kind, learned/replay-candidate coverage,
  rejection reasons, per-signature write/read/pair network p50/p95/max, and
  projected RTT savings. They are observability-only and do not change J2534
  behavior.
- `shadow_local` installs a learned plan on the local reverse client only after
  mode/capability gates pass and a short configurable plan delay lets same-burst
  learned signatures join the first plan. GM `A9 81 xx` signatures are skipped
  from shadow execution by default and logged as `sweep.plan.skipped` unless
  `VCI_PROXY_LOCAL_SWEEP_SHADOW_ALLOW_GM_A9_PACKET=1` is explicitly set.
- v1 shadow transport uses server-driven non-blocking `SWEEP_STATUS_REQ/RSP`
  plus immediate `SWEEP_DRAIN_RESULTS_REQ/RSP`.
- In `shadow_local`, shadow results are stored only for comparison and are
  never served to GDS2.
- Shadow comparison logs distinguish startup/not-drained windows
  (`sweep.shadow.not_ready`) from true missing shadow records
  (`sweep.shadow.missing`).
- In `observe_only` and `shadow_local`, real `WRITE_MSGS_REQ` and
  `READ_MSGS_REQ` continue through the existing normal proxy path; local sweep
  does not synthesize replies or skip forwarding.
- In `active_replay`, the cloud may synthesize only an exact learned
  non-GM-A9 write/read pair whose latest shadow generation is fresh,
  success-coded, non-empty, and repeatedly comparison-clean. All other traffic
  falls back to the normal tunnel/read-ahead/transaction path.

Long-term design idea:

- Cloud sends a subscription or polling plan to the local side.
- Local reverse client performs repeated J2534 polling near the VCI.
- Cloud receives raw or decoded updates as a stream.
- Cloud-side GDS2/DLL calls are satisfied from a strictly ordered local-side message queue.

Expected effect:

- Largest reduction in tunnel RTT count.
- Best fit for continuous live-data displays.

Risks:

- Most invasive.
- More likely to diverge from ordinary J2534 DLL behavior.
- Requires strong correctness tests and probably protocol-specific tuning.

Use replay only after observe/inventory shows useful non-GM-A9 replay-candidate
coverage and `shadow_local` proves `sweep.shadow.match` on the same signatures.
Do not use GM `A9 81 xx` absence of replay events as a failure signal.

### Phase 5 Remaining Work

The following remain undone, unsupported, or not proven for product rollout:

- broad `active_replay` outside the exact-signature canary path
- production use of synthetic write success replies
- production use of synthetic `READ_MSGS_RSP` replies
- skipped real tunnel forwarding for any traffic that has not passed the
  shadow-match gate
- serving shadow data through `_try_serve_cached()` or the read-ahead FIFO
- unsolicited result push
- blocking long-poll drain
- production rollout controls
- allowlist expansion beyond exact UDS `0x22`, OBD Mode 01, and strict
  observed GM `A9 81 xx` request shapes
- adaptive scheduler tuning
- replay decision logic that is driven by full-session inventory coverage and
  shadow-fidelity summaries rather than the current exact-signature runtime
  gate alone

## Optimizations Not Recommended

Do not implement these without a new design review:

- Caching real `READ_MSGS` data responses as reusable cache entries. `ReadMsgs` consumes data; replaying it can duplicate or reorder ECU messages.
- Increasing `ReadMsgsCache` TTL to seconds. That may reduce tunnel calls but can directly worsen live-data freshness.
- Arbitrarily parallelizing J2534 calls. Driver and ECU session state may not be thread-safe or order-independent.
- Reordering requests across channels unless each operation is proven independent.
- Masking the problem only at the UI layer by smoothing or delaying value display. That does not fix stale diagnostic data.

## Implementation Sequence

Recommended order:

1. Add Phase 0 observability.
2. Implement Phase 1 post-write empty-cache bypass.
3. Run real-vehicle A/B test on Engine Data.
4. Implement Phase 2 adaptive TTL only if Phase 1 improves freshness but increases idle tunnel traffic too much.
5. Prototype Phase 3 local-side read-ahead behind a disabled-by-default feature flag.
6. Promote Phase 3 only after logs prove consume-once FIFO correctness.
7. Use `VCI_PROXY_LOCAL_SWEEP=1` with `VCI_PROXY_LOCAL_SWEEP_MODE=observe_only`
   to collect sweep evidence when read-ahead/transaction are still insufficient.
8. Keep `VCI_PROXY_LOCAL_SWEEP_MODE=shadow_local` in comparison-only mode for
   short windows. With the current Engine Control Module / Engine Data evidence,
   keep `VCI_PROXY_LOCAL_SWEEP_SHADOW_ALLOW_GM_A9_PACKET=0`; the learned GM A9
   signatures should log `sweep.plan.skipped` rather than starting a high-rate
   local shadow loop.
9. Prove at least one repeated non-GM-A9 UDS/OBD signature reaches
   `sweep.shadow.match` with fresh, success-coded, non-empty shadow results and
   no Data Display regression.
10. Enable the exact-signature `active_replay` canary only for signatures that
    passed that shadow-match gate, then prove `active_replay_armed` /
    `active_replay_served` reduces forwarded write crossings and improves DID
    cadence. Write a separate ADR/spec before any GM A9 execution or broader
    replay rollout.

## Suggested Code Touch Points

| Area | Likely files |
| --- | --- |
| Empty cache invalidation / adaptive TTL | `vci_proxy/cache_read_msgs.py`, `vci_proxy/reverse_server.py`, `vci_proxy/config.py` |
| Runtime request observability | `vci_proxy/reverse_server.py`, `vci_proxy/benchmark.py`, `agent_docs/ops/product_observability.md` |
| Protocol sideband / transaction messages | `vci_proxy/protocol.py`, `vci_proxy/reverse_server.py`, `vci_proxy/reverse_client.py` |
| Local read-ahead execution | `vci_proxy/reverse_client.py` |
| Local sweep observe/shadow | `vci_proxy/sweep_*.py`, `vci_proxy/reverse_server.py`, `vci_proxy/reverse_client.py`, `vci_proxy/protocol.py` |
| Tests | `tests/test_vci_proxy_caches.py`, `tests/test_reverse_server.py`, `tests/test_reverse_client.py`, `tests/test_reverse_tunnel_integration.py`, `tests/test_proxy_benchmark.py` |

## Test Strategy

Minimum regression coverage before changing runtime behavior:

- `ReadMsgsCache` invalidates or bypasses after `WRITE_MSGS_REQ`.
- Post-write bypass does not affect unrelated channels.
- Cache state clears on `DISCONNECT_REQ` and `CLOSE_REQ`.
- Adaptive TTL chooses active vs idle TTL correctly.
- Runtime observability emits decoded read/write metadata without leaking payload-sensitive data.

Additional coverage for read-ahead:

- Prefetch FIFO is consume-once.
- Prefetch FIFO preserves message order.
- Prefetch FIFO is cleared on channel state mutation.
- A partial prefetched response plus tunnel fallback does not duplicate messages.
- Feature flag disabled preserves current request/response behavior byte-for-byte where practical.

Additional coverage for local sweep observe/shadow:

- classifier/signature allowlist and redaction
- stable-loop learner threshold and would-have-hit estimates
- internal `SWEEP_*` message encoding
- shadow store isolation from `_try_serve_cached()` and prefetch FIFO
- foreground J2534 calls serialized with shadow calls
- DLL-facing traffic never observes `SWEEP_*`

Recommended test command:

```powershell
venv32\Scripts\python.exe -m pytest `
  tests\test_vci_proxy_caches.py `
  tests\test_reverse_server.py `
  tests\test_reverse_client.py `
  tests\test_reverse_tunnel_integration.py `
  tests\test_proxy_benchmark.py -q
```

## Rollout Plan

Roll out in guarded stages:

1. `observe_only`: collect Phase 0 fields, no behavior change.
2. `post_write_bypass`: enable Phase 1 for one real-vehicle test session.
3. `adaptive_empty_cache`: enable Phase 2 if needed.
4. `read_ahead_shadow`: execute read-ahead locally but do not serve prefetched messages to GDS2; compare what would have been served.
5. `read_ahead_enabled`: serve prefetched messages only after shadow results match expected ordering.
6. `local_sweep_observe_only`: learn read-only Data Display sweep signatures without extra ECU traffic.
7. `local_sweep_shadow_local`: run a short local shadow plan and compare only; do not synthesize replies or skip normal GDS2 forwarding.
8. `active_replay` canary: enable only for exact non-GM-A9 signatures that
   already passed the shadow-match gate, then prove reduced forwarded writes
   and faster DID cadence before any broader rollout.

Rollback trigger examples:

- Engine Speed freshness gets worse.
- Duplicate or missing ECU frames are detected.
- GDS2 shows communication errors.
- Tunnel-quality p95 moves into `block`.
- J2534 error rate increases after enabling a feature flag.

## Acceptance Criteria

A successful optimization should meet all of these:

- Cloud Engine Data `Engine Speed` delay moves materially closer to local GDS2 behavior.
- No duplicate, missing, or reordered J2534 messages are observed in A/B logs.
- `READ_MSGS_REQ(data)` p95 `network_ms` remains within the deployment gate from `agent_docs/reports/network_ms.md`.
- Real tunnel round trips per live-data refresh window decrease or post-write freshness improves enough to justify any added traffic.
- Feature flags allow immediate rollback without code changes.

Current status against these criteria:

- stability criterion is partially met for recent safe-transport and ECU runs:
  Data Display can refresh without replay serving, and `shadow_local` can
  negotiate, start plans, execute local items, and drain results;
- value-level Engine Speed improvement is not met. Real-vehicle runs contained
  meaningful Engine Speed changes, but the focus DID cadence stayed multi-second
  (`~9.1s` p50 in the later run, worse in earlier runs), which does not match
  the local `1-2s` target;
- RTT-count reduction for the full page is still incomplete. Nearby tunnel p95
  was often only `38-40ms`, yet each Engine Speed change window still involved
  dozens of forwarded requests. This points to page-wide serial request count,
  especially remaining write-side crossings, rather than a single slow tunnel
  request;
- ECU battery-voltage runs are useful mechanism tests, but the latest evidence
  is not sufficient to prove freshness improvement because the repeated
  foreground traffic was dominated by GM `A9 81 xx`, which remains
  observe-only / inventory-only;
- slow-link guard behavior remains unproven unless a future WAN/degraded-link
  run crosses the configured guard threshold.

## Open Questions

- Which Engine Data groups expose repeated non-GM-A9 UDS/OBD read-only
  signatures that can safely produce `sweep.shadow.match`?
- What is the minimum write-side reduction path that can safely remove one
  foreground tunnel crossing per repeated read-only DID without changing GDS2's
  visible J2534 semantics?
- Does any read-side issue remain after read-ahead/transaction, or has the
  remaining visible lag now moved decisively to page-wide write-side crossing
  count?
- Under an intentionally slow tunnel or a naturally degraded WAN, does the
  transaction guard arm and suppress collection without causing disconnects?
- Are there protocol-specific differences between CAN, ISO15765, and other J2534 protocols that require separate read-ahead tuning?
- What is the best freshness target for cloud GDS2: match local `1-2s`, or accept a defined cloud threshold such as `<3s`?
