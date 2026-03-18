# Diagnostic Platform RPA — Cloud Remote Vehicle Diagnostics

## WHY

This project is evolving from a **GDS2-only automation stack** into a **multi-software diagnostic platform**. The goal stays the same: mechanics connect a local VCI device, trigger simple actions, and receive results without directly operating OEM software in the cloud.

## WHAT

The codebase now has five product layers:

| Layer | Location | Purpose |
|---|---|---|
| **VCI Proxy Tunnel** | `vci_proxy/` | Connect cloud diagnostic software to local VCI hardware via reverse TCP + virtual J2534 DLL |
| **Platform Core** | `diagnostic_platform/` | Shared contracts, backend registry, standardized schemas, and SSE helpers for all diagnostic software |
| **Backend Facades** | `backends/`, `backends/gds2/` | Per-software backend implementations behind the shared `DiagnosticBackend` contract |
| **RPA Automation** | `src/` | GDS2-specific automation, Java Agent IPC, navigation, streaming, recovery, and AI diagnosis |
| **API + Client UX** | `app.py`, `diagnostics_api.py`, `navigate_api.py`, `session_api.py`, `vci_proxy/client_gui.py`, `vci_proxy/diagnostics_window.py` | Thin Flask entry point plus mechanic-facing local tray/diagnostics UI |

`app.py` is now a **thin Flask entry point** that registers blueprints only.

## CURRENT STATUS (2026-03-18)

- GDS2 remains the only fully implemented backend, but the platform structure now targets **multiple OEM diagnostic software stacks**
- Step 1 cleanup is complete
- Step 2 **GDS2 backend extraction** is complete:
  - shared `DiagnosticBackend` abstraction defined in `diagnostic_platform/contracts.py`
  - `BackendRegistry` and standard dataclasses define the cross-backend contract
  - `backends/gds2/backend.py` exposes `GDS2DiagnosticBackend` as the GDS2 facade over the existing `src/` implementation
- `diagnostics_api.py` is backend-oriented and uses the shared platform layer plus standardized SSE helpers from `diagnostic_platform/sse.py`
- `session_api.py` uses the backend abstraction for GDS2 session lifecycle/orchestration and keeps its own session event queues for `/api/session/*`
- `navigate_api.py` remains the LangGraph navigation surface for GDS2 agentic page navigation
- Phase 3 diagnostics flow is implemented under `/api/diagnose/*`
- Phase 3.5 AI-powered diagnosis is implemented:
  - ZhipuAI glm-4.7 with `thinking={"type": "disabled"}`
  - 30s sliding collection window with dual-rate sampling and delta-compressed payloads
  - SSE progress + streamed verdict output
  - timeout protection and deferred SSE cleanup
- Agentic navigation is implemented:
  - deterministic + HITL + AI fallback graph
  - LanceDB knowledge base
  - `/api/navigate/*` SSE flow integrated with the client GUI
- Local tray diagnostics UX remains the primary mechanic-facing product path

## PLATFORM ARCHITECTURE

### Shared platform contract

All backend implementations are converging on a common interface:

- `DiagnosticBackend` — abstract API for start/connect/select/read/stream/action/state operations
- `BackendRegistry` — lookup/registration layer for available backends and supported brands
- Standard schemas:
  - `VehicleContext`
  - `DTC`
  - `LiveDataPoint`
  - `DiagnosticPayload`
  - `BackendState`
  - `ClearResult`
  - `ActionResult`
  - `LiveDataStream`

### Directory structure to document and use going forward

```text
diagnostic_platform/
  contracts.py      # DiagnosticBackend ABC + standard dataclasses + BackendRegistry
  sse.py            # shared SSE helpers for diagnostics/live-data style streams

backends/
  gds2/
    backend.py      # GDS2DiagnosticBackend facade over existing src/ workflow/controller

src/
  ...               # GDS2-specific automation/runtime implementation
```

Documentation should use `diagnostic_platform/` as the canonical name for the platform layer.

## HOW

### Environment setup

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

### Cloud runtime (required)

Start each service in a separate terminal on the cloud VM:

```bash
# Terminal A: VCI Proxy server
python -m vci_proxy.reverse_server

# Terminal B: Flask API entry point
python app.py --port 8080

# Terminal C: Start GDS2 with Java Agent (manual/project-specific)
```

Expected listening ports:

- `9000`: reverse_server VCI listener
- `9001`: reverse_server local proxy listener (virtual DLL side)
- `8080`: Flask API (`/api/diagnose/*`, `/api/navigate/*`, `/api/session/*`)

### Local client runtime

Development mode:

```bash
python -m vci_proxy.client_gui
```

