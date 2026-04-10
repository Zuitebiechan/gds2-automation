# Code Structure

## Scope

This document describes package ownership, major module groups, and the intended dependency direction inside the codebase.

This document does not try to be the API reference or the runtime sequence spec. For behavior, read `agent_docs/core/api_design.md` and `agent_docs/core/runtime_flows.md`.

## Package Ownership

## `server/`

`server/` owns HTTP glue only.

Key responsibilities:

- create the Flask app and register supported blueprints
- validate request inputs at the route boundary
- call runtime/application helpers
- serialize JSON or SSE responses
- translate known exceptions into HTTP status codes

Important files:

- `server/app.py`
- `server/api/session.py`
- `server/api/diagnostics.py`
- `server/api/navigate.py`
- `server/api/session_dependencies.py`
- `server/api/session_ai_handlers.py`
- `server/api/session_live_data_handlers.py`
- `server/api/session_navigation_handlers.py`

`server/` should not become the place where backend-specific GDS2 workflow logic is reintroduced.

## `diagnostic_platform/`

`diagnostic_platform/` is the canonical platform-layer namespace.

It owns:

- shared contract types in `diagnostic_platform/contracts.py`
- shared deterministic action schema in `diagnostic_platform/action_schema.py`
- shared deterministic action-runtime primitives in `diagnostic_platform/action_runtime.py`
- shared business-session models in `diagnostic_platform/session_models.py`
- shared branch-planning result models in `diagnostic_platform/branch_planning.py`
- the business-session orchestrator boundary in `diagnostic_platform/session_orchestrator.py`
- backend-registry bootstrap in `diagnostic_platform/backend_registry.py`
- runtime state and execution helpers under `diagnostic_platform/runtime/`
- shared SSE utilities such as `diagnostic_platform/sse.py`

The runtime package is split by concern:

| File | Responsibility |
| --- | --- |
| `diagnostic_platform/runtime/worker_runtime.py` | Worker-scoped state, operation locks, active backend bundle, session bindings |
| `diagnostic_platform/runtime/session_lifecycle.py` | Start, abort, and status payload helpers for business sessions |
| `diagnostic_platform/runtime/session_preflight.py` | Diagnostics start preflight, network gate, override handling |
| `diagnostic_platform/runtime/session_decisions.py` | Backend/network/branch decision resolution helpers |
| `diagnostic_platform/runtime/session_actions.py` | Session-level action, navigation, AI, DTC, and live-data helpers |
| `diagnostic_platform/runtime/session_backends.py` | Backend resolution and capability enforcement for sessions |
| `diagnostic_platform/runtime/session_state.py` | Session binding helpers for worker-local subordinate state |
| `diagnostic_platform/runtime/session_streams.py` | SSE stream generation for sessions, navigation, AI, and live data |
| `diagnostic_platform/runtime/diagnostics_runtime.py` | Direct diagnostics API helpers |
| `diagnostic_platform/runtime/navigation_runtime.py` | Standalone navigation runtime used by direct navigation and session subflows |

## `backends/`

`backends/` owns backend facades that present OEM-specific functionality through the shared contract.

Today:

- `backends/gds2/backend.py` exposes the GDS2 backend descriptor, capability list, core contract methods, live-data/AI collection, navigation/action bridges, and state adaptation
- `backends/gds2/controller_runtime.py` bridges the backend facade to the underlying GDS2 workflow/controller stack
- `backends/gds2/planner.py` owns GDS2-specific constrained branch planning heuristics
- `backends/gds2/action_adapter.py` owns the deterministic action adapter that bridges executor steps to the GDS2 workflow/controller
- `backends/gds2/action_runtime.py` owns GDS2-specific UI state, page capability rules, and policy validation for deterministic actions

Future OEM implementations should be added as new siblings under `backends/`, not as branches inside the GDS2 code.

## `src/`

`src/` is currently the GDS2-specific implementation space.

Major areas:

| Path | Responsibility |
| --- | --- |
| `src/diagnosis/` | AI-engine and diagnosis helpers |
| `src/discovery/` | discovery/runtime support |
| `src/native/` | native automation helpers |
| `src/navigation/` | page model and navigation controller logic |
| `src/streaming/` | data collectors and streaming buffers |
| `src/workflows/` | workflow abstractions such as Data Viewer |
| `src/gds2_orchestration/` | compatibility exports for legacy deterministic GDS2 orchestration import paths |

The former `src/agentic` namespace has already been removed. Planner and action-adapter ownership now lives under `backends/gds2/`, while `src/gds2_orchestration/` remains as a legacy compatibility layer.

## `vci_proxy/`

`vci_proxy/` is a separate subsystem that spans both local and cloud concerns.

It owns:

- reverse tunnel server and local proxy listener
- reverse tunnel client and J2534 driver wrapper
- configuration and authentication
- protocol encoding/decoding
- short-lived caches around J2534 operations
- tunnel-quality grading and persisted snapshots
- the local diagnostics GUI window and tray client

## Intended Dependency Direction

The intended high-level direction is:

`server/` -> `diagnostic_platform/` -> `backends/` -> backend-specific implementation

For the current codebase, that backend-specific implementation is mostly inside `src/`.

Operationally, `vci_proxy/` is a sibling subsystem rather than a dependency of the Flask API layer. It is consumed at runtime by deployment topology, not by direct imports across every module.

## Current Transitional Couplings

The codebase is mid-transition from a GDS2-first implementation to a capability-first platform.

Because of that, some platform-adjacent modules still depend on GDS2-owned types:

- the deterministic action runtime still depends on GDS2-specific `UIState`, page capability rules, and page semantics under `backends/gds2/`
- constrained planning and action-adapter ownership is GDS2-specific and lives under `backends/gds2/`

Those couplings are important to understand, but they do not change the intended architectural rule: future OEMs should plug in through `diagnostic_platform/` plus `backends/`, not by adding more product logic to `src/`.

## Session Wiring Split

Inside the current server/runtime implementation:

- `server/api/session.py` owns route declarations and request boundary handling
- `server/api/session_dependencies.py` resolves the bound orchestrator, backend, AI engine, and backend-owned executor wiring
- `diagnostic_platform/runtime/*` owns reusable runtime actions and status logic

This split exists to keep HTTP glue thin while the platform-runtime layer absorbs business behavior.

## Naming Rules

- Use `diagnostic_platform` as the platform-layer name in code and docs.
- Use `backend_name` as the canonical backend identifier in public payloads.
- Treat `workflow` only as a deprecated compatibility alias in HTTP payloads.
- Treat `src/` as GDS2-specific unless a module is intentionally generalized.

## Where To Read Next

- Runtime architecture and worker model: `agent_docs/core/platform_architecture.md`
- Capability-first backend model: `agent_docs/core/backend_architecture.md`
- Route behavior and error semantics: `agent_docs/core/api_design.md`
