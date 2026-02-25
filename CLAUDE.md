# GDS2 RPA — Cloud Remote Vehicle Diagnostics

## WHY

One-button remote vehicle diagnostics. Users connect a local VCI device, click simple actions, and receive results without operating OEM software directly. GDS2 runs in the cloud; RPA automation + VCI Proxy bridge cloud software to local hardware.

## WHAT

This project has three product layers:

| Layer | Location | Purpose |
|---|---|---|
| **VCI Proxy Tunnel** | `vci_proxy/` | Connect cloud GDS2 to local VCI via reverse TCP + virtual J2534 DLL |
| **RPA Automation** | `src/` | State-aware GDS2 automation (NavigationController + DataViewerWorkflow + Java Agent) |
| **Client UX** | `vci_proxy/client_gui.py`, `vci_proxy/diagnostics_window.py` | Local system-tray exe for mechanics (connectivity + diagnostics UI) |

`app.py` and `templates/` remain a **debug/service layer**, not the final mechanic-facing product.

## CURRENT STATUS (2026-02-25)

- Phase 1 client packaging is done (tray app + config persistence + reconnect status)
- Phase 3 diagnostics API is implemented under `/api/diagnose/*`
- Phase 3.5 AI-Powered Diagnosis is **in progress**:
  - ZhipuAI glm-4.7-flash integration (free 200K context model)
  - 30s sliding window buffer for live sensor data
  - Delta-compressed timeline payload for LLM
  - SSE-streamed progress + LLM result to client
- Local diagnostics window is integrated into exe:
  - Start Diagnostics
  - Select Module
  - Select Data Category
  - AI Diagnose (primary), Read DTCs / Start Stream (secondary)
- Packaging/runtime stability fixes are in place (Pillow/PyInstaller/Diagnostics launch)

## HOW

### Environment setup

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

### Cloud runtime (required)

Start each service in a separate terminal on cloud VM:

```bash
# Terminal A: VCI Proxy server
python -m vci_proxy.reverse_server

# Terminal B: Flask API / debug UI backend
python app.py --port 8080

# Terminal C: Start GDS2 with Java Agent (manual/project-specific)
```

Expected listening ports:
- `9000`: reverse_server VCI listener
- `9001`: reverse_server local proxy listener (for virtual DLL)
- `8080`: Flask API (`/api/diagnose/*`)

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

### Config persistence behavior (expected)

Client config is saved to:

`%APPDATA%\VCI_Proxy\config.json`

If host is already saved, next launch auto-connects (no first-run input dialog). To reset first-run behavior, delete this file.

## PHASE 3 API CONTRACT

All endpoints are under `/api/diagnose`:

- `POST /start` — one-button start (auto-connect `VCI Proxy (Remote)`, return modules)
- `POST /select_module` — select module and return data categories
- `GET /dtcs` — read DTCs from current Data Display context
- `POST /live_data/start` — start AgentDataCollector streaming on selected data category
- `GET /live_data/events` — SSE stream
- `POST /live_data/stop` — stop stream and navigate back
- `POST /ai_diagnose` — start 30s collection + AI analysis, return session_id
- `GET /ai_diagnose/events?session_id=...` — SSE progress + streamed LLM verdict
- `POST /ai_diagnose/retry` — retry LLM with cached payload

## REQUIRED UX FLOW (MECHANIC)

1. Start Diagnostics
2. Select Module and click **Select**
3. Select Data Category
4. Click **AI Diagnose** (primary action) — collects 30s data, sends to AI, streams result
5. Optionally: **Read DTCs** or **Start Stream** (advanced/fallback)

This sequence ensures GDS2 is on Data Display page where DTC and live data are valid.

## KEY CONSTRAINTS

- GDS2 is JavaFX, but Device Explorer is Win32 dialog (different automation path)
- Java Agent JSON path: `~/gds2-data/latest.json`
- GDS2 commonly uses GBK encoding
- One GDS2 instance per machine (single-instance lock)
- ZhipuAI API key stored in `%APPDATA%/VCI_Proxy/config.json` (never in source code)
- AI Diagnose requires 30s data collection window before LLM call

## REFERENCE DOCS

| Document | Purpose |
|---|---|
| `agent_docs/architecture.md` | End-to-end architecture and data flow |
| `agent_docs/vci_proxy.md` | VCI Proxy protocol/cache/auth details |
| `agent_docs/rpa_automation.md` | GDS2 navigation/workflow/API details |
| `agent_docs/roadmap.md` | Delivery status and upcoming phases |
