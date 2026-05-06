# VCI Proxy And Tunnel

## Scope

This document covers the reverse tunnel, local tray client, virtual J2534 path, tunnel authentication, cache behavior, and tunnel-quality tracking.

This document does not restate all deployment commands. For environment setup and runtime commands, read `agent_docs/ops/deployment_and_operations.md`.

For the staged plan to reduce cloud GDS2 live-data latency in the Proxy J2534 architecture, read `agent_docs/ops/proxy_j2534_latency_optimization.md`.
For the long-term local-side Data Display sweep scheduler design, read
`agent_docs/ops/proxy_j2534_local_sweep_scheduler.md`.

## Purpose

The VCI proxy subsystem allows cloud-side OEM software to communicate with a real J2534 device that physically exists on the local side.

The core data path is:

1. OEM software on the cloud machine calls into the virtual J2534 DLL
2. the DLL forwards calls to the local proxy listener on `localhost:9001`
3. the reverse server bridges those requests across the tunnel
4. the reverse client executes real J2534 calls on the local machine
5. responses flow back through the same path

## Main Components

| Path | Role |
| --- | --- |
| `vci_proxy/reverse_server.py` | Cloud-side reverse listener plus local proxy endpoint for the virtual DLL |
| `vci_proxy/reverse_client.py` | Local-side outbound tunnel client and J2534 executor |
| `vci_proxy/client_gui.py` | Local tray GUI that starts/configures the reverse client |
| `vci_proxy/j2534_driver.py` | Local J2534 driver wrapper |
| `vci_proxy/protocol.py` | Binary protocol definitions and message codecs |
| `vci_proxy/auth.py` | HMAC-based PSK authentication helpers |
| `vci_proxy/tunnel_quality.py` | Tunnel-quality snapshot grading and persistence |
| `vci_proxy/cache_read_msgs.py` | short-circuit cache for `ReadMsgs` empty-buffer behavior |
| `vci_proxy/cache_filter_dedup.py` | StartFilter request deduplication |
| `vci_proxy/cache_ioctl.py` | read-only IOCTL caching |
| `vci_proxy/sweep_classifier.py` | allowlist classifier for local sweep observe/shadow |
| `vci_proxy/sweep_inventory.py` | observability-only request inventory, coverage, and RTT-cost estimator for learned sweep signatures |
| `vci_proxy/sweep_learner.py` | cloud-side stable-loop learner for Data Display sweeps |
| `vci_proxy/sweep_executor.py` | local serial shadow executor |
| `vci_proxy/sweep_shadow_store.py` | comparison-only shadow result store |
| `vci_proxy/sweep_protocol.py` | internal `SWEEP_*` control payload helpers |
| `vci_proxy/benchmark.py` | benchmark event helpers and JSONL output |
| `vci_proxy/virtual_dll/` | cloud-side virtual J2534 DLL implementation and component README |

## Reverse Server

`vci_proxy/reverse_server.py` runs on the cloud side.

Responsibilities:

- listen for the local reverse client on port `9000`
- expose a local proxy listener on `127.0.0.1:9001`
- authenticate the reverse client when PSK auth is enabled
- optionally terminate TLS for the reverse-client listener
- proxy binary protocol requests/responses
- maintain request sequencing and pending futures
- apply request-side caches
- track tunnel quality and persist snapshots
- optionally write benchmark events
- learn and compare local sweep plans when disabled-by-default local sweep modes are enabled

## Reverse Client

`vci_proxy/reverse_client.py` runs on the local side.

Responsibilities:

- establish and maintain the outbound connection to the cloud reverse server
- perform registration/auth handshake
- optionally verify the reverse server certificate and hostname over TLS
- load the real local J2534 driver
- execute incoming J2534 requests
- keep reconnecting with backoff when disconnected
- optionally pre-warm device-open behavior
- optionally run a guarded local sweep shadow executor after cloud-side plan installation

## Local Tray GUI

`vci_proxy/client_gui.py` is the operator-facing local client.

Responsibilities:

- persist config in `%APPDATA%\VCI_Proxy\config.json`
- collect server address, ports, auth token, and J2534 driver path
- persist optional reverse-tunnel TLS trust settings
- auto-discover installed J2534 drivers where possible
- launch the architecture-matched local J2534 worker executable
- start the reverse client in the background
- present connection status through the tray icon

## Virtual J2534 DLL

The component under `vci_proxy/virtual_dll/` is the cloud-side shim loaded by OEM software.

