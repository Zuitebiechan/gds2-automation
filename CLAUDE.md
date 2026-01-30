# RPA_demo Project Memory

**Last Updated:** 2026-01-30
**Status:** Production Ready - Web UI + CLI + Java Agent Integration

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
This project uses **RPA (Robotic Process Automation) + AI** to automate vehicle diagnostic software like **GDS2** (General Motors Diagnostic System 2). The long-term goal is to deploy this in production environments for actual vehicle diagnostics automation.

### Current Phase: Production Ready with Web UI
- **Objective:** Automate vehicle diagnostics data collection
- **Scope:** Read data from any module/category (Engine Data, Misfire Data, etc.)
- **Result:** Successfully reading data from multiple categories with HTML report parsing
- **Interface:** Web UI (recommended) + CLI for automation
- **Architecture:** Hybrid approach combining PyAutoGUI+OpenCV (buttons) with keyboard navigation (lists)

### Confirmed Working Workflows
1. **Web UI 3-Step Workflow** - Fetch Modules → Fetch Categories → Get DTCs
2. **Real-time Data Monitoring** - Select data category, monitor parameter changes via SSE
3. **Read Vehicle DTC** - Read all DTCs from vehicle (31 DTCs from HTML report)
4. **Read Data Display** - Read data from specific module and category (e.g., Engine Control Module → Misfire Data)

### Assumptions (Confirmed Working)
- GDS2 is already open at Main Menu
- User is already logged in
- Hardware is connected (SM2 USB VCI device)
- Vehicle data is loaded in GDS2

---

## Architecture Overview

### Hybrid Automation Architecture

```
┌─────────────────────────────────────────┐
│         Workflow Layer                  │  ← Business process orchestration
│   - ReadVehicleDTCWorkflow              │
│   - ReadDataDisplayWorkflow             │
│   - On-demand discovery integration     │
└─────────────────┬───────────────────────┘
                  │
┌─────────────────▼───────────────────────┐
│    Hybrid Automation Layer              │  ← PyAutoGUI + Keyboard Navigation
│   - PyAutoGUI+OpenCV: buttons/devices   │
│   - Keyboard: DOWN+ENTER for lists      │
│   - Template matching for fast selection│
└─────────────────┬───────────────────────┘
                  │
┌─────────────────▼───────────────────────┐
│      Discovery & Mapping Layer          │  ← pywinauto list enumeration
│   - VehicleDiscovery: get list items    │
│   - VehicleMapping: store/retrieve JSON │
│   - On-demand module/data discovery     │
└─────────────────┬───────────────────────┘
                  │
┌─────────────────▼───────────────────────┐
│         Driver Layer                    │  ← Low-level UI automation
│   - pywinauto: list item enumeration    │
│   - PyAutoGUI: mouse/keyboard control   │
│   - OpenCV: template matching           │
└─────────────────────────────────────────┘
```

### Keyboard Navigation with On-Demand Discovery

