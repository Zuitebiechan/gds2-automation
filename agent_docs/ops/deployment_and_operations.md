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

For Windows cloud nodes, the repository also includes a one-click startup flow:

```bash
copy scripts\cloud_service_config.example.cmd scripts\cloud_service_config.cmd
scripts\cloud_start_services.bat
```

Expected config handling:

- put the shared reverse-tunnel token in `scripts\cloud_service_config.cmd` as `VCI_PROXY_AUTH_TOKEN`
- optionally set `DIAGNOSTIC_API_TOKEN` there for API auth
- optionally set `DIAGNOSTIC_API_PUBLIC=1` only for temporary direct-connect debugging
- optionally set `VCI_PROXY_TLS_ENABLED=1` plus `VCI_PROXY_TLS_CERT` and `VCI_PROXY_TLS_KEY`
- keep `scripts\cloud_service_config.cmd` local to the server and out of source control

The startup script:

- loads `scripts\cloud_service_config.cmd` if it exists
- refuses to start unless `VCI_PROXY_AUTH_TOKEN` is defined after config/env load
- starts `python -m vci_proxy.reverse_server --auth-token ...`
- starts `python app.py --port 8080`
- writes logs to `logs\vci_proxy.log` and `logs\flask_api.log`
- skips GDS2 automatically in Session 0 such as Windows Scheduled Task startup

For boot auto-start on a Windows cloud server, run once as Administrator:

```bash
scripts\cloud_register_autostart.bat
```

That scheduled task runs `scripts\cloud_start_services.bat` at system startup and
expects `scripts\cloud_service_config.cmd` to already exist on the server.

Security defaults:

- `python app.py` binds to `127.0.0.1` by default. Add `--public` only when the API must be reachable remotely.
- Set `DIAGNOSTIC_API_TOKEN` to require `Authorization: Bearer <token>` or `X-API-Token: <token>` on `/api/*`.
- CORS is disabled by default. Enable it with `--cors` or `DIAGNOSTIC_API_ENABLE_CORS=1`, and prefer `DIAGNOSTIC_API_CORS_ORIGINS` to scope allowed origins.
- The reverse tunnel defaults to PSK authentication enabled. Use the same `--auth-token` on the cloud reverse server and the local client.

Product observability defaults:

