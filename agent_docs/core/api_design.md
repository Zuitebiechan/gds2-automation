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

Some conflict/state responses also include machine-readable `error_code` values.
Current examples include:

- `active_session_exists`
- `session_not_running`
- `navigation_not_awaiting_decision`
- `navigation_session_terminated`
- `navigation_decision_mismatch`

JSON error responses served under `/api/*` include `request_id` when request
observability is installed. This is additive: existing `success` and `error`
fields remain present. Success JSON payloads and SSE messages do not use this
error-correlation field.

## SSE Conventions

Current SSE streams begin with:

- `event: connected`

Current keepalive format:

- `: keepalive`

Route-scoped event contracts:

| Route | Events |
| --- | --- |
| `GET /api/session/events` | `connected`, keepalive comment, `progress`, `decision_required`, `decision_resolved`, `decision_timeout`, `network_quality_changed`, `error`, `done` |
| `GET /api/session/navigate/events` | `connected`, keepalive comment, `progress`, `decision_required`, `error`, `done` |
| `GET /api/navigate/events` | `connected`, keepalive comment, `progress`, `decision_required`, `error`, `done` |

Navigation streams must not emit business-session-only events such as
`network_quality_changed`, `decision_timeout`, or `decision_resolved`.

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
- `POST /api/session/logs/upload`

### Key semantics

- `/bootstrap/ready` is the lightweight readiness probe used by booting-node monitors
- `/bootstrap` allocates one existing hot-pool node or returns `202 capacity_pending` while cold-start capacity is still booting
- `/bootstrap/bind` associates one earlier node assignment with the concrete business session id after `/start` succeeds on the assigned node
- `/bootstrap/release` returns one unused or completed assignment back to the pool; callers may request `reprobe` so one failing node goes back through readiness checks instead of returning directly to `IDLE`
- `/start` creates the business session and may return `awaiting_decision`
- `/start_diagnostics` performs backend startup and tunnel-quality preflight
- `/decision` is shared by backend-selection, network-override, and branch-resolution gates
- read-oriented session routes such as `/events` and `/status` should not claim worker ownership, bind sessions, or create backend bundles as side effects
- `/execute` is capability-gated by `GENERIC_ACTIONS`
- session AI/live/navigation subroutes are all capability-gated
- session clear-DTC is capability-gated by `CLEAR_DTCS`
- session clear-DTC is intended to run from the current `Data Display` page; explicit module/category input is optional and only needed when the caller wants forced context reconciliation
- `/status` returns session state plus backend summary and network snapshot details when available
- `/logs/upload` ingests local observability artifacts staged by the tray client

### Active-session snapshot

The active-session snapshot is a read-only observability mirror. It is not the
runtime authority, does not bind or resume sessions, and does not provide
crash-safe recovery.

Stable snapshot fields:

- `session_id`
- `backend_name`
- `operation_kind`
- `selected_module`
- `selected_data_category`
- `current_page`
- `navigation_session_id`
- `ai_session_id`
- `live_data_active`
- `connection_epoch`
- `updated_at`

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

Recovered direct diagnostics responses preserve compatibility fields such as
`recovered`, `recovery_target`, and `reasoning`. `/api/diagnose/dtcs` error
responses preserve `dtcs: []`.

## `/api/navigate/*`

This is the standalone navigation surface.

Current routes:

- `POST /api/navigate/start`
- `GET /api/navigate/events`
- `POST /api/navigate/decision`
- `GET /api/navigate/status`
- `POST /api/navigate/abort`

Use this surface when you want standalone UI navigation behavior without creating a business session.

Current runtime binding rule:

- direct `/api/navigate/*` no longer bootstraps a backend/controller/viewer on demand
- `POST /api/navigate/start` requires an already-active worker-scoped backend bundle with a navigation runtime
- when no active backend navigation runtime is available, `POST /api/navigate/start` returns `409 Conflict`
- `GET /api/navigate/status` performs a runtime-only navigation-session lookup: it returns `200` for an existing navigation session and `404` for a missing one, without requiring an active backend handle
- `POST /api/navigate/decision` and `POST /api/navigate/abort` keep their existing handle-or-runtime fallback behavior

## Compatibility Notes

- Clients should migrate to `backend_name`.
- Clients should not treat `workflow` as the long-term canonical field.
- The unified GUI should prefer sending vehicle identity such as `brand`, and let the platform resolve backend selection.
- Explicit `backend_name` input still exists for compatibility and tooling, but it is not the preferred product-facing routing model.

## Read Next

- Runtime architecture: `agent_docs/core/platform_architecture.md`
- Backend model: `agent_docs/core/backend_architecture.md`
- Execution sequences and decision behavior: `agent_docs/core/runtime_flows.md`