Responsibilities:

- expose a standard J2534 DLL surface to OEM software
- forward requests to `localhost:9001`
- allow cloud-side OEM software to speak to local hardware through the tunnel

The component-scoped guide is `vci_proxy/virtual_dll/README.md`.

## Protocol And Registration

The wire protocol is defined in `vci_proxy/protocol.py`.

At connection time:

- auth is enabled by default, and the client sends `AUTH_REQ`
- otherwise the legacy two-phase heartbeat registration is used

## Authentication

Auth helpers live in `vci_proxy/auth.py`.

Current design:

- pre-shared-key auth enabled by default
- HMAC-SHA256 over the current timestamp
- replay protection through allowed timestamp drift plus one-time rejection of
  repeated auth requests seen inside the drift window
- the local reverse client fails fast when auth is enabled but no token is
  configured, instead of attempting an unauthenticated tunnel
- PSK auth still runs even when TLS is enabled, so transport encryption and
  application-level client authentication layer together

Important rule:

- the token is never sent in plaintext

## TLS Transport

TLS for the reverse tunnel is optional and backward-compatible.

Current behavior:

- TLS applies only to the reverse client/server leg on port `9000`
- the local proxy listener on `127.0.0.1:9001` remains a localhost-only cleartext hop
- the tunnel now enforces a minimum TLS version of `1.2` when TLS is enabled
- client certificate verification is optional and disabled by default
- the client uses the system trust store by default, or a custom CA bundle when configured
- there is intentionally no insecure "skip certificate verification" mode

### Reverse server flags

Use these on `vci_proxy/reverse_server.py`:

- `--tls`
- `--tls-cert <server-cert.pem>`
- `--tls-key <server-key.pem>`
- `--tls-ca <client-ca.pem>` when mutually authenticated client certificates are required
- `--tls-require-client-cert` to require a trusted client certificate

Example:

```bash
python -m vci_proxy.reverse_server ^
  --auth-token <shared-token> ^
  --tls ^
  --tls-cert <server-cert.pem> ^
  --tls-key <server-key.pem>
```

For mTLS:

```bash
python -m vci_proxy.reverse_server ^
  --auth-token <shared-token> ^
  --tls ^
  --tls-cert <server-cert.pem> ^
  --tls-key <server-key.pem> ^
  --tls-ca <client-ca.pem> ^
  --tls-require-client-cert
```

### Reverse client flags

Use these on `vci_proxy/reverse_client.py`:

- `--tls`
- `--tls-ca <server-ca.pem>` to trust a private CA bundle instead of relying only on the system trust store
- `--tls-server-name <dns-name>` to override the hostname used for certificate validation

Example:

```bash
python -m vci_proxy.reverse_client ^
  --host <server-host> ^
  --auth-token <shared-token> ^
  --tls ^
  --tls-ca <server-ca.pem> ^
  --tls-server-name <server-dns-name>
```

### Tray-client config

`vci_proxy/client_gui.py` persists the local client configuration in `%APPDATA%\VCI_Proxy\config.json`.

TLS-related keys are:

- `tls_enabled`
- `tls_ca_file`
- `tls_server_name`

Read-ahead keys are also accepted for guarded Phase 3 testing:

- `read_ahead_enabled`
- `read_ahead_window_ms`
- `read_ahead_max_reads`
- `read_ahead_read_timeout_ms`
- `read_ahead_max_messages`
- `read_ahead_transaction_enabled`

These values are passed through to `vci_proxy.reverse_client.ReverseProxyClient`
when the tray app starts the local tunnel client. The shared
`VCI_PROXY_READ_AHEAD*` environment variables override these saved values at
runtime, which lets deployment scripts turn the feature on or off without
editing the tray JSON file.

## Cache Layers

The proxy stack currently includes multiple request-side optimizations.

### `ReadMsgs` cache

`ReadMsgsCache` short-circuits repeated empty-buffer polls for a short TTL.
The cloud reverse server now also marks same-channel `WRITE_MSGS_REQ` calls.
When `read_cache_post_write_bypass_ms` is greater than `0`, a write invalidates
that channel's empty-read cache and forces immediate same-channel `READ_MSGS_REQ`
calls through the tunnel during the configured bypass window. Set the bypass
window to `0` to disable only the post-write invalidation/forced-bypass behavior.
When Phase 2 adaptive TTL is enabled, writes may still put the channel into active
TTL mode. To fully approximate the older fixed-TTL behavior, also set
`--read-cache-active-window-ms 0` and `--read-cache-max-timeout-ms -1`.