- cloud artifacts live under `%PROGRAMDATA%\RPA_Diagnostic\observability\cloud\`
- set `PRODUCT_LOG_CLOUD_ROOT` to move cloud observability artifacts to an explicit directory such as `D:\RPA_Diagnostic\observability\cloud`
- local artifacts live under `%APPDATA%\VCI_Proxy\observability\`
- the cloud server now accepts internal artifact uploads at `POST /api/session/logs/upload`
- the cloud server also exposes changed cloud log artifacts at `POST /api/session/logs/sync`
- the tray client runs a best-effort background observability uploader using the assigned node `api_base_url` when available, otherwise its configured `api_scheme`, `host`, `api_port`, and `api_token`
- the tray client mirrors changed cloud-side log artifacts under `%APPDATA%\VCI_Proxy\observability\cloud_mirror\`
- retention defaults can be overridden with:
  - `PRODUCT_LOG_CLOUD_ROOT`
  - `PRODUCT_LOGS_ENABLED`
  - `PRODUCT_LOG_RETENTION_DAYS_RAW`
  - `PRODUCT_LOG_RETENTION_DAYS_SESSION_TRACE`
  - `PRODUCT_LOG_RETENTION_DAYS_INCIDENT`
  - `PRODUCT_LOG_UPLOAD_ENABLED`
  - `PRODUCT_LOG_MAX_ARTIFACT_MB`

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
- `scripts/local_zone/aws_launch_smoke_test.py`
- `scripts/local_zone/start_edge_proxy.ps1`
- `scripts/local_zone/test_edge_health.ps1`
- `scripts/local_zone/deploy_customer_node.ps1`
- `scripts/local_zone/register_edge_autostart.bat`
- `scripts/local_zone/unregister_edge_autostart.bat`
- `scripts/local_zone/customer_node.env.example`
- `scripts/local_zone/README.md`
- `agent_docs/ops/aws_local_zone_customer_node.md`

### Node allocation and readiness env

When running the automated Local Zone node-allocation MVP, configure these env vars on the control-plane API process:

- `DIAGNOSTIC_NODE_INVENTORY_FILE`: shared JSON inventory path for node state
- `DIAGNOSTIC_NODE_LEASE_FILE`: shared JSON lease path for assignment state
- `DIAGNOSTIC_AWS_REGION`: AWS region used to create the EC2 client
- `DIAGNOSTIC_NODE_LAUNCH_TEMPLATE`: EC2 launch template name for one worker node
- `DIAGNOSTIC_NODE_INSTANCE_TYPE`: instance type for cold-start launches
- `DIAGNOSTIC_NODE_SUBNET_ID`: subnet id for the target Local Zone
- `DIAGNOSTIC_NODE_SECURITY_GROUP_IDS`: optional comma-separated security group ids
- `DIAGNOSTIC_NODE_DEFAULT_ZONE`: default Local Zone placement such as `us-west-2-lax-1a`
- `DIAGNOSTIC_NODE_DEFAULT_METRO`: default metro label such as `los-angeles`
- `DIAGNOSTIC_NODE_DEFAULT_API_BASE`: default node API base used in returned assignments and readiness checks
- `DIAGNOSTIC_NODE_DEFAULT_TUNNEL_HOST`: default reverse-tunnel host for the assigned node
- `DIAGNOSTIC_NODE_READINESS_ENABLED`: set to `1` to enable background polling for booting nodes
- `DIAGNOSTIC_NODE_READINESS_INTERVAL_SEC`: optional polling interval in seconds
- `DIAGNOSTIC_NODE_READINESS_TIMEOUT_SEC`: optional HTTP timeout for the readiness probe
- `DIAGNOSTIC_NODE_READINESS_PATH`: optional probe path, defaults to `/api/session/bootstrap/ready`
- `DIAGNOSTIC_NODE_READINESS_API_TOKEN`: optional API token sent only by the readiness probe; if omitted, the monitor falls back to `DIAGNOSTIC_API_TOKEN`
- `DIAGNOSTIC_NODE_ZONE_CATALOG_JSON`: optional multi-zone routing catalog as JSON
- `DIAGNOSTIC_NODE_ZONE_CATALOG_FILE`: optional path to a JSON catalog file such as `scripts/local_zone/zone_catalog.example.json`
- `DIAGNOSTIC_NODE_GEO_ROUTING_ENABLED`: set to `1` to enable geo-aware bootstrap routing
- `DIAGNOSTIC_NODE_TRUST_CLOUDFRONT_HEADERS`: set to `1` to trust CloudFront viewer location headers on the bootstrap API

Single-zone mode:

- use `DIAGNOSTIC_NODE_SUBNET_ID`, `DIAGNOSTIC_NODE_DEFAULT_ZONE`, `DIAGNOSTIC_NODE_DEFAULT_METRO`, `DIAGNOSTIC_NODE_DEFAULT_API_BASE`, and `DIAGNOSTIC_NODE_DEFAULT_TUNNEL_HOST`

Multi-zone mode:

- keep `DIAGNOSTIC_AWS_REGION`, `DIAGNOSTIC_NODE_LAUNCH_TEMPLATE`, and usually `DIAGNOSTIC_NODE_INSTANCE_TYPE` as shared defaults
- put per-zone fields in the zone catalog
- each catalog entry can override:
  - `zone`
  - `metro`
  - `subnet_id`
  - `api_base_url`
  - `tunnel_host`
  - optional `instance_type`
  - optional `launch_template_name`
  - optional `security_group_ids`
- each catalog entry can also carry routing hints such as `time_zones` and `cities`

CloudFront bootstrap entry:

- put CloudFront only in front of the control-plane bootstrap hostname, not in front of every worker node
- forward `CloudFront-Viewer-City` and `CloudFront-Viewer-Time-Zone` to the origin
- only enable header trust when requests are expected to arrive through CloudFront
- if direct debug traffic bypasses CloudFront, the API ignores missing CloudFront headers and continues to use client hints plus default routing

Resolver behavior:

- exact `preferred_zone` match first
- then exact `preferred_metro`
- then `client_city`
- then `client_time_zone`
- then `organization_city`
- then `organization_time_zone`
- then trusted `CloudFront-Viewer-City`
- then trusted `CloudFront-Viewer-Time-Zone`
- then `DIAGNOSTIC_NODE_DEFAULT_ZONE`
- then `DIAGNOSTIC_NODE_DEFAULT_METRO`
- otherwise the first catalog entry
- when a `city` hint and a `time_zone` hint disagree, city wins because routing is metro-first in this rollout
- when client hints and CloudFront hints disagree, the client hint wins and the API records a conflict warning
- when the zone catalog contains duplicate `city` or `time_zone` mappings across metros, startup logs a warning and the resolver keeps a deterministic catalog-order result

Current MVP behavior:

- the control-plane API allocates or launches nodes through `/api/session/bootstrap`
- bootstrap routing decisions now log `selected_zone`, `selected_metro`, `route_source`, `fallback_used`, and `signal_conflict`
- bootstrap routing logs do not store raw public client IPs
- new nodes stay `BOOTING` until the background readiness monitor can reach `/api/session/bootstrap/ready`
- once the probe succeeds, the node is promoted to healthy idle capacity and becomes allocatable on the next bootstrap retry

For pre-production AWS validation, use:

```bash
python scripts/local_zone/aws_launch_smoke_test.py
```

Behavior:

- default mode uses EC2 `DryRun` and confirms credentials plus launch permissions
- when a zone catalog is configured, the smoke test can validate route inference with flags such as `--client-time-zone "America/Chicago"` before the EC2 call
- the smoke test validates the same zone-catalog routing precedence used by bootstrap for client hints; CloudFront header simulation is covered by server-side tests
- `--live` performs a real `RunInstances` call
- live mode terminates the created instance immediately unless `--keep-instance` is passed

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
powershell -ExecutionPolicy Bypass -File scripts/build_vci_proxy_client.ps1
```

The final packaged layout is:

- `dist/VCI_Proxy_Client/VCI_Proxy_Client.exe`
- `dist/VCI_Proxy_Client/workers/VCI_Proxy_J2534_Worker_x86.exe`
- `dist/VCI_Proxy_Client/workers/VCI_Proxy_J2534_Worker_x64.exe`

The tray client selects the matching worker executable automatically from the
packaged `workers/` directory based on the detected J2534 DLL architecture.

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
