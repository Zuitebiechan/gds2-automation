# Project Structure

## Scope

This document describes the repository layout and the ownership of top-level directories and root files.

This document does not explain runtime behavior or HTTP semantics. For those, read `agent_docs/core/platform_architecture.md`, `agent_docs/core/backend_architecture.md`, and `agent_docs/core/api_design.md`.

## Top-Level Layout

| Path | Purpose | Notes |
| --- | --- | --- |
| `agent_docs/` | Authoritative project documentation | Use this first after the code itself |
| `backends/` | OEM/backend facades behind the shared contract | `gds2` is the only production backend today |
| `diagnostic_platform/` | Platform-neutral contracts, registry, runtime helpers, SSE helpers | Canonical platform-layer namespace |
| `server/` | Supported Flask blueprints and API glue | Owns `/api/session/*`, `/api/diagnose/*`, `/api/navigate/*` |
| `src/` | GDS2-specific automation and deterministic orchestration | Treat as GDS2-specific unless clearly generalized |
| `tests/` | Durable regression tests | Do not use for throwaway validation |
| `vci_proxy/` | Reverse tunnel, tray client, J2534 integration, diagnostics window | Spans cloud-side reverse server and local-side client |
| `reports/` | Raw or ad-hoc measurement artifacts | Not part of the authoritative design doc set |
| `vendor/` | Bundled vendor/runtime dependencies | Includes the Java agent bundle area |
| `data/` | Runtime data and curated assets | Some items are regenerated or machine-local |
| `scripts/` | Helper scripts | Use case-specific, not core runtime |
| `archive/` | Historical leftovers outside the main doc set | Not authoritative architecture guidance |

## Root Files

| Path | Purpose |
| --- | --- |
| `README.md` | Project entrypoint and high-level documentation index |
| `AGENTS.md` | Lightweight working guide for coding agents |
| `app.py` | Backward-compatible thin entrypoint that delegates to `server/app.py` |
| `pyinstaller_client.spec` | Windows client packaging spec |
| `requirements-cloud.txt` | Cloud/server Python dependency set |
| `requirements-client.txt` | Local/client Python dependency set |
| `setup_windows.bat` | Windows environment bootstrap convenience script |
| `.gitignore` | Repository ignore policy, including documentation exceptions |
| `.env.example` | Environment variable example template |

## Documentation Layout

| Path | Role |
| --- | --- |
| `agent_docs/README.md` | Index for the authoritative documentation set |
| `agent_docs/core/` | Durable architecture, API, runtime, and quality docs |
| `agent_docs/ops/` | Deployment, tunnel, and operational docs |
| `agent_docs/reports/` | Curated report files that support decisions |
| `agent_docs/archive/` | Historical documents retained for reference |
| `reports/network_benchmarks/` | Raw benchmark outputs and ad-hoc measurement artifacts |
| `tests/README.md` | Test-directory-local guide, intentionally narrow in scope |
| `vci_proxy/virtual_dll/README.md` | Component-scoped readme for the virtual DLL only |

Authoritative design decisions should live in `agent_docs/core/` or `agent_docs/ops/`, not in component READMEs.

## Code Layout By Top-Level Package

### `diagnostic_platform/`

Owns shared platform concerns:

- backend contracts and descriptors
- backend registry bootstrap
- worker-runtime state
- session, diagnostics, navigation, and SSE runtime helpers

Detailed package ownership is documented in `agent_docs/core/code_structure.md`.

### `backends/`

Owns per-backend facades that implement the shared platform contract.

Current state:

- `backends/gds2/` is the only production backend implementation

Backend responsibilities are documented in `agent_docs/core/backend_architecture.md`.

### `server/`

Owns the Flask bootstrap and the supported public API surfaces.

Current structure:

- `server/app.py`: app creation and blueprint registration
- `server/api/session.py`: business-session routes
- `server/api/diagnostics.py`: direct diagnostics routes
- `server/api/navigate.py`: direct navigation routes
- `server/api/session_*_handlers.py`: session domain handlers
- `server/api/session_dependencies.py`: session-side runtime wiring helpers

API design is documented in `agent_docs/core/api_design.md`.

### `src/`

Owns GDS2-specific implementation detail, including:

- diagnosis helpers
- device discovery/runtime glue
- native automation helpers
- navigation controller logic
- streaming and collector logic
- workflows such as Data Viewer
- deterministic GDS2 orchestration under `src/gds2_orchestration/`

This area should not become the place where future OEM backends are added.

### `vci_proxy/`

Owns:

- the reverse tunnel server
- the reverse tunnel client
- the local tray GUI
- J2534 driver wrapping
- tunnel-quality tracking
- protocol/auth/cache helpers
- the cloud-side virtual DLL component

Operational detail is in `agent_docs/ops/vci_proxy_and_tunnel.md`.

## Generated, Local, and Non-Authoritative Paths

The repository also contains machine-local or generated directories that should not be treated as part of the designed source layout:

- `.agents/`
- `.pytest_cache/`
- `.pytest_tmp/`
- `.tmp/`
- `build/`
- `dist/`
- `logs/`
- `__pycache__/`

These may exist during development, but they are not part of the intentional architecture.

## Why Some Files Still Exist At The Root

Not every root file is accidental clutter.

Some root-level files are intentionally top-level because they are project entrypoints or packaging/bootstrap files:

- `README.md`
- `AGENTS.md`
- `app.py`
- dependency manifests
- PyInstaller spec
- setup script

If a root-level file is not a project entrypoint, packaging file, or repository policy file, it should be evaluated critically before adding more of the same pattern.

## Read Next

- Package and import ownership: `agent_docs/core/code_structure.md`
- Platform runtime architecture: `agent_docs/core/platform_architecture.md`
- Backend plugin model: `agent_docs/core/backend_architecture.md`