The empty-read cache is adaptive:

- quiet channels use the idle TTL, default `150ms`
- active channels use the active TTL, default `25ms`
- active mode lasts for `500ms` after writes, data reads, filter mutations, or
  mutating IOCTLs
- only `ReadMsgs` polls with timeout at or below `25ms` are cacheable by default

Reverse server flags:

- `--no-read-cache`
- `--read-cache-ttl <milliseconds>`; idle empty-cache TTL
- `--read-cache-post-write-bypass-ms <milliseconds>`; default `150`, `0` disables
  the post-write invalidation/bypass behavior
- `--read-cache-active-ttl-ms <milliseconds>`; default `25`
- `--read-cache-active-window-ms <milliseconds>`; default `500`
- `--read-cache-max-timeout-ms <milliseconds>`; default `25`, `-1` allows all
  `ReadMsgs` timeouts to use empty-cache hits

### Local-side `ReadMsgs` read-ahead

Phase 3 read-ahead is disabled by default. It must be enabled on both the cloud
reverse server and the local reverse client. During tunnel authentication, the
server advertises `read_ahead=1` only when its flag is enabled; the local client
will not perform read-ahead without that capability marker. This avoids the
unsafe mismatch where a client consumes local ECU frames but a server is not
prepared to preserve them.

After a successful `WRITE_MSGS_REQ`, the local client can trigger a bounded
number of local `PassThruReadMsgs` calls near the VCI. Any data frames consumed
by those local reads are returned to the server in an internal `WRITE_MSGS_RSP`
prefetch bundle, stripped before the virtual DLL sees the response, and stored
in a per-channel consume-once FIFO.

This is not a reusable data cache. Each prefetched frame was consumed once from
the local J2534 driver and can be served at most once to a later cloud-side
`READ_MSGS_REQ`. The FIFO is checked before the empty-read cache so a recent
`BUFFER_EMPTY` entry cannot hide prefetched data.

An ordinary later `WRITE_MSGS_REQ` does not clear already-prefetched frames.
Those frames were already consumed from the local driver, so dropping them would
create missing data and break queue ordering. They remain ahead of any newer
read-ahead frames and are served in FIFO order.

A failed write is treated differently. If a `WRITE_MSGS_REQ` returns an error,
cannot be forwarded, or times out while waiting for a local response, the cloud
server clears pending prefetched frames for that channel before any later
`READ_MSGS_REQ` can use them.

Unified runtime config:

- `VCI_PROXY_READ_AHEAD=1` enables read-ahead for both reverse server and
  reverse client processes when present in their environment.
- `VCI_PROXY_READ_AHEAD_WINDOW_MS=200`
- `VCI_PROXY_READ_AHEAD_MAX_READS=3`
- `VCI_PROXY_READ_AHEAD_READ_TIMEOUT_MS=0`
- `VCI_PROXY_READ_AHEAD_MAX_MESSAGES=16`
- `VCI_PROXY_READ_AHEAD_TRANSACTION=0`
- `VCI_PROXY_READ_AHEAD_TRANSACTION_MAX_NETWORK_MS=750`
- `VCI_PROXY_READ_AHEAD_TRANSACTION_COOLDOWN_MS=10000`

The reverse server also loads the repo `.env` file when `python-dotenv` is
installed, so the cloud process can be started with the normal command after the
settings are in `.env` or the service manager environment. The tray client
applies the same environment names as runtime overrides on top of its
`%APPDATA%/VCI_Proxy/config.json` values. If an env value is present, it wins
over the saved tray value; unset it to return control to the saved config.

"Set on cloud and local" means the variable must be visible to the process that
uses it:

- cloud: the environment of `python -m vci_proxy.reverse_server`, the cloud
  service wrapper, or the repo `.env` file loaded by that process;
- local: the environment of the tray process or direct
  `python -m vci_proxy.reverse_client` process. The local tray also has saved
  JSON config, but explicit environment variables override it.

After startup, verify configuration from observability rather than assuming the
environment was inherited correctly. The cloud `process.lifecycle.started` event
should show the read-ahead/transaction flags, and the local
`reverse_client.lifecycle.auth_succeeded` reason should include
`read_ahead=1` and `write_collect=1` when both sides are enabled.

Reverse server and reverse client CLI overrides:

