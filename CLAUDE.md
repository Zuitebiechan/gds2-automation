# RPA_demo Project Memory

**Last Updated:** 2026-02-11
**Branch:** feature/vci-proxy
**Status:** Cloud Remote Diagnostics Platform - VCI Proxy validated, entering productization phase

---

## Table of Contents
1. [Project Vision and Purpose](#project-vision-and-purpose)
2. [Architecture Overview](#architecture-overview)
3. [Project Structure](#project-structure)
4. [Layer 1: VCI Proxy Tunnel (Core Infrastructure)](#layer-1-vci-proxy-tunnel)
5. [Layer 2: RPA Automation (Per-Software Plugin)](#layer-2-rpa-automation)
6. [Layer 3: Debug Web UI (Temporary)](#layer-3-debug-web-ui)
7. [GDS2 Integration Details](#gds2-integration-details)
8. [Key Design Decisions](#key-design-decisions)
9. [Data Flow](#data-flow)
10. [Development Workflow](#development-workflow)
11. [Current Scope and Roadmap](#current-scope-and-roadmap)
12. [Reference Documents](#reference-documents)

---

## Project Vision and Purpose

### Ultimate Goal

Turn professional vehicle diagnostics into a **one-button service**. The end user (car owner or technician) should never see or interact with OEM diagnostic software. They plug in a VCI device, open a simple app, and press a button to get results.

```
User's experience:          What happens behind the scenes:

  "Read DTCs"               App -> Cloud API -> RPA automates GDS2
     [button]               -> VCI tunnel -> local hardware -> car
                             -> DTCs extracted -> returned to app
       |
       v
  P0300 - Random misfire    User has no idea GDS2 exists.
  P0171 - System lean       They just see the results.
  P0420 - Catalyst low
```

### Why This Matters

Traditionally, a technician needs:
- Expensive OEM diagnostic software ($$$)
- Training to operate complex software
- Software installed on a specific laptop

This platform eliminates all three: OEM software runs in the cloud, RPA automates it, and the user gets a simple interface. The VCI Proxy tunnel bridges the cloud software to the user's local hardware over the internet.

### Target Users
- **Car owners** who want to perform their own diagnostics with minimal knowledge
- **Independent mechanics** who need OEM-level diagnostics without dealership software
- Anyone with a compatible VCI device and internet connection

### Platform Strategy
- **GDS2 (General Motors)** is the first supported OEM diagnostic tool
- Future: BMW ISTA, Toyota Techstream, Ford IDS, VW ODIS, etc.
- Each OEM tool gets its own RPA automation module; all share the VCI Proxy infrastructure
- The user-facing interface remains the same regardless of which OEM tool runs behind the scenes

### Current Phase
- VCI Proxy tunnel validated end-to-end (including multi-port verification)
- GDS2 cloud deployment working with RPA automation
- Entering **productization phase**: packaging client, simplifying user flow
- VCI support: Scanmatik SM2/SM3 (USB), with MDI/MDI2 planned

---

## Architecture Overview

### High-Level Architecture

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

### Three Layers

| Layer | Role | Lifespan |
|-------|------|----------|
| **VCI Proxy Tunnel** | Core infrastructure: bridge cloud software to local hardware | Permanent, shared by all OEM tools |
| **RPA Automation** | Per-OEM-tool UI automation module (currently: GDS2) | Permanent, one per OEM tool |
| **Debug Web UI** | Flask app for debugging/validating UI automation | Temporary, removed once automation is stable |

### Target Product Architecture

```
User-Facing Layer (future):
  Mobile App / Mini-Program / Simple Web Page
  - "Read DTCs" button
  - "Live Data" button
  - Connection code input
       |
       v
Session Manager (future):
  - Creates/destroys cloud VMs per user
  - Generates connection codes
  - Manages user authentication
       |
       v
Diagnostic API Layer (future):
  - "Read All DTCs" -> unified response format
  - "Start Live Data" -> SSE stream
  - Translates simple commands into OEM-specific RPA sequences
       |
       v
RPA Layer (per OEM tool):            VCI Proxy Tunnel:
  GDS2 adapter (working)              reverse_server (working)
  ISTA adapter (future)                reverse_client (working)
  ODIS adapter (future)                virtual_j2534.dll (working)
       |                                    |
       v                                    v
  OEM Software in Cloud VM             User's Local VCI Hardware
```

---

## Project Structure

```
RPA_demo/
|
|-- vci_proxy/                      # LAYER 1: VCI Proxy Tunnel (core infra)
|   |-- __init__.py                 # v0.3.0, exports all public classes
|   |-- protocol.py                 # Binary protocol: 14-byte header, J2534 msg types
|   |-- reverse_server.py           # Cloud-side server (port 9000 + 9001)
|   |-- reverse_client.py           # Local-side client (connects to cloud)
|   |-- j2534_driver.py             # ctypes wrapper for real J2534 DLL
|   |-- config.py                   # Frozen dataclass configuration
|   |-- auth.py                     # HMAC-SHA256 PSK authentication
|   |-- cache_read_msgs.py          # P1-1: ReadMsgs BUFFER_EMPTY cache
|   |-- cache_filter_dedup.py       # P2-1: StartFilter deduplication
|   |-- cache_vbatt.py              # P2-2: READ_VBATT response cache
|   |-- test_stability.py           # Stability/integration tests
|   |-- virtual_dll/                # C DLL that GDS2 loads instead of real HW
|   |   |-- virtual_j2534.c         # Virtual J2534 DLL source
|   |   |-- virtual_j2534.def       # DLL export definitions
|   |   |-- j2534.h                 # J2534 API header
|   |   |-- build_msvc.bat          # MSVC build script
|   |   |-- register_vci_proxy.reg  # Windows registry entries
|   |   +-- README.md               # Build instructions
|   +-- scripts/
|       |-- start_proxy.bat
|       |-- stop_proxy.bat
|       +-- install_service.bat
|
|-- src/                            # LAYER 2: RPA Automation (GDS2-specific)
|   |-- core/
|   |   |-- driver.py               # GDS2Driver (pywinauto wrapper)
|   |   |-- locators.py             # Centralized UI element definitions
|   |   +-- template_matcher.py     # Multi-scale image template matching
|   |-- navigation/
|   |   +-- controller.py           # NavigationController + GDS2Page enum
|   |-- streaming/
|   |   |-- agent_navigator.py      # AgentNavigator: JSON command/response
|   |   +-- agent_data_collector.py # AgentDataCollector: 100ms poll + SSE
|   |-- native/
|   |   +-- device_explorer.py      # DeviceExplorerController (Win32)
|   |-- discovery/
|   |   +-- vehicle_mapping.py      # VehicleMapping + VehicleDiscovery
|   |-- utils/
|   |   +-- report_parser.py        # GDS2 HTML report parsing
|   +-- workflows/
|       |-- data_viewer.py          # PRIMARY: DataViewerWorkflow
|       |-- interactive_workflow.py  # Step-by-step navigation
|       +-- read_data_display_agent.py  # CLI workflow wrapper
|
|-- app.py                          # LAYER 3: Flask Debug Web UI backend
|-- templates/index.html            # Debug Web UI frontend
|-- main.py                         # CLI: web, demo, inspect, discover
|-- scripts/                        # Utility and deployment scripts
|   |-- cloud_setup_gds2_agent.bat
|   |-- cloud_start_webui.bat
|   +-- create_shortcut.ps1
|-- mappings/                       # Auto-generated discovery data
|-- requirements.txt                # Full dev deps
|-- requirements-minimal.txt        # Minimal (pywinauto + Pillow)
|-- requirements-cloud.txt          # Cloud only (flask + flask-cors)
+-- CLAUDE.md                       # This file
```

---

## Layer 1: VCI Proxy Tunnel

The core infrastructure that makes remote diagnostics possible. This layer is **OEM-tool-agnostic** and shared by all future diagnostic software.

### How It Works

1. **Cloud side**: GDS2 loads `virtual_j2534.dll` instead of a real device driver
2. The virtual DLL converts J2534 API calls into TCP messages (custom binary protocol)
3. Messages travel through `reverse_server.py` to `reverse_client.py` over the internet
4. **Local side**: `reverse_client.py` calls the real J2534 DLL via ctypes, which talks to the physical VCI hardware

### Binary Protocol

```
+----------+----------+----------+----------+
|  Magic   |  Length  |  MsgType | Sequence |
|  4 bytes |  4 bytes |  2 bytes |  4 bytes |
+----------+----------+----------+----------+
|              Message Body                  |
|            (Variable Length)               |
+--------------------------------------------+
```

- **Magic**: `0x4A325334` ("J2S4"), **Header**: 14 bytes
- **Message types**: Full J2534 API (Open, Close, Connect, Disconnect, ReadMsgs, WriteMsgs, StartFilter, StopFilter, Ioctl, ReadVersion)
- **Request/Response**: Requests `0x00xx`, Responses `0x80xx`
- **Special**: `0x00FE` Auth, `0x00FF` Heartbeat

### Authentication (P1-2)

- HMAC-SHA256 over PSK. Client sends `AUTH_REQ(timestamp, HMAC(key, timestamp))`
- Replay protection: rejects timestamps with >5 minute drift
- Backward compatible: legacy clients register via heartbeat when auth disabled

### Caching (Latency Optimization)

| Cache | File | Purpose | TTL |
|-------|------|---------|-----|
| **ReadMsgs** (P1-1) | `cache_read_msgs.py` | Short-circuit BUFFER_EMPTY | 50ms per-channel |
| **Filter Dedup** (P2-1) | `cache_filter_dedup.py` | Deduplicate StartFilter | SHA-256 key |
| **VBATT** (P2-2) | `cache_vbatt.py` | Cache battery voltage | 5s global |

All caches invalidate on Disconnect/Close. VBATT runs on **both** server and client.

### Key Components

| Component | File | Runs On | Description |
|-----------|------|---------|-------------|
| `ReverseProxyServer` | `reverse_server.py` | Cloud | Dual-port asyncio: `:9000` VCI client, `:9001` virtual DLL |
| `ReverseProxyClient` | `reverse_client.py` | Local | Auto-reconnect with exponential backoff |
| `J2534Driver` | `j2534_driver.py` | Local | ctypes wrapper for Scanmatik DLL |
| `virtual_j2534.dll` | `virtual_dll/` | Cloud | C DLL GDS2 loads as real hardware |
| `ProxyConfig` | `config.py` | Both | Frozen dataclass, `from_args()` for CLI |

### Supported VCI Hardware

| Device | Status |
|--------|--------|
| Scanmatik SM2 USB | Working |
| Scanmatik SM3 | Working |
| GM MDI / MDI2 | Planned |

---

## Layer 2: RPA Automation

Per-OEM-tool UI automation. Currently: **GDS2 (General Motors)**. Future OEM tools get their own modules.

### GDS2 Automation Stack

| Component | File | Purpose |
|-----------|------|---------|
| **DataViewerWorkflow** | `src/workflows/data_viewer.py` | Primary: Device -> Module -> Data |
| **NavigationController** | `src/navigation/controller.py` | Page detection + transitions |
| **AgentNavigator** | `src/streaming/agent_navigator.py` | JSON IPC with Java Agent (`~/gds2-data/`) |
| **AgentDataCollector** | `src/streaming/agent_data_collector.py` | 100ms polling, change detection, SSE |
| **DeviceExplorerController** | `src/native/device_explorer.py` | Win32 API for Device Explorer |
| **VehicleMapping** | `src/discovery/vehicle_mapping.py` | JSON-persisted module/data cache |

### GDS2 Page Flow

```
MAIN_MENU -> DEVICE_EXPLORER -> VEHICLE_SELECTION -> DIAGNOSTICS_MENU
  -> MODULE_LIST -> MODULE_SUBMENU -> DATA_LIST -> [SUB_DATA_LIST] -> DATA_DISPLAY
```

### Java Agent Communication

Injected into GDS2's JVM. IPC via JSON files:
- `~/gds2-data/command.json` (Python writes)
- `~/gds2-data/result.json` (Agent writes)
- `~/gds2-data/latest.json` (Agent continuously updates)

### Device Explorer

Win32 dialog (not JavaFX). Cross-process memory reading, keyboard simulation.

---

## Layer 3: Debug Web UI

**Temporary.** For debugging UI automation only. Not the end-user product. Removed once automation is stable.

### API Endpoints

| Group | Endpoint | Method | Purpose |
|-------|----------|--------|---------|
| Viewer | `/api/viewer/start` | POST | Init, get devices/modules |
| | `/api/viewer/connect` | POST | Connect device |
| | `/api/viewer/select_module` | POST | Select module |
| | `/api/viewer/select_data` | POST | Select data, start monitor |
| | `/api/viewer/stop` | POST | Stop monitoring |
| Stream | `/api/stream/events` | GET | SSE real-time data |
| Agent | `/api/agent/status` | GET | Agent availability |
| | `/api/agent/dtcs` | GET | Get DTCs |
| | `/api/agent/snapshot` | GET | Latest snapshot |

---

## GDS2 Integration Details

- **Type**: JavaFX desktop app + Win32 dialogs (Device Explorer)
- **Platform**: Windows only
- **Agent**: Custom Java Agent injected at JVM startup
- **Key**: Device Explorer is Win32; page detection via button/list inspection; Home button disabled on some pages (use Vehicle Menu shortcut); GDS2 uses GBK encoding

---

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Reverse connection (local -> cloud)** | Avoids NAT/firewall at user location |
| **Virtual DLL** | GDS2 thinks hardware is local. Zero mods to GDS2 |
| **Java Agent (not PyAutoGUI)** | Direct JVM: 100ms collection, no screen dependency |
| **Win32 API for Device Explorer** | Not JavaFX. Agent can't control it |
| **Server-side caching** | ~70% round-trip reduction |
| **Custom binary protocol** | Minimal overhead for high-freq J2534 |
| **Per-OEM RPA modules** | Each tool has unique UI |
| **Debug Web UI is temporary** | Not the product interface |

---

## Data Flow

```
User action -> DataViewerWorkflow -> AgentNavigator -> Java Agent -> GDS2
  -> virtual_j2534.dll -> reverse_server (cache check) -> Internet
  -> reverse_client (VBATT cache) -> real J2534 DLL -> USB -> VCI -> Vehicle ECU
  -> response returns same path
  -> AgentDataCollector (100ms poll) -> SSE -> Web UI
```

---

## Development Workflow

```bash
python main.py web                  # Debug Web UI (:8080)
python main.py demo                 # CLI demo workflow
python main.py inspect              # Inspect GDS2 UI
python -m vci_proxy.reverse_server  # Cloud: proxy server
python -m vci_proxy.reverse_client --host <ip>  # Local: connect
```

### Cloud Setup

```bash
scripts\cloud_setup_gds2_agent.bat
regedit /s vci_proxy\virtual_dll\register_vci_proxy.reg
python -m vci_proxy.reverse_server
```

---

## Current Scope and Roadmap

### Completed (2026-02-11)

- [x] GDS2 cloud deployment + Java Agent
- [x] VCI Proxy tunnel (reverse connection, binary protocol, PSK auth)
- [x] 3-layer caching (server) + VBATT cache (client)
- [x] Full GDS2 UI automation + 100ms data collection
- [x] DTC extraction, Debug Web UI + SSE
- [x] SM2/SM3 VCI support
- [x] Code quality: protocol decoder validation, dispatch pattern, constant cleanup, DLL buffer overflow fixes
- [x] Multi-port validation: Virtual DLL reads `VCI_PROXY_PORT` env var, two independent tunnels verified

### Multi-Session Validation Results (2026-02-11)

Tested running two independent server/client tunnels (ports 9000/9001 and 9100/9002):
- **DLL env var mechanism**: Working. Both ports connect successfully.
- **Independent tunnels**: Working. Two server/client pairs operate without interference.
- **GDS2 dual-instance**: Not possible. GDS2 has a single-instance lock per machine.
- **Conclusion**: Multi-user requires one VM/container per user. Architecture is sound; deployment uses VM isolation rather than port isolation on a single machine.

### Phase 1: Client Packaging (Next)

Package `reverse_client` as a standalone Windows executable so users don't need Python.

- [ ] **PyInstaller exe**: Bundle reverse_client + all deps into single `VCI_Proxy_Client.exe`
- [ ] **Simple GUI**: System tray icon showing connection status (connected/disconnected/reconnecting)
- [ ] **Connection input**: Prompt for server address + auth token (or connection code) on first run
- [ ] **Auto-reconnect indicator**: Visual feedback during reconnection with backoff
- [ ] **Installer (optional)**: .msi or Inno Setup installer with desktop shortcut

### Phase 2: Simplified Connection Flow

Replace manual parameter entry with a connection code system.

- [ ] **Connection code API**: Backend generates short codes (e.g., `A3X7K9`) mapping to {host, port, token}
- [ ] **Client-side resolution**: Client enters code → fetches connection params from API → connects automatically
- [ ] **Code lifecycle**: Codes expire after use or timeout; tied to a specific session/user

### Phase 3: One-Button Diagnostics API

Expose high-level diagnostic operations as simple API calls. This is the core product value.

- [ ] **"Read All DTCs" API**: Single call that navigates GDS2 through all modules, collects all DTCs, returns structured result
- [ ] **"Live Data Stream" API**: Single call that navigates to a data category and starts SSE streaming
- [ ] **Simplified mobile UI**: Minimal phone-friendly page with two buttons (Read DTCs / Live Data)
- [ ] **Progress feedback**: Real-time status updates during long operations ("Connecting to ECU...", "Scanning module 3/12...")

### Phase 4: Session Manager

Automated VM/container lifecycle management for multi-user support.

- [ ] **Session Manager service**: Create/destroy cloud VMs on demand via Alibaba Cloud API
- [ ] **VM image**: Pre-built image with GDS2 + Java Agent + reverse_server + all configs
- [ ] **User authentication**: Login system with per-user session allocation
- [ ] **Resource monitoring**: Track active sessions, idle timeout, auto-cleanup
- [ ] **Connection code integration**: Session creation generates connection code for client

### Future

- [ ] MDI/MDI2 VCI support
- [ ] More OEM tools (BMW ISTA, Toyota Techstream, Ford IDS, VW ODIS)
- [ ] Per-OEM RPA adapters with unified diagnostic API
- [ ] WeChat mini-program or mobile app as primary user interface
- [ ] Physical device / dedicated hardware client (long-term)

---

## Reference Documents

| Document | Location |
|----------|----------|
| GDS2 User Guide | `res/GM-GDS2-User-Guide.pdf` |
| Control Mapping | `docs/GDS2_CONTROL_MAPPING.md` |
| Workflow Diagram | `docs/WORKFLOW_DIAGRAM.md` |
| Virtual DLL README | `vci_proxy/virtual_dll/README.md` |

---

**Update this document when architecture or implementation changes.**
