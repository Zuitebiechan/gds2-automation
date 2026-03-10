# GDS2 Agentic Refactor Execution Plan

**Last Updated**: 2026-03-10  
**Scope**: GDS2 only (first migration target)  
**Status**: LangGraph-based navigation is the primary implementation; Stack B (session API) retained for future integration

## Current Branch Status (2026-03-10)

The agentic refactor took a different path than the original G1-G5 phased plan. Instead of building up from Action DSL + deterministic executor + constrained planner, the team built a LangGraph-based hybrid navigator with native tool-calling and LanceDB RAG. The G1-G5 "Stack B" code (contracts, executor, planner, policy_guard, session_orchestrator) still exists and is used by `session_api.py`, but it's not the primary navigation system.

### Primary implementation (LangGraph stack)

- `src/agentic/graph.py` + `nodes.py` ... LangGraph hybrid navigation graph (deterministic + HITL + AI agent)
- `src/agentic/tools.py` ... 9 native tool-calling functions (action + observation + signal) for ZhipuAI
- `src/agentic/knowledge_base.py` ... LanceDB knowledge base with RAG (12 pages, 4 error patterns, 186 icons, 7 screenshots)
- `src/agentic/llm_factory.py` ... ZhipuAI/Gemini/OpenAI factory for navigation agent
- `scripts/init_knowledge_base.py` ... Knowledge base seed data initializer
- Retry logic (2 attempts), wall-clock timeout (300s), step limit (50)
- Recovery wiring (AnomalyDetector + RecoveryManager integration)
- Navigation trace recording to LanceDB for auto-learning
- Page screenshots added to knowledge base seed data

### Stack B (session API path, retained for future use)

- `src/agentic/contracts/action_schema.py`, `state_schema.py` ... G1 contracts
- `src/agentic/capability_registry.py`, `policy_guard.py` ... G1 scaffolding
- `src/agentic/executor.py` ... G2 deterministic executor
- `src/agentic/adapters/gds2_adapter.py` ... G2 adapter
- `src/agentic/planner.py` ... G4 narrow branch planner
- `src/agentic/session_orchestrator.py` ... G3 session orchestrator
- `session_api.py` + `app.py` blueprint registration for `/api/session/*`

### Verified vs. still open

- **Verified in code/tests**: action schema, policy guard, executor, planner, adapter, session orchestrator, session API, graph compilation/imports
- **Verified locally with real GDS2**: LangGraph-based navigation flow to `data_display`
- **Not yet closed out**: SSE/DecisionGate replacing console HITL, LangGraph-to-session-API integration, broader page coverage, full manual acceptance of session-mode GUI + cloud workflow

### Practical interpretation