- `--read-ahead`; enables read-ahead for this process
- `--no-read-ahead`; disables read-ahead even if `VCI_PROXY_READ_AHEAD` is set
- `--read-ahead-window-ms <milliseconds>`; default `200`, `0` disables local
  read collection
- `--read-ahead-max-reads <count>`; default `3`
- `--read-ahead-read-timeout-ms <milliseconds>`; default `0`
- `--read-ahead-max-messages <count>`; default `16`
- `--read-ahead-transaction`; enables the internal
  `WRITE_AND_COLLECT_READS_REQ` RPC when both sides advertise support
- `--no-read-ahead-transaction`; disables that internal transaction path even if
  `VCI_PROXY_READ_AHEAD_TRANSACTION` is set
- `--read-ahead-transaction-max-network-ms <milliseconds>`; default `750`,
  `0` disables the slow-link guard
- `--read-ahead-transaction-cooldown-ms <milliseconds>`; default `10000`,
  `0` disables the slow-link guard cooldown

### Internal `WRITE_AND_COLLECT_READS` transaction

The Phase 4 transaction path is disabled by default. It is a server-to-client
internal frame, not a J2534 API surface. When enabled on both sides, the cloud
server can wrap an ordinary DLL-side `WRITE_MSGS_REQ` as
`WRITE_AND_COLLECT_READS_REQ` and include bounded collection parameters. The
local client performs the write, then local read collection, and returns the same
standard `WRITE_MSGS_RSP` plus the existing internal `PRF0` prefetch bundle.

Compatibility guardrails:

- the server sends the transaction only when its transaction flag is enabled and
  the authenticated client advertised `write_collect=1`
- older clients and clients without the flag receive ordinary `WRITE_MSGS_REQ`
- GDS2 never sees `WRITE_AND_COLLECT_READS_REQ`; it only sees normal J2534
  request/response frames
- prefetched data remains consume-once FIFO data and is stripped before the
  write response reaches the DLL
- if a tunnel response reaches
  `VCI_PROXY_READ_AHEAD_TRANSACTION_MAX_NETWORK_MS`, the cloud server arms a
  temporary no-collect guard for
  `VCI_PROXY_READ_AHEAD_TRANSACTION_COOLDOWN_MS`. During that window it still
  sends `WRITE_AND_COLLECT_READS_REQ` to transaction-capable clients, but with a
  zero collection budget so the local client performs only the foreground write.
  `proxy.request.forwarded_to_tunnel` then records reason
  `write_collect_guarded_no_collect`, and
  `read_ahead.transaction.guard_armed` records the triggering slow response.

Operational validation as of `2026-05-06`:

- latest ECU Data Display run confirmed transaction capability was active:
  local auth reason included `write_collect=1`, and forwarded write events used
  `reason=write_collect_transaction`;
- no request reached the default `750ms` slow-link threshold in that run, so
  `read_ahead.transaction.guard_armed` and
  `write_collect_guarded_no_collect` correctly remained absent;
- a future slow-link or throttled-network run is still required before claiming
  the guard branch is proven.

FIFO cleanup:

- `DISCONNECT_REQ` clears that channel
- `CLOSE_REQ` clears all channels
- `START_FILTER_REQ`, `STOP_FILTER_REQ`, and non-cacheable/mutating `IOCTL_REQ`
  clear the affected channel

Current limitation: if GDS2 requests more messages than are prefetched, the
server can return fewer messages from the FIFO rather than merging with an
additional tunnel read. That keeps the first implementation conservative and
avoids duplicating consumed frames. Real-vehicle A/B logs should decide whether a
partial FIFO plus tunnel-merge hardening pass is worthwhile.

### Local sweep observe/shadow

The first local sweep scheduler stage is disabled by default. It is configured
with shared environment variables and matching CLI flags on the reverse server
and reverse client:

