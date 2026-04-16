# Runtime Flows

## Scope

This document describes how the platform behaves at runtime for the main session, diagnostics, navigation, DTC, live-data, AI, and abort paths.

This document does not enumerate every file or route declaration. For those, read `agent_docs/core/code_structure.md` and `agent_docs/core/api_design.md`.

## Main Business-Session Flow

### 1. Bootstrap node allocation

Entries:

- `POST /api/session/bootstrap`
- `GET /api/session/bootstrap/ready`
- `POST /api/session/bootstrap/bind`
- `POST /api/session/bootstrap/release`

Sequence:

1. The client sends vehicle identity and optional preferred zone/metro to `POST /api/session/bootstrap`.
2. The allocator tries to claim one healthy idle node from inventory.
3. If an idle node exists, the API returns the assigned node endpoint plus `assignment_id`.
4. If no idle node exists but a booting node is already pending, the API returns `202 capacity_pending` and tells the client to retry.
5. If no idle or pending node exists and AWS provisioning is configured:
   - the provisioner launches a new EC2 instance in the selected Local Zone
   - inventory records that node as `BOOTING`
   - the API returns `202 capacity_pending`
6. The background readiness monitor polls `GET /api/session/bootstrap/ready` on booting nodes until the node API is reachable, then promotes the node from `BOOTING` to `IDLE`.
7. Once the client successfully starts `/api/session/start` on the assigned node, it calls `POST /api/session/bootstrap/bind`.
8. If session startup fails or the session later ends, the client calls `POST /api/session/bootstrap/release` to return the node to the pool.
9. If the assigned node returns a gateway/startup failure before `/start` succeeds, the client may release it with `reprobe` so the control plane moves that node back to readiness probing instead of handing it out again immediately.

### 2. Session creation

Entry:

- `POST /api/session/start`

Sequence:

1. The API creates a `SessionContext` from request input.
2. `start_business_session` calls `SessionOrchestrator.start_session`.
3. The orchestrator resolves the backend through the registry.
4. One of these outcomes occurs:
   - direct runnable backend selected
   - `awaiting_decision` with backend options
   - manual/non-runnable waiting state
5. The worker binds the active business session id.

Initial response includes:

- `session_id`
- `status`
- `backend_name`
- `workflow` alias
- `capabilities`
- optional `decision`

### 3. Backend decision flow

Entry:

- `POST /api/session/decision`

When the pending decision is backend selection:

1. The client submits `session_id`, `decision_id`, and selected option id.
2. The orchestrator applies the decision.
3. The session transitions back to `running`.
4. The selected `backend_name` becomes the runnable backend for the session.

## 4. Diagnostics start flow

Entry:

- `POST /api/session/start_diagnostics`

Sequence:

1. Resolve the bound session and backend.
2. Enforce `CORE_SESSION`.
3. Read preflight information from the backend, including tunnel-quality snapshot if the backend exposes it.
4. If tunnel quality is blocked and there is no effective override:
   - raise a `network_quality` decision gate
   - return `decision_required`
5. Otherwise create a `WorkerOperation` named `start_diagnostics`.
6. Emit progress indicating backend startup.
7. Call backend `start()`.
8. Read backend state and module list.
9. Reset subordinate session bindings:
   - clear navigation binding
   - clear AI binding
   - mark live data inactive
10. Emit progress that backend startup completed.
11. Return modules, VIN, device, and current tunnel snapshot.

If the operation is cancelled:

- backend startup state is reset when possible
- the worker operation ends cooperatively

## 5. Network-quality override flow

When diagnostics start is blocked by tunnel quality:

1. The platform raises a decision with options such as continue/cancel.
2. If the user chooses continue:
   - a network override is recorded against the current connection epoch
   - diagnostics start resumes immediately
3. If the user chooses cancel:
   - the override is cleared
   - diagnostics startup is cancelled cleanly

The override is epoch-bound. If the connection epoch changes, the previous override is invalidated.

## 6. Module selection flow

Entry:

- `POST /api/session/select_module`

Sequence:

1. Ensure the session is `running`.
2. Resolve the active backend.
3. Try backend-neutral module selection first.
4. If deterministic selection encounters ambiguity, raise a branch decision gate.
5. On success, update the session’s selected module.

## 7. Data-category selection flow

Entry:

- `POST /api/session/select_data_category`

Sequence:

1. Ensure the session is `running`.
2. Resolve the active backend.
3. Try backend-neutral data-category selection first.
4. If deterministic selection encounters ambiguity, raise a branch decision gate.
5. On success, update the session’s selected data category.

## 8. Branch-decision resume flow

When module/category selection raises a branch ambiguity:

1. The platform raises a `branch` decision gate.
2. The client submits the selected branch option through `POST /api/session/decision`.
3. The runtime resumes the interrupted action using the selected branch.
4. If another ambiguity appears, a new branch gate can be raised.

## Session-Bound Navigation Flow

### Start

Entry:

- `POST /api/session/navigate/start`

Sequence:

