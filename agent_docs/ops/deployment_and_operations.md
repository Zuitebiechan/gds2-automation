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

### Recommended production edge pattern

For customer-facing cloud nodes, prefer:

- keep Flask bound to `127.0.0.1:8080`
- place a reverse proxy such as Caddy or IIS in front of Flask
- expose only `443` publicly for the diagnostics GUI
- keep `8080` private to the cloud node
- expose `9000` for the reverse tunnel, ideally with TLS enabled

This pattern is the preferred shape for one-customer-per-node deployments,
including AWS Local Zone worker nodes.

Example public flow:

```text
GUI -> https://customer-node.example.com:443 -> reverse proxy -> 127.0.0.1:8080
```

Example reverse-proxy flow:

```text
Local reverse client -> tls://customer-node.example.com:9000 -> reverse_server
```

Repository templates for this pattern:

- `scripts/local_zone/Caddyfile.example`
- `scripts/local_zone/start_edge_proxy.ps1`
- `scripts/local_zone/test_edge_health.ps1`
- `scripts/local_zone/deploy_customer_node.ps1`
- `scripts/local_zone/register_edge_autostart.bat`
- `scripts/local_zone/unregister_edge_autostart.bat`
- `scripts/local_zone/customer_node.env.example`
- `scripts/local_zone/README.md`
- `agent_docs/ops/aws_local_zone_customer_node.md`

### Temporary public-debug mode

When you need to validate business behavior quickly before the full `443`
edge is ready, a temporary direct-connect mode is allowed:

```bash
python app.py --port 8080 --public
```

Use this only for short-lived testing. In that mode:

- Flask listens on `0.0.0.0:8080`
- the diagnostics GUI should use `api_scheme=http`
- the diagnostics GUI should use `api_port=8080`
- the reverse tunnel still uses `9000`

Important rule:

- the GUI builds its API base URL from `api_scheme`, `host`, and `api_port`
- those values must match the actual public API entrypoint
- a common failure mode is leaving the GUI on `https://<host>:443` while the
  cloud node is only exposing `http://<host>:8080`

Once the direct-connect flow works, switch back to the production shape:

- Flask on `127.0.0.1:8080`
- reverse proxy on `443`
- GUI configured for `https` on `443`

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

The diagnostics GUI now also supports these API settings in
`%APPDATA%\VCI_Proxy\config.json`:

- `api_scheme`: `http` or `https`
- `api_port`: API listener or proxy port such as `443`
- `api_token`: optional API token sent as `X-API-Token`

For production customer nodes behind a reverse proxy, prefer:

- `api_scheme=https`
- `host=<customer-domain>`
- `api_port=443`
- `api_token=<per-node api token>` when `DIAGNOSTIC_API_TOKEN` is enabled

For temporary direct-connect testing, use:

- `api_scheme=http`
- `host=<public-ip-or-debug-host>`
- `api_port=8080`

Do not leave the GUI on `https:443` unless a reverse proxy is actually
listening on `443`.

### Windows client build

```bash
pyinstaller --clean --noconfirm pyinstaller_client.spec
```

## Ports

Current default ports:

- `8080`: Flask API
- `9000`: reverse tunnel listener for the local proxy client
- `9001`: cloud-side local proxy listener used by the virtual J2534 DLL

Recommended production exposure:

- `443`: public diagnostics API entrypoint through reverse proxy
- `9000`: public reverse tunnel entrypoint
- `8080`: private loopback-only Flask upstream

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
