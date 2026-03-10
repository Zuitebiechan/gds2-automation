# Roadmap

**Last Updated**: 2026-03-10  
**Status**: Phase 1 complete, Phase 3 complete, Phase 3.5 complete, agentic navigation implemented (navigate API + client wired)

## Completed Foundations

- [x] GDS2 cloud deployment + Java Agent
- [x] VCI Proxy tunnel (reverse connection, binary protocol, PSK auth)
- [x] 3-layer caching (server) + VBATT cache (client)
- [x] Full GDS2 UI automation + 100ms data collection
- [x] DTC extraction + streaming debug APIs
- [x] SM2/SM3 VCI support
- [x] Multi-port validation and runtime stability fixes

## Phase 1: Client Packaging ✅

- [x] PyInstaller bundle (`VCI_Proxy_Client.exe`)
- [x] System tray connectivity UI (connected/connecting/disconnected/error)
- [x] First-run settings dialog (host, tunnel port, API port, token, DLL path)
- [x] Config persistence in `%APPDATA%/VCI_Proxy/config.json`
- [x] Auto-reconnect status updates in tray icon
- [ ] Installer package (.msi/Inno Setup) — deferred

## Phase 2: Connection Code Flow (Deferred)

Deferred until multi-user/session manager has business priority.

- [ ] Connection-code API (host/port/token resolution)
- [ ] Client-side code input + resolution
- [ ] Code lifecycle and expiry management

## Phase 3: Diagnostics API + Local UX (Implemented)

### Cloud APIs (`/api/diagnose/*`)

- [x] `POST /start` — one-button start + auto-connect `VCI Proxy (Remote)`
- [x] `POST /select_module` — select module, return data categories
- [x] `GET /dtcs` — read DTCs from current Data Display context
- [x] `POST /live_data/start` — start AgentDataCollector at selected category
- [x] `GET /live_data/events` — SSE data stream
- [x] `POST /live_data/stop` — stop stream and navigate back

### Local mechanic-facing exe UX

- [x] Diagnostics window integrated into tray client
- [x] UX flow enforced:
  1. Start Diagnostics
  2. Select Module + click Select
  3. Select Data Category
  4. Read DTCs / Start Stream
- [x] Real-time SSE table updates in local UI
- [x] Build/runtime fixes (PyInstaller + Pillow + diagnostics launch reliability)

### Current validation focus

- [ ] Final in-vehicle verification of DTC presence/absence behavior across modules
- [ ] Final in-vehicle verification of live parameter value-change visibility under dynamic conditions

## Phase 3.5: AI-Powered Diagnosis ✅

Target UX: mechanics click one button, AI analyzes DTCs + live sensor data, returns structured verdict.

### Architecture

- **Trigger**: On-demand ("AI Diagnose" button) — post-click 30s data collection
- **LLM**: ZhipuAI glm-4.7 (thinking disabled, all tokens → content output)
- **Data pipeline**: AgentDataCollector → DiagnosticBuffer (30s sliding window) → delta-compressed payload → LLM → SSE-streamed result
- **Comm pattern**: SSE progress events (collecting → analyzing → streamed result)

### Cloud APIs (`/api/diagnose/*`)

- [x] `POST /ai_diagnose` — start 30s data collection + LLM analysis, return session_id
- [x] `GET /ai_diagnose/events?session_id=...` — SSE progress + streamed LLM result
- [x] `POST /ai_diagnose/retry` — retry LLM call with cached payload (skip re-collection)

### New components

- [x] `src/streaming/diagnostic_buffer.py` — DiagnosticBuffer: 30s ring buffer, dual-rate sampling, delta compression
- [x] `src/diagnosis/llm_client.py` — ZhipuAI streaming wrapper + prompt assembly + verdict parsing
- [x] `src/diagnosis/ai_engine.py` — Orchestration: collector → buffer → DTCs → LLM → SSE

### Client UX changes