```python
# Workflow automatically discovers and navigates
workflow = ReadDataDisplayWorkflow(vehicle_id="current_vehicle")
result = workflow.execute(
    vci_device="SM2 USB",
    target_module="[K20] Engine Control Module",
    data_category="Misfire Data",
)

# Behind the scenes:
# 1. If module list not discovered → discover now → save to JSON
# 2. Get module index from JSON → press DOWN N times → press ENTER
# 3. If data categories not discovered → discover now → save to JSON
# 4. Get data index from JSON → press DOWN N times → press ENTER
# 5. Click Create Report using PyAutoGUI+OpenCV
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
│   ├── streaming/                # Real-time data streaming
│   │   ├── __init__.py
│   │   ├── agent_navigator.py    # Java Agent communication
│   │   ├── agent_data_collector.py  # Agent-based data collection
│   │   └── realtime_collector.py # HTML report-based collection (legacy)
│   │
│   ├── ui/                       # UI components
│   │   ├── __init__.py
│   │   └── module_selector.py    # Module selection UI
│   │
│   ├── utils/                    # Utilities
│   │   ├── __init__.py
│   │   └── report_parser.py      # HTML report parsing
│   │
│   └── workflows/                # Business workflows
│       ├── __init__.py
│       ├── read_data_display_agent.py  # Main workflow (Java Agent)
│       ├── read_data_display.py        # Legacy workflow (PyAutoGUI)
│       ├── base_workflow.py            # Legacy base class
│       ├── module_data_display.py      # Module data display
│       └── module_discovery.py         # Module discovery
│
├── templates/                    # Web UI templates
│   └── index.html                # Main Web UI page
│
├── scripts/                      # Utility scripts
│   ├── run_demo.py               # Demo execution script
│   ├── run_discovery.py          # Manual discovery utility
│   ├── run_module_data.py        # Module data script
│   ├── inspect_gds2.py           # UI inspection tool
│   ├── inspect_agent.py          # Agent inspection tool
│   ├── inspect_data_display.py   # Data display inspection
│   ├── test_connection.py        # Connection testing
│   ├── test_import.py            # Import verification
│   ├── test_agent_collector.py   # Agent collector tests
│   ├── test_agent_navigation.py  # Agent navigation tests
│   ├── test_e2e_agent_flow.py    # E2E agent flow tests
│   ├── test_e2e_full.py          # Full E2E tests
│   ├── test_e2e_module_data.py   # Module data E2E tests
│   ├── check_windows.py          # Windows check utility
│   ├── setup_tesseract.py        # Tesseract setup
│   ├── setup_scenic_view.py      # Scenic View setup
│   ├── legacy/                   # Legacy test scripts
│   │   └── ...
│   └── exploration/              # Exploration scripts
│       └── ...
│
├── mappings/                     # Auto-generated discovery data
│   └── current_vehicle.json      # Module and data category mappings
│
├── images/                       # Template images for PyAutoGUI
│   ├── buttons/                  # Button templates
│   ├── devices/                  # Device templates
│   ├── list_items/               # List item templates
│   └── pages/                    # Page header templates
│
├── docs/                         # Documentation
│   ├── SCROLLING_SUPPORT.md      # Scrolling implementation notes
│   ├── GDS2_CONTROL_MAPPING.md   # UI control mapping
│   ├── REALTIME_DATA_STREAMING.md # Streaming documentation
│   └── WORKFLOW_DIAGRAM.md       # Navigation diagrams
│
├── tests/                        # pytest tests
│   ├── __init__.py
│   ├── conftest.py
│   └── test_navigation_to_data_display.py
│
├── res/                          # Resources
│   └── GM-GDS2-User-Guide.pdf    # Official GDS2 User Guide
│
├── main.py                       # CLI entry point (web, demo, inspect, discover)
├── app.py                        # Flask Web UI backend
├── CLAUDE.md                     # This file
├── README.md                     # Project README
├── requirements.txt              # Full dependencies
├── requirements-minimal.txt      # Minimal dependencies
└── venv/                         # Virtual environment
```

### Key Files

| File | Purpose |
|------|---------|
| `main.py` | CLI entry point: `web`, `demo`, `inspect`, `discover` commands |
| `app.py` | Flask Web UI backend with REST API |
| `templates/index.html` | Web UI frontend with 3-step workflow |
| `src/core/driver.py` | Low-level UI automation (pywinauto wrapper) |
| `src/core/template_matcher.py` | Multi-scale template matching for buttons/devices |
| `src/discovery/vehicle_mapping.py` | Discovery system for module/data lists |
| `src/native/device_explorer.py` | Windows API-based Device Explorer automation |
| `src/workflows/read_data_display_agent.py` | Main workflow using Java Agent |
| `src/workflows/read_data_display.py` | Legacy workflow (PyAutoGUI+OpenCV) |
| `src/streaming/agent_navigator.py` | Java Agent communication |
| `src/streaming/agent_data_collector.py` | Agent-based real-time data collection |
| `src/streaming/realtime_collector.py` | HTML report-based data collection (legacy) |
| `src/utils/report_parser.py` | HTML report parsing (data items + DTCs) |
| `mappings/current_vehicle.json` | Auto-generated module/data mappings |
| `images/buttons/*.png` | Template images for button detection |
| `images/devices/*.png` | Template images for device selection |

---

## Design Principles

### 1. Hybrid Automation Strategy

