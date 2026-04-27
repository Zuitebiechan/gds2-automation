# Backend Architecture

## Scope

This document defines the capability-first backend model, the backend registry, session-to-backend resolution behavior, and the contract expected from future OEM integrations.

This document does not restate the full repository tree or deployment setup. For those, read `agent_docs/core/project_structure.md` and `agent_docs/ops/deployment_and_operations.md`.

## Backend Model

The platform treats each OEM diagnostic software integration as a backend.

A backend is responsible for translating OEM-specific runtime behavior into the shared platform contract defined in `diagnostic_platform/contracts.py`.

Current production backend:

- `gds2`

Current implementation path:

- facade: `backends/gds2/backend.py`
- runtime bridge: `backends/gds2/controller_runtime.py`
- registry/path runtime: `backends/gds2/registry_navigation_runtime.py`

## Canonical Platform Types

The canonical backend-facing types live in `diagnostic_platform/contracts.py`.

### `BackendCapability`

Current platform capabilities:

- `CORE_SESSION`
- `READ_DTCS`
- `LIVE_DATA`
- `AI_DATA_COLLECTION`
- `NAVIGATION`
- `GENERIC_ACTIONS`
- `CLEAR_DTCS`

These capabilities are the platform’s source of truth for feature gating.

### `BackendDescriptor`

Each backend exposes:

- `backend_name`
- `display_name`
- `supported_brands`
- `capabilities`
- `default_for_brands`
- `ui_mode`

### `ActiveBackendBundle`

This is the worker-local execution bundle for the currently bound backend.

It stores:

- backend identity and descriptor
- backend instance
- optional live-data handle
- optional AI collection handle
- optional navigation handle
- optional generic-action executor
- backend-private cached objects

## Contract Layers

### Core contract

All backends are expected to support the core session lifecycle.

In practical terms, the shared backend contract covers:

- start and stop
- backend state
- module and data-category selection
- DTC read capability when advertised
- optional VCI connection

Current abstract base class: `DiagnosticBackend` in `diagnostic_platform/contracts.py`.

### Extension behavior

Some capabilities are optional. The platform detects and gates them separately:

- live data
- AI payload collection
- navigation
- generic actions
- clear DTCs

In the current implementation, extension support is represented by a combination of:

- capability advertisement in `BackendDescriptor`
- concrete method implementation on the backend class
- route/session-side capability checks in `diagnostic_platform/runtime/*`

## Backend Registry

The registry bootstrap lives in `diagnostic_platform/backend_registry.py`.

Current behavior:

- build a singleton `BackendRegistry`
- register available backend instances
- expose descriptor lookup and brand-resolution helpers

Today only `GDS2DiagnosticBackend` is registered.

## Brand Resolution

Brand resolution is a registry responsibility, not a GUI responsibility.

The registry supports these outcomes:

### Unique direct match

If one backend supports the submitted brand uniquely, the session can bind that backend directly.

### Preferred/default match

If the brand matches a backend’s default alias list, that backend can be selected directly.

### Ambiguous match

If multiple backends support the brand, the session enters `awaiting_decision` and exposes backend options through a decision gate.

### No runnable backend

If no backend resolves directly, the session can remain in a non-runnable/manual choice state and wait for explicit decision logic.

## Public Payload Semantics

The public backend field is:

- `backend_name`

Compatibility alias:

- `workflow` is still emitted as a deprecated alias for `backend_name`

The backend field should not be treated as a GUI-owned primary choice in the normal product path.

## GDS2 Backend Responsibilities

`backends/gds2/backend.py` currently owns:

- the `gds2` backend descriptor
- GM brand and alias support such as `gds2` and `gm china`
- core startup and state adaptation
- module/category selection
- DTC read
- backend-owned live-data session start/stop
- backend-owned AI payload collection
- navigation bridge
- generic-action runtime bridge

Current navigation/runtime ownership note:

- `registry_runtime` is the production navigation source for GDS2 backend operations
- `backends/gds2/controller_runtime.py` now owns controller/state bridging only

Advertised GDS2 capabilities today:

- `CORE_SESSION`
- `READ_DTCS`
- `LIVE_DATA`
- `AI_DATA_COLLECTION`
- `NAVIGATION`
- `GENERIC_ACTIONS`
- `CLEAR_DTCS`

## Current GDS2 Legacy Island

The platform layer no longer treats GDS2 as the only conceptual backend, but some GDS2-owned implementation still remains outside `backends/gds2/`.

Current examples:

- legacy deterministic orchestration compatibility exports in `src/gds2_orchestration/`
- page navigation behavior in `src/navigation/`
- data collection and streaming helpers in `src/streaming/`

Current shared action primitives live in `diagnostic_platform/action_schema.py` and `diagnostic_platform/action_runtime.py`; GDS2-specific planner, policy, and adapter behavior lives under `backends/gds2/`.

## Capability Enforcement

Capability checks are used in the session and diagnostics APIs before execution.

Examples:

- session live-data flows require `LIVE_DATA`
- session AI diagnose flows require `AI_DATA_COLLECTION`
- session navigation flows require `NAVIGATION`
- session execute flows require `GENERIC_ACTIONS`
- session and diagnostics clear-DTC flows require `CLEAR_DTCS`

Unsupported capability requests are surfaced as deterministic failures, typically `501 Not Implemented`.

## Adding A New Backend

The intended path for a future OEM backend is:

1. Create `backends/<backend_name>/`
2. Implement a backend facade that satisfies the shared contract in `diagnostic_platform/contracts.py`
3. Define the backend descriptor
4. Add backend-specific runtime bridge code under that backend package
5. Register the backend in `diagnostic_platform/backend_registry.py`
6. Add backend-neutral regression tests plus backend-specific regression tests
7. Avoid modifying session route files unless the platform contract itself changes

## Design Rules

- Do not hard-code future OEM routing into `server/` routes.
- Do not make the GUI own backend-selection policy.
- Do not copy GDS2 page-model assumptions into the shared platform contract.
- Keep backend-specific collection, navigation guard, and recovery behavior inside the backend implementation whenever possible.

## Read Next

- Runtime architecture: `agent_docs/core/platform_architecture.md`
- Supported HTTP behavior: `agent_docs/core/api_design.md`
- Session and navigation sequences: `agent_docs/core/runtime_flows.md`