- `VCI_PROXY_LOCAL_SWEEP=1`
- `VCI_PROXY_LOCAL_SWEEP_MODE=observe_only` or `shadow_local`
- `VCI_PROXY_LOCAL_SWEEP_MIN_CYCLES=2`
- `VCI_PROXY_LOCAL_SWEEP_MAX_ITEMS=128`
- `VCI_PROXY_LOCAL_SWEEP_ALLOW_UDS_RDBI=1`
- `VCI_PROXY_LOCAL_SWEEP_ALLOW_OBD_MODE01=1`
- `VCI_PROXY_LOCAL_SWEEP_ALLOW_GM_A9_PACKET=1`
- `VCI_PROXY_LOCAL_SWEEP_SHADOW_ALLOW_GM_A9_PACKET=0`
- `VCI_PROXY_LOCAL_SWEEP_MAX_RESULT_AGE_MS=1000`
- `VCI_PROXY_LOCAL_SWEEP_MIN_ITEM_INTERVAL_MS=250`
- `VCI_PROXY_LOCAL_SWEEP_READ_TIMEOUT_MS=0`
- `VCI_PROXY_LOCAL_SWEEP_SHADOW_MAX_SECONDS=120`
- `VCI_PROXY_LOCAL_SWEEP_PLAN_DELAY_MS=300`
- `VCI_PROXY_LOCAL_SWEEP_MISMATCH_THRESHOLD=3`
- `VCI_PROXY_LOCAL_SWEEP_ERROR_THRESHOLD=3`

`observe_only` is cloud-side only. It observes normal `WRITE_MSGS_REQ` and later
`READ_MSGS_RSP(data)` pairs, learns stable allowlisted UDS `0x22`, OBD Mode 01
one-identifier, and strict CAN-ID-prefixed GM `A9 81 xx` packet request
signatures, and emits redacted sweep observability. The GM `A9 81 xx` shape is
enabled by `VCI_PROXY_LOCAL_SWEEP_ALLOW_GM_A9_PACKET` and is limited to
`0x7E0..0x7EF` logical ECU targets because it was added from observed Engine
Control Module / Engine Data traffic. It does not require protocol,
reverse-client, local runtime, or local J2534 call changes.

`shadow_local` starts only when both sides are configured for the mode and the
local client advertises `sweep_shadow=1` during tunnel authentication. After the
first learned candidate, the cloud waits `VCI_PROXY_LOCAL_SWEEP_PLAN_DELAY_MS`
(default `300ms`) before starting the plan so other signatures learned in the
same short Data Display burst can join the first plan. The cloud server then
sends internal `SWEEP_PLAN_START_REQ/RSP`, `SWEEP_PLAN_STOP_REQ/RSP`,
`SWEEP_STATUS_REQ/RSP`, and `SWEEP_DRAIN_RESULTS_REQ/RSP` frames. These are
internal server-to-client control frames; the virtual DLL and GDS2 never see
them.

GM `A9 81 xx` signatures remain observable in `shadow_local`, but they are not
included in local shadow execution by default. The cloud emits
`sweep.plan.skipped` with reason `gm_a9_packet_shadow_disabled` when the learned
plan contains only these guarded signatures. Set
`VCI_PROXY_LOCAL_SWEEP_SHADOW_ALLOW_GM_A9_PACKET=1` only for a tightly bounded
experiment after an `observe_only` baseline proves Data Display stability.

The v1 transport uses server-driven non-blocking `STATUS` plus immediate
`DRAIN`. Drain returns queued shadow results or an empty result set without
waiting. Unsolicited result push and blocking long-poll drain are not part of
this stage.

The local shadow executor is serial and uses the same J2534 driver-call path as
foreground requests. Foreground traffic cancels or pauses shadow work before the
next local shadow driver call, and a shared driver-call lock prevents overlapping
foreground/shadow J2534 calls. Cacheable/read-only foreground IOCTLs such as
`READ_VBATT`, `READ_PROG_VOLTAGE`, and `GET_CONFIG` pause through the shared
lock but do not cancel the local shadow plan. Non-cacheable or mutating IOCTLs
still stop shadow work. `VCI_PROXY_LOCAL_SWEEP_MIN_ITEM_INTERVAL_MS` has a
runtime floor of `250ms`; lower configured values are raised to that floor to
avoid high-rate shadow loops competing with GDS2's foreground Data Display
traffic.

Shadow data is comparison-only:

- it is stored in `SweepShadowStore`
- it is compared with normal GDS2-visible `READ_MSGS_RSP` bodies
- it is never used by `_try_serve_cached()`
- it is never written into `PrefetchReadMsgsBuffer`
- it never fulfills normal `READ_MSGS_REQ`

The stage cancels active shadow plans on disconnect, close, filter mutation,
non-cacheable or mutating IOCTL, failed writes, connection epoch changes,
max-seconds expiry, repeated mismatch/error thresholds, and communication-error
escalation.