**PyAutoGUI + OpenCV** for buttons and devices:
- Fast template matching for fixed UI elements
- Device selection: ~1 second (vs ~1 minute with VLM)
- Button clicking: "Diagnostics", "Module Diagnostics", "Data Display", "Create Report"

**Keyboard Navigation** for dynamic lists:
- Reliable list item selection (DOWN N times + ENTER)
- Works regardless of screen size or DPI
- No coordinate-based clicking issues

**pywinauto Discovery** for list enumeration:
- Enumerate all list items without scrolling
- Save to JSON for reuse
- On-demand discovery per module

### 2. On-Demand Discovery Pattern

```python
class ReadDataDisplayWorkflow:
    def _select_module_keyboard(self, target_module: str):
        # Check if module list discovered
        if not self.mapping.has_module_list(self.vehicle_id):
            # Discover now using pywinauto
            self._discover_and_save_modules()

        # Get index from JSON and navigate
        module_index = self.mapping.get_module_index(self.vehicle_id, target_module)

        # Press DOWN N times, then ENTER
        for _ in range(module_index):
            pyautogui.press('down')
        pyautogui.press('enter')
```

**Benefits:**
- Module list: discovered once, reused forever
- Data categories: discovered per-module as needed
- Fast startup (no pre-discovery delay)
- JSON persistence across sessions

### 3. Template Matching for Performance

Device selection optimized:
```python
# Before: VLM API call (~60 seconds with timeout)
self.click_text_vlm("SM2 USB")

# After: Template matching (~1 second)
self.click_device("sm2_usb", confidence=0.85)
```

### 4. Centralized Locators (Legacy)

All UI elements defined in `src/core/locators.py` for Page Object workflows:

```python
from src.core.locators import Loc

# Usage in pages
self.driver.click_button(Loc.MainMenu.DIAGNOSTICS_BTN)
self.driver.wait_for_element(Loc.DTCPage.CLEAR_DTCS_BTN)
```

**Note:** New Data Display workflow uses PyAutoGUI+OpenCV instead of locators.

### 5. HTML Report Parsing

More reliable than UI scraping:

```python
from src.utils.report_parser import GDS2ReportParser

parser = GDS2ReportParser()

# Parse DTCs
dtc_data = parser.parse_dtc_report(report_path)
# Returns: vehicle_info, module_status, dtc_list

# Parse data items
data = parser.parse_data_display_report(report_path)
# Returns: vehicle_info, data_items
```

### 6. Real-time Streaming Architecture

```python
from src.streaming import RealtimeDataCollector

collector = RealtimeDataCollector(
    on_data_change=callback,   # Called when parameters change
    on_full_data=callback,     # Called with all parameters
    on_error=callback,         # Called on errors
    interval_seconds=3.0       # Collection interval
)
collector.start()  # Starts background thread
# ... periodically clicks Create Report, parses HTML
collector.stop()   # Stops background thread
```

**Duplicate Parameter Handling:**
- Parameters with same name but different units (e.g., "Turbocharger Bypass Solenoid Valve Command" with "On" vs "0%") are tracked separately using `unique_key = name|unit`

---

## Current Implementation Status

### Production Ready (Verified 2026-01-26)

**Data Display Workflow:**
```
Main Menu → Diagnostics (PyAutoGUI) →
Device Explorer (template matching) →
Vehicle Selection (PyAutoGUI) →
Module Diagnostics (PyAutoGUI) →
Module List (keyboard + discovery) →
Module Submenu (PyAutoGUI) →
Data Display (PyAutoGUI) →
Data List (keyboard + discovery) →
Create Report (PyAutoGUI)
```

**Successfully Tested:**
- Engine Control Module → Ignition Data (index 16)
- Engine Control Module → Misfire Data (index 19)
- Automatic warning dialog dismissal
- Device selection in ~1 second (template matching)

### Fully Implemented

- [x] **Core Framework**
  - [x] GDS2Driver with pywinauto
  - [x] PyAutoGUI + OpenCV integration
  - [x] Template matching for buttons/devices
  - [x] Multi-scale template matching

- [x] **Web UI**
  - [x] Flask backend with REST API
  - [x] 3-step workflow interface (Fetch Modules → Fetch Categories → Get DTCs)
  - [x] Real-time state tracking
  - [x] Data table display with CSV download
  - [x] DTC parsing and display from HTML reports

