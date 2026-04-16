# API Design

## Scope

This document describes the supported HTTP surfaces, route groups, payload conventions, SSE behavior, and common error semantics.

This document does not try to explain all internal runtime logic. For the actual execution sequences, read `agent_docs/core/runtime_flows.md`.

## Supported API Surfaces

The supported public surfaces are:

- `/api/session/*`
- `/api/diagnose/*`
- `/api/navigate/*`

Recommended product-facing surface:

- `/api/session/*`

Lower-level but still supported surfaces:

- `/api/diagnose/*`
- `/api/navigate/*`

## Common Response Conventions

### Canonical backend field

Public payloads use:

- `backend_name`

Compatibility alias:

- `workflow`

`workflow` is retained only as a deprecated alias of `backend_name`.

### Standard top-level fields

Common JSON response fields include some combination of:

- `success`
- `session_id`
- `status`
- `backend_name`
- `workflow`
- `capabilities`
- `result`
- `decision`
- `error`

### Error semantics

The handlers currently follow these broad conventions:

- `400 Bad Request`: invalid request input or invalid operation parameters
- `404 Not Found`: missing session, missing subordinate session, or missing event binding target
- `409 Conflict`: invalid runtime state, already-running work, or cancelled/busy conditions
- `501 Not Implemented`: capability not supported by the bound backend
- `500 Internal Server Error`: unexpected failure

## SSE Conventions

Current SSE streams begin with:

- `event: connected`

Current keepalive format:

- `: keepalive`

Common session event types:

- `progress`
- `decision_required`
- `decision_resolved`
- `decision_timeout`
- `network_quality_changed`
- `error`
- `done`

## `/api/session/*`

This is the business-session API.

Current routes:

- `GET /api/session/bootstrap/ready`
- `POST /api/session/bootstrap`
- `POST /api/session/bootstrap/bind`
- `POST /api/session/bootstrap/release`
- `POST /api/session/start`
- `POST /api/session/start_diagnostics`
- `POST /api/session/execute`
- `GET /api/session/events`
- `POST /api/session/decision`
- `POST /api/session/select_module`
- `POST /api/session/select_data_category`
- `POST /api/session/ai_diagnose`
- `GET /api/session/ai_diagnose/events`
- `POST /api/session/ai_diagnose/retry`
- `POST /api/session/dtcs`
- `POST /api/session/clear_dtcs`
- `POST /api/session/live_data/start`
- `GET /api/session/live_data/events`
- `POST /api/session/live_data/stop`
- `POST /api/session/navigate/start`
- `GET /api/session/navigate/events`
- `POST /api/session/navigate/decision`
- `POST /api/session/navigate/abort`
- `GET /api/session/navigate/status`
- `POST /api/session/abort`
- `GET /api/session/status`

### Key semantics

- `/bootstrap/ready` is the lightweight readiness probe used by booting-node monitors
- `/bootstrap` allocates one existing hot-pool node or returns `202 capacity_pending` while cold-start capacity is still booting
- `/bootstrap/bind` associates one earlier node assignment with the concrete business session id after `/start` succeeds on the assigned node
- `/bootstrap/release` returns one unused or completed assignment back to the pool; callers may request `reprobe` so one failing node goes back through readiness checks instead of returning directly to `IDLE`
- `/start` creates the business session and may return `awaiting_decision`
- `/start_diagnostics` performs backend startup and tunnel-quality preflight
- `/decision` is shared by backend-selection, network-override, and branch-resolution gates
- `/execute` is capability-gated by `GENERIC_ACTIONS`
- session AI/live/navigation subroutes are all capability-gated
- session clear-DTC is capability-gated by `CLEAR_DTCS`
- session clear-DTC is intended to run from the current `Data Display` page; explicit module/category input is optional and only needed when the caller wants forced context reconciliation
- `/status` returns session state plus backend summary and network snapshot details when available

## `/api/diagnose/*`

This is the lower-level direct diagnostics surface.

Current routes:

- `POST /api/diagnose/start`
- `GET /api/diagnose/dtcs`
- `POST /api/diagnose/clear_dtcs`
- `POST /api/diagnose/select_module`
- `POST /api/diagnose/live_data/start`
- `GET /api/diagnose/live_data/events`
- `POST /api/diagnose/live_data/stop`
- `POST /api/diagnose/ai_diagnose`
- `GET /api/diagnose/ai_diagnose/events`
- `POST /api/diagnose/ai_diagnose/retry`

Use this surface when you need direct backend capability access without business-session orchestration.

## `/api/navigate/*`

This is the standalone navigation surface.

Current routes:

- `POST /api/navigate/start`
- `GET /api/navigate/events`
- `POST /api/navigate/decision`
- `GET /api/navigate/status`
- `POST /api/navigate/abort`

Use this surface when you want standalone UI navigation behavior without creating a business session.

## Compatibility Notes

- Clients should migrate to `backend_name`.
- Clients should not treat `workflow` as the long-term canonical field.
- The unified GUI should prefer sending vehicle identity such as `brand`, and let the platform resolve backend selection.
- Explicit `backend_name` input still exists for compatibility and tooling, but it is not the preferred product-facing routing model.

## Read Next

- Runtime architecture: `agent_docs/core/platform_architecture.md`
- Backend model: `agent_docs/core/backend_architecture.md`
- Execution sequences and decision behavior: `agent_docs/core/runtime_flows.md`