1. Ensure the session supports `NAVIGATION`.
2. Ensure no conflicting worker-exclusive operation is active.
3. Start a navigation sub-session through `navigation_runtime`.
4. Bind the navigation session id to the business session.
5. Emit navigation-start progress.

### Stream

Entry:

- `GET /api/session/navigate/events`

Behavior:

- emits `connected`
- forwards `progress`
- forwards `decision_required`
- emits `done` or `error`
- emits keepalive comments while idle

### Decision

Entry:

- `POST /api/session/navigate/decision`

Behavior:

- resumes one paused navigation sub-session with the selected item

### Completion

When navigation reaches its goal:

- session selections such as module/data category are updated from the navigation result when available
- the navigation binding is cleared

### Abort

Entry:

- `POST /api/session/navigate/abort`

Behavior:

- the sub-session is marked aborted
- pending wait states are released
- the business-session navigation binding is cleared

## Direct Navigation Flow

The standalone `/api/navigate/*` surface uses the same navigation runtime but is not bound to a business session.

## DTC Read Flow

### Session-bound

Entry:

- `POST /api/session/dtcs`

Sequence:

1. Ensure session supports `READ_DTCS`.
2. Resolve module/data-category/vehicle context from request, session selection, or backend state.
3. Call backend DTC read behavior.
4. Return normalized DTC entries.

### Direct diagnostics

Entry:

- `GET /api/diagnose/dtcs`

This performs a similar operation directly against the backend outside the business-session model.

## Clear-DTC Flow

### Session-bound

Entry:

- `POST /api/session/clear_dtcs`

Sequence:

1. Ensure session supports `CLEAR_DTCS`.
2. Reject the operation if live-data, navigation, or AI execution is still active for the session.
3. Acquire a worker-exclusive `clear_dtcs` operation lock.
4. Detect the backend's current page/state.
5. If the request explicitly supplies `module` or `data_category`, reconcile that target context before clearing.
6. Otherwise, treat the current `Data Display` context as authoritative and avoid hidden module/category reselection.
7. Call backend clear-DTC behavior.
8. Return normalized clear result payload with `cleared_count`, `message`, and final page context.

### Direct diagnostics

Entry:

- `POST /api/diagnose/clear_dtcs`

Behavior:

- resolve module/data-category context when supplied
- call backend clear-DTC behavior directly
- return normalized clear result payload

## Live-Data Flow

### Session-bound start

Entry:

- `POST /api/session/live_data/start`

Sequence:

1. Ensure `LIVE_DATA` capability.
2. Resolve the active backend and selected data category.
3. Start backend-owned live-data collection or a generic live-data path.
4. Bind the live-data-active flag to the business session.
5. Publish stream events through the scoped agent-event hub.

### Session-bound events

Entry:

- `GET /api/session/live_data/events`

Behavior:

- binds to a session-specific scoped stream
- emits `connected`
- forwards backend-owned streaming messages
- emits keepalive comments while idle

### Session-bound stop

Entry:

- `POST /api/session/live_data/stop`

Behavior:

- stop backend-owned or generic live-data collection
- clear the session live-data-active flag

## AI Diagnosis Flow

### Session-bound start

Entry:

- `POST /api/session/ai_diagnose`

Sequence:

1. Ensure `AI_DATA_COLLECTION` capability.
2. Resolve VIN, module, and data category from request/session/backend state.
3. Ask the backend to collect a standardized `DiagnosticPayload`.
4. Start an AI-engine session from that payload.
5. Bind the AI session id to the business session.
6. Stream AI events over the AI event endpoint.

### Session-bound events

Entry:

- `GET /api/session/ai_diagnose/events`

Behavior:

- emits `connected`
- forwards engine events
- clears the AI binding on terminal `done` or `error`

### Session-bound retry

Entry:

- `POST /api/session/ai_diagnose/retry`

Behavior:

- retry from a cached payload id using the shared AI engine

## Session Event Stream Behavior

The business-session event stream:

- begins with `connected`
- forwards orchestrator event messages
- periodically checks decision timeout
- emits `network_quality_changed` when the tunnel snapshot changes while the client is subscribed
- ends on terminal `done`

## Abort Flow

Entry:

- `POST /api/session/abort`

Sequence:

1. Cancel any active worker-exclusive operation for the session.
2. Abort active execution such as live data, navigation, or AI when bound.
3. Mark the business session aborted through the orchestrator.
4. Clear business-session bindings from the worker runtime.

## Important Runtime Facts

- The session API is the primary business-session surface.
- Direct diagnostics and direct navigation are still supported but are not the main product path.
- One worker currently owns one active business session and one active backend bundle.
- GDS2 startup can be cancelled cooperatively; the backend then tries to reset startup state.
- Tunnel quality can change while a session is running, and the session event stream can surface that change asynchronously.

## Read Next

- Route-level reference: `agent_docs/core/api_design.md`
- Worker/runtime ownership: `agent_docs/core/platform_architecture.md`
- Tunnel-quality internals: `agent_docs/ops/vci_proxy_and_tunnel.md`