- [x] **Real-time Data Monitoring**
  - [x] Server-Sent Events (SSE) for real-time updates
  - [x] Agent-based data collection (100ms interval)
  - [x] HTML report-based collection (legacy, 3s interval)
  - [x] Parameter change detection with unique key (name + unit)
  - [x] Automatic HTML report cleanup (keeps latest 50)
  - [x] Auto-navigate from Data List to Data Display on start
  - [x] Auto-return to Data List on stop

- [x] **Discovery System**
  - [x] VehicleDiscovery - enumerate list items with pywinauto
  - [x] VehicleMapping - JSON persistence
  - [x] On-demand module/data discovery
  - [x] Discovery utility script

- [x] **Keyboard Navigation**
  - [x] List item selection (DOWN + ENTER)
  - [x] Focus-aware navigation (start at index 0)
  - [x] Automatic discovery integration

- [x] **Workflows**
  - [x] ReadDataDisplayAgentWorkflow - Java Agent based (recommended)
  - [x] ReadDataDisplayWorkflow - Keyboard + Discovery (legacy)
  - [x] Automatic warning dialog handling

- [x] **Java Agent Integration**
  - [x] AgentNavigator - Agent communication
  - [x] AgentDataCollector - High-frequency data collection
  - [x] 100ms collection interval
  - [x] DTC auto-extraction

- [x] **Utilities**
  - [x] GDS2ReportParser for HTML reports
  - [x] Template matching utilities
  - [x] Discovery scripts

### Not Yet Implemented

- [ ] ClearDTCWorkflow
- [ ] ReadModuleDTCWorkflow (specific module)
- [ ] Multi-vehicle session handling

---

## Web UI Guide

### Starting the Web UI

```bash
# Start Web UI (recommended)
python main.py web

# With custom port
python main.py web --port 8000

# With debug mode
python main.py web --debug
```

Open http://localhost:8080 in your browser.

### 3-Step Workflow

The Web UI provides a guided 3-step workflow for data collection:

| Step | Button | GDS2 Start State | GDS2 End State | Description |
|------|--------|------------------|----------------|-------------|
| **1** | **Fetch Modules** | Main Menu | Module List | Navigate to Module List, discover all modules |
| **2** | **Fetch Data Categories** | Module List | Data List | Select module, navigate to Data List, discover categories |
| **3** | **Get DTCs** | Data List | Data List | Select data, fetch report, parse DTCs, click Back |

**Key Features:**
- Each step clearly indicates what GDS2 state is expected
- Step 3 parses HTML report for DTCs and returns structured data
- Step 3 can be repeated to fetch different data categories
- After Step 3, GDS2 returns to Data List for continuous querying

### Real-time Data Monitoring

The Web UI also provides real-time parameter monitoring:

1. Complete Steps 1 & 2 to discover modules and data categories
2. Select a data category from the monitoring dropdown
3. Click **Start Monitoring** - GDS2 auto-navigates to Data Display
4. System periodically clicks Create Report and parses HTML for all parameters
5. Parameter changes are detected and streamed to Web UI via SSE
6. Click **Stop** - GDS2 auto-returns to Data List

### Workflow Diagram

