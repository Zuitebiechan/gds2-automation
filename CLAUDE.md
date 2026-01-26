# RPA_demo Project Memory

**Last Updated:** 2026-01-26
**Status:** Production Ready - Keyboard Navigation with On-Demand Discovery

---

## Table of Contents
1. [Project Vision & Purpose](#project-vision--purpose)
2. [Architecture Overview](#architecture-overview)
3. [Project Structure](#project-structure)
4. [Design Principles](#design-principles)
5. [Current Implementation Status](#current-implementation-status)
6. [Key Components Deep Dive](#key-components-deep-dive)
7. [GDS2 Integration Details](#gds2-integration-details)
8. [Working Demo Flow](#working-demo-flow)
9. [Navigation Guide](#navigation-guide)
10. [Current Scope & Limitations](#current-scope--limitations)
11. [Development Workflow](#development-workflow)
12. [Reference Documents](#reference-documents)

---

## Project Vision & Purpose

### The Big Picture
This project uses **RPA (Robotic Process Automation) + AI** to automate vehicle diagnostic software like **GDS2** (General Motors Diagnostic System 2). The long-term goal is to deploy this in production environments for actual vehicle diagnostics automation.

### Current Phase: Production Ready with Hybrid Automation
- **Objective:** Automate vehicle diagnostics data collection
- **Scope:** Read data from any module/category (Engine Data, Misfire Data, etc.)
- **Result:** Successfully reading data from multiple categories with HTML report parsing
- **Architecture:** Hybrid approach combining PyAutoGUI+OpenCV (buttons) with keyboard navigation (lists)

### Confirmed Working Workflows
1. **Read Vehicle DTC** - Read all DTCs from vehicle (31 DTCs from HTML report)
2. **Read Data Display** - Read data from specific module and category (e.g., Engine Control Module → Misfire Data)

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
│   │   ├── exceptions.py         # Custom exception hierarchy
│   │   └── interfaces/           # Abstract interfaces
│   │       ├── __init__.py
│   │       └── screenshot.py     # Screenshot comparison interface
│   │
│   ├── discovery/                # Discovery system (NEW)
│   │   ├── __init__.py
│   │   └── vehicle_mapping.py    # VehicleDiscovery & VehicleMapping
│   │
│   ├── pages/                    # Page Object classes (legacy DTC workflow)
│   │   ├── __init__.py
│   │   ├── base_page.py
│   │   ├── main_menu_page.py
│   │   ├── device_explorer_page.py
│   │   ├── vehicle_selection_page.py
│   │   ├── diagnostics_menu_page.py
│   │   ├── vehicle_diagnostics_page.py
│   │   └── dtc_page.py
│   │
│   ├── workflows/                # Business workflows
│   │   ├── __init__.py
│   │   ├── base_workflow.py      # BaseWorkflow with PyAutoGUI+OpenCV
│   │   ├── read_vehicle_dtc.py   # Read DTC workflow (Page Objects)
│   │   └── read_data_display.py  # Read Data Display (Keyboard+Discovery)
│   │
│   ├── vision/                   # Vision features
│   │   ├── __init__.py
│   │   ├── screenshot_comparator.py  # OpenCV screenshot comparison
│   │   └── vlm_finder.py         # VLM-based element finding (fallback)
│   │
│   └── utils/                    # Utilities
│       ├── __init__.py
│       └── report_parser.py      # HTML report parsing
│
├── scripts/                      # Utility scripts
│   ├── run_demo.py               # Legacy demo execution script
│   ├── run_discovery.py          # Manual discovery utility
│   ├── inspect_gds2.py           # UI inspection tool
│   └── test_connection.py        # Connection testing
│
├── mappings/                     # Auto-generated discovery data (NEW)
│   └── current_vehicle.json      # Module and data category mappings
│
├── images/                       # Template images for PyAutoGUI (NEW)
│   ├── buttons/                  # Button templates
│   │   ├── diagnostics.png
│   │   ├── module_diagnostics.png
│   │   ├── data_display.png
│   │   ├── enter.png
│   │   ├── create_report.png
│   │   └── ok.png
│   └── devices/                  # Device templates
│       └── sm2_usb.png
│
├── docs/                         # Documentation
│   ├── CLAUDE.md                 # This file
│   ├── SCROLLING_SUPPORT.md      # Scrolling implementation notes
│   ├── GDS2_CONTROL_MAPPING.md   # UI control mapping
│   └── WORKFLOW_DIAGRAM.md       # Navigation diagrams
│
├── res/                          # Resources
│   └── GM-GDS2-User-Guide.pdf    # Official GDS2 User Guide
│
├── main.py                       # CLI entry point (UPDATED)
├── requirements-minimal.txt      # Dependencies
└── venv/                         # Virtual environment
```

### Key Files

| File | Purpose |
|------|---------|
| `src/core/driver.py` | Low-level UI automation (pywinauto wrapper) |
| `src/discovery/vehicle_mapping.py` | Discovery system for module/data lists |
| `src/workflows/base_workflow.py` | PyAutoGUI+OpenCV button/device clicking |
| `src/workflows/read_data_display.py` | Main workflow with keyboard navigation |
| `src/workflows/read_vehicle_dtc.py` | Legacy DTC reading (Page Objects) |
| `src/utils/report_parser.py` | HTML report parsing |
| `mappings/current_vehicle.json` | Auto-generated module/data mappings |
| `images/buttons/*.png` | Template images for button detection |
| `images/devices/*.png` | Template images for device selection |
| `main.py` | CLI entry point with all commands |

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
from src.utils.report_parser import DTCReportParser

parser = DTCReportParser()
result = parser.parse_dtc_report(report_path)
# Returns: vehicle_info, module_status, dtc_list
```

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
  - [x] Custom exception hierarchy

- [x] **Discovery System** (NEW)
  - [x] VehicleDiscovery - enumerate list items with pywinauto
  - [x] VehicleMapping - JSON persistence
  - [x] On-demand module/data discovery
  - [x] Discovery utility script

- [x] **Keyboard Navigation** (NEW)
  - [x] List item selection (DOWN + ENTER)
  - [x] Focus-aware navigation (start at index 0)
  - [x] Automatic discovery integration

- [x] **Workflows**
  - [x] ReadDataDisplayWorkflow - Keyboard + Discovery
  - [x] ReadVehicleDTCWorkflow - Page Objects (legacy)
  - [x] Automatic warning dialog handling

- [x] **Page Objects** (Legacy DTC Workflow)
  - [x] MainMenuPage, DeviceExplorerPage, VehicleSelectionPage
  - [x] DiagnosticsMenuPage, VehicleDiagnosticsPage, DTCPage

- [x] **Utilities**
  - [x] DTCReportParser for HTML reports
  - [x] Template matching utilities
  - [x] Discovery scripts

### Not Yet Implemented

- [ ] ClearDTCWorkflow
- [ ] ReadModuleDTCWorkflow (specific module)
- [ ] VLM fallback for unknown elements
- [ ] OCR verification for data values
- [ ] Multi-vehicle session handling

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

### Page Objects (`src/pages/`)

Each page class handles one GDS2 screen.

**BasePage methods:**
- `is_displayed()` - Check if page is active
- `has_home_button()` / `has_back_button()` - Check navigation
- `click_home()` / `click_back()` - Navigate

**Page-specific methods return next page:**
```python
class DiagnosticsMenuPage(BasePage):
    def select_vehicle_diagnostics(self) -> 'VehicleDiagnosticsPage':
        self.driver.click_list_item(Loc.DiagnosticsMenu.VEHICLE_DIAGNOSTICS)
        self.driver.click_button(Loc.DiagnosticsMenu.ENTER_BTN)
        return VehicleDiagnosticsPage(self.driver)
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

**Main entry point:**
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
from src.workflows import ReadDataDisplayWorkflow

workflow = ReadDataDisplayWorkflow(vehicle_id="current_vehicle")
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
python -c "from src.workflows import ReadDataDisplayWorkflow; print('OK')"
python -c "from src.discovery import VehicleDiscovery, VehicleMapping; print('OK')"
python -c "from src.workflows.base_workflow import BaseWorkflow; print('OK')"
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

## Recent Changes (2026-01-26)

### Major Updates
1. **Keyboard Navigation** - Replaced coordinate-based clicking with DOWN+ENTER navigation
2. **On-Demand Discovery** - Automatic module/data list discovery using pywinauto
3. **Template Matching** - Optimized Device Explorer from ~60s to ~1s
4. **Warning Dialog Handling** - Automatic detection and dismissal of OK button popups
5. **Code Integration** - Merged V3 workflow as main version, removed V1/V2
6. **CLI Integration** - Full command-line interface in main.py with all options

### Files Added
- `src/discovery/vehicle_mapping.py` - Discovery system
- `src/vision/vlm_finder.py` - VLM fallback (optional)
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
