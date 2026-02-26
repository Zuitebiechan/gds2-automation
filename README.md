# GDS2 RPA — Cloud Remote Vehicle Diagnostics

Remote vehicle diagnostics for GM GDS2 using:

- **Cloud GDS2 + Java Agent** for data extraction
- **VCI Proxy tunnel** for cloud↔local hardware bridge
- **Local tray client exe** for mechanic-facing operations

---

## Architecture at a Glance

1. Cloud runs GDS2 + RPA automation + Flask APIs
2. Local machine runs VCI proxy client and tray diagnostics UI
3. Tunnel bridges cloud J2534 calls to local VCI hardware

Key cloud ports:

- `9000` — reverse VCI listener
- `9001` — local proxy listener (virtual DLL side)
- `8080` — Flask API (`/api/diagnose/*`)

---

## Current Product Flow (Phase 3 + 3.5)

In local exe Diagnostics window:

1. **Start Diagnostics**
2. Select **Module** and click **Select**
3. Select **Data Category**
4. Click **AI Diagnose** (primary) — collects 30s data, sends to AI, streams structured verdict
5. Optionally: **Read DTCs** or **Start Stream** (advanced/fallback)

This sequence ensures GDS2 is on Data Display page where DTC/live data are valid.

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

# Terminal B: Flask API
python app.py --port 8080

# Terminal C: Start GDS2 with Java Agent (manual/project-specific)
# Keep Agent writing to ~/gds2-data/latest.json
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

## Config Persistence (Expected)

Client config file:

`%APPDATA%\VCI_Proxy\config.json`

If host/port are already saved, next launch auto-connects and will not show first-run input again.

Reset first-run behavior:

```powershell
Remove-Item "$env:APPDATA\VCI_Proxy\config.json" -Force
```

---

## API Endpoints

### Diagnostics API

| Endpoint | Method | Description |
|---|---|---|
| `/api/diagnose/start` | POST | Auto-start and connect to `VCI Proxy (Remote)`, returns modules + VIN/device context |
| `/api/diagnose/select_module` | POST | Select module and return data categories |
| `/api/diagnose/dtcs` | GET | Read DTCs from current Data Display context |
| `/api/diagnose/live_data/start` | POST | Start live stream collector |
| `/api/diagnose/live_data/events` | GET | SSE stream |
| `/api/diagnose/live_data/stop` | POST | Stop stream and navigate back |
| `/api/diagnose/ai_diagnose` | POST | Start 30s data collection + AI analysis, return session_id |
| `/api/diagnose/ai_diagnose/events` | GET | SSE progress + streamed LLM verdict |
| `/api/diagnose/ai_diagnose/retry` | POST | Retry LLM call with cached payload |

### Existing debug/service APIs

- `/api/viewer/*`
- `/api/stream/*`
- `/api/agent/*`

---

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

Use clean rebuild in the active venv and ensure ABI-matching Pillow wheel is installed.

### 4) Diagnostics tray click no response

Use latest client build; errors are now surfaced with explicit dialogs instead of silent failure.

---

## Documentation Index

- `CLAUDE.md` — concise operational guide
- `agent_docs/architecture.md` — end-to-end architecture
- `agent_docs/vci_proxy.md` — tunnel/protocol/cache details
- `agent_docs/rpa_automation.md` — workflow/page/API details
- `agent_docs/roadmap.md` — delivery status and next phases
