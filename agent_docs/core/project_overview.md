# Project Overview

## Scope

This document defines what the repository is, what it currently supports, and which terms the rest of the documentation uses.

This document does not describe the repository tree or file ownership in detail. For that, read `agent_docs/core/project_structure.md` and `agent_docs/core/code_structure.md`.

## Product Definition

This repository implements a cloud remote vehicle diagnostics platform.

Today it contains:

- a platform-neutral backend contract and worker-runtime layer in `diagnostic_platform/`
- a GDS2 backend facade in `backends/gds2/`
- GDS2-specific automation, navigation, streaming, and legacy orchestration compatibility exports in `src/`
- a supported Flask API surface in `server/`
- a reverse-tunnel and local tray client stack in `vci_proxy/`

The repository is no longer treated as a GDS2-only application, even though `gds2` is still the only production backend registered today.

## Current Product Goal

The product goal is to let a mechanic or operator start diagnostics from a unified GUI, provide vehicle identity such as brand, and let the backend platform decide which OEM software backend should handle the session.

The current primary mechanic-facing flow is:

1. Start diagnostics session
2. Select module
3. Select data category
4. Run AI diagnosis
5. Optionally read DTCs or start live streaming

For GDS2, DTC and live-data operations are expected to happen from the Data Display page. Runtime specifics are documented in `agent_docs/core/runtime_flows.md`.

## Supported Public API Surfaces

Only these HTTP surfaces are treated as supported product surfaces:

- `/api/session/*`
- `/api/navigate/*`

The intended product-facing surface is `/api/session/*`.

The other supported lower-level surface is:

- `/api/navigate/*` exposes standalone navigation execution

Legacy direct diagnostics routes have been removed; diagnostics clients should use `/api/session/*`.

Route-by-route details are documented in `agent_docs/core/api_design.md`.

## Runtime Model

The current worker model is:

- `1 worker process = 1 active business session = 1 active backend bundle`

Within that model:

- a business session is managed by `SessionOrchestrator`
- the worker-scoped execution state lives in `WorkerRuntime`
- the active backend binding lives in `ActiveBackendBundle`
- live-data, AI, and navigation are subordinate operations bound to the current business session

The runtime architecture is described in `agent_docs/core/platform_architecture.md`.

## Current Backend Scope

The platform is designed around backend-neutral contracts, but the current implementation scope is still transitional:

- `gds2` is the only registered production backend
- `src/gds2_orchestration/` is now mainly a compatibility export layer
- some shared action names still carry GDS2 terminology while the capability-first transition is completed

Those backend-boundary details are documented in `agent_docs/core/backend_architecture.md` and `agent_docs/core/code_structure.md`.

## Deployment Shape

At a high level, the system is split between a cloud side and a local side.

Cloud side:

- Flask API
- worker runtime
- OEM software runtime such as GDS2
- virtual J2534 DLL loaded by OEM software
- reverse tunnel server

Local side:

- reverse tunnel client
- tray GUI
- real J2534 driver and local VCI device

Operational detail is documented in `agent_docs/ops/deployment_and_operations.md` and `agent_docs/ops/vci_proxy_and_tunnel.md`.

## Terminology

### Business session

A user-visible diagnostics session created through `/api/session/start`. It tracks vehicle context, backend selection, state, decisions, and subordinate execution bindings.

### Backend

One OEM-specific diagnostic software integration that implements the shared `DiagnosticBackend` contract. Example: `gds2`.

### Backend descriptor

The metadata record that describes one backend: its `backend_name`, `display_name`, supported brands, default brand aliases, capabilities, and UI mode.

### Capability

A platform-level feature flag such as `READ_DTCS`, `LIVE_DATA`, `AI_DATA_COLLECTION`, `NAVIGATION`, or `GENERIC_ACTIONS`. Session and route behavior should be gated by capability, not by hard-coded backend names.

### Active backend bundle

The worker-local runtime bundle that binds the current session to one backend instance plus optional live-data, AI, navigation, and action-executor state.

### Decision gate

A pending user decision raised by the platform, such as backend selection, network override, or branch disambiguation.

### Navigation session

A subordinate execution session used to drive a UI-navigation task. It is distinct from the business session but can be bound to one.

### AI diagnosis session

A subordinate AI-engine execution session created after the platform has collected a standardized diagnostic payload.

### Tunnel quality

The measured health of the reverse tunnel between cloud and local sides. It is used during diagnostics preflight to allow, warn, or block startup. Details live in `agent_docs/ops/vci_proxy_and_tunnel.md`.

## Current Constraints

- Only one active business session is supported per worker process.
- Only one production backend is registered today.
- The public API uses `backend_name` as the canonical backend field.
- `workflow` is retained only as a deprecated compatibility alias for `backend_name`.
- The GUI should collect vehicle identity and submit it to the platform; backend routing should be resolved by the backend registry instead of being treated as a GUI-owned concern.

## What This Repository Is Not

- It is not a generic AI-agent experimentation repo.
- It is not a multi-worker session scheduler.
- It is not a repository where each new OEM should copy GDS2-specific runtime internals into `src/`.

New OEM support should be added through `diagnostic_platform/` plus `backends/`, as described in `agent_docs/core/backend_architecture.md`.

## Read Next

- Repository tree: `agent_docs/core/project_structure.md`
- Code ownership boundaries: `agent_docs/core/code_structure.md`
- Platform architecture: `agent_docs/core/platform_architecture.md`
- Backend model: `agent_docs/core/backend_architecture.md`
- APIs: `agent_docs/core/api_design.md`