```
┌─────────────┐     Step 1      ┌─────────────┐     Step 2      ┌─────────────┐
│  Main Menu  │ ──────────────> │ Module List │ ──────────────> │  Data List  │
└─────────────┘  Fetch Modules  └─────────────┘  Fetch Categories└──────┬──────┘
                                                                        │
                                                                        │ Step 3
                                                                        │ Search
                                                                        ▼
                                                                ┌─────────────┐
                                                                │Data Display │
                                                                │  + Report   │
                                                                └──────┬──────┘
                                                                        │
                                                                        │ Auto Back
                                                                        ▼
                                                                ┌─────────────┐
                                                                │  Data List  │ ◄── Repeat Step 3
                                                                └─────────────┘
```

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/fetch_modules` | POST | Step 1: Discover modules |
| `/api/fetch_categories` | POST | Step 2: Select module, discover categories |
| `/api/search_data` | POST | Get DTCs: Fetch data, parse DTCs, create report, back |
| `/api/get_dtcs` | POST | Get DTCs (alternative): Fetch DTC-specific data |
| `/api/modules` | GET | Get cached module list |
| `/api/data_categories` | GET | Get cached data categories |
| `/api/state` | GET | Get current GDS2 state |
| `/api/stream/start` | POST | Start real-time monitoring (with data_category) |
| `/api/stream/stop` | POST | Stop monitoring, return to Data List |
| `/api/stream/events` | GET | SSE endpoint for real-time data |
| `/api/stream/status` | GET | Get streaming status |

---

## Key Components Deep Dive

### GDS2Driver (`src/core/driver.py`)

Low-level UI automation wrapper for pywinauto.

**Key Methods:**

| Method | Purpose |
|--------|---------|
| `connect()` | Connect to running GDS2 |
| `find_element(locator)` | Find element by Locator |
| `click_button(locator)` | Click button with optional verification |
| `click_list_item(locator)` | Click list item |
| `wait_for_element(locator)` | Wait for element to appear |
| `wait_for_element_enabled(locator)` | Wait for element to be enabled |
| `wait_for_ui_stable()` | Wait for UI to stop changing |

**Accepts Both:**
```python
# Locator object (preferred)
driver.click_button(Loc.MainMenu.DIAGNOSTICS_BTN)

# String + control type (fallback)
driver.click_button("Diagnostics", "Button")
```

### Locators (`src/core/locators.py`)

Centralized UI element definitions from GDS2 User Guide.

```python
@dataclass
class Locator:
    title: str
    control_type: str
    description: str = ""

class GDS2Locators:
    class MainMenu:
        DIAGNOSTICS_BTN = Locator("Diagnostics", "Button", "Vehicle diagnostics")

    class DTCPage:
        CLEAR_DTCS_BTN = Locator("Clear DTCs", "Button", "Clear displayed DTCs")
        CREATE_REPORT_BTN = Locator("Create Report", "Button", "Generate HTML report")
```

---

## GDS2 Integration Details

### Application Info
- **Type:** JavaFX desktop application
- **Developer:** General Motors
- **Platform:** Windows only

### Key Discoveries

1. **Keyboard Navigation Works Best** - Pressing DOWN N times + ENTER is most reliable for lists
2. **pywinauto for Discovery** - Can enumerate all list items without scrolling using `descendants(control_type='ListItem')`
3. **Template Matching is Fast** - PyAutoGUI + OpenCV ~1 second vs VLM ~60 seconds
4. **Focus Starts at Top** - When entering list page, focus is always on first item (index 0)
5. **Data Loading** - Check if "Create Report" button is enabled/visible
6. **Data Extraction** - Parse HTML reports for structured data
7. **Warning Dialogs** - Automatically detect and dismiss OK button popups

### UI Automation Best Practices

- Use PyAutoGUI + OpenCV for buttons and fixed elements (fast, reliable)
- Use keyboard navigation (DOWN + ENTER) for dynamic list selection
- Use pywinauto for list item enumeration and discovery
- Parse HTML reports instead of scraping UI tables
- Store discovered mappings in JSON for reuse
- Handle warning dialogs automatically with OK button detection

---

## Working Demo Flow

### Navigation Path (Data Display Workflow)

```
1. Main Menu
   └── Click "Diagnostics" button (PyAutoGUI)

2. Device Explorer (popup)
   └── Select "SM2 USB" (template matching ~1s)
   └── Click "Continue" (PyAutoGUI)

3. Vehicle Selection
   └── Click "Enter" (PyAutoGUI)

4. Diagnostics Menu
   └── Click "Module Diagnostics" (PyAutoGUI)

5. Module List (keyboard navigation)
   └── Discover modules if needed (pywinauto)
   └── Get index from JSON
   └── Press DOWN N times
   └── Press ENTER
   └── Example: "[K20] Engine Control Module" (index 5)

6. Module Submenu
   └── Click "Data Display" (PyAutoGUI)
   └── Handle warning dialog if present (auto-dismiss)

7. Data List (keyboard navigation)
   └── Discover data categories if needed (pywinauto)
   └── Get index from JSON
   └── Press DOWN N times
   └── Press ENTER
   └── Example: "Misfire Data" (index 19)

