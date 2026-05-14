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
- `read_ahead_max_empty_reads`
- `read_ahead_max_consecutive_empty_reads`
- `read_ahead_min_drain_ms`
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
After a real post-write `ReadMsgs(BUFFER_EMPTY)` result is observed, that
confirmed empty result clears the write-bypass marker for the channel; later
short-timeout polls can use the normal active empty-cache TTL instead of paying
the rest of the bypass window.
When Phase 2 adaptive TTL is enabled, writes may still put the channel into active
TTL mode. To fully approximate the older fixed-TTL behavior, also set
`--read-cache-active-window-ms 0` and `--read-cache-max-timeout-ms -1`.
To keep active mode enabled but disable the learned active-TTL raise, set
`--read-cache-active-adaptive-ttl-max-ms` to the same value as
`--read-cache-active-ttl-ms`.

The empty-read cache is adaptive:

- quiet channels use the idle TTL, default `150ms`
- active channels use the active TTL, default `25ms`
- active channels can temporarily raise the effective TTL up to `70ms` when
  repeated real `ReadMsgs(BUFFER_EMPTY)` replies show a stable empty polling
  cadence; the estimate resets after real data or cache invalidation
- active mode lasts for `500ms` after writes, data reads, filter mutations, or
  mutating IOCTLs
- only `ReadMsgs` polls with timeout at or below `25ms` are cacheable by default

Reverse server flags:

- `--no-read-cache`
- `--read-cache-ttl <milliseconds>`; idle empty-cache TTL
- `--read-cache-post-write-bypass-ms <milliseconds>`; default `150`, `0` disables
  the post-write invalidation/bypass behavior
- `--read-cache-active-ttl-ms <milliseconds>`; default `25`
- `--read-cache-active-adaptive-ttl-max-ms <milliseconds>`; default `70`, caps
  the learned active empty-cache TTL
- `--read-cache-active-adaptive-ttl-margin-ms <milliseconds>`; default `8`, added
  to the observed confirmed-empty polling gap
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
- `VCI_PROXY_READ_AHEAD_WRITE_COLLECT_MAX_READS=6`
- `VCI_PROXY_READ_AHEAD_READ_TIMEOUT_MS=0`
- `VCI_PROXY_READ_AHEAD_MAX_MESSAGES=16`
- `VCI_PROXY_READ_AHEAD_MAX_EMPTY_READS=0`
- `VCI_PROXY_READ_AHEAD_MAX_CONSECUTIVE_EMPTY_READS=0`
- `VCI_PROXY_READ_AHEAD_MIN_DRAIN_MS=0`
- `VCI_PROXY_READ_AHEAD_TRANSACTION=0`
- `VCI_PROXY_READ_AHEAD_TRANSACTION_MAX_NETWORK_MS=400`
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
`read_ahead=1`, `read_collect=1`, and `write_collect=1` when both sides are
enabled.

Reverse server and reverse client CLI overrides:

- `--read-ahead`; enables read-ahead for this process
- `--no-read-ahead`; disables read-ahead even if `VCI_PROXY_READ_AHEAD` is set
- `--read-ahead-window-ms <milliseconds>`; default `200`, `0` disables local
  read collection
- `--read-ahead-max-reads <count>`; default `3`, used as the generic and
  read-tail collection cap
- `--read-ahead-write-collect-max-reads <count>`; default `6`, used for
  post-write transaction collection without raising the read-tail cap
- `--read-ahead-read-timeout-ms <milliseconds>`; default `0`
- `--read-ahead-max-messages <count>`; default `16`
- `--read-ahead-max-empty-reads <count>`; default `0`, disabled unless
  explicitly set
- `--read-ahead-max-consecutive-empty-reads <count>`; default `0`, disabled
  unless explicitly set
- `--read-ahead-min-drain-ms <milliseconds>`; default `0`, disabled unless
  explicitly set. When set, early empty read-ahead polls do not stop collection
  before this minimum drain window elapses; the local client spaces retries
  briefly so delayed ECU frames can still be captured near the VCI.
- `--read-ahead-transaction`; enables the internal
  `WRITE_AND_COLLECT_READS_REQ` RPC when both sides advertise support
- `--no-read-ahead-transaction`; disables that internal transaction path even if
  `VCI_PROXY_READ_AHEAD_TRANSACTION` is set
- `--read-ahead-transaction-max-network-ms <milliseconds>`; default `400`,
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
- write-collect uses the separate
  `VCI_PROXY_READ_AHEAD_WRITE_COLLECT_MAX_READS` budget so post-write bursts can
  drain more deeply while `READ_AND_COLLECT_READS_REQ` remains capped by the
  tighter `VCI_PROXY_READ_AHEAD_MAX_READS`/server-side read-tail guard
