# Roadmap

**Last Updated**: 2026-02-25  
**Status**: Phase 1 complete, Phase 3 under final validation, Phase 3.5 in progress

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

## Phase 3.5: AI-Powered Diagnosis (In Progress)

Target UX: mechanics click one button, AI analyzes DTCs + live sensor data, returns structured verdict.

### Architecture

- **Trigger**: On-demand ("AI Diagnose" button) — post-click 30s data collection
- **LLM**: ZhipuAI glm-4.7-flash (free, 200K context)
- **Data pipeline**: AgentDataCollector → DiagnosticBuffer (30s sliding window) → delta-compressed payload → LLM → SSE-streamed result
- **Comm pattern**: SSE progress events (collecting → analyzing → streamed result)

### Cloud APIs (`/api/diagnose/*`)

- [ ] `POST /ai_diagnose` — start 30s data collection + LLM analysis, return session_id
- [ ] `GET /ai_diagnose/events?session_id=...` — SSE progress + streamed LLM result
- [ ] `POST /ai_diagnose/retry` — retry LLM call with cached payload (skip re-collection)

### New components

- [ ] `src/streaming/diagnostic_buffer.py` — DiagnosticBuffer: 30s ring buffer, dual-rate sampling, delta compression
- [ ] `src/diagnosis/llm_client.py` — ZhipuAI streaming wrapper + prompt assembly
- [ ] `src/diagnosis/ai_engine.py` — Orchestration: collector → buffer → DTCs → LLM → SSE

### Client UX changes

- [ ] "AI Diagnose" as primary button (large, prominent)
- [ ] "Read DTCs" / "Start Stream" moved to secondary/advanced row
- [ ] Progress display during 30s collection + LLM analysis
- [ ] Streamed result panel showing AI verdict in real-time

### Data payload to LLM

- Vehicle context (VIN, module)
- DTCs with full metadata (code, description, status, type, symptom)
- 30s live data as delta-compressed timeline (initial state + timestamped changes)
- Pre-flagged significant parameter changes with timing

### Future (V2 — deferred)

- [ ] DTC knowledge lookup (curated GM DTC → root cause JSON)
- [ ] RAG pipeline with Technical Service Bulletins
## Phase 4: Session Manager (Planned)

- [ ] On-demand VM lifecycle (create/destroy cloud sessions)
- [ ] Prebuilt VM image with GDS2 + agent + reverse server
- [ ] User/session allocation and auth
- [ ] Resource monitoring, timeout cleanup

## Future

- [ ] MDI/MDI2 VCI support
- [ ] Additional OEM tool adapters (BMW ISTA, Toyota, Ford, VW)
- [ ] Unified diagnostic API across OEM stacks
- [ ] Mechanic-facing mobile/miniprogram client
