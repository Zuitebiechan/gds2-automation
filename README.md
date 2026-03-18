# Diagnostic Platform RPA — Cloud Remote Vehicle Diagnostics

Remote vehicle diagnostics platform with:

- **Cloud OEM diagnostic software + Java Agent / automation**
- **VCI Proxy tunnel** for cloud↔local hardware bridging
- **Shared backend contract layer** for multi-software expansion
- **Local tray client UX** for mechanics

GDS2 is the first fully implemented backend. The repository is now structured for additional OEM software backends through `diagnostic_platform/` + `backends/`.

---

## Architecture at a Glance

1. Cloud runs OEM diagnostic software, backend APIs, and RPA automation
2. Local machine runs the VCI proxy client and diagnostics tray UI
3. Tunnel bridges cloud J2534 calls to local VCI hardware
4. Shared contracts standardize backend behavior across OEM tools

Key cloud ports:

- `9000` — reverse VCI listener
- `9001` — local proxy listener (virtual DLL side)
- `8080` — Flask API (`/api/diagnose/*`, `/api/navigate/*`, `/api/session/*`)

---

## Platform Layers

| Layer | Location | Purpose |
|---|---|---|
| **VCI Proxy Tunnel** | `vci_proxy/` | Bridge cloud diagnostic software to local VCI hardware |
| **Platform Core** | `diagnostic_platform/` | `DiagnosticBackend`, `BackendRegistry`, standard schemas, SSE helpers |
| **Backend Facades** | `backends/`, `backends/gds2/` | Per-software adapters behind the shared backend contract |
| **RPA Automation** | `src/` | GDS2-specific navigation, streaming, recovery, AI diagnosis |
| **API + Client UX** | `app.py`, `diagnostics_api.py`, `navigate_api.py`, `session_api.py`, `vci_proxy/*.py` | Thin Flask entry point and mechanic-facing local UX |

---

## Current Product Flow

In the local Diagnostics window:

1. **Start Diagnostics**
2. Select **Module** and click **Select**
3. Select **Data Category**
4. Click **AI Diagnose** — collects 30s data, runs AI analysis, streams the verdict
5. Optionally use **Read DTCs** or **Start Stream**

This keeps GDS2 on the Data Display page where DTC and live-data operations are valid.

---

## Setup

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

For client-only build/runtime:

```bash
pip install -r requirements-client.txt
```

---

## Cloud Runtime

Start each service in a separate terminal:

```bash
# Terminal A: VCI Proxy server
python -m vci_proxy.reverse_server

# Terminal B: Flask API entry point
python app.py --port 8080

# Terminal C: Start GDS2 with Java Agent (manual/project-specific)
# Keep agent writing to ~/gds2-data/latest.json
```

Quick checks:

```bash
curl http://127.0.0.1:8080/test
netstat -ano | findstr :8080
netstat -ano | findstr :9000
netstat -ano | findstr :9001
```

---

## Local Runtime

### Development mode

```bash
python -m vci_proxy.client_gui
```

### Build exe

```bash
pyinstaller --clean --noconfirm pyinstaller_client.spec
```

Run:

```bash
dist\VCI_Proxy_Client\VCI_Proxy_Client.exe
```

> Do not run executables from `build/`; only run from `dist/...` output.

---

## Config Persistence

Client config file:

`%APPDATA%\VCI_Proxy\config.json`

If host/port are already saved, next launch auto-connects.

Reset first-run behavior:

```powershell
Remove-Item "$env:APPDATA\VCI_Proxy\config.json" -Force
```

---

## Supported API Surfaces

Only these APIs are supported:

### Diagnostics API

| Endpoint | Method | Description |
|---|---|---|
| `/api/diagnose/start` | POST | Auto-start and connect to `VCI Proxy (Remote)`, returns modules + context |
| `/api/diagnose/select_module` | POST | Select module and return data categories |
| `/api/diagnose/dtcs` | GET | Read DTCs from current Data Display context |
| `/api/diagnose/live_data/start` | POST | Start live stream collector |
| `/api/diagnose/live_data/events` | GET | SSE stream |
| `/api/diagnose/live_data/stop` | POST | Stop stream and navigate back |
| `/api/diagnose/ai_diagnose` | POST | Start 30s collection + AI analysis |
| `/api/diagnose/ai_diagnose/events` | GET | SSE progress + streamed verdict |
| `/api/diagnose/ai_diagnose/retry` | POST | Retry AI call with cached payload |

### Navigate API

| Endpoint | Method | Description |
|---|---|---|
| `/api/navigate/start` | POST | Start LangGraph navigation session |
| `/api/navigate/events` | GET | SSE stream: progress / decision_required / done / error |
| `/api/navigate/decision` | POST | Submit paused HITL choice |
| `/api/navigate/status` | GET | Query navigation session status |
| `/api/navigate/abort` | POST | Abort running navigation session |

### Session API

| Endpoint | Method | Description |
|---|---|---|
| `/api/session/start` | POST | Start session and select workflow/backend context |
| `/api/session/start_diagnostics` | POST | Start GDS2 diagnostics for a running session |
| `/api/session/execute` | POST | Execute one guarded backend action |
| `/api/session/events` | GET | SSE stream for session lifecycle and decisions |
| `/api/session/decision` | POST | Resolve a pending decision gate |
| `/api/session/select_module` | POST | Session-aware module selection |
| `/api/session/select_data_category` | POST | Session-aware category selection |
| `/api/session/abort` | POST | Abort the current session safely |
| `/api/session/status` | GET | Query current session state |

## Troubleshooting

### 1) `502 Bad Gateway` from diagnostics endpoints

Usually Flask is not running or not reachable. Verify `:8080` listener and `/test`.

### 2) PyInstaller WinError 5 (`PermissionError` under dist)

The old exe is still running and locking files.

```powershell
taskkill /IM VCI_Proxy_Client.exe /F
python -m PyInstaller --clean --noconfirm --distpath dist_fix --workpath build_fix pyinstaller_client.spec
```

### 3) Pillow `_imaging` import errors

Use a clean rebuild in the active venv and ensure the installed Pillow wheel matches the Python ABI.

### 4) Diagnostics tray click no response

Use the latest client build; errors are surfaced with explicit dialogs instead of silent failure.

---

## Documentation Index

- `CLAUDE.md` — concise operational guide
- `agent_docs/architecture.md` — end-to-end architecture and platform layering
- `agent_docs/vci_proxy.md` — tunnel/protocol/cache details
- `agent_docs/rpa_automation.md` — GDS2 automation/runtime/API details
- `agent_docs/roadmap.md` — delivery status and migration progress
- `agent_docs/gds2_agentic_navigation_implementation.md` — LangGraph + LanceDB implementation details