- the write-collect budget is a hard cap, not an unconditional loop: after local
  collection has captured data, the local client treats
  `VCI_PROXY_READ_AHEAD_MAX_READS` as a soft stop so continuous response streams
  do not make every foreground `WRITE_MSGS_REQ` pay the full deeper
  write-collect budget; this soft stop also wins over the minimum drain window
  after data has already been collected
- if a tunnel response reaches
  `VCI_PROXY_READ_AHEAD_TRANSACTION_MAX_NETWORK_MS`, the cloud server arms a
  temporary no-collect guard for
  `VCI_PROXY_READ_AHEAD_TRANSACTION_COOLDOWN_MS`. During that window it still
  sends `WRITE_AND_COLLECT_READS_REQ` to transaction-capable clients, but with a
  zero collection budget so the local client performs only the foreground write.
  `proxy.request.forwarded_to_tunnel` then records reason
  `write_collect_guarded_no_collect`, and
  `read_ahead.transaction.guard_armed` records the triggering slow response.
  Lightweight `READ_AND_COLLECT_READS_REQ` read-tail collection remains enabled
  during the guard on the standard non-deepened budget so a single slow read does
  not make the following serial `ReadMsgs` loop lose FIFO tail collection.

The same transaction capability now also supports a narrower
`READ_AND_COLLECT_READS_REQ` path for Data Display tail bursts. When both sides
advertise `read_collect=1`, a DLL-facing non-blocking `READ_MSGS_REQ`
(`timeout=0`) can be wrapped so the local client performs the real foreground
read first, returns that exact foreground result to GDS2, then uses a bounded
zero-min-drain collection budget to consume any immediately queued tail frames
into the same consume-once FIFO. This tail collection now runs even when the
foreground read itself returns `BUFFER_EMPTY`: GDS2 still receives that real
empty foreground result, while any frame that arrives during the immediate local
tail probe can be served to the next serial `READ_MSGS_REQ` from FIFO. When
`VCI_PROXY_READ_AHEAD_MIN_DRAIN_MS` is configured, read-tail collection uses a
separate hard cap of `8ms` so a `40ms` post-write drain setting does not make
every foreground read wait the full write-collect window. The read-tail budget
is intentionally tighter than write collection: at most `40ms`, `3` extra local
read calls, `16` messages, and `read_timeout_ms=0`. Blocking reads are not
wrapped, and the slow transaction guard falls back to ordinary `READ_MSGS_REQ`
forwarding while active. This reduces later serial `ReadMsgs` tunnel trips
without replaying or decoding GM A9 payloads.

When read-tail or write-tail collection reaches a real local
`ReadMsgs(BUFFER_EMPTY)` boundary, the local client can append that empty
confirmation to the internal `PRF0` bundle. The cloud server strips it before
replying to the DLL, does not put it in the consume-once data FIFO, and records
it only in the short-lived `ReadMsgsCache` after the foreground response has
already been recorded. This suppresses the next immediate serial empty poll
without caching, duplicating, or decoding any real ECU data frame.

When recent evidence shows the FIFO just received data or was just exhausted by
an oversized non-blocking `ReadMsgs`, the next read-tail transaction can
temporarily deepen its local read budget up to the existing write-collect cap.
This targets the observed partial-hit-then-immediate-miss pattern while keeping
confirmed-empty foreground reads on the lighter standard path. Cloud events add
`read_collect_budget_reason`, `read_collect_budget_deepened`,
`read_collect_collect_window_ms`, `read_collect_max_reads`,
`read_collect_read_timeout_ms`, and `read_collect_max_messages` to show which
budget was used.
If a short-lived confirmed empty follows immediately after a FIFO drain that
served data, the next read-tail probe may also deepen once under
`read_collect_budget_reason=after_confirmed_empty_following_prefetch_drain`.
That confirmed-empty deepening is one-shot per FIFO drain: after one deepened
probe, later reads tied to the same drained FIFO return to the standard light
budget until new FIFO data is drained. Pure confirmed-empty polling remains on
the standard light read-tail budget, so this does not turn idle empty loops into
deeper local polling.

Oversized non-blocking `ReadMsgs` calls can request far more frames than the
FIFO is allowed to hold, for example `num_msgs=300` against a 16-frame FIFO.
When such a request partially drains FIFO data and the local client advertises
read-collect support, the cloud now performs a reduced
`READ_AND_COLLECT_READS_REQ` for the underfilled remainder and merges those
tunnel frames with the FIFO frames before replying to the DLL. This preserves
the foreground J2534 response shape while avoiding the older pattern where a
small partial FIFO hit was returned immediately and the next serial read paid
another tunnel RTT. If read-collect is unavailable, the server falls back to the
legacy direct partial response rather than blocking the DLL.

