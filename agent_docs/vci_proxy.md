# VCI Proxy Tunnel

The core infrastructure that makes remote diagnostics possible. This layer is **OEM-tool-agnostic** and shared by all future diagnostic software.

## How It Works

1. **Cloud side**: GDS2 loads `virtual_j2534.dll` instead of a real device driver
2. The virtual DLL converts J2534 API calls into TCP messages (custom binary protocol)
3. Messages travel through `reverse_server.py` to `reverse_client.py` over the internet
4. **Local side**: `reverse_client.py` calls the real J2534 DLL via ctypes, which talks to the physical VCI hardware

## Binary Protocol

```
+----------+----------+----------+----------+
|  Magic   |  Length  |  MsgType | Sequence |
|  4 bytes |  4 bytes |  2 bytes |  4 bytes |
+----------+----------+----------+----------+
|              Message Body                  |
|            (Variable Length)               |
+--------------------------------------------+
```

- **Magic**: `0x4A325334` ("J2S4"), **Header**: 14 bytes
- **Message types**: Full J2534 API (Open, Close, Connect, Disconnect, ReadMsgs, WriteMsgs, StartFilter, StopFilter, Ioctl, ReadVersion)
- **Request/Response**: Requests `0x00xx`, Responses `0x80xx`
- **Special**: `0x00FE` Auth, `0x00FF` Heartbeat

## Authentication

- HMAC-SHA256 over PSK. Client sends `AUTH_REQ(timestamp, HMAC(key, timestamp))`
- Replay protection: rejects timestamps with >5 minute drift
- Backward compatible: legacy clients register via heartbeat when auth disabled
- Implementation: `vci_proxy/auth.py`

### Two-Phase Heartbeat Registration (auth disabled)

To prevent internet scanners from being accepted as VCI clients, legacy heartbeat mode now uses a two-step handshake:

1. Client sends `HEARTBEAT` (phase 1)
2. Server replies `HEARTBEAT_ACK`
3. Client must send a second `HEARTBEAT` within 5 seconds (phase 2)
4. Server replies `HEARTBEAT_ACK` and registers the connection

This blocks random probes that accidentally match protocol headers but cannot complete a valid second phase.

## Caching (Latency Optimization)

| Cache | File | Purpose | TTL |
|-------|------|---------|-----|
| **ReadMsgs** | `cache_read_msgs.py` | Short-circuit BUFFER_EMPTY | 150ms per-channel |
| **Filter Dedup** | `cache_filter_dedup.py` | Deduplicate StartFilter | SHA-256 key |
| **IOCTL** | `cache_ioctl.py` | Cache read-only IOCTLs (GET_CONFIG, READ_VBATT, READ_PROG_VOLTAGE) | 5s per-(channel, ioctl_id) |
| **VBATT** *(legacy)* | `cache_vbatt.py` | Superseded by IOCTL cache; kept for backward compat | 5s global |

All caches invalidate on Disconnect/Close. IOCTL cache runs on **both** server and client.

## Key Components

| Component | File | Runs On | Description |
|-----------|------|---------|-------------|
| `ReverseProxyServer` | `reverse_server.py` | Cloud | Dual-port asyncio: `:9000` VCI client, `:9001` virtual DLL |
| `ReverseProxyClient` | `reverse_client.py` | Local | Auto-reconnect with exponential backoff, keepalive, heartbeat |
| `J2534Driver` | `j2534_driver.py` | Local | ctypes wrapper with registry auto-discovery for J2534 drivers |
| `virtual_j2534.dll` | `virtual_dll/` | Cloud | C DLL GDS2 loads as real hardware |
| `ProxyConfig` | `config.py` | Both | Frozen dataclass, `from_args()` for CLI |

## Supported VCI Hardware

| Device | Status |
|--------|--------|
| Scanmatik SM2 USB | Working |
| Scanmatik SM3 | Working |
| GM MDI / MDI2 | Registry auto-discovery supported; hardware validation pending |

## Stability Hardening (2026-03)

- **Scanner-resistant registration**: two-phase legacy heartbeat handshake on cloud `reverse_server`
- **SM2 cold-start mitigation**: background pre-warm `PassThruOpen` in `reverse_client` after registration
- **NAT timeout mitigation**: TCP keepalive + shorter heartbeat interval for long-lived idle tunnels
- **Client diagnostics logging**: `%APPDATA%\VCI_Proxy\client.log` for tray exe runs (console-less mode)

## Performance Optimization (2026-03)

- **TCP_NODELAY**: Set on all sockets (server VCI port, proxy port, client connection) to eliminate Nagle algorithm delay
- **ReadMsgs cache TTL**: Increased from 50ms to 150ms to intercept more BUFFER_EMPTY polls
- **Generalized IOCTL cache** (`cache_ioctl.py`): Caches all read-only IOCTLs (GET_CONFIG `0x01`, READ_VBATT `0x03`, READ_PROG_VOLTAGE `0x09`) with 5s TTL per `(channel_id, ioctl_id)`, replacing the narrow VBATT-only cache. Write-type IOCTLs (SET_CONFIG, CLEAR_*_BUFFER) are never cached.
- **Benchmark tooling**: Client-side timing trailer (`hw_ms`) decomposes round-trip into network vs hardware latency. `generate_benchmark_report()` outputs Markdown reports with methodology, per-message-type latency tables, READ_MSGS empty/data bucketing, and cache effectiveness.
- **WriteMsgs caching deliberately skipped**: Caching write return codes would mask actual write failures and could break ECU communication.

Implementation notes:

- Pre-warm is non-blocking (`asyncio.ensure_future`) so request loop starts immediately.
- If `OPEN_REQ` arrives during pre-warm, client waits for pre-warm task and reuses cached handle/result.
- Authentication path writes are independent of proxy forwarding lock, avoiding handshake ACK delays under reconnect churn.

## New Machine Onboarding (Local PC)

Minimum required setup for another local PC:

1. Install VCI vendor driver (SM2/SM3)
2. Launch client and set cloud host/port (`9000`) + API port (`8080`)
3. Leave J2534 on auto-detect unless manual override is needed
4. Ensure outbound access to cloud `9000` and `8080`
5. If auth token is enabled on server, configure same token in client

Reminder: one cloud `reverse_server` instance currently holds one active VCI client at a time; newer connection replaces older one.

## Cloud Setup

```bash
scripts\cloud_setup_gds2_agent.bat
regedit /s vci_proxy\virtual_dll\register_vci_proxy.reg
python -m vci_proxy.reverse_server
```

## Runtime Notes

- Run `python -m vci_proxy.reverse_server` from project root (or any folder where `vci_proxy` package is importable).
- If Windows reports `No module named 'vci_proxy'`, current working directory is wrong or PYTHONPATH does not include the project.
- Keep only one active local VCI client process to avoid reconnect churn/noisy disconnect logs.

## Multi-Session Validation (2026-02-11)

Tested running two independent server/client tunnels (ports 9000/9001 and 9100/9002):
- **DLL env var mechanism**: Working. Both ports connect successfully.
- **Independent tunnels**: Working. Two server/client pairs operate without interference.
- **GDS2 dual-instance**: Not possible. GDS2 has a single-instance lock per machine.
- **Conclusion**: Multi-user requires one VM/container per user.
