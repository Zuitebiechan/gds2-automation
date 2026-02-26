# Architecture Overview

## High-Level Architecture

```
                        Internet
                           |
    +----------------------+-----------------------+
    |                 Cloud Server                  |
    |               (Alibaba Cloud)                 |
    |                                               |
    |  +-------------------+  +------------------+  |
    |  | OEM Diagnostic SW |  | RPA Automation   |  |
    |  | (GDS2 + Agent)    |  | (Python)         |  |
    |  +--------+----------+  +--------+---------+  |
    |           |  J2534 API           |             |
    |  +--------v----------+  +--------v---------+  |
    |  | virtual_j2534.dll |  | Debug Web UI     |  |
    |  | (Fake J2534 DLL)  |  | (Flask, temp)    |  |
    |  +--------+----------+  +------------------+  |
    |           |                                    |
    |  +--------v----------+                         |
    |  | reverse_server.py |                         |
    |  | :9001 (DLL conn)  |                         |
    |  | :9000 (VCI conn)  |                         |
    |  | + 3-layer cache   |                         |
    |  +--------+----------+                         |
    +-----------|-----------+-----------------------+
                | TCP tunnel (reverse connection)
    +-----------|-----------+-----------------------+
    |           |           User's Local Machine    |
    |  +--------v----------+                         |
    |  | reverse_client.py |                         |
    |  | + PSK auth        |                         |
    |  | + VBATT cache     |                         |
    |  +--------+----------+                         |
    |           | ctypes FFI                          |
    |  +--------v----------+                         |
    |  | Real J2534 DLL    |                         |
    |  | (Scanmatik SM2/3) |                         |
    |  +--------+----------+                         |
    |           | USB                                |
    |  +--------v----------+                         |
    |  | VCI Device        |                         |
    |  |      | OBD-II     |                         |
    |  |   Vehicle ECU     |                         |
    |  +-------------------+                         |
    +-----------------------------------------------+
```

## Three Layers

| Layer | Role | Lifespan |
|-------|------|----------|
| **VCI Proxy Tunnel** (`vci_proxy/`) | Core infrastructure: bridge cloud software to local hardware | Permanent, shared by all OEM tools |
| **RPA Automation** (`src/`) | Per-OEM-tool UI automation module (currently: GDS2) | Permanent, one per OEM tool |
| **Client UX + Service Layer** (`vci_proxy/client_gui.py`, `vci_proxy/diagnostics_window.py`, `app.py`) | Mechanic-facing local exe UX + cloud diagnostics/debug APIs | Current delivery path |

## Project Structure

```
RPA_demo/
├── vci_proxy/                      # LAYER 1: VCI Proxy Tunnel (core infra)
│   ├── protocol.py                 # Binary protocol: 14-byte header, J2534 msg types
│   ├── reverse_server.py           # Cloud-side server (port 9000 + 9001)
│   ├── reverse_client.py           # Local-side client (connects to cloud)
│   ├── j2534_driver.py             # ctypes wrapper for real J2534 DLL
│   ├── config.py                   # Frozen dataclass configuration
│   ├── auth.py                     # HMAC-SHA256 PSK authentication
│   ├── cache_read_msgs.py          # ReadMsgs BUFFER_EMPTY cache
│   ├── cache_filter_dedup.py       # StartFilter deduplication
│   ├── cache_vbatt.py              # READ_VBATT response cache
│   └── virtual_dll/                # C DLL that GDS2 loads instead of real HW
│       ├── virtual_j2534.c         # Virtual J2534 DLL source
│       └── register_vci_proxy.reg  # Windows registry entries
│
├── src/                            # LAYER 2: RPA Automation (GDS2-specific)
│   ├── core/                       # Reserved core package (currently minimal)
│   ├── navigation/
│   │   └── controller.py           # NavigationController + GDS2Page enum
│   ├── streaming/
│   │   ├── agent_navigator.py      # AgentNavigator: JSON command/response
│   │   ├── agent_data_collector.py # AgentDataCollector: 100ms poll + SSE
│   │   └── diagnostic_buffer.py   # DiagnosticBuffer: 30s window + delta compression
│   ├── diagnosis/                  # Phase 3.5: AI-powered diagnosis
│   │   ├── llm_client.py          # ZhipuAI streaming wrapper + prompt assembly
│   │   └── ai_engine.py           # Orchestration: collector → buffer → LLM → SSE
│   ├── native/
│   │   └── device_explorer.py      # DeviceExplorerController (Win32)
│   ├── discovery/
│   │   └── vehicle_mapping.py      # VehicleMapping + VehicleDiscovery
│   ├── recovery/                   # AI-powered exception recovery
│   └── workflows/
│       ├── data_viewer.py          # PRIMARY: DataViewerWorkflow
│       ├── interactive_workflow.py # Step-by-step navigation
│       └── read_data_display_agent.py  # CLI workflow wrapper
├── app.py                          # Flask service/debug backend (includes /api/diagnose)
├── diagnostics_api.py              # Phase 3 diagnostics API blueprint
├── templates/index.html            # Debug Web UI frontend
├── vci_proxy/client_gui.py         # Local tray client (mechanic-facing)
├── vci_proxy/diagnostics_window.py # Local diagnostics UX (module/category/DTC/live)
└── tests/                          # pytest test suite
```

## Data Flow

```
User action -> DataViewerWorkflow -> AgentNavigator -> Java Agent -> GDS2
  -> virtual_j2534.dll -> reverse_server (cache check) -> Internet
  -> reverse_client (VBATT cache) -> real J2534 DLL -> USB -> VCI -> Vehicle ECU
  -> response returns same path
  -> AgentDataCollector (100ms poll) -> SSE -> Local Diagnostics Window (primary)
                                                  and Debug Web UI (service/debug)

AI Diagnosis flow (Phase 3.5):
  User clicks "AI Diagnose"
  -> AgentDataCollector -> DiagnosticBuffer (30s accumulation)
  -> delta-compressed payload (initial state + timestamped changes)
  -> ZhipuAI glm-4.7 (streaming, thinking disabled) -> SSE -> Local Diagnostics Window
```

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Reverse connection (local → cloud) | Avoids NAT/firewall at user location |
| Virtual DLL | GDS2 thinks hardware is local. Zero mods to GDS2 |
| Java Agent (not PyAutoGUI) | Direct JVM: 100ms collection, no screen dependency |
| Win32 API for Device Explorer | Not JavaFX — Agent can't control it |
| Server-side caching | ~70% round-trip reduction |
| Custom binary protocol | Minimal overhead for high-freq J2534 |
| Per-OEM RPA modules | Each tool has unique UI |
| Debug Web UI is temporary | Not the product interface |

## Target Product Architecture

```
User-Facing Layer (future):
  Mobile App / Mini-Program / Simple Web Page
       |
Session Manager (future):
  - Creates/destroys cloud VMs per user
  - Generates connection codes
       |
Diagnostic API Layer (future):
  - "Read All DTCs" -> unified response format
  - "Start Live Data" -> SSE stream
       |
RPA Layer (per OEM tool):            VCI Proxy Tunnel:
  GDS2 adapter (working)              reverse_server (working)
  ISTA adapter (future)                reverse_client (working)
  ODIS adapter (future)                virtual_j2534.dll (working)
       |                                    |
  OEM Software in Cloud VM             User's Local VCI Hardware
```