8. Data Display Page
   └── Wait for "Create Report" button visible
   └── Click "Create Report" (PyAutoGUI)

9. Parse HTML Report
   └── %LOCALAPPDATA%/Temp/GDS 2/Data Display_*.html
```

### Run the Demo

**Web UI (Recommended):**
```bash
# Start Web UI
python main.py web

# Open http://localhost:8080 in browser
# Follow the 3-step workflow
```

**CLI entry point:**
```bash
# Default: Engine Data
python main.py demo

# Custom module and data category
python main.py demo --module "[K20] Engine Control Module" --data "Misfire Data"

# With verbose logging
python main.py demo --module "[K20] Engine Control Module" --data "Ignition Data" -v

# Custom VCI device
python main.py demo --vci "SM2 USB" --module "[K20] Engine Control Module" --data "Engine Data"
```

**Discovery utility:**
```bash
python main.py discover
```

**Inspect GDS2 UI:**
```bash
python main.py inspect
```

**Python code:**
```python
from src.workflows import ReadDataDisplayAgentWorkflow

workflow = ReadDataDisplayAgentWorkflow()
result = workflow.execute(
    vci_device="SM2 USB",
    target_module="[K20] Engine Control Module",
    data_category="Misfire Data",
)

if result["success"]:
    print(f"Report: {result['report_path']}")
```

---

## Navigation Guide

### "I want to..."

**Read data from a new module/category:**
```bash
python main.py demo --module "[Module Name]" --data "Data Category Name"
```
The system will automatically discover and navigate to it.

**Add support for a new button:**
1. Take screenshot of the button
2. Save as `images/buttons/button_name.png`
3. Use `self.click_button("button_name")` in workflow

**Add support for a new device:**
1. Take screenshot of the device in Device Explorer
2. Save as `images/devices/device_name.png`
3. Use `self.click_device("device_name")` in workflow

**Manually discover vehicle structure:**
```bash
python main.py discover
```
Then navigate to the desired page in GDS2 and select discovery options.

**Add a new workflow:**
1. Create `src/workflows/new_workflow.py`
2. Inherit from `BaseWorkflow`
3. Use PyAutoGUI+OpenCV for buttons
4. Use keyboard navigation + discovery for lists
5. Export in `src/workflows/__init__.py`

**Debug navigation issues:**
1. Run `python main.py inspect` to check element properties
2. Check template images in `images/` directory
3. Run with `-v` flag for verbose logging
4. Check `mappings/current_vehicle.json` for discovered indices

---

## Current Scope & Limitations

### What Works
- Connect to running GDS2 (Windows only)
- Navigate to any module and data category
- Automatic on-demand discovery of module/data lists
- Keyboard navigation (DOWN + ENTER) for list selection
- Template matching for buttons and devices (~1 second)
- Automatic warning dialog dismissal
- HTML report generation and parsing
- JSON persistence of discovered mappings

### Successfully Tested
- Engine Control Module → Engine Data (default)
- Engine Control Module → Ignition Data (index 16, discovered)
- Engine Control Module → Misfire Data (index 19, discovered)
- Device Explorer selection in ~1 second (template matching)
- Warning dialog auto-dismissal

### Known Limitations
- GDS2 must be open at Main Menu before starting
- VCI device must be connected and vehicle selected
- Windows only (pywinauto, PyAutoGUI)
- Single vehicle session per execution
- Template images must exist for new buttons/devices
- Discovery requires manual navigation to list pages first time

### Performance
- Device selection: ~1 second (template matching)
- Module/data discovery: ~2-3 seconds per list (first time only)
- Button clicking: <1 second (PyAutoGUI)
- List navigation: ~50ms per item (keyboard)
- Total workflow: ~15-30 seconds depending on discovery needs

---

## Development Workflow

### Quick Start
```bash
# Activate venv
venv\Scripts\activate

# Run demo with default settings (Engine Data)
python main.py demo

# Run with custom module and data
python main.py demo --module "[K20] Engine Control Module" --data "Misfire Data" -v
```

### Discovery Workflow
```bash
# Option 1: Run discovery utility
python main.py discover

