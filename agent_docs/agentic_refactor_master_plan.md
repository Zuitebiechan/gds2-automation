# Agentic UI Automation Refactor Master Plan

**Last Updated**: 2026-03-09  
**Status**: Planning complete; branch now contains partial implementation of the planned architecture

## Implementation Snapshot (2026-03-09)

Since this plan was written, the branch has moved beyond planning:

- A typed **Action DSL** exists under `src/agentic/contracts/`
- A **deterministic executor** + **policy guard** + **GDS2 adapter** exist under `src/agentic/`
- A **session orchestrator** and `/api/session/*` Flask blueprint exist and are test-covered
- A separate **LangGraph-based hybrid navigation prototype** exists for local GDS2 validation

What is still not complete is the full end-to-end convergence of these layers into one production-default orchestration path with complete observability, replay, and broad runtime validation.

## WHY

Current GDS2 UI automation is production-usable for one stable path, but it is still mostly hardcoded and difficult to scale to:

- multiple workflows inside one OEM tool,
- multiple OEM tools (GDS2, ISTA, ODIS, etc.),
- dynamic branches and uncertain runtime states.

We need to evolve from **hardcoded flow automation** to a **reusable agentic orchestration architecture** while preserving deterministic execution safety.

## WHAT

Build a reusable architecture:

- **One Agent Core** (planner + policy + state + HITL protocol)
- **Per-App Adapters** (GDS2 adapter first; more OEM adapters later)
- **Deterministic Executors** (Java Agent / Win32 actions remain deterministic)
- **Schema-Based Action DSL** (allow-listed tools only)

This is **not** free-form autonomous clicking.  
This is a **constrained AI orchestration layer** on top of deterministic UI execution.

## Guiding Principles

1. **Planner decides, Executor acts**
   - LLM never directly sends raw UI clicks.
2. **Allow-list first**
   - Only predefined actions with JSON schema are executable.
3. **HITL by design**
   - Uncertain branch -> ask user through GUI options.
4. **Every step is verifiable**
   - `precondition -> action -> postcondition`.
5. **Replayability**
   - Every run must be debuggable from event logs + snapshots.
6. **Single-instance safety**
   - Keep one-tool-per-machine lock guarantees.

## Current Problems (Validated)

| Problem | Current Behavior | Impact |
|---|---|---|
| Hardcoded route assumptions | Button/list text anchors and fixed path depth | New intermediate page can break whole flow |
| Runtime uncertainty handling limited | Recovery exists but mostly modal/timeout oriented | Branching flexibility is limited |
| App-specific coupling | `GDS2Page`, GDS2 text heuristics, GDS2-specific workflows | Hard to scale to additional OEM software |
| Executor-observer coupling | Data parsing tied to one JSON schema and one app context | Extension cost rises with each app |

## Target Architecture

```text
+---------------------------------------------------------------+
|                         EXE GUI (User)                        |
|  - Vehicle input (brand/model/VIN)                            |
|  - Decision cards for uncertain steps                          |
+------------------------------+--------------------------------+
                               |
                               v
+---------------------------------------------------------------+
|                 Session Orchestrator (Cloud)                  |
|  - Session lifecycle, VM routing, OEM app selection           |
|  - SSE events: progress / decision_required / decision_done    |
+------------------------------+--------------------------------+
                               |
                               v
+---------------------------------------------------------------+
|                       Agent Core (Reusable)                   |
|  Planner  |  Policy Guard  |  State Store  |  HITL Gateway   |
|  (LLM)    | (allow-list)   | (typed state) | (structured ask) |
+------------------------------+--------------------------------+
                               |
                               v
+---------------------------------------------------------------+
|                 Deterministic Executor (Reusable)             |
|  - Execute Action DSL steps                                   |
|  - Preconditions/Postconditions                               |
|  - Retry / timeout / rollback                                 |
+------------------------------+--------------------------------+
                               |
                               v
+---------------------------------------------------------------+
|                    App Adapter Layer (Per OEM)               |
|  GDS2Adapter (first) | ISTAAdapter | ODISAdapter | ...        |
|  - Observe page state                                         |
|  - Map generic actions to app-specific commands               |
+------------------------------+--------------------------------+
                               |
                               v
+---------------------------------------------------------------+
|           Existing Low-level Drivers (Keep/Re-use)            |
|  Java Agent (JavaFX) + Win32 Device Explorer + VCI Proxy      |
+---------------------------------------------------------------+
```

