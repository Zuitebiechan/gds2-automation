# RPA_demo Project Memory

**Last Updated:** 2026-02-03
**Status:** Production Ready - Simplified Data Viewer + Java Agent Integration

---

## Table of Contents
1. [Project Vision & Purpose](#project-vision--purpose)
2. [Architecture Overview](#architecture-overview)
3. [Project Structure](#project-structure)
4. [Design Principles](#design-principles)
5. [Current Implementation Status](#current-implementation-status)
6. [Web UI Guide](#web-ui-guide)
7. [Key Components Deep Dive](#key-components-deep-dive)
8. [GDS2 Integration Details](#gds2-integration-details)
9. [Working Demo Flow](#working-demo-flow)
10. [Navigation Guide](#navigation-guide)
11. [Current Scope & Limitations](#current-scope--limitations)
12. [Development Workflow](#development-workflow)
13. [Reference Documents](#reference-documents)

---

## Project Vision & Purpose

### The Big Picture
This project uses **RPA (Robotic Process Automation) + Java Agent** to automate vehicle diagnostic software like **GDS2** (General Motors Diagnostic System 2). The long-term goal is to deploy this in production environments for actual vehicle diagnostics automation.

### Current Phase: Simplified Data Viewer
- **Objective:** Automate vehicle diagnostics data collection with minimal user interaction
- **Interface:** Simplified 3-dropdown UI: Device → Module → Data Category
- **Architecture:** Java Agent for data extraction + Windows API for navigation
- **Result:** High-frequency monitoring (100ms interval) with automatic navigation

### Confirmed Working Workflows
1. **Data Viewer** - Simplified 3-dropdown UI with automatic navigation
2. **Real-time Monitoring** - 100ms Agent-based parameter streaming via SSE
3. **Device Switching** - Change VCI device from any page
4. **DTC Auto-extraction** - DTCs automatically included in every snapshot

### Assumptions (Confirmed Working)
- GDS2 is open with Java Agent attached
- Hardware is connected (SM2 USB VCI device)
- Vehicle data is loaded in GDS2

---

## Architecture Overview

### Agent-Based Architecture

```
┌─────────────────────────────────────────┐
│         Workflow Layer                  │  ← Business process orchestration
│   - DataViewerWorkflow (PRIMARY)        │
│   - InteractiveWorkflow                 │
│   - ReadDataDisplayAgentWorkflow        │
└─────────────────┬───────────────────────┘
                  │
┌─────────────────▼───────────────────────┐
│    Navigation Layer                     │  ← GDS2 page transitions
│   - NavigationController                │
│   - AgentNavigator (Java Agent comms)   │
│   - DeviceExplorerController (Win32)    │
└─────────────────┬───────────────────────┘
                  │
┌─────────────────▼───────────────────────┐
│      Data Collection Layer              │  ← Real-time monitoring
│   - AgentDataCollector (100ms)          │
│   - SSE Broadcasting                    │
│   - Parameter change detection          │
└─────────────────┬───────────────────────┘
                  │
┌─────────────────▼───────────────────────┐
│      Discovery & Mapping Layer          │  ← List enumeration
│   - VehicleDiscovery: enumerate lists   │
│   - VehicleMapping: JSON persistence    │
│   - On-demand module/data discovery     │
└─────────────────────────────────────────┘
```

### Data Viewer Flow

```python
# Simplified workflow - system handles all navigation
viewer = DataViewerWorkflow()

# Step 1: Start - get devices or modules if connected
result = viewer.start()
# Returns: {"devices": [...]} or {"modules": [...], "device_connected": True}

# Step 2: Connect device - navigate to Module List
result = viewer.connect_device("SM2 USB")
# Returns: {"modules": [...], "vin": "..."}

# Step 3: Select module - navigate to Data List
result = viewer.select_module("[K20] Engine Control Module")
# Returns: {"data_categories": [...]}

# Step 4: Select data - navigate to Data Display, start monitoring
result = viewer.select_data_category("Engine Data")
# Returns: {"monitoring": True}

# Change any selection - system auto-navigates
viewer.get_available_devices()  # Navigate to Device Explorer
viewer.select_module("Other Module")  # Uses Vehicle Menu shortcut
```

---

## Project Structure

```
RPA_demo/
├── src/                          # Main source code
│   ├── __init__.py
│   │
│   ├── core/                     # Core framework
│   │   ├── __init__.py
│   │   ├── driver.py             # GDS2Driver - pywinauto wrapper
│   │   ├── locators.py           # Centralized UI element definitions
│   │   └── template_matcher.py   # Multi-scale template matching
│   │
│   ├── discovery/                # Discovery system
│   │   ├── __init__.py
│   │   └── vehicle_mapping.py    # VehicleDiscovery & VehicleMapping
│   │
│   ├── native/                   # Windows API integration
│   │   ├── __init__.py
│   │   └── device_explorer.py    # Device Explorer automation
│   │
│   ├── navigation/               # Navigation system
│   │   ├── __init__.py
│   │   └── controller.py         # NavigationController
│   │
│   ├── streaming/                # Real-time data streaming
│   │   ├── __init__.py
│   │   ├── agent_navigator.py    # Java Agent communication
│   │   └── agent_data_collector.py  # Agent-based data collection
│   │
│   ├── utils/                    # Utilities
│   │   ├── __init__.py
│   │   └── report_parser.py      # HTML report parsing
│   │
│   └── workflows/                # Business workflows
│       ├── __init__.py
│       ├── data_viewer.py        # PRIMARY - Simplified Data Viewer
│       ├── interactive_workflow.py  # Step-by-step navigation
│       └── read_data_display_agent.py  # CLI workflow wrapper
│
├── templates/                    # Web UI templates
│   └── index.html                # Main Web UI page (Data Viewer)
│
├── scripts/                      # Utility scripts
│   ├── run_demo.py               # Demo execution script
│   ├── run_discovery.py          # Manual discovery utility
│   ├── inspect_gds2.py           # UI inspection tool
│   ├── inspect_agent.py          # Agent inspection tool
│   ├── test_agent_collector.py   # Agent collector tests
│   ├── test_agent_navigation.py  # Agent navigation tests
│   ├── test_e2e_agent_flow.py    # E2E agent flow tests
│   ├── test_e2e_full.py          # Full E2E tests
│   ├── legacy/                   # Legacy test scripts
│   └── exploration/              # Exploration scripts
│
├── mappings/                     # Auto-generated discovery data
│   └── current_vehicle.json      # Module and data category mappings
│
├── images/                       # Template images
│   ├── buttons/                  # Button templates
│   ├── devices/                  # Device templates
│   └── pages/                    # Page header templates
│
├── docs/                         # Documentation
│   ├── GDS2_CONTROL_MAPPING.md   # UI control mapping
│   └── WORKFLOW_DIAGRAM.md       # Navigation diagrams
│
├── tests/                        # pytest tests
│   ├── __init__.py
│   ├── conftest.py
│   └── test_navigation_to_data_display.py
│
├── main.py                       # CLI entry point
├── app.py                        # Flask Web UI backend
├── CLAUDE.md                     # This file
└── README.md                     # Project README
```

### Key Files

| File | Purpose |
|------|---------|
| `main.py` | CLI entry point: `web`, `demo`, `inspect` commands |
| `app.py` | Flask Web UI backend with Data Viewer API |
| `templates/index.html` | Web UI frontend - Data Viewer |
| `src/workflows/data_viewer.py` | **PRIMARY** - Simplified Data Viewer workflow |
| `src/workflows/interactive_workflow.py` | Step-by-step interactive navigation |
| `src/navigation/controller.py` | NavigationController - page transitions |
| `src/native/device_explorer.py` | Windows API Device Explorer automation |
| `src/streaming/agent_navigator.py` | Java Agent communication |
| `src/streaming/agent_data_collector.py` | Agent-based real-time data collection |
| `mappings/current_vehicle.json` | Auto-generated module/data mappings |

---

## Design Principles

### 1. Simplified User Interface

**Three Dropdowns:**
- Device: Select VCI device (SM2 USB, MDI, etc.)
- Module: Select vehicle module (Engine Control Module, etc.)
- Data Category: Select data to monitor (Engine Data, Misfire Data, etc.)

**System handles all navigation:**
- User selects → System navigates → Results appear
- Change any selection → System stops monitoring, navigates back, resumes

### 2. Smart Navigation

**Navigation shortcuts for efficiency:**
| Change | Navigation Path |
|--------|----------------|
| Change Data | Back → Data List → select new |
| Change Module | Vehicle Menu → Diagnostics Menu → Module Diagnostics → Module List |
| Change Device | Navigate to Main Menu → start from scratch |

### 3. Java Agent for Data Collection

**High-frequency monitoring:**
```python
from src.streaming import AgentDataCollector

collector = AgentDataCollector(
    on_snapshot=callback,      # Called every interval
    on_param_change=callback,  # Called when parameters change
    on_dtc_change=callback,    # Called when DTCs change
    on_error=callback,         # Called on errors
    interval_ms=100            # 100ms collection interval
)
collector.start()
```

**Performance comparison:**
| Metric | HTML Method | Agent Method |
|--------|-------------|--------------|
| Min Interval | 3000ms | 100ms |
| Latency | ~1500ms | ~50ms |
| CPU Usage | High | Low |
| UI Interaction | Required | None |

### 4. Windows API for Device Explorer

Device Explorer is a Win32 dialog, not JavaFX:
```python
from src.native import DeviceExplorerController

explorer = DeviceExplorerController()
if explorer.find_dialog():
    devices = explorer.get_devices()
    explorer.select_device("SM2 USB")
    explorer.click_continue()
```

---

## Current Implementation Status

### Production Ready (Verified 2026-02-03)

**Data Viewer Workflow:**
- Start → Device selection or Module List (if connected)
- Connect → Navigate through Device Explorer to Module List
- Select Module → Navigate to Data List
- Select Data → Navigate to Data Display, start monitoring
- Change any selection → Smart back-navigation

### Fully Implemented

- [x] **Data Viewer**
  - [x] Simplified 3-dropdown UI
  - [x] Automatic navigation
  - [x] Device switching from any page
  - [x] Smart back-navigation

- [x] **Navigation System**
  - [x] NavigationController for page transitions
  - [x] Page detection via Agent
  - [x] Button enabled/disabled state detection
  - [x] GDS2Page enum for all pages

- [x] **Real-time Data Monitoring**
  - [x] Agent-based collection (100ms interval)
  - [x] Server-Sent Events (SSE) streaming
  - [x] Parameter change detection
  - [x] DTC auto-extraction

- [x] **Device Explorer**
  - [x] Windows API automation (not JavaFX)
  - [x] Device enumeration
  - [x] Device selection
  - [x] Continue button handling

- [x] **Discovery System**
  - [x] VehicleDiscovery - enumerate list items
  - [x] VehicleMapping - JSON persistence
  - [x] On-demand discovery

### Not Yet Implemented

- [ ] ClearDTCWorkflow
- [ ] Multi-vehicle session handling
- [ ] VIN-based cache optimization

---

## Web UI Guide

### Starting the Web UI

```bash
# Start Web UI (recommended)
python main.py web

# With custom port
python main.py web --port 8000
```

Open http://localhost:8080 in your browser.

### Data Viewer Workflow

**User Flow:**
1. **Click Start** - System scans for devices or navigates to Module List if connected
2. **Select Device** - Choose VCI device from dropdown
3. **Select Module** - System navigates and discovers data categories
4. **Select Data Category** - System navigates to Data Display, starts monitoring

**Features:**
- Change Device: Click ⟳ button to switch devices
- Change Module: Just select a different module
- Change Data: Just select a different data category
- All transitions are automatic

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/viewer/start` | POST | Initialize, get devices or modules |
| `/api/viewer/connect` | POST | Connect device, get modules |
| `/api/viewer/change_device` | POST | Navigate to Device Explorer |
| `/api/viewer/select_module` | POST | Select module, get data categories |
| `/api/viewer/select_data` | POST | Select data, start monitoring |
| `/api/viewer/stop` | POST | Stop monitoring |
| `/api/viewer/state` | GET | Get current viewer state |
| `/api/stream/events` | GET | SSE endpoint for real-time data |
| `/api/stream/start` | POST | Start streaming |
| `/api/stream/stop` | POST | Stop streaming |
| `/api/agent/status` | GET | Check Agent availability |
| `/api/agent/dtcs` | GET | Get DTCs directly |
| `/api/agent/snapshot` | GET | Get latest snapshot |

---

## Key Components Deep Dive

### DataViewerWorkflow (`src/workflows/data_viewer.py`)

Primary workflow for the simplified Data Viewer.

**Key Methods:**

| Method | Purpose |
|--------|---------|
| `start()` | Initialize, get devices or navigate to Module List |
| `connect_device(device)` | Connect to device, navigate to Module List |
| `get_available_devices()` | Navigate to Device Explorer, get device list |
| `select_module(module)` | Select module, navigate to Data List |
| `select_data_category(category)` | Select data, start monitoring |
| `stop_monitoring()` | Stop the Agent collector |
| `get_state()` | Get current viewer state |

### NavigationController (`src/navigation/controller.py`)

Handles GDS2 page navigation.

**Key Methods:**

| Method | Purpose |
|--------|---------|
| `detect_current_page()` | Detect current GDS2 page |
| `go_home()` | Click Home button |
| `go_back()` | Click Back button |
| `go_vehicle_menu()` | Click Vehicle Menu button |
| `click_button(name)` | Click named button |
| `select_list_item(name)` | Select item from list |
| `wait_for_list()` | Wait for list to appear, return items |

### DeviceExplorerController (`src/native/device_explorer.py`)

Windows API automation for Device Explorer dialog.

**Key Methods:**

| Method | Purpose |
|--------|---------|
| `find_dialog()` | Find Device Explorer window |
| `get_devices()` | Get list of available devices |
| `select_device(name)` | Select device by name |
| `click_continue()` | Click Continue button |

---

## GDS2 Integration Details

### Application Info
- **Type:** JavaFX desktop application (main window)
- **Device Explorer:** Win32 dialog (not JavaFX)
- **Developer:** General Motors
- **Platform:** Windows only

### Key Discoveries

1. **Device Explorer is Win32** - Not JavaFX, requires Windows API automation
2. **Page Detection via Agent** - Agent provides window titles and labels
3. **Button State Detection** - Check enabled/disabled before clicking
4. **Vehicle Menu Shortcut** - Fastest path to change modules
5. **Home Button Disabled** - On some pages, use Back button loop instead

### GDS2Page Enum

```python
class GDS2Page(Enum):
    MAIN_MENU = "main_menu"
    DEVICE_EXPLORER = "device_explorer"
    VEHICLE_SELECTION = "vehicle_selection"
    DIAGNOSTICS_MENU = "diagnostics_menu"
    MODULE_LIST = "module_list"
    MODULE_SUBMENU = "module_submenu"
    DATA_LIST = "data_list"
    SUB_DATA_LIST = "sub_data_list"
    DATA_DISPLAY = "data_display"
    UNKNOWN = "unknown"
```

---

## Working Demo Flow

### Navigation Path (Data Viewer)

```
1. Start
   └── Check if device connected
       ├── Connected: Navigate to Module List → return modules
       └── Not connected: Navigate to Device Explorer → return devices

2. Connect Device (if at Device Explorer)
   └── Select device → Click Continue
   └── Vehicle Selection → Click Enter
   └── Diagnostics Menu → Module Diagnostics
   └── Module List → discover modules

3. Select Module
   └── Module List → select module
   └── Module Submenu → Data Display
   └── Data List → discover data categories

4. Select Data Category
   └── Data List → select category
   └── Data Display → start Agent monitoring

5. Change Module (from anywhere)
   └── Vehicle Menu → Diagnostics Menu
   └── Module Diagnostics → Module List
   └── (continue from step 3)

6. Change Device (from anywhere)
   └── Navigate to Vehicle Selection
   └── Disconnect → Select Device
   └── Device Explorer → (continue from step 2)
```

### Run the Demo

**Web UI (Recommended):**
```bash
python main.py web
# Open http://localhost:8080
```

**CLI:**
```bash
python main.py demo --module "[K20] Engine Control Module" --data "Misfire Data"
```

---

## Navigation Guide

### "I want to..."

**View live data from a module:**
1. Open Web UI: `python main.py web`
2. Click Start
3. Select device (or skip if connected)
4. Select module
5. Select data category

**Switch to a different module:**
Just select a different module from the dropdown. System uses Vehicle Menu shortcut.

**Switch to a different device:**
Click the ⟳ button next to device dropdown. System navigates to Device Explorer.

**Add a new workflow:**
1. Create `src/workflows/new_workflow.py`
2. Use DataViewerWorkflow as reference
3. Export in `src/workflows/__init__.py`

---

## Current Scope & Limitations

### What Works
- Connect to running GDS2 with Java Agent
- Navigate to any module and data category
- Automatic device switching from any page
- High-frequency monitoring (100ms)
- DTC auto-extraction
- Smart back-navigation

### Known Limitations
- GDS2 must be running with Java Agent
- VCI device must be connected
- Windows only
- Single vehicle session per execution

### Performance

| Metric | Value |
|--------|-------|
| Collection Interval | 100ms |
| Latency | ~50ms |
| Device Selection | ~2 seconds |
| Module Discovery | ~3 seconds |
| Navigation | ~2-5 seconds per page |

---

## Development Workflow

### Quick Start
```bash
# Activate venv
venv\Scripts\activate

# Start Web UI
python main.py web

# Run CLI demo
python main.py demo --module "[K20] Engine Control Module" --data "Engine Data"
```

### Test Imports
```bash
python -c "from src.workflows import DataViewerWorkflow; print('OK')"
python -c "from src.streaming import AgentNavigator, AgentDataCollector; print('OK')"
python -c "from src.native import DeviceExplorerController; print('OK')"
```

### Troubleshooting

**Agent not available:**
- Ensure GDS2 started with Java Agent attached
- Check `%USERPROFILE%\gds2-data\latest.json` exists

**Device Explorer not responding:**
- Device Explorer is Win32, not JavaFX
- Ensure GDS2 window is visible

**Navigation stuck:**
- Check for popup dialogs in GDS2
- Verify button is enabled before clicking

---

## Reference Documents

| Document | Location | Purpose |
|----------|----------|---------|
| GDS2 User Guide | `res/GM-GDS2-User-Guide.pdf` | Official button names, navigation flow |
| Control Mapping | `docs/GDS2_CONTROL_MAPPING.md` | UI control type reference |
| Workflow Diagram | `docs/WORKFLOW_DIAGRAM.md` | Navigation flow diagrams |

---

## Recent Changes (2026-02-03)

### Major Update: Simplified Data Viewer
1. **Replaced 3-step workflow** with simplified Data Viewer UI
   - Three dropdowns: Device → Module → Data Category
   - Automatic navigation in background
   - Smart back-navigation when selection changes

2. **Removed legacy APIs and code**
   - Removed `/api/nav/*` endpoints
   - Removed `/api/fetch_modules`, `/api/fetch_categories`, etc.
   - Removed legacy 3-step workflow code
   - Cleaned up unused files

3. **Improved device switching**
   - Added "Change Device" button (⟳)
   - Navigate to Device Explorer from any page
   - Proper disconnect and reconnect flow

4. **Fixed device-already-connected flow**
   - `start()` now navigates to Module List if device connected
   - Returns modules directly instead of requiring separate connect step

### Files Modified
- `src/workflows/data_viewer.py` - Major updates for device switching
- `src/workflows/__init__.py` - Updated exports
- `app.py` - Removed legacy APIs, kept only `/api/viewer/*`, `/api/stream/*`, `/api/agent/*`
- `templates/index.html` - Updated to Data Viewer only
- `README.md` - Complete rewrite for Data Viewer
- `CLAUDE.md` - Complete rewrite for Data Viewer

### Files Deleted
- `src/workflows/module_data_display.py` - Broken imports
- `src/workflows/module_discovery.py` - Broken imports
- `scripts/run_module_data.py` - Dead code
- `scripts/test_e2e_module_data.py` - Dead code

---

**This document should be updated when architecture or implementation changes.**