The read-tail drain stops after the first `BUFFER_EMPTY` when no tail data has
been collected after the capped minimum-drain window. If tail data was already
collected, one empty-read grace attempt is allowed before stopping. When the
normal read-tail budget is already exhausted, the client may spend one extra
local read attempt for this after-data grace path, still bounded by the
collection deadline and message limit. The client can wait briefly, capped at
`8ms`, before that grace retry so short burst gaps can still be captured without
returning to an unconditional three-read drain. Observability reports
`empty_after_data_grace_extra_read_used=true` when this extra attempt was needed.
`empty_after_data_grace_empty` means the grace retry was actually attempted and
also returned empty. If that extra grace read returns data instead, the client
may spend one more boundary-confirmation probe before stopping. Observability
reports this as `empty_after_data_grace_data_extra_read_used=true` with
`empty_after_data_grace_data_extra_read_attempts` and
`empty_after_data_grace_data_extra_read_limit`; collection stops with
`empty_after_data_grace_data_extra_empty` when the confirmation probe finds the
burst boundary, or `empty_after_data_grace_data_extra_limit` when that bounded
probe also returns data.
If the read-tail probe reaches its normal `max_reads` budget while the latest
local read still returned data, the client may spend a small fixed number
(currently `2`) of additional data-continuation probes before stopping. This is reported as
`extra_read_after_data_at_max_used=true` with
`extra_read_after_data_at_max_attempts` and
`extra_read_after_data_at_max_limit`; collection stops with
`extra_read_after_data_at_max_limit` when the bounded final probe also returned
data, or `extra_read_after_data_at_max_empty` when it found the burst boundary.
The full data-at-budget continuation budget is not stacked on top of an already
budget-extending empty-grace retry; the grace-data path gets only the single
boundary-confirmation probe described above.
This only changes opportunistic extra local reads; it does not drop data or
change the foreground `READ_MSGS_RSP`, because later ECU frames remain in the
real J2534 queue for the next GDS2 read. Cloud FIFO hit events include
`prefetch_source_counts` and `prefetch_age_*_ms` fields so local
simulated-cloud tests can separate `read_collect` and `write_collect`
usefulness and measure how long prefetched frames waited before DLL
consumption. FIFO miss events include `prefetch_miss_detail`, read-collect
eligibility/block reason, empty-cache state, and last prefetch record/drain/read
result ages so analysis can distinguish no prior local collection, a
just-exhausted FIFO, a recently confirmed empty read, or a temporarily
unavailable read-collect path.
Response events include `prefetch_empty_confirmation_count` and
`prefetch_empty_cache_recorded` when a tail empty boundary was converted into a
short-lived empty-cache entry.

Operational validation as of `2026-05-06`:

- latest ECU Data Display run confirmed transaction capability was active:
  local auth reason included `write_collect=1`, and forwarded write events used
  `reason=write_collect_transaction`;
- no request reached the then-default `750ms` slow-link threshold in that run, so
  `read_ahead.transaction.guard_armed` and
  `write_collect_guarded_no_collect` correctly remained absent;
- a future slow-link or throttled-network run is still required before claiming
  the guard branch is proven.

FIFO cleanup:

- `DISCONNECT_REQ` clears that channel
- `CLOSE_REQ` clears all channels
- `START_FILTER_REQ`, `STOP_FILTER_REQ`, and non-cacheable/mutating `IOCTL_REQ`
  clear the affected channel

If GDS2 requests more messages than are currently prefetched, the server now
drains the available FIFO frames, forwards one reduced `READ_MSGS_REQ` for the
remaining count, and returns the prefetched frames first followed by any tunnel
frames. If the reduced tunnel read returns `BUFFER_EMPTY`, the server returns
the prefetched frames with success. If the reduced tunnel read returns another
error, the server restores the drained FIFO frames and returns the real tunnel
error to preserve J2534-visible semantics. Same-channel FIFO drain/merge is
serialized with a per-channel lock so another `READ_MSGS_REQ` cannot observe
the FIFO while a partial underfill fallback is still waiting on the tunnel.

For oversized non-blocking reads, the server uses a tighter fast path. When
`READ_MSGS_REQ` has `timeout=0`, the FIFO already contains data, and the request
asks for more messages than the FIFO can ever hold, the server returns the
available prefetched frames immediately with `reason=prefetch_partial_hit`
instead of issuing a reduced tunnel read. This preserves non-blocking J2534
semantics because `num_msgs` is a maximum, not a required fill count, and avoids
the observed `num_msgs=300` Data Display pattern paying another tunnel RTT after
local read-ahead already captured fresh frames.

