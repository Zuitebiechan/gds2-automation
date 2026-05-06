# Proxy J2534 Latency Optimization Plan

## Document Role

| Field | Content |
| --- | --- |
| Type | Optimization plan / implementation guide |
| Status | Phases 1-4 plus first Phase 5 observe/shadow stage implemented behind disabled-by-default flags |
| Owner scope | Proxy J2534 reverse tunnel latency, especially cloud GDS2 live data freshness |
| Primary code paths | `vci_proxy/reverse_server.py`, `vci_proxy/reverse_client.py`, `vci_proxy/protocol.py`, `vci_proxy/cache_read_msgs.py` |
| Related docs | `agent_docs/ops/vci_proxy_and_tunnel.md`, `agent_docs/reports/network_ms.md`, `agent_docs/ops/product_observability.md`, `agent_docs/ops/proxy_j2534_local_sweep_scheduler.md` |

## One-Line Conclusion

Cloud GDS2 live data lag is mainly amplified by `J2534 serial request count x tunnel round-trip cost`. The current optimization stack preserves the synchronous J2534 behavior visible to GDS2 while adding safer cache invalidation, adaptive empty-read handling, local-side read-ahead, transaction RPCs, and a first local sweep observe/shadow stage. The sweep stage does not yet reduce DLL-visible round trips because replay remains disabled.

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
| `READ_MSGS_REQ(data)` count and p95 `network_ms` | Real data delivery cost |
| `READ_MSGS_REQ(empty)` count and p95 `network_ms` | Polling noise cost |
| `READ_MSGS_REQ` cache hit rate | Empty-poll suppression effectiveness |
| `WRITE_MSGS_REQ -> first READ_MSGS_REQ(data)` elapsed time | Query-response freshness |
| Post-write `READ_MSGS_REQ` cache hits | Detect empty cache hiding possible ECU responses |
| Real tunnel round trips per display refresh window | Main optimization target |
| Tunnel-quality p95 | Deployment/network gate |

Existing focused value observability should be used when analyzing real vehicle runs:

- `agent.collector.focus_parameters_sampled`
- focused parameters: `Engine Speed`, `Accelerator Pedal Position`
- session/page context: `data_display`, selected data category, live-data active state

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
read_cache_active_window_ms = 500
```

Implementation note:

- The reverse server keeps the existing `--read-cache-ttl` flag as the idle TTL.
- Active TTL is exposed as `--read-cache-active-ttl-ms`.
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
VCI_PROXY_READ_AHEAD_TRANSACTION=0
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
`--read-ahead-max-messages`, `--read-ahead-transaction`, and
`--no-read-ahead-transaction`.

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
- If GDS2 requests more messages than prefetched, the current implementation can
  return fewer messages from the FIFO, matching normal `PassThruReadMsgs`
  behavior. A future hardening pass can add partial FIFO plus tunnel merge if
  logs show it is necessary.
- If a `READ_MSGS_REQ` arrives while read-ahead is in progress, a future
  hardening pass may wait for a very small grace window, for example `20-40ms`,
  then fall back to a real tunnel read.

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
- The local client still returns a standard `WRITE_MSGS_RSP` plus the same
  internal consume-once `PRF0` prefetch bundle used by Phase 3. The server strips
  the bundle before replying to GDS2 and records the frames in the existing FIFO.

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

### Phase 5: Local-Side Polling Subscription

Goal: move high-frequency J2534 polling near the VCI and stream results to the cloud.

The first guarded implementation stage is now available as `observe_only` and
`shadow_local`. It is still disabled by default and does not synthesize GDS2
responses.

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
- Shadow results are stored only for comparison and are never served to GDS2.
- Shadow comparison logs distinguish startup/not-drained windows
  (`sweep.shadow.not_ready`) from true missing shadow records
  (`sweep.shadow.missing`).
- Real `WRITE_MSGS_REQ` and `READ_MSGS_REQ` continue through the existing
  normal proxy path; local sweep does not synthesize replies or skip forwarding.

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

Use replay only after Phases 1-4 and the Phase 5 observe/shadow evidence prove
that tunnel RTT count remains the limiting factor and shadow results match
normal GDS2-visible responses.

### Phase 5 Not Implemented Yet

The following remain undone and disabled:

- `active_replay`
- synthetic write success replies
- synthetic `READ_MSGS_RSP` replies
- skipped real tunnel forwarding
- serving shadow data through `_try_serve_cached()` or the read-ahead FIFO
- unsolicited result push
- blocking long-poll drain
- production rollout controls
- allowlist expansion beyond exact UDS `0x22`, OBD Mode 01, and strict
  observed GM `A9 81 xx` request shapes
- adaptive scheduler tuning
- replay decision logic that consumes inventory coverage and shadow-fidelity
  evidence

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
8. After observe sign-off, use `VCI_PROXY_LOCAL_SWEEP_MODE=shadow_local` only
   for short comparison windows; keep GM A9 shadow disabled unless a separate
   baseline proves Data Display remains stable.
9. Write a separate ADR/spec before any `active_replay` implementation.

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
8. future `active_replay`: separate design and approval required.

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

## Open Questions

- Which Engine Data groups use the observed strict GM `A9 81 xx` packet
  request shape, and do their shadow responses match the normal GDS2-visible
  responses across ECU/vehicle variants?
- Are there protocol-specific differences between CAN, ISO15765, and other J2534 protocols that require separate read-ahead tuning?
- Should read-ahead be allowed globally, or only for allowlisted modules/data categories after validation?
- What is the best freshness target for cloud GDS2: match local `1-2s`, or accept a defined cloud threshold such as `<3s`?