## Core Contracts

### 1) Action DSL (cross-app)

Each step must be typed and validated:

```json
{
  "action": "select_module",
  "args": {"module_id": "K20"},
  "preconditions": ["page in [module_list]"],
  "postconditions": ["page in [module_submenu,data_display]"],
  "timeout_sec": 20,
  "retry_policy": {"max_attempts": 2, "backoff_sec": 1.5},
  "risk_level": "normal"
}
```

### 2) HITL Protocol

- `decision_required`: cloud -> GUI (structured options)
- `decision_submit`: GUI -> cloud (selected option id)
- `decision_timeout`: fallback action if user does not respond

### 3) App Adapter Capability Contract

Each adapter must implement:

- `detect_state()`
- `list_choices(domain)`
- `execute_action(step)`
- `can_handle(step)`
- `recover_from(anomaly)`

## Phase Plan

## Phase A — Foundation (No LLM planning yet)

- [ ] Introduce Action DSL schema and validator
- [ ] Build deterministic executor around existing GDS2 operations
- [ ] Add event-sourced run logs (step start/end, assertion results)
- [ ] Add artifacts per step (snapshot json + key UI markers)

**Exit Criteria**
- Existing hardcoded GDS2 flow runs through executor with same success rate.

## Phase B — HITL + Policy Guard

- [ ] Add uncertainty gates (ambiguous choices, unknown page, repeated no-op)
- [ ] Add `decision_required` / `decision_submit` API + SSE events
- [ ] Add GUI decision component in exe
- [ ] Add strict allow-list policy and risk-tag checks

**Exit Criteria**
- Uncertain branches are always resolved via structured user choice; no free-form blind execution.

## Phase C — Constrained Planner (Narrow Scope)

- [ ] Planner can choose next action only from validated action list
- [ ] Planner limited to one branch domain first (module/category selection)
- [ ] Add bounded replan count and deterministic fallback
- [ ] Add regression replay on recorded sessions

**Exit Criteria**
- Planner improves branch success without reducing safety metrics.

## Phase D — Multi-App Expansion

- [ ] Define adapter plugin registry
- [ ] Add 2nd OEM app adapter prototype
- [ ] Add app capability registry + routing policy
- [ ] Keep core unchanged while adding app-specific adapters

**Exit Criteria**
- New app onboarded by adapter + config, no core logic rewrite.

## Reliability Checklist (Must-Have)

- [ ] Actionability checks before execution
- [ ] Loop/stagnation detection (same action + same state repeated)
- [ ] Timeout strategy with bounded retries
- [ ] Safe fallback path (`go_back`, `home`, `abort`)
- [ ] HITL escalation policy for low confidence
- [ ] Full run observability (correlation id, step traces, artifacts)

## API Evolution (Cloud <-> EXE)

| Endpoint/Event | Purpose |
|---|---|
| `POST /api/session/start` | Start session and route app by brand/model/VIN |
| `GET /api/session/events` (SSE) | Progress + decision events |
| `POST /api/session/decision` | Submit user choice for pending decision |
| `POST /api/session/abort` | User-safe abort |
| `POST /api/session/retry` | Deterministic retry from checkpoint |

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Planner hallucination | Allow-list + schema validation + guardrails |
| Infinite loops | Replan cap + stagnation detector + forced HITL |
| App UI drift | Adapter-local state detection + fallback transitions |
| Operational complexity | Event logs + replay tooling + phased rollout |
| Latency spikes | Keep deterministic path fallback, planner scoped early |

## Definition of Done (Master)

- [ ] A reusable Agent Core exists with typed contracts.
- [ ] GDS2 runs through the new orchestration path with no regression.
- [ ] GUI HITL loop works end-to-end in packaged exe.
- [ ] At least one additional app can be integrated via adapter without core rewrite.
- [ ] Observability and replay are available for postmortem/debug.

## Out of Scope (Current Refactor)

- Full autonomous free-form desktop agent
- Large RAG knowledge system for diagnosis reasoning (can be phase later)
- MCP integrations for external systems (optional later)

## Deliverables

- [x] Architecture + plan document (this file)
- [ ] Action DSL schema files
- [ ] Executor skeleton and adapter registry
- [ ] GDS2 adapter migration
- [ ] GUI decision protocol integration
