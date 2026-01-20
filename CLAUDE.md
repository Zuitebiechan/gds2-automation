# RPA_demo Project Memory

**Last Updated:** 2026-01-20
**Status:** Demo Working - Page Object Pattern Implemented

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

### Current Phase: Page Object Pattern Implemented
- **Objective:** Validate RPA for diagnostic automation
- **Scope:** Read DTC (Diagnostic Trouble Codes) from all vehicle modules
- **Result:** Successfully reading 31 DTCs from connected vehicle via HTML report parsing
- **Architecture:** Clean Page Object pattern with fluent navigation and centralized locators

### Assumptions (Confirmed Working)
- GDS2 is already open at Main Menu
- User is already logged in
- Hardware is connected (SM2 USB VCI device)
- Vehicle data is loaded in GDS2

---

## Architecture Overview

### Three-Layer Architecture

```
┌─────────────────────────────────────────┐
│         Workflow Layer                  │  ← Business process orchestration
│   - ReadVehicleDTCWorkflow              │
│   - Fluent navigation chains            │
└─────────────────┬───────────────────────┘
                  │
┌─────────────────▼───────────────────────┐
│         Page Object Layer               │  ← UI abstraction
│   - MainMenuPage, DTCPage, etc.         │
│   - Each page returns next page         │
│   - Uses centralized Locators           │
└─────────────────┬───────────────────────┘
                  │
┌─────────────────▼───────────────────────┐
│         Driver Layer                    │  ← Low-level UI automation
│   - GDS2Driver (pywinauto wrapper)      │
│   - DPI-aware clicking                  │
│   - Screenshot comparison               │
└─────────────────────────────────────────┘
```

### Fluent Navigation Pattern

```python
# Clean, readable navigation chain
dtc_page = (main_menu
    .click_diagnostics()      # Returns DeviceExplorerPage
    .select_device("SM2 USB") # Returns VehicleSelectionPage
    .click_enter()            # Returns DiagnosticsMenuPage
    .select_vehicle_diagnostics()  # Returns VehicleDiagnosticsPage
    .select_vehicle_dtc_info())    # Returns DTCPage
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
│   ├── pages/                    # Page Object classes
│   │   ├── __init__.py           # Exports all pages
│   │   ├── base_page.py          # BasePage abstract class
│   │   ├── main_menu_page.py     # Main Menu
│   │   ├── device_explorer_page.py    # Device Explorer popup
│   │   ├── vehicle_selection_page.py  # Vehicle Selection
│   │   ├── diagnostics_menu_page.py   # Diagnostics Menu
│   │   ├── vehicle_diagnostics_page.py # Vehicle Diagnostics submenu
│   │   └── dtc_page.py           # DTC Information page
│   │
│   ├── workflows/                # Business workflows
│   │   ├── __init__.py
│   │   ├── base_workflow.py      # BaseWorkflow abstract class
│   │   └── read_vehicle_dtc.py   # Read DTC workflow
│   │
│   ├── vision/                   # Vision features (optional)
│   │   ├── __init__.py
│   │   └── screenshot_comparator.py  # OpenCV screenshot comparison
│   │
│   └── utils/                    # Utilities
│       ├── __init__.py
│       └── report_parser.py      # HTML report parsing
│
├── scripts/                      # Entry point scripts
│   ├── run_demo.py               # Demo execution
│   ├── inspect_gds2.py           # UI inspection tool
│   └── test_connection.py        # Connection testing
│
├── docs/                         # Documentation
│   ├── CLAUDE.md                 # This file
│   ├── GDS2_CONTROL_MAPPING.md   # UI control mapping
│   └── WORKFLOW_DIAGRAM.md       # Navigation diagrams
│
├── res/                          # Resources
│   └── GM-GDS2-User-Guide.pdf    # Official GDS2 User Guide
│
├── main.py                       # CLI entry point
├── requirements-minimal.txt      # Dependencies
└── venv/                         # Virtual environment
```

### Key Files

