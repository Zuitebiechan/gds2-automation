# Diagnostic Platform RPA

Cloud remote vehicle diagnostics platform with:

- a capability-first backend contract layer
- a cloud-side diagnostics API and worker runtime
- a local VCI proxy tunnel and tray client
- a GDS2 implementation as the first production backend

This repository is no longer a GDS2-only codebase. GDS2 is the first backend, but the platform now separates:

- platform-neutral contracts and runtime code in `diagnostic_platform/`
- OEM/backend-specific implementations in `backends/`
- GDS2-specific automation plus legacy orchestration compatibility exports in `src/`
- local proxy, tray client, and tunnel code in `vci_proxy/`

## Documentation

The authoritative documentation set lives in `agent_docs/`.

- Documentation index: `agent_docs/README.md`
- Project overview: `agent_docs/core/project_overview.md`
- Project structure: `agent_docs/core/project_structure.md`
- Code structure: `agent_docs/core/code_structure.md`
- Platform architecture: `agent_docs/core/platform_architecture.md`
- Backend architecture: `agent_docs/core/backend_architecture.md`
- API design: `agent_docs/core/api_design.md`
- Runtime flows: `agent_docs/core/runtime_flows.md`
- Deployment and operations: `agent_docs/ops/deployment_and_operations.md`
- VCI proxy and tunnel: `agent_docs/ops/vci_proxy_and_tunnel.md`
- Testing and quality: `agent_docs/core/testing_and_quality.md`
- Reports index: `agent_docs/reports/README.md`

For benchmark artifacts and ad-hoc local measurements, use `reports/network_benchmarks/`. That directory is not part of the authoritative documentation set.

## Current Scope

- Supported production backend: `gds2`
- Supported public API surfaces:
  - `/api/session/*`
  - `/api/navigate/*`
- Current worker model:
  - `1 worker process = 1 active business session = 1 active backend bundle`

## Quick Start

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

## Running the Cloud Side

You can still start each component in its own terminal:

```bash
# Terminal A: reverse tunnel listener
python -m vci_proxy.reverse_server --auth-token <shared-token>

# Terminal B: Flask API (binds to 127.0.0.1 by default)
python app.py --port 8080

# Terminal C: OEM diagnostics software runtime
# For GDS2, keep the Java Agent writing to ~/gds2-data/latest.json
```

For direct source launches, set:

- `PRODUCT_LOG_CLOUD_ROOT=D:\RPA_Diagnostic\observability\cloud` for structured observability artifacts
- `LOG_DIR=D:\RPA_Diagnostic\logs` for Flask and reverse-server text logs

Both `python app.py` and `python -m vci_proxy.reverse_server` also read the repo-root `.env` file when present, so these paths can be persisted there without using the startup script.

For Windows cloud nodes, there is now a simpler one-click path:

```bash
copy scripts\cloud_service_config.example.cmd scripts\cloud_service_config.cmd
scripts\cloud_start_services.bat
```

That startup script:

- loads `scripts\cloud_service_config.cmd` when present
- requires `VCI_PROXY_AUTH_TOKEN`
- defaults structured cloud observability to `D:\RPA_Diagnostic\observability\cloud`
- defaults reverse-server and Flask terminal logs to `D:\RPA_Diagnostic\logs`
- skips GDS2 automatically when run in Session 0

To register boot auto-start on the cloud server, run once as Administrator:

```bash
scripts\cloud_register_autostart.bat
```

Operational notes:

- The API now binds to `127.0.0.1` by default. Add `--public` only when you intentionally want remote access.
- Set `DIAGNOSTIC_API_TOKEN` to require `Authorization: Bearer <token>` or `X-API-Token: <token>` on `/api/*`.
- Enable `--cors` only for trusted browser clients, and prefer `DIAGNOSTIC_API_CORS_ORIGINS` to scope allowed origins.
- When the reverse tunnel crosses an untrusted network, enable TLS on both ends. The full flag set and certificate guidance live in `agent_docs/ops/vci_proxy_and_tunnel.md`.

Default ports:

- `9000`: reverse VCI listener
- `9001`: local proxy listener (virtual DLL side)
- `8080`: Flask API

## Running the Local Side

Development mode:

```bash
python -m vci_proxy.client_gui
```

The local tray client now expects an auth token to be configured before it will
start the reverse tunnel. Use the same shared token on both sides.
TLS trust settings can also be stored in `%APPDATA%\VCI_Proxy\config.json`
using `tls_enabled`, `tls_ca_file`, and `tls_server_name`.

Build the Windows client:

```bash
powershell -ExecutionPolicy Bypass -File scripts/build_vci_proxy_client.ps1
```

The packaged client now ships as:

- `VCI_Proxy_Client.exe`
- `workers/VCI_Proxy_J2534_Worker_x86.exe`
- `workers/VCI_Proxy_J2534_Worker_x64.exe`

The tray client selects the matching worker executable automatically based on
the chosen J2534 DLL architecture, so customers do not need to install a
matching Python runtime.

## Key Runtime Paths

- API entrypoint: `app.py`
- Flask bootstrap: `server/app.py`
- Backend registry bootstrap: `diagnostic_platform/backend_registry.py`
- Local client config: `%APPDATA%\VCI_Proxy\config.json`
- Tunnel-quality snapshot: `%PROGRAMDATA%\VCI_Proxy\tunnel_quality.json`
- GDS2 Java Agent output: `~/gds2-data/latest.json`

## Compatibility Notes

- `backend_name` is the canonical backend field in session responses.
- `workflow` is retained only as a deprecated alias for `backend_name`.
- The platform now resolves brands to backends through the backend registry. The GUI should provide vehicle identity inputs such as brand/model/VIN, not backend-specific routing decisions.

## Where To Read Next

- For repository layout and ownership boundaries, read `agent_docs/core/project_structure.md`.
- For code package responsibilities, read `agent_docs/core/code_structure.md`.
- For end-to-end architecture, read `agent_docs/core/platform_architecture.md`.
- For API contracts and route semantics, read `agent_docs/core/api_design.md`.
- For operational setup, deployment, and config paths, read `agent_docs/ops/deployment_and_operations.md`.