### Local sweep observe/shadow

The first local sweep scheduler stage is disabled by default. It is configured
with shared environment variables and matching CLI flags on the reverse server
and reverse client:

- `VCI_PROXY_LOCAL_SWEEP=1`
- `VCI_PROXY_LOCAL_SWEEP_MODE=observe_only`, `shadow_local`, or `active_replay`
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
- `VCI_PROXY_LOCAL_SWEEP_INCLUDE_UDS_DIDS=` optional comma-separated UDS DID allowlist such as `0x000c,0x0031`
- `VCI_PROXY_LOCAL_SWEEP_EXCLUDE_UDS_DIDS=` optional comma-separated UDS DID blocklist such as `0x0031`

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

Shadow plans preserve the foreground `ReadMsgs` message count shape learned
from GDS2, such as the observed `num_msgs=300` Data Display reads, but
`VCI_PROXY_LOCAL_SWEEP_READ_TIMEOUT_MS` is treated as a minimum shadow-only read
timeout. This lets focused fidelity runs test `timeout=1ms` without changing the
foreground DLL-visible `ReadMsgs(timeout=0)` semantics. The cloud
`sweep.plan.started` event reports the effective
`sweep_plan_read_timeout_ms`, and `process.lifecycle.started` reports
`local_sweep_read_timeout_ms`.

When shadow transport is enabled but the effective startup
`local_sweep_read_timeout_ms` is still `0`, the cloud emits
`sweep.config.warning` with
`failure_code=local_sweep_shadow_read_timeout_zero`. Treat that as a test setup
failure for focused shadow-tail validation: the local executor will not run the
bounded echo-only tail-read path until the cloud reverse server is restarted
with a positive `VCI_PROXY_LOCAL_SWEEP_READ_TIMEOUT_MS`.

For focused fidelity debugging, `shadow_local` can now filter only the shadow
plan without changing what GDS2 requests on the page. `VCI_PROXY_LOCAL_SWEEP_INCLUDE_UDS_DIDS`
keeps only listed `UDS 0x22` DIDs in the local shadow plan, and
`VCI_PROXY_LOCAL_SWEEP_EXCLUDE_UDS_DIDS` removes listed DIDs from that plan.
These filters apply only to `uds_did` signatures and are intended for narrow
comparison runs such as "shadow only `0x000C`" or "exclude known-bad `0x0031`"
while the page still exercises the full foreground request stream.

GM `A9 81 xx` signatures remain observable, but in the current implementation
they are forced back to observe-only / inventory-only across both
`shadow_local` and `active_replay`. The cloud emits `sweep.plan.skipped` with
reason `gm_a9_packet_observe_only` when the learned plan contains only these
guarded signatures. The legacy `VCI_PROXY_LOCAL_SWEEP_SHADOW_ALLOW_GM_A9_PACKET`
flag is still logged for compatibility and rollback analysis, but it does not
re-open GM A9 execution in the current runtime.

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

When a shadow read with a positive timeout returns only a single 4-byte
CAN-ID/echo-like frame in the `0x7E0..0x7EF` range, the local executor performs
up to two extra bounded local tail reads using the same shadow timeout and
merges non-echo data frames into that shadow result. This is disabled for
`read_timeout_ms=0` plans. Local `sweep.item.finished` events include
`read_timeout_ms`, `tail_read_triggered`, `tail_read_attempts`,
`tail_read_data_reads`, `tail_read_timeout_ms`, and per-attempt
`read_attempts` so a real-vehicle run can prove whether `000007e862000c...`
was captured locally after an initial `000007e0` frame.

Shadow data is comparison-only:

- it is stored in `SweepShadowStore`
- it is compared with normal GDS2-visible `READ_MSGS_RSP` bodies
- it is never used by `_try_serve_cached()`
- it is never written into `PrefetchReadMsgsBuffer`
- it never fulfills normal `READ_MSGS_REQ`
- exact-signature `active_replay` now requires the latest shadow generation to
  be fresh, `return_code == 0`, decoded `READ_MSGS_RSP` data to be non-empty,
  and the latest generation to have reached the replay clean-match streak
  threshold before DLL-facing arm/serve can happen

The stage cancels active shadow plans on disconnect, close, filter mutation,
non-cacheable or mutating IOCTL, failed writes, connection epoch changes,
max-seconds expiry, repeated mismatch/error thresholds, and communication-error
escalation.

