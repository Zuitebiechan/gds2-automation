# VCI Proxy And Tunnel

## Scope

This document covers the reverse tunnel, local tray client, virtual J2534 path, tunnel authentication, cache behavior, and tunnel-quality tracking.

This document does not restate all deployment commands. For environment setup and runtime commands, read `agent_docs/ops/deployment_and_operations.md`.

For the staged plan to reduce cloud GDS2 live-data latency in the Proxy J2534 architecture, read `agent_docs/ops/proxy_j2534_latency_optimization.md`.

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

These values are passed through to `vci_proxy.reverse_client.ReverseProxyClient`
when the tray app starts the local tunnel client.

## Cache Layers

The proxy stack currently includes multiple request-side optimizations.

### `ReadMsgs` cache

`ReadMsgsCache` short-circuits repeated empty-buffer polls for a short TTL.

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