Packaged mode:

```bash
pyinstaller --clean --noconfirm pyinstaller_client.spec
```

Run:

```bash
dist\VCI_Proxy_Client\VCI_Proxy_Client.exe
```

### Config persistence behavior

Client config is stored at:

`%APPDATA%\VCI_Proxy\config.json`

If host is already saved, next launch auto-connects. Delete the file to reset first-run behavior.

## API CONTRACTS

Only these API surfaces remain supported:

- `/api/diagnose/*`
- `/api/navigate/*`
- `/api/session/*`

### Diagnostics API (`/api/diagnose/*`)

- `POST /api/diagnose/start` — one-button start, auto-connect `VCI Proxy (Remote)`, return modules + state context
- `POST /api/diagnose/select_module` — select module and return data categories
- `GET /api/diagnose/dtcs` — read DTCs from current Data Display context
- `POST /api/diagnose/live_data/start` — start live collection for selected category
- `GET /api/diagnose/live_data/events` — diagnostics SSE stream
- `POST /api/diagnose/live_data/stop` — stop stream and navigate back
- `POST /api/diagnose/ai_diagnose` — start 30s collection + AI analysis, return session_id
- `GET /api/diagnose/ai_diagnose/events?session_id=...` — SSE progress + streamed verdict
- `POST /api/diagnose/ai_diagnose/retry` — retry AI analysis with cached payload

### Navigate API (`/api/navigate/*`)

- `POST /api/navigate/start` — start LangGraph navigation session
- `GET /api/navigate/events?session_id=...` — SSE stream (`progress`, `decision_required`, `done`, `error`)
- `POST /api/navigate/decision` — submit a paused HITL choice
- `GET /api/navigate/status?session_id=...` — query navigation session status
- `POST /api/navigate/abort` — abort navigation session

### Session API (`/api/session/*`)

- `POST /api/session/start` — start a diagnostic session and select workflow/backend context
- `POST /api/session/start_diagnostics` — start GDS2 diagnostics for a running session
- `POST /api/session/execute` — execute one guarded backend action
- `GET /api/session/events?session_id=...` — SSE stream for session lifecycle and decisions
- `POST /api/session/decision` — resolve a pending session decision gate
- `POST /api/session/select_module` — session-aware module selection
- `POST /api/session/select_data_category` — session-aware category selection
- `POST /api/session/abort` — abort session safely
- `GET /api/session/status?session_id=...` — query current session state

## REQUIRED UX FLOW (MECHANIC)

1. Start Diagnostics
2. Select Module and click **Select**
3. Select Data Category
4. Click **AI Diagnose** — collects 30s data and streams AI output
5. Optionally use **Read DTCs** or **Start Stream** as advanced/fallback actions

This keeps GDS2 on the Data Display page where DTC and live-data operations are valid.

## KEY CONSTRAINTS

- GDS2 is JavaFX, but Device Explorer is Win32 and follows a separate automation path
- Java Agent JSON path: `~/gds2-data/latest.json`
- GDS2 commonly uses GBK encoding
- One GDS2 instance per machine
- ZhipuAI API key lives in `%APPDATA%/VCI_Proxy/config.json`, never in source
- AI diagnosis requires a 30s data collection window before the LLM call
- Knowledge base embeddings use `all-MiniLM-L6-v2`
- Agentic navigation uses LangGraph + native tool-calling
- GDS2 is the first backend; future OEM tools should plug in through `diagnostic_platform/` + `backends/`

## REFERENCE DOCS

| Document | Purpose |
|---|---|
| `README.md` | Project overview and operator setup |
| `agent_docs/architecture.md` | End-to-end architecture and platform layering |
| `agent_docs/rpa_automation.md` | GDS2 automation/runtime/API details |
| `agent_docs/roadmap.md` | Delivery status and migration progress |
| `agent_docs/gds2_agentic_navigation_implementation.md` | LangGraph + LanceDB implementation details |
| `agent_docs/vci_proxy.md` | Tunnel, protocol, and hardware bridge details |

## CODE HYGIENE RULES

### Dead code removal

When adding new code, refactoring, or building new frameworks, always check whether existing code or frameworks can be deleted or simplified. Do not leave obsolete modules, unused imports, stale config files, or abandoned features sitting in the repo. Every change is an opportunity to clean up.

### Test lifecycle

Tests fall into two categories:

- **Regression tests** — generic, long-lived. Any future change must still pass these. Keep them in `tests/` permanently.
- **One-off tests** — written to validate a specific change or debug a specific issue. Delete them after the test passes and the change is merged. Do not accumulate throwaway test scripts in `scripts/` or `tests/`.