- [x] "AI Diagnose" as primary button (large, prominent)
- [x] "Read DTCs" / "Start Stream" moved to secondary/advanced row
- [x] Progress display during 30s collection + LLM analysis
- [x] Streamed result panel showing AI verdict in real-time

### Data payload to LLM

- Vehicle context (VIN, module)
- DTCs with full metadata (code, description, status, type, symptom)
- 30s live data as delta-compressed timeline (initial state + timestamped changes)
- Pre-flagged significant parameter changes with timing

### Robustness (battle-tested)

- Streaming timeout protection: 60s per-chunk, 180s total stream
- Non-stream fallback when streaming returns 0 content chunks
- Verdict parsing: JSON → markdown code block → brace extraction → ast.literal_eval
- Deferred event queue cleanup (30s Timer) to prevent SSE 404 race conditions
- Single active session enforcement (409 reject on concurrent requests)

### Known model history

- glm-4.7-flash: **rejected** — reasoning model spent all max_tokens on thinking, 0 content output
- glm-4.7 with `thinking={"type": "disabled"}`: **working** — correct structured output
- DeepSeek V3: approved fallback if glm-4.7 has stability issues

### Future (V2 — deferred)

- [ ] DTC knowledge lookup (curated GM DTC → root cause JSON)
## Agentic UI Automation Refactor (In Progress)

Goal: replace brittle hardcoded navigation assumptions with a safer hybrid architecture:

- deterministic steps for uncontested paths,
- HITL for module/data choices,
- constrained AI handling for unknown/runtime-varying states.

### Already completed on this branch

- [x] LangGraph hybrid navigation graph (`src/agentic/graph.py`, `nodes.py`)
- [x] Native tool-calling with ZhipuAI (9 tools in `src/agentic/tools.py`)
- [x] LanceDB knowledge base with RAG (12 pages, 4 error patterns, 186 icons, 7 screenshots)
- [x] Retry logic (2 attempts), wall-clock timeout (300s), step limit (50)
- [x] Recovery wiring (AnomalyDetector + RecoveryManager integration)
- [x] Navigation trace recording to LanceDB
- [x] Page screenshots added to knowledge base seed data
- [x] ZhipuAI/Gemini/OpenAI factory for navigation agent (`src/agentic/llm_factory.py`)
- [x] LanceDB knowledge base interface + initializer (`src/agentic/knowledge_base.py`, `scripts/init_knowledge_base.py`)
- [x] Action DSL contracts (`src/agentic/contracts/*`) (Stack B, session API path, retained for future use)
- [x] Capability registry + policy guard + deterministic executor (Stack B, session API path, retained for future use)
- [x] GDS2 action adapter bridging executor to real workflow/controller
- [x] Session orchestrator + `/api/session/*` backend blueprint + unit tests (Stack B, session API path, retained for future use)
- [x] Client session-mode call sites / decision handling hooks in `vci_proxy/diagnostics_window.py`
- [x] Navigate API blueprint (`/api/navigate/*`) with SSE event stream and HITL decision flow
- [x] `run_with_event_queue()` in `graph.py` — queue-based IPC for Flask integration
- [x] Client GUI rewired: Start Agent Diagnostics calls `/api/navigate/start`, consumes SSE events, handles HITL decisions via dropdown

### Still remaining

- [ ] Broader coverage for device explorer / error dialogs / unknown intermediate pages
- [ ] Full manual acceptance of navigate-mode GUI + cloud flow
- [ ] End-to-end in-vehicle test with real GDS2

## Phase 4: Session Manager / Session Productization (Planned)

- [ ] On-demand VM lifecycle (create/destroy cloud sessions)
- [ ] Prebuilt VM image with GDS2 + agent + reverse server
- [ ] User/session allocation and auth
- [ ] Resource monitoring, timeout cleanup

## Future

- [ ] MDI/MDI2 VCI support
- [ ] Additional OEM tool adapters (BMW ISTA, Toyota, Ford, VW)
- [ ] Unified diagnostic API across OEM stacks
- [ ] Mechanic-facing mobile/miniprogram client