Shadow comparison logs distinguish not-yet-comparable reads from true missing
shadow data. `sweep.shadow.not_ready` means the cloud has no active plan yet, a
plan is still in the delay window, or an active plan has not drained any results.
`sweep.shadow.missing` is reserved for later reads where comparison should have
been possible but no matching shadow result was available.

Operational validation as of `2026-05-08`:

- latest ECU Data Display run used `VCI_PROXY_LOCAL_SWEEP=1` and
  `VCI_PROXY_LOCAL_SWEEP_MODE=active_replay` with
  `VCI_PROXY_LOCAL_SWEEP_ALLOW_GM_A9_PACKET=1` and
  `VCI_PROXY_LOCAL_SWEEP_SHADOW_ALLOW_GM_A9_PACKET=0`;
- the cloud started a local sweep plan and drained shadow batches repeatedly
  during Data Display, but the run recorded no
  `proxy.request.active_replay_armed` and no
  `proxy.request.active_replay_served`;
- the visible regression was a latched Data Display page: collector samples held
  the entry-page values instead of updating in place, while foreground
  `READ_MSGS_REQ(data)` payloads still changed underneath;
- local shadow items finished with `return_code=18`, which maps to
  `ERR_NOT_UNIQUE` in the current J2534 error table and should be treated as an
  invalid shadow result rather than replay-ready evidence;
- this is not safe evidence for GM A9 execution. For the current design, GM
  `A9 81 xx` must remain observe-only / inventory-only, even when
  `active_replay` is enabled for other future-safe signatures.

Operational validation as of `2026-05-06`:

- latest ECU Data Display run used `VCI_PROXY_LOCAL_SWEEP=1` and
  `VCI_PROXY_LOCAL_SWEEP_MODE=shadow_local` with
  `VCI_PROXY_LOCAL_SWEEP_SHADOW_ALLOW_GM_A9_PACKET=0`;
- the server learned a strict GM `A9 81 xx` signature, then skipped the plan with
  `reason=gm_a9_packet_observe_only`;
- `sweep.plan.started` and `sweep.batch.drained` were absent, so no local shadow
  result comparison occurred in that run;
- this is the intended safe state for current GM A9-only Engine Data evidence.

Implemented but still experimental in this stage:

- `active_replay` for exact learned signatures only, with DLL-facing replay
  gated on a fresh, validated shadow result already being present; replay now
  also requires `return_code == 0`, non-empty decoded read data, and a clean
  shadow-vs-real comparison streak for the latest shadow generation
- synthetic `WRITE_MSGS_RSP` / `READ_MSGS_RSP` only inside that narrow replay
  path

Still not implemented or not supported in this stage:

- broad skipped real forwarding outside the exact-signature replay path
- serving shadow data to GDS2
- production rollout controls
- allowlist expansion beyond exact UDS `0x22`, OBD Mode 01, and strict observed
  GM `A9 81 xx` request shapes
- adaptive sweep-rate tuning
- safe GM `A9 81 xx` shadow execution or GM `A9 81 xx` replay as a supported
  configuration

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

### Real-vehicle Engine Speed validation handoff

For the 2026-05-13 real-vehicle validation, analyze the run as a safe
transport-layer test, not as a GM A9 replay test.

Configuration expectations:

- cloud and local should both advertise read-ahead and transaction support;
- `VCI_PROXY_LOCAL_SWEEP_MODE` may be configured as `observe_only`, but GM
  `A9 81 xx` must remain observe-only / inventory-only;
- `VCI_PROXY_LOCAL_SWEEP_SHADOW_ALLOW_GM_A9_PACKET=0` is required for this
  validation lane;
- expected logs are under:
  `C:\Users\shsww\projects\RPA_demo\vci_proxy\cloud_mirror` on cloud and
  `C:\Users\shsww\AppData\Roaming\VCI_Proxy` locally.

If the agent collector is active, use focused value freshness rather than
payload guesses:

```powershell
python scripts/analyze_battery_voltage_freshness.py `
  --cloud-root "C:\Users\shsww\projects\RPA_demo\vci_proxy\cloud_mirror" `
  --focus-key engine_speed `
  --min-delta 100 `
  --json reports/engine_speed_freshness.json `
  --report reports/engine_speed_freshness.md
```

If the collector is not active, fall back to proxy-layer evidence:

- `live_inter_request_gap_ms` / `proxy.j2534.cadence_gap`;
- `READ_MSGS_REQ(data)` vs `READ_MSGS_REQ(empty)` response events;
- FIFO `cache_decision` reasons and underfill merge outcomes;
- best-effort `*_engine_speed_candidate_rpm` only when standard OBD/UDS engine
  speed response shapes are visible.

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