| File | Purpose |
|------|---------|
| `src/core/driver.py` | Low-level UI automation (pywinauto wrapper) |
| `src/core/locators.py` | All UI element definitions (from GDS2 User Guide) |
| `src/pages/*.py` | Page Object classes for each GDS2 screen |
| `src/workflows/read_vehicle_dtc.py` | DTC reading workflow |
| `src/utils/report_parser.py` | HTML report parsing |
| `res/GM-GDS2-User-Guide.pdf` | Official button names reference |

---

## Design Principles

### 1. Centralized Locators

All UI elements defined in `src/core/locators.py`:

```python
from src.core.locators import Loc

# Usage in pages
self.driver.click_button(Loc.MainMenu.DIAGNOSTICS_BTN)
self.driver.wait_for_element(Loc.DTCPage.CLEAR_DTCS_BTN)
```

**Locator Categories (from GDS2 User Guide):**
- `Loc.Navigation` - Bottom navigation bar (Back, Home, Enter, etc.)
- `Loc.MainMenu` - Main menu buttons
- `Loc.DeviceExplorer` - Device selection popup
- `Loc.VehicleSelection` - Vehicle selection page
- `Loc.DiagnosticsMenu` - Diagnostics menu items
- `Loc.VehicleDiagnostics` - Vehicle diagnostics submenu
- `Loc.DTCPage` - DTC display controls
- `Loc.DataDisplay` - Data/PID display controls
- `Loc.ModuleDiagnostics` - Module diagnostics functions
- `Loc.Dialog` - Common dialog buttons

### 2. Page Object Pattern

Each GDS2 page is a class that:
- Identifies itself via specific elements
- Provides actions that return the next page
- Uses centralized locators

```python
class MainMenuPage(BasePage):
    def is_displayed(self) -> bool:
        return self.driver.element_exists(Loc.MainMenu.DIAGNOSTICS_BTN)

    def click_diagnostics(self) -> 'DeviceExplorerPage':
        self.driver.click_button(Loc.MainMenu.DIAGNOSTICS_BTN)
        return DeviceExplorerPage(self.driver)
```

### 3. Fluent Navigation

Methods return the next page for chaining:

```python
# In workflow
dtc_page = (main_menu
    .click_diagnostics()
    .select_device(vci_device)
    .click_enter()
    .select_vehicle_diagnostics()
    .select_vehicle_dtc_info())
```

### 4. Screenshot-Based Verification (Optional)

When enabled, uses OpenCV to verify UI actions worked:

```python
# In driver.py - click_element()
if self._screenshot_comparator:
    changed, elapsed = self._screenshot_comparator.capture_and_wait_for_change(
        action_callback=do_click,
        timeout=10,
        threshold=0.90,
    )
```

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

### Demo Working (Verified 2026-01-20)

**Full Demo Flow:**
```
Main Menu → Diagnostics → Device Explorer → Vehicle Selection →
Diagnostics Menu → Vehicle Diagnostics → Vehicle DTC Information →
Wait for Data → Create Report → Parse HTML
```

**Results:** 31 DTCs successfully parsed from HTML report

### Fully Implemented

- [x] **Core Framework**
  - [x] GDS2Driver with DPI-aware clicking
  - [x] Centralized locators (official button names)
  - [x] Custom exception hierarchy
  - [x] Screenshot comparison interface

- [x] **Page Objects**
  - [x] MainMenuPage
  - [x] DeviceExplorerPage
  - [x] VehicleSelectionPage
  - [x] DiagnosticsMenuPage
  - [x] VehicleDiagnosticsPage
  - [x] DTCPage

- [x] **Workflows**
  - [x] ReadVehicleDTCWorkflow with fluent navigation

- [x] **Utilities**
  - [x] DTCReportParser for HTML reports
  - [x] Data classes: DTCInfo, VehicleInfo, ModuleStatus

- [x] **Vision (Optional)**
  - [x] OpenCV screenshot comparator

### Not Yet Implemented

