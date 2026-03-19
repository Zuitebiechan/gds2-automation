# Roadmap

**Last Updated**: 2026-03-18  
**Status**: Step 1 complete, Step 2 complete, Phase 3 complete, Phase 3.5 complete, GDS2 agentic navigation implemented

## Completed Foundations

- [x] GDS2 cloud deployment + Java Agent
- [x] VCI Proxy tunnel (reverse connection, binary protocol, PSK auth)
- [x] 3-layer caching (server) + VBATT cache (client)
- [x] Full GDS2 UI automation + 100ms data collection
- [x] SM2/SM3 VCI support
- [x] Multi-port validation and runtime stability fixes

## Step 1: Legacy Browser Surface Cleanup ✅

- [x] Remove old browser debug UI from supported architecture
- [x] Reduce `app.py` to a thin Flask entry point that registers blueprints only
- [x] Remove obsolete API families from supported documentation
- [x] Standardize supported API surfaces to:
  - `/api/diagnose/*`
  - `/api/navigate/*`
  - `/api/session/*`

## Step 2: GDS2 Backend Extraction ✅

- [x] Define shared `DiagnosticBackend` abstraction
- [x] Add shared platform dataclasses and `BackendRegistry`
- [x] Add shared diagnostics/live-data SSE helpers
- [x] Extract GDS2 facade into `backends/gds2/backend.py`
- [x] Re-orient diagnostics/session flows around backend-facing architecture
- [x] Standardize documentation on `diagnostic_platform/` as the platform layer name

## Phase 1: Client Packaging ✅

- [x] PyInstaller bundle (`VCI_Proxy_Client.exe`)
- [x] System tray connectivity UI (connected/connecting/disconnected/error)
- [x] First-run settings dialog (host, tunnel port, API port, token, DLL path)
- [x] Config persistence in `%APPDATA%/VCI_Proxy/config.json`
- [x] Auto-reconnect status updates in tray icon
- [ ] Installer package (.msi/Inno Setup) — deferred

## Phase 2: Connection Code Flow (Deferred)

Deferred until multi-user/session-manager work becomes a priority.

- [ ] Connection-code API (host/port/token resolution)
- [ ] Client-side code input + resolution
- [ ] Code lifecycle and expiry management

## Phase 3: Diagnostics API + Local UX ✅

### Diagnostics API (`/api/diagnose/*`)

- [x] `POST /api/diagnose/start`
- [x] `POST /api/diagnose/select_module`
- [x] `GET /api/diagnose/dtcs`
- [x] `POST /api/diagnose/live_data/start`
- [x] `GET /api/diagnose/live_data/events`
- [x] `POST /api/diagnose/live_data/stop`

### Local mechanic-facing UX

- [x] Diagnostics window integrated into tray client
- [x] UX flow enforced:
  1. Start Diagnostics
  2. Select Module + click Select
  3. Select Data Category
  4. Read DTCs / Start Stream / AI Diagnose
- [x] Real-time SSE updates in local UI
- [x] Build/runtime fixes for packaged client flow

### Current validation focus

- [ ] Final in-vehicle verification of DTC behavior across modules
- [ ] Final in-vehicle verification of live parameter value-change visibility under dynamic conditions

## Phase 3.5: AI-Powered Diagnosis ✅

- [x] `POST /api/diagnose/ai_diagnose`
- [x] `GET /api/diagnose/ai_diagnose/events`
- [x] `POST /api/diagnose/ai_diagnose/retry`
- [x] 30s collection window + dual-rate sampling + delta-compressed payloads
- [x] ZhipuAI glm-4.7 with thinking disabled
- [x] SSE progress + streamed verdict output
- [x] Deferred event cleanup and timeout protection

## Agentic Navigation ✅

- [x] LangGraph hybrid navigation graph (`src/agentic/graph.py`, `nodes.py`)
- [x] Native tool-calling with ZhipuAI (`src/agentic/tools.py`)
- [x] Rule-based page hints + AI fallback navigation
- [x] Navigate API blueprint (`/api/navigate/*`) with SSE + HITL flow
- [x] Client GUI integration for navigation progress and decisions
- [x] Retry logic and timeout limits

### Still remaining

- [ ] Broader coverage for device explorer / error dialogs / unknown intermediate pages
- [ ] Full manual acceptance of navigate-mode GUI + cloud flow
- [ ] End-to-end in-vehicle test with real GDS2

## Session-Oriented Orchestration (Implemented for GDS2, still evolving)

- [x] `/api/session/*` blueprint and in-memory orchestrator
- [x] Session lifecycle, decision gates, and SSE event flow
- [x] Session-aware start/select/decision/abort/status endpoints
- [x] GDS2-backed session operations through retained executor/adapter path
- [ ] Broader convergence between session orchestration and the long-term multi-backend platform model

## Phase 4: Session Manager / Productization (Planned)

- [ ] On-demand VM lifecycle (create/destroy cloud sessions)
- [ ] Prebuilt VM image with GDS2 + agent + reverse server
- [ ] User/session allocation and auth
- [ ] Resource monitoring and timeout cleanup

## Future Platform Work

### Step 3: Generalize Shared Diagnosis Services (Next)

Goal: Make the AI diagnosis pipeline backend-agnostic so any future OEM backend can feed data into the same analysis engine.

- [ ] `LLMClient` system prompt: replace hard-coded "GM/GDS2" with dynamic `vehicle_context.brand` / `vehicle_context.software` injection
- [ ] `AIEngine` / `DiagnosticBuffer`: accept standardized `DiagnosticPayload` from `diagnostic_platform.contracts` instead of raw GDS2-shaped dicts
- [ ] `AgentDataCollector`: generalize the collection interface so non-Java-Agent backends can provide live data through the same pipeline
- [ ] `route_workflow()` in `SessionOrchestrator`: extend from `GM_BRANDS -> gds2` to a real multi-brand lookup via `BackendRegistry`
- [ ] Move `daily_report` and other non-GDS2-specific concepts out of GDS2-specific paths

### Step 4: Onboard Second OEM Software Backend

Goal: Validate that the `DiagnosticBackend` contract actually works for a non-GDS2 software stack.

- [ ] Choose the second OEM software (e.g. Honda HDS, Toyota Techstream)
- [ ] Create `backends/<name>/backend.py` implementing `DiagnosticBackend`
- [ ] Use a different automation runtime (pywinauto / UIA / OCR) — this validates that the contract is not GDS2-shaped
- [ ] Verify that `diagnostics_api.py` and `session_api.py` work without GDS2-specific changes
- [ ] If the contract needs adjustment, iterate on `diagnostic_platform/contracts.py` and update GDS2 backend accordingly
- [ ] Document the second backend's page model, action set, and automation stack

### Infrastructure (Planned, parallel to Step 3–4)

- [ ] Cloud Windows worker isolation: one OEM software per worker, session-bound
- [ ] Worker scheduler: allocate/release workers per diagnostic session
- [ ] VCI tunnel binding: route tunnel to the correct worker/software instance
- [ ] On-demand VM lifecycle (create/destroy cloud sessions)
- [ ] Prebuilt VM images per OEM software
- [ ] Additional OEM backends via `DiagnosticBackend`
- [ ] Unified diagnostic API behavior across OEM stacks
- [ ] MDI/MDI2 VCI support
- [ ] Mechanic-facing mobile/miniprogram client