Shadow comparison logs distinguish not-yet-comparable reads from true missing
shadow data. `sweep.shadow.not_ready` means the cloud has no active plan yet, a
plan is still in the delay window, or an active plan has not drained any results.
`sweep.shadow.missing` is reserved for later reads where comparison should have
been possible but no matching shadow result was available.

Operational validation as of `2026-05-06`:

- latest ECU Data Display run used `VCI_PROXY_LOCAL_SWEEP=1` and
  `VCI_PROXY_LOCAL_SWEEP_MODE=shadow_local` with
  `VCI_PROXY_LOCAL_SWEEP_SHADOW_ALLOW_GM_A9_PACKET=0`;
- the server learned a strict GM `A9 81 xx` signature, then skipped the plan with
  `reason=gm_a9_packet_shadow_disabled`;
- `sweep.plan.started` and `sweep.batch.drained` were absent, so no local shadow
  result comparison occurred in that run;
- this is the intended safe state for current GM A9-only Engine Data evidence.

Not implemented in this stage:

- `active_replay`
- synthetic `WRITE_MSGS_RSP`
- synthetic `READ_MSGS_RSP`
- skipped real forwarding
- serving shadow data to GDS2
- production rollout controls
- allowlist expansion beyond exact UDS `0x22`, OBD Mode 01, and strict observed
  GM `A9 81 xx` request shapes
- adaptive sweep-rate tuning
- safe GM `A9 81 xx` shadow execution by default

### Manual GDS2 latency observability

When the cloud GDS2 UI is operated manually, the Flask/session live-data
collector is not active and there may be no `Engine Speed` value-level samples.
The reverse server therefore emits proxy-layer evidence that still works for
manual tests:

- `READ_MSGS_REQ` and `WRITE_MSGS_REQ` events include per-channel
  `live_inter_request_gap_ms` and `live_inter_request_gap_bucket` values.
- Gaps at or above `1000ms` emit `proxy.j2534.cadence_gap`; `ge_3000ms` marks
  the class of stall that can plausibly match visible multi-second Data Display
  lag.
- Read/write payload evidence is redacted to length, SHA-256 digest, and a
  16-byte hex prefix sample. Full CAN/J2534 payloads are not written.
- For observed 11-bit GM CAN-ID-prefixed payload samples in the
  `0x500..0x7FF` range, observability also includes `can_id`, `can_id_hex`,
  `can_payload_length`, and `can_payload_prefix_hex`. Strict GM `A9 81 xx`
  request samples add `gm_request_service_id`, `gm_request_subfunction`,
  `gm_request_packet_id`, and `gm_request_packet_id_hex`; `0x500..0x5FF`
  response samples add `gm_data_packet_id` and `gm_data_packet_id_hex`.
- If a payload visibly matches standard `41 0C` or `62 F4 0C` Engine Speed
  response patterns, the event includes a best-effort
  `*_engine_speed_candidate_rpm` field for correlation only.

### Filter deduplication

`FilterDeduplicationCache` avoids duplicate filter setup work.

### IOCTL cache

`IoctlCache` caches selected read-only IOCTL responses.

## Tunnel-Quality Tracking

Tunnel-quality logic lives in `vci_proxy/tunnel_quality.py`.

Key behavior:

- maintain a rolling window of probe samples
- compute `last`, `p50`, and `p95`
- classify grade as `good`, `warn`, or `block`
- publish `status` and `reason`
- persist a normalized snapshot to `%PROGRAMDATA%\VCI_Proxy\tunnel_quality.json`

The platform consumes this snapshot during diagnostics preflight.

Important current defaults:

- window size: 5 samples
- freshness window: 10 seconds
- hysteresis windows: 2
- grade thresholds:
  - `good` when `p95 <= 80ms`
  - `warn` when `p95 <= 150ms`
  - `block` otherwise

## Benchmarks

Benchmark helpers live in `vci_proxy/benchmark.py`.

They support:

- proxy benchmark event construction
- JSONL benchmark output
- response-trailer timing extraction

Curated benchmark/report material is indexed in `agent_docs/reports/README.md`.

## Relationship To Session Runtime

The VCI proxy subsystem is operational infrastructure for cloud/local transport.

The business-session runtime uses it indirectly through:

- backend startup/preflight
- tunnel-quality snapshot reads
- J2534 communication through the virtual DLL and reverse server

## Read Next

- operations/setup: `agent_docs/ops/deployment_and_operations.md`
- virtual DLL component note: `vci_proxy/virtual_dll/README.md`
- runtime flow impact: `agent_docs/core/runtime_flows.md`