- [ ] ClearDTCWorkflow
- [ ] ReadModuleDTCWorkflow (specific module)
- [ ] ReadPIDWorkflow
- [ ] VLM fallback for element finding
- [ ] OCR verification

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

1. **Dynamic Auto IDs** - Use `title` + `control_type`, not `auto_id`
2. **JavaFX Lists** - Use keyboard navigation (`{DOWN}`, `{ENTER}`)
3. **Data Loading** - Check if "Clear DTCs" button is enabled
4. **Data Extraction** - Parse HTML reports, not UI tables

### UI Automation Tips

- Use `invoke()` instead of `click_input()` for JavaFX buttons
- Use keyboard navigation for list selection
- Parse HTML reports for structured data
- Handle popup windows with `Desktop()` (Device Explorer)

---

## Working Demo Flow

### Navigation Path

```
1. Main Menu
   └── Click "Diagnostics" button

2. Device Explorer (popup)
   └── Select "SM2 USB"
   └── Click "Continue"

3. Vehicle Selection
   └── Click "Enter"

4. Diagnostics Menu
   └── Select "Vehicle Diagnostics"
   └── Click "Enter"

5. Vehicle Diagnostics
   └── Select "Vehicle DTC Information"
   └── Click "Enter"

6. DTC Information
   └── Wait for "Clear DTCs" enabled
   └── Click "Create Report"

7. Parse HTML Report
   └── %LOCALAPPDATA%/Temp/GDS 2/DTC Display_*.html
```

### Run the Demo

**Option 1: main.py**
```bash
python main.py demo
```

**Option 2: Direct script**
```bash
python scripts/run_demo.py
```

**Option 3: Python code**
```python
from src.core.driver import GDS2Driver
from src.workflows.read_vehicle_dtc import ReadVehicleDTCWorkflow

with GDS2Driver() as driver:
    driver.connect()
    workflow = ReadVehicleDTCWorkflow(driver)
    result = workflow.execute(vci_device="SM2 USB")
    print(f"Found {len(result['dtc_list'])} DTCs")
```

---

## Navigation Guide

### "I want to..."

**Add a new page object:**
1. Create `src/pages/new_page.py`
2. Inherit from `BasePage`
3. Implement `name`, `is_displayed()`
4. Add navigation methods that return next page
5. Export in `src/pages/__init__.py`

**Add a new workflow:**
1. Create `src/workflows/new_workflow.py`
2. Inherit from `BaseWorkflow`
3. Use fluent page navigation
4. Export in `src/workflows/__init__.py`

**Add new UI elements:**
1. Update `src/core/locators.py`
2. Add to appropriate class (MainMenu, DTCPage, etc.)
3. Reference GDS2 User Guide for official names

**Debug navigation issues:**
1. Run `python main.py inspect`
2. Check element titles and control types
3. Update locators.py accordingly

---

## Current Scope & Limitations

### What Works
- Connect to running GDS2
- Full navigation to Vehicle DTC Information
- Wait for data loading (button state check)
- Create and parse HTML report
- Extract 31 DTCs with structured data

### Known Limitations
- GDS2 must be open at Main Menu
- SM2 USB VCI must be connected
- Windows only (pywinauto)
- Single vehicle session
- HTML report dependency

---

## Development Workflow

### Quick Start
```bash
# Activate venv
venv\Scripts\activate

# Run demo
python main.py demo
```

### Test Imports
```bash
python -c "from src.pages import MainMenuPage, DTCPage; print('OK')"
python -c "from src.core.locators import Loc; print('OK')"
python -c "from src.workflows import ReadVehicleDTCWorkflow; print('OK')"
```

---

## Reference Documents

| Document | Location | Purpose |
|----------|----------|---------|
| GDS2 User Guide | `res/GM-GDS2-User-Guide.pdf` | Official button names, navigation flow |
| Control Mapping | `docs/GDS2_CONTROL_MAPPING.md` | UI control type reference |
| Workflow Diagram | `docs/WORKFLOW_DIAGRAM.md` | Navigation flow diagrams |

---

**This document should be updated when architecture or implementation changes.**