# Option 2: Discovery happens automatically on first run
# Just run the workflow with any module/data, it will discover as needed
python main.py demo --module "New Module" --data "New Data Category"
```

### Adding New Template Images
```bash
# 1. Navigate GDS2 to the desired page
# 2. Take screenshot of the button/device
# 3. Crop to just the element (with some padding)
# 4. Save to images/buttons/ or images/devices/
# 5. Use lowercase with underscores: create_report.png, sm2_usb.png
```

### Test Imports
```bash
python -c "from src.workflows import ReadDataDisplayAgentWorkflow; print('OK')"
python -c "from src.streaming import AgentNavigator, AgentDataCollector; print('OK')"
python -c "from src.native import handle_device_explorer; print('OK')"
python -c "from src.discovery import VehicleDiscovery, VehicleMapping; print('OK')"
```

### Troubleshooting

**Template matching not working:**
- Check image exists in `images/buttons/` or `images/devices/`
- Try lower confidence: `self.click_button("name", confidence=0.7)`
- Ensure screenshot is grayscale-compatible
- Check GDS2 window is in focus and fully visible

**Discovery not finding items:**
- Ensure GDS2 window title contains "GDS 2"
- Check that list page is fully loaded
- Verify pywinauto can see the window: `python main.py inspect`

**Keyboard navigation selecting wrong item:**
- Check `mappings/current_vehicle.json` for correct indices
- Delete JSON file and re-discover if vehicle changed
- Ensure focus is on list (click list area first if needed)

---

## Reference Documents

| Document | Location | Purpose |
|----------|----------|---------|
| GDS2 User Guide | `res/GM-GDS2-User-Guide.pdf` | Official button names, navigation flow |
| Scrolling Support | `docs/SCROLLING_SUPPORT.md` | Scrolling implementation notes and lessons learned |
| Control Mapping | `docs/GDS2_CONTROL_MAPPING.md` | UI control type reference |
| Workflow Diagram | `docs/WORKFLOW_DIAGRAM.md` | Navigation flow diagrams |
| Cleanup Summary | `CLEANUP_SUMMARY.md` | History of removed/deprecated code |

---

## Recent Changes (2026-01-30)

### Code Cleanup & Optimization
1. **Fixed Hardcoded Paths** - All paths now use relative `Path(__file__)` references
   - `src/core/template_matcher.py` - IMAGES_DIR
   - `src/workflows/base_workflow.py` - IMAGES_DIR
   - `src/streaming/realtime_collector.py` - template_dir
   - `scripts/test_import.py` - sys.path

2. **Deleted Unused Modules**
   - `src/core/ocr.py` - Never imported
   - `src/core/exceptions.py` - Never imported
   - `src/core/interfaces/` - Empty directory
   - `src/vision/` - VLM and screenshot comparator (unused)
   - `src/config/` - Empty directory
   - `src/pages/` - Unused Page Objects (module_list_page.py, etc.)

3. **Organized Scripts**
   - Created `scripts/legacy/` for old test scripts
   - Created `scripts/exploration/` for exploration scripts
   - Moved 7 legacy tests and 5 exploration scripts

4. **Cleaned Misc Files**
   - Removed `nul`, `scenicView.properties`, `templates/index.html.backup`

### Major Updates: Java Agent Integration
1. **High-Frequency Agent Monitoring** - Direct JVM data extraction
   - 100ms collection interval (vs 3000ms with HTML parsing)
   - ~50ms latency (vs ~1500ms waiting for file writes)
   - No UI interaction required (no Create Report clicking)
   - DTCs automatically included in every snapshot

2. **Enhanced Java Agent (v2.0)**
   - Table type detection: `dtc`, `data_display`, `unknown`
   - Page context detection from window titles and labels
   - Compact JSON output for high-frequency writes
   - Extraction duration metrics
   - Default 100ms interval (minimum 50ms)

3. **New AgentDataCollector Class**
   - Polls `%USERPROFILE%\gds2-data\latest.json`
   - Parses both v1 and v2 Agent JSON formats
   - Parameter change detection with unique keys (name|unit)
   - DTC change detection (added/removed)
   - Callbacks: `on_snapshot`, `on_param_change`, `on_dtc_change`

4. **Web UI: Agent Monitor Tab**
   - Real-time Agent status indicator
   - High-frequency interval options: 100ms, 200ms, 500ms, 1s, 2s
   - Live DTC panel with count badge
   - Parameter changes log
   - Extraction latency display

5. **New API Endpoints**
   - `/api/agent/status` - Check Agent availability
   - `/api/agent/stream/start` - Start Agent streaming
   - `/api/agent/stream/stop` - Stop Agent streaming
   - `/api/agent/stream/events` - SSE endpoint
   - `/api/agent/snapshot` - Get latest snapshot

### Files Added (2026-01-30)
- `src/streaming/agent_data_collector.py` - New Agent-based collector
- `scripts/test_agent_collector.py` - Agent collector tests

### Files Modified (2026-01-30)
- `src/streaming/__init__.py` - Export new classes
- `app.py` - Agent streaming endpoints
- `templates/index.html` - Agent Monitor tab
- Java: `DataExtractor.java` - Table type detection, page context, compact JSON
- Java: `GDS2Agent.java` - 100ms default interval, validation

### Performance Comparison

| Metric | HTML Method | Agent Method |
|--------|-------------|--------------|
| Min Interval | 3000ms | 100ms |
| Latency | ~1500ms | ~50ms |
| CPU Usage | High (template matching) | Low (JSON parse) |
| UI Interaction | Click Create Report | None |
| DTC Extraction | Separate step | Auto-included |

---

## Previous Changes (2026-01-28)

### Major Updates
1. **Real-time Data Monitoring** - Background data streaming via SSE
   - Select data category from dropdown, auto-navigate to Data Display
   - Periodically click Create Report and parse HTML for all parameters
   - Detect parameter changes and stream to Web UI
   - Auto-return to Data List on stop
2. **Get DTCs** - Step 3 renamed, now parses HTML for DTC information
   - Returns vehicle_info, dtc_list, module_status alongside data items
   - Displays DTCs table (Code, Module, Description, Status) in Web UI
3. **Duplicate Parameter Handling** - Unique key (name|unit) for parameters with same name but different units
4. **Auto HTML Report Cleanup** - Keeps latest 50 HTML reports to prevent disk space issues
5. **Dual Device Template Matching** - Try highlighted SM2 USB template first, fall back to normal

### Previous Changes (2026-01-26)
1. **Flask Web UI** - New visual interface for GDS2 automation
   - 3-step workflow: Fetch Modules → Fetch Categories → Get DTCs
   - Real-time state tracking and display
   - Data table with CSV download
2. **Keyboard Navigation** - Replaced coordinate-based clicking with DOWN+ENTER navigation
3. **On-Demand Discovery** - Automatic module/data list discovery using pywinauto
4. **Template Matching** - Optimized Device Explorer from ~60s to ~1s
5. **Warning Dialog Handling** - Automatic detection and dismissal of OK button popups
6. **CLI Integration** - Full command-line interface with `web`, `demo`, `inspect`, `discover` commands

### Files Added (2026-01-28)
- `src/streaming/__init__.py` - Streaming module
- `src/streaming/realtime_collector.py` - Background data collection
- `images/devices/sm2_usb_highlight.png` - Highlighted device template
- `images/manifest.json` - Template images manifest
- `images/buttons/back.png`, `home.png`, `refresh.png`, etc. - Additional button templates
- `images/list_items/*.png` - List item templates
- `images/pages/*.png` - Page header templates

### Files Added (2026-01-26)
- `app.py` - Flask Web UI backend
- `templates/index.html` - Web UI frontend
- `src/discovery/vehicle_mapping.py` - Discovery system
- `scripts/run_discovery.py` - Discovery utility
- `images/buttons/*.png` - Button templates
- `images/devices/*.png` - Device templates
- `mappings/current_vehicle.json` - Auto-generated mappings
- `docs/SCROLLING_SUPPORT.md` - Scrolling implementation notes

### Files Removed/Cleaned
- Old test scripts (test_scrolling.py, test_list_selection.py, etc.)
- V1/V2 workflow versions (read_data_display_v*.py)
- Legacy page object files (no longer used by main workflow)

---

**This document should be updated when architecture or implementation changes.**