Treat G1 as **complete** (contracts exist, used by Stack B). G2 as **complete** (executor works, used by Stack B). G3 as **backend complete, end-to-end integration pending** (session API exists but LangGraph doesn't route through it yet). G4 is **deferred** ... the LangGraph + RAG approach replaces the narrow constrained planner originally envisioned. G5 is **partially started** via navigation trace recording to LanceDB, but the full replay harness and KPI dashboard are not built.

## WHY

GDS2 current automation is stable for the primary flow, but:

- branch handling is still path-assumption heavy,
- adaptation to unexpected states is limited,
- expansion to additional workflows is expensive.

This plan migrates GDS2 from hardcoded orchestration to a constrained agentic architecture while preserving current reliability.

## WHAT (Target for GDS2)

For GDS2, we will deliver:

- a typed Action DSL for GDS2 capabilities,
- deterministic executor with assertions and retries,
- HITL decision loop in existing exe diagnostics GUI,
- constrained planner for narrow decision domains,
- backward-compatible fallback to current deterministic flow.

## Baseline (Current GDS2 Ownership)

| Module | Current Responsibility | Refactor Direction |
|---|---|---|
| `src/navigation/controller.py` | Page detection + page transitions | Split into `state_observer` + `navigation_actions` |
| `src/workflows/data_viewer.py` | End-to-end orchestration | Replace with DSL-driven execution graph |
| `src/streaming/agent_navigator.py` | Java Agent command IPC | Keep as deterministic driver backend |
| `src/native/device_explorer.py` | Win32 Device Explorer automation | Keep as deterministic driver backend |
| `src/recovery/*` | anomaly detection + AI recovery | Reuse anomaly signals as planner/executor inputs |
| `diagnostics_api.py` | API bridge to workflow | Add session/decision APIs and event model |
| `vci_proxy/diagnostics_window.py` | GUI interactions | Add decision cards + action confirmation UI |

## Migration Constraints (Must Keep)

- Keep Java Agent path: `~/gds2-data/latest.json`
- Keep one GDS2 instance per machine lock
- Keep existing `/api/diagnose/*` behavior backward compatible initially
- Keep current deterministic fallback path available during rollout
- Keep Device Explorer Win32 path (Java Agent cannot own it)

## Phase G1 — Contracts and Scaffolding

### Tasks

- [ ] Add `src/agentic/contracts/action_schema.py` (or equivalent) for Action DSL
- [ ] Add `src/agentic/contracts/state_schema.py` for typed runtime state
- [ ] Define GDS2 capability catalog (allow-list):
  - [ ] `start_diagnostics`
  - [ ] `select_device`
  - [ ] `click_enter`
  - [ ] `choose_diagnostics_mode`
  - [ ] `select_module`
  - [ ] `select_data_category`
  - [ ] `select_sub_category`
  - [ ] `read_dtcs`
  - [ ] `start_live_stream`
  - [ ] `go_back`
  - [ ] `go_home`
  - [ ] `abort_session`
- [ ] Add policy guard for action validation + risk tags

### Deliverables

- [ ] Action schema + validators
- [ ] State schema + serializers
- [ ] GDS2 capability registry

### Exit Criteria

- All actions are schema-validated before execution.

## Phase G2 — Deterministic Executor for GDS2

### Tasks

- [ ] Add `gds2_executor` that executes DSL steps against existing drivers
- [ ] Add step contract enforcement:
  - [ ] preconditions check
  - [ ] postconditions check
  - [ ] timeout + retry policy
- [ ] Add bounded fallback policies:
  - [ ] `retry_same_step`
  - [ ] `go_back`
  - [ ] `go_home`
  - [ ] `abort`
- [ ] Add run log with correlation ID and per-step traces

### Deliverables

- [ ] Executor runtime module
- [ ] Unified step event model
- [ ] Backward-compatible execution adapter for current workflow

### Exit Criteria

- Existing happy-path GDS2 flow can be fully executed by the new executor with no success-rate regression.

## Phase G3 — HITL Integration (GUI + API)

### Tasks

- [ ] Extend cloud API:
  - [ ] `POST /api/session/start`
  - [ ] `GET /api/session/events` (SSE)
  - [ ] `POST /api/session/decision`
- [ ] Add SSE event types:
  - [ ] `progress`
  - [ ] `decision_required`
  - [ ] `decision_resolved`
  - [ ] `error`
  - [ ] `done`
- [ ] Add GUI decision component in `diagnostics_window.py`:
  - [ ] render structured options (button/radio)
  - [ ] timeout display
  - [ ] submit decision back to cloud
- [ ] Add deterministic timeout fallback if no user response

### Deliverables

- [ ] End-to-end decision loop (Cloud -> GUI -> Cloud)
- [ ] Decision audit logs

### Exit Criteria

- Ambiguous branch always triggers structured user choice; no blind guess.

## Phase G4 — Constrained Planner (Narrow Rollout)

### Initial Planner Scope (only these decisions)

- [ ] module selection disambiguation
- [ ] data category / sub-category disambiguation
- [ ] timeout strategy choice (`wait_longer` vs `go_back`)

### Tasks

- [ ] Add planner input state snapshot and output schema
- [ ] Add validator: planner output must map to allow-list action
- [ ] Add replan cap (`max_replans_per_step`)
- [ ] Add deterministic fallback if planner invalid/unavailable

### Deliverables

- [ ] planner module + strict output validation
- [ ] rollout switch (feature flag)

### Exit Criteria

- Planner increases branch completion and does not violate safety rules.

## Phase G5 — Observability and Replay

### Tasks

- [ ] Persist per-step artifacts:
  - [ ] page detection snapshot
  - [ ] button/list inventories
  - [ ] key Java Agent context excerpt
- [ ] Add replay harness for session debugging
- [ ] Add KPIs dashboard fields:
  - [ ] success rate
  - [ ] mean step latency
  - [ ] HITL rate
  - [ ] top failure reasons

### Exit Criteria

- Any failed run can be reconstructed with deterministic evidence.

## Work Breakdown by File/Module

| Area | Planned Change |
|---|---|
| `src/workflows/data_viewer.py` | Extract orchestration logic into DSL-compatible action sequences |
| `src/navigation/controller.py` | Expose normalized state observer APIs for executor/planner |
| `src/recovery/anomaly_detector.py` | Convert anomaly outputs into structured executor/planner signals |
| `diagnostics_api.py` | Add session/decision endpoints + event channel |
| `vci_proxy/diagnostics_window.py` | Add decision UX and response submission |
| `src/agentic/*` (new) | contracts, executor, planner, policy, state store |

## Testing and Verification Plan

### Unit

- [ ] schema validation tests for Action DSL
- [ ] policy guard tests for allow-list and risk checks
- [ ] executor tests for retry/timeout/postcondition behavior

### Integration

- [ ] end-to-end deterministic happy path (main -> data display)
- [ ] Device Explorer + Java Agent mixed-path transitions
- [ ] decision-required path with simulated GUI response

### Regression

- [ ] ensure `/api/diagnose/*` legacy path still works during migration
- [ ] compare baseline success metrics before/after migration

## Rollout Strategy

1. **Stage 0**: dark launch (record-only planner output, no execution)
2. **Stage 1**: planner enabled for one decision type only
3. **Stage 2**: planner enabled for module/category branches
4. **Stage 3**: planner-enabled default with deterministic fallback always on

## Rollback Strategy

- Feature flag: `AGENTIC_MODE_ENABLED=false` -> immediate fallback to current workflow path
- Keep old `DataViewerWorkflow` execution path until Stage 3 success criteria pass
- Preserve compatibility in diagnostics APIs during migration window

## Definition of Done (GDS2)

- [ ] GDS2 refactored flow runs under new executor in production conditions
- [ ] GUI HITL is used for uncertain decisions and logged
- [ ] Constrained planner stays inside allow-list contract
- [ ] Existing mechanic UX remains simple and stable
- [ ] Baseline reliability is maintained or improved

## Immediate Implementation Order (Next Sprint)

1. `G1` contracts + capability registry
2. `G2` deterministic executor skeleton
3. `G3` decision protocol (API + GUI)
4. first integration run on GDS2 happy path

## Notes on RAG / MCP / ReAct for This Plan

_Original plan notes (written before LangGraph implementation):_

- **ReAct**: optional implementation style for planner internals; not required for first release.
- **RAG**: not required for UI step planning; postpone to diagnosis-knowledge improvements.
- **MCP**: optional for external integrations; not required for core GDS2 migration.

_Actual outcome_: RAG is now central to navigation via LanceDB knowledge base. The LangGraph agent queries similar pages, error patterns, and tool suggestions during every AI fallback step. ReAct-style string parsing was skipped entirely in favor of native tool-calling.
