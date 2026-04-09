# Deployment And Operations

## Scope

This document covers environment setup, runtime commands, ports, config and secret locations, operational prerequisites, and common deployment responsibilities.

This document does not explain the reverse tunnel protocol in depth. For that, read `agent_docs/ops/vci_proxy_and_tunnel.md`.

## Deployment Topology

The current system is operated across two machines or two runtime zones:

### Cloud side

- Flask API
- worker runtime
- OEM diagnostics software such as GDS2
- virtual J2534 DLL
- reverse tunnel server

### Local side

- reverse tunnel client
- tray GUI
- real J2534 driver
- physical VCI and vehicle access

## Python Environments

### Cloud/server environment

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements-cloud.txt
```

### Local/client environment

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements-client.txt
```

## Main Runtime Commands

### Cloud side

Run these in separate terminals:

```bash
python -m vci_proxy.reverse_server --auth-token <shared-token>
python app.py --port 8080
```

Security defaults:

- `python app.py` binds to `127.0.0.1` by default. Add `--public` only when the API must be reachable remotely.
- Set `DIAGNOSTIC_API_TOKEN` to require `Authorization: Bearer <token>` or `X-API-Token: <token>` on `/api/*`.
- CORS is disabled by default. Enable it with `--cors` or `DIAGNOSTIC_API_ENABLE_CORS=1`, and prefer `DIAGNOSTIC_API_CORS_ORIGINS` to scope allowed origins.
- The reverse tunnel defaults to PSK authentication enabled. Use the same `--auth-token` on the cloud reverse server and the local client.

For internet-facing or otherwise untrusted networks, enable TLS on the reverse tunnel:

```bash
python -m vci_proxy.reverse_server ^
  --auth-token <shared-token> ^
  --tls ^
  --tls-cert <server-cert.pem> ^
  --tls-key <server-key.pem>
```

Add `--tls-ca <client-ca.pem> --tls-require-client-cert` if you want the reverse
server to require mutually authenticated client certificates.

Also keep the OEM runtime available:

- for GDS2, the Java agent should be writing to `~/gds2-data/latest.json`

### Local side

Development mode:

```bash
python -m vci_proxy.client_gui
```

The reverse tunnel now defaults to PSK auth enabled. Configure the same shared
token on the reverse server and in the local tray client before connecting.

For CLI-driven local clients, the equivalent TLS flags are:

```bash
python -m vci_proxy.reverse_client ^
  --host <server-host> ^
  --auth-token <shared-token> ^
  --tls ^
  --tls-ca <server-ca.pem> ^
  --tls-server-name <server-dns-name>
```

The tray client persists TLS trust settings in `%APPDATA%\VCI_Proxy\config.json`
using `tls_enabled`, `tls_ca_file`, and `tls_server_name`.

### Windows client build

```bash
pyinstaller --clean --noconfirm pyinstaller_client.spec
```

## Ports

Current default ports:

- `8080`: Flask API
- `9000`: reverse tunnel listener for the local proxy client
- `9001`: cloud-side local proxy listener used by the virtual J2534 DLL

## Important Runtime Paths

| Path | Purpose |
| --- | --- |
| `app.py` | thin top-level API entrypoint |
| `server/app.py` | Flask app bootstrap |
| `%APPDATA%\VCI_Proxy\config.json` | local GUI/client configuration and ZhipuAI key storage |
| `%PROGRAMDATA%\VCI_Proxy\tunnel_quality.json` | persisted tunnel-quality snapshot |
| `~/gds2-data/latest.json` | GDS2 Java-agent output |
| `gds2_web.log` | current API log file produced by `server/app.py` |

## Secrets And Sensitive Config

Current secret-handling rule:

- ZhipuAI API keys belong in `%APPDATA%\VCI_Proxy\config.json`
- reverse-tunnel PSK tokens and `DIAGNOSTIC_API_TOKEN` should be injected through local config or environment variables, not committed into source
- TLS private keys and private CA bundles should be stored outside the repository and provisioned per environment
- do not commit API keys or other secrets into source

## GDS2 Operational Facts

These facts matter operationally:

- GDS2 is a separate OEM software process
- GDS2 is JavaFX, but Device Explorer follows a separate Win32 automation path
- one GDS2 instance is assumed per machine
- GBK encoding may appear in GDS2-facing behavior
- live diagnostics features assume GDS2 reaches the Data Display page
- AI diagnosis requires a 30-second data-collection window before the LLM call

## Startup Responsibilities

### API process

`server/app.py` owns:

- Flask creation
- CORS enablement
- optional API token enforcement for `/api/*`
- blueprint registration
- log setup
- host/port handling

`app.py` should stay a thin delegating wrapper.

### Reverse tunnel server

`vci_proxy/reverse_server.py` should be running before the local reverse client attempts to connect.
It terminates the reverse client listener on port `9000`, optionally with TLS and client-certificate enforcement.

### Local tray client

`vci_proxy/client_gui.py` owns:

- GUI configuration
- reverse-client lifecycle
- J2534 driver selection
- local config persistence
- persisted reverse-tunnel trust settings

## Packaging Notes

The local client packaging path is based on:

- `pyinstaller_client.spec`

The cloud side is currently run from source rather than through a packaged artifact.

## Read Next

- Tunnel/proxy internals: `agent_docs/ops/vci_proxy_and_tunnel.md`
- Platform runtime behavior: `agent_docs/core/platform_architecture.md`
- Business-session and AI/live/navigation sequences: `agent_docs/core/runtime_flows.md`
