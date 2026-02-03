# Module Data Display Implementation Plan

**Status:** ✅ IMPLEMENTATION COMPLETE
**Last Updated:** 2026-01-21

## Overview

This plan covers implementing a Module Data Display workflow that:
1. Discovers all available modules and their data items (per-vehicle, automatic)
2. Allows users to select a module and view its data display
3. Handles scrolling for long lists
4. Provides a simple Tkinter user interface
5. Creates reports and parses HTML for data extraction

---

## Design Decisions (User Confirmed)

| Decision | Choice |
|----------|--------|
| Tesseract | Bundle with project |
| UI Framework | Tkinter (simpler) |
| Discovery trigger | Automatic on first launch |
| Partial failure | Save partial results, auto-rediscover missing modules |
| Registry storage | Per-vehicle (check VIN, use existing or discover new) |
| Data extraction | Click "Create Report" → Parse HTML (no record/freeze buttons) |

---

## Current State

```
Existing Components:
├── src/core/
│   ├── image_driver.py      ← Core automation (PyAutoGUI + OpenCV)
│   ├── image_locators.py    ← Image-based locators
│   └── exceptions.py        ← Custom exceptions
├── src/pages/
│   ├── base_page.py         ← Base page class
│   ├── main_menu_page.py    ← Main Menu
│   ├── device_explorer_page.py
│   ├── vehicle_selection_page.py
│   ├── diagnostics_menu_page.py  ← Has select_module_diagnostics() stub
│   ├── vehicle_diagnostics_page.py
│   └── dtc_page.py
├── src/workflows/
│   ├── base_workflow.py     ← Base workflow class
│   └── read_vehicle_dtc.py  ← Existing DTC workflow
├── src/utils/
│   └── report_parser.py     ← HTML report parsing (REUSE)
└── images/                  ← Screenshot images for locators
```

---

## Target State

```
New/Modified Components:
├── src/core/
│   ├── image_driver.py      ← ADD: OCR methods, scroll methods
│   ├── image_locators.py    ← ADD: New locators for module diagnostics
│   └── ocr.py               ← NEW: OCR utilities (pytesseract wrapper)
├── src/pages/
│   ├── diagnostics_menu_page.py  ← MODIFY: Implement select_module_diagnostics()
│   ├── module_list_page.py       ← NEW: Module selection with scrolling
│   ├── module_options_page.py    ← NEW: Options page (DTCs, Data Display, etc.)
│   ├── data_selection_page.py    ← NEW: Data item selection (if needed)
│   └── data_display_page.py      ← NEW: Final data display page
├── src/workflows/
│   ├── module_discovery.py       ← NEW: Discovery workflow (per-vehicle)
│   └── module_data_display.py    ← NEW: Data display workflow
├── src/utils/
│   └── report_parser.py          ← MODIFY: Add Data Display report parsing
├── src/config/
│   └── registries/               ← NEW: Per-vehicle registry storage
│       ├── {vin_1}.json
│       ├── {vin_2}.json
│       └── ...
├── src/ui/
│   └── module_selector.py        ← NEW: Simple Tkinter GUI
├── vendor/
│   └── tesseract/                ← NEW: Bundled Tesseract OCR
└── images/
    ├── buttons/
    │   ├── data_display.png      ← NEW: "Data Display" option
    │   └── create_report.png     ← EXISTING: Reuse from DTCPage
    └── indicators/
        └── scroll_indicator.png  ← NEW: Scroll indicator (if needed)
```

---

## Key Workflow: Data Extraction

**Important:** Data Display page does NOT have record/freeze buttons.
Instead, we use the same approach as DTC workflow:

```
Data Display Page
    → Click "Create Report" button
    → Wait for HTML file to be created
    → Parse HTML file using report_parser.py
    → Return structured data to user
```

This reuses the existing `DTCReportParser` pattern and `ImageLoc.DTCPage.CREATE_REPORT_BTN`.

---

## Per-Vehicle Registry System

### Flow Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                        Application Start                         │
└─────────────────────────────┬───────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│              Connect to GDS2, Get Vehicle VIN                    │
│                    (from Vehicle Selection page)                 │
└─────────────────────────────┬───────────────────────────────────┘
                              │
                              ▼
                    ┌─────────────────────┐
                    │ Registry exists for │
                    │     this VIN?       │
                    └──────────┬──────────┘
                               │
              ┌────────────────┴────────────────┐
              │ YES                             │ NO
              ▼                                 ▼
┌─────────────────────────┐       ┌─────────────────────────┐
│  Load existing registry │       │   Run auto-discovery    │
│  src/config/registries/ │       │   Save to registries/   │
│      {vin}.json         │       │      {vin}.json         │
└───────────┬─────────────┘       └───────────┬─────────────┘
            │                                 │
            └────────────────┬────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Show Module Selector UI                       │
│                  (populated from registry)                       │
└─────────────────────────────────────────────────────────────────┘
```

### Registry File Structure

**Location:** `src/config/registries/{vin}.json`

```json
{
    "vin": "1G1YY22G965104756",
    "vehicle_info": {
        "year": "2024",
        "make": "Chevrolet",
        "model": "Corvette"
    },
    "discovery_date": "2026-01-21T10:30:00",
    "discovery_status": "complete",
    "modules": {
        "Engine Control Module": {
            "has_data_selection": false,
            "data_items": [],
            "discovered": true
        },
        "Body Control Module": {
            "has_data_selection": true,
            "data_items": ["Door Status", "Window Position", "Lighting Control"],
            "discovered": true
        },
        "Transmission Control Module": {
            "has_data_selection": false,
            "data_items": [],
            "discovered": true
        }
    },
    "partial_discovery": {
        "last_module_index": 15,
        "total_modules": 42,
        "failed_modules": ["Some Module That Failed"]
    }
}
```

### Auto-Rediscovery Logic

```python
def get_module_info(self, module_name: str) -> dict:
    """Get module info, auto-discover if missing."""

    registry = self._load_registry()

    if module_name in registry["modules"]:
        module_info = registry["modules"][module_name]
        if module_info.get("discovered", False):
            return module_info

    # Module not discovered or missing - discover it now
    self._logger.info(f"Module '{module_name}' not in registry, discovering...")
    module_info = self._discover_single_module(module_name)

    # Update registry
    registry["modules"][module_name] = module_info
    self._save_registry(registry)

    return module_info
```

---

## Implementation Phases

### Phase 1: OCR Foundation + Tesseract Bundling
**Goal:** Add text recognition capability with bundled Tesseract

#### 1.1 Bundle Tesseract OCR
**Location:** `vendor/tesseract/`

```
vendor/
└── tesseract/
    ├── tesseract.exe         ← Main executable
    ├── tessdata/             ← Language data
    │   └── eng.traineddata   ← English training data
    └── *.dll                 ← Required DLLs
```

**Setup script:** `scripts/setup_tesseract.py`
- Downloads Tesseract portable version
- Extracts to vendor/tesseract/
- Configures pytesseract to use bundled version

#### 1.2 Create OCR Utility Module
**File:** `src/core/ocr.py`

```python
"""
OCR utilities using pytesseract with bundled Tesseract.
"""

import pytesseract
from pathlib import Path

# Configure pytesseract to use bundled Tesseract
TESSERACT_PATH = Path(__file__).parent.parent.parent / "vendor" / "tesseract" / "tesseract.exe"
if TESSERACT_PATH.exists():
    pytesseract.pytesseract.tesseract_cmd = str(TESSERACT_PATH)


def read_text_from_image(image, preprocess: bool = True) -> str:
    """Extract all text from image."""

def find_text_location(image, target_text: str) -> tuple[int, int, int, int] | None:
    """Find bounding box of text on screen. Returns (x, y, w, h) or None."""

def read_all_text_with_locations(image) -> list[dict]:
    """Read all text with their locations. Returns [{text, x, y, w, h}, ...]"""

def preprocess_for_ocr(image):
    """Preprocess image for better OCR accuracy."""
```

#### 1.3 Add OCR Methods to ImageDriver
**File:** `src/core/image_driver.py`

```python
# New Methods:
def read_screen_text(self) -> str:
    """Read all text currently on screen."""

def find_text_on_screen(self, text: str, timeout: float = 5) -> tuple[int, int] | None:
    """Find center coordinates of text on screen."""

def click_text(self, text: str, timeout: float = 5) -> bool:
    """Find text on screen and click its center."""

def read_menu_items(self) -> list[str]:
    """Read all menu item texts visible on screen."""
```

---

### Phase 2: Scroll Handling
**Goal:** Handle scrollable lists in GDS2

#### 2.1 Add Scroll Methods to ImageDriver
**File:** `src/core/image_driver.py`

```python
# New Methods:
def scroll_down(self, clicks: int = 3) -> None:
    """Scroll down using mouse wheel."""

def scroll_up(self, clicks: int = 3) -> None:
    """Scroll up using mouse wheel."""

def scroll_to_top(self) -> None:
    """Scroll to top of current list."""

def read_all_items_with_scroll(self) -> list[str]:
    """
    Read all items in a scrollable list.
    Scrolls down repeatedly until no new items appear.
    Returns deduplicated list of all items.
    """
```

#### 2.2 Scroll Detection Logic

```python
def read_all_items_with_scroll(self) -> list[str]:
    all_items = []
    previous_items = []
    max_scroll_attempts = 20

    # First, scroll to top
    self.scroll_to_top()
    time.sleep(0.5)

    for _ in range(max_scroll_attempts):
        # Read current visible items
        current_items = self.read_menu_items()

        # Add new items (deduplicate)
        for item in current_items:
            if item not in all_items:
                all_items.append(item)

        # Check if we've reached the end (no new items)
        if set(current_items) == set(previous_items):
            break

        previous_items = current_items

        # Scroll down
        self.scroll_down(clicks=3)
        time.sleep(0.5)

    return all_items
```

---

### Phase 3: New Image Locators
**Goal:** Add locators for Module Diagnostics flow

#### 3.1 Screenshots to Capture

| Image | Purpose | Notes |
|-------|---------|-------|
| `data_display.png` | "Data Display" menu option | List item in module options |
| `create_report.png` | Already exists | Reuse `ImageLoc.DTCPage.CREATE_REPORT_BTN` |

#### 3.2 Update Image Locators
**File:** `src/core/image_locators.py`

```python
class ModuleOptions:
    """Options shown after selecting a module."""
    DATA_DISPLAY = _img("list_items", "data_display", "Data Display option")

class DataDisplayPage:
    """Data Display screen elements."""
    # Reuse existing locator
    CREATE_REPORT_BTN = _img("buttons", "create_report", "Create Report button", 0.85)
```

---

### Phase 4: New Page Objects
**Goal:** Create page objects for Module Diagnostics flow

#### 4.1 Module List Page
**File:** `src/pages/module_list_page.py`

```python
class ModuleListPage(BasePage):
    """
    Page showing list of available modules.
    Uses OCR to read module names and scrolling for full list.
    """

    @property
    def name(self) -> str:
        return "Module List"

    def is_displayed(self) -> bool:
        """Check if at module list (look for known module or back button)."""

    def get_visible_modules(self) -> list[str]:
        """Get module names currently visible on screen (OCR)."""

    def get_all_modules(self) -> list[str]:
        """Get all module names including scrolled items."""

    def select_module(self, module_name: str) -> 'ModuleOptionsPage':
        """Scroll to module and click it."""

    def scroll_to_and_click(self, item_name: str) -> bool:
        """Scroll until item is visible, then click it."""
```

#### 4.2 Module Options Page
**File:** `src/pages/module_options_page.py`

```python
class ModuleOptionsPage(BasePage):
    """
    Page showing options for a module (DTCs, Data Display, etc.).
    """

    @property
    def name(self) -> str:
        return "Module Options"

    def is_displayed(self) -> bool:
        """Check if at module options (Data Display option visible)."""

    def click_data_display(self) -> 'DataSelectionPage | DataDisplayPage':
        """
        Click Data Display option.
        Returns DataSelectionPage if there are sub-items,
        or DataDisplayPage if it goes directly to data.
        """
```

#### 4.3 Data Selection Page
**File:** `src/pages/data_selection_page.py`

```python
class DataSelectionPage(BasePage):
    """
    Page for selecting specific data items (if module has multiple).
    Uses OCR for reading items, scrolling for full list.
    """

    @property
    def name(self) -> str:
        return "Data Selection"

    def is_displayed(self) -> bool:
        """Check if at data selection screen."""

    def get_all_items(self) -> list[str]:
        """Get all data item names (with scrolling)."""

    def select_item(self, item_name: str) -> 'DataDisplayPage':
        """Scroll to item and click it."""
```

#### 4.4 Data Display Page
**File:** `src/pages/data_display_page.py`

```python
class DataDisplayPage(BasePage):
    """
    Final page showing actual data values.
    Data extraction via Create Report → Parse HTML.
    """

    @property
    def name(self) -> str:
        return "Data Display"

    def is_displayed(self) -> bool:
        """Check if at data display (Create Report button visible)."""

    def wait_for_data_loaded(self, timeout: float = 60) -> 'DataDisplayPage':
        """Wait for data to load."""

    def create_report_and_parse(self, timeout: float = 30) -> dict:
        """
        Click Create Report, wait for HTML file, parse and return data.

        Returns:
            {
                "success": True/False,
                "data": {...parsed data...},
                "report_path": "path/to/report.html"
            }
        """

    def _find_latest_report(self) -> Path | None:
        """Find the most recent Data Display report file."""
```

#### 4.5 Update Diagnostics Menu Page
**File:** `src/pages/diagnostics_menu_page.py`

```python
def select_module_diagnostics(self) -> 'ModuleListPage':
    """Navigate to Module Diagnostics."""
    from .module_list_page import ModuleListPage

    self._logger.info("Selecting Module Diagnostics...")
    self.driver.click_element(ImageLoc.DiagnosticsMenu.MODULE_DIAGNOSTICS)
    time.sleep(0.5)

    return ModuleListPage(self.driver).wait_for_page(timeout=15)
```

---

### Phase 5: Discovery Workflow
**Goal:** Per-vehicle module discovery with partial result saving

#### 5.1 Discovery Workflow
**File:** `src/workflows/module_discovery.py`

```python
class ModuleDiscoveryWorkflow(BaseWorkflow):
    """
    Discovers all modules and their data items for a vehicle.
    Saves results to per-vehicle registry file.
    Supports partial discovery and resumption.
    """

    REGISTRY_DIR = Path(__file__).parent.parent / "config" / "registries"

    def execute(self, vci_device: str = "SM2 USB",
                resume: bool = True) -> dict:
        """
        Discover all modules for current vehicle.

        Args:
            vci_device: VCI device name
            resume: If True, resume from partial discovery

        Returns:
            Complete registry dictionary
        """

    def _get_vehicle_vin(self) -> str:
        """Get VIN from Vehicle Selection page."""

    def _load_or_create_registry(self, vin: str) -> dict:
        """Load existing registry or create new one."""

    def _save_registry(self, registry: dict) -> None:
        """Save registry to file (called after each module for safety)."""

    def _discover_single_module(self, module_name: str) -> dict:
        """
        Discover a single module's data items.

        Returns:
            {
                "has_data_selection": True/False,
                "data_items": [...],
                "discovered": True
            }
        """

    def _navigate_to_module_list(self) -> ModuleListPage:
        """Navigate from main menu to module list."""

    def _navigate_back_to_module_list(self) -> ModuleListPage:
        """Navigate back to module list from any depth."""
```

#### 5.2 Discovery Progress Tracking

```python
def execute(self, vci_device: str = "SM2 USB", resume: bool = True) -> dict:
    # Get VIN and load/create registry
    vin = self._get_vehicle_vin()
    registry = self._load_or_create_registry(vin)

    # Navigate to module list
    module_list = self._navigate_to_module_list()

    # Get all module names
    all_modules = module_list.get_all_modules()
    total = len(all_modules)

    # Update registry with module count
    registry["partial_discovery"]["total_modules"] = total

    # Discover each module
    for i, module_name in enumerate(all_modules):
        # Skip already discovered modules if resuming
        if resume and module_name in registry["modules"]:
            if registry["modules"][module_name].get("discovered", False):
                self._logger.info(f"[{i+1}/{total}] Skipping {module_name} (already discovered)")
                continue

        self._logger.info(f"[{i+1}/{total}] Discovering {module_name}...")

        try:
            module_info = self._discover_single_module(module_name)
            registry["modules"][module_name] = module_info
        except Exception as e:
            self._logger.error(f"Failed to discover {module_name}: {e}")
            registry["partial_discovery"]["failed_modules"].append(module_name)
            registry["modules"][module_name] = {"discovered": False, "error": str(e)}

        # Save after each module (partial results)
        registry["partial_discovery"]["last_module_index"] = i
        self._save_registry(registry)

        # Navigate back to module list
        self._navigate_back_to_module_list()

    # Mark discovery as complete
    registry["discovery_status"] = "complete"
    registry["discovery_date"] = datetime.now().isoformat()
    self._save_registry(registry)

    return registry
```

---

### Phase 6: Module Data Display Workflow
**Goal:** Navigate to module and extract data via HTML report

#### 6.1 Data Display Workflow
**File:** `src/workflows/module_data_display.py`

```python
class ModuleDataDisplayWorkflow(BaseWorkflow):
    """
    Workflow to view module data display.
    Uses pre-discovered registry, auto-discovers missing modules.
    Extracts data via Create Report → Parse HTML.
    """

    def __init__(self, driver):
        super().__init__(driver)
        self.registry = None
        self.current_vin = None

    def execute(self,
                vci_device: str = "SM2 USB",
                module_name: str = None,
                data_item: str = None) -> dict:
        """
        Navigate to module data display and extract data.

        Args:
            vci_device: VCI device name
            module_name: Module to select (required)
            data_item: Data item (required if module has_data_selection)

        Returns:
            {
                "success": True/False,
                "module": "...",
                "data_item": "..." or None,
                "data": {...extracted data...},
                "report_path": "...",
                "error": "..." (if failed)
            }
        """

    def ensure_registry_loaded(self, vci_device: str) -> dict:
        """
        Ensure registry is loaded for current vehicle.
        Auto-discovers if no registry exists.
        """
        vin = self._get_current_vin()

        if self.current_vin != vin or self.registry is None:
            self.registry = self._load_or_discover_registry(vin, vci_device)
            self.current_vin = vin

        return self.registry

    def _load_or_discover_registry(self, vin: str, vci_device: str) -> dict:
        """Load existing registry or run discovery."""
        registry_path = self._get_registry_path(vin)

        if registry_path.exists():
            self._logger.info(f"Loading existing registry for VIN {vin}")
            return json.loads(registry_path.read_text())
        else:
            self._logger.info(f"No registry for VIN {vin}, running discovery...")
            discovery = ModuleDiscoveryWorkflow(self.driver)
            return discovery.execute(vci_device=vci_device)

    def _auto_discover_module(self, module_name: str) -> dict:
        """Discover a single module that's missing from registry."""
        # ... discover and update registry

    def get_available_modules(self) -> list[str]:
        """Return list of all discovered modules."""

    def get_data_items(self, module_name: str) -> list[str]:
        """Return data items for a module."""

    def requires_data_selection(self, module_name: str) -> bool:
        """Check if module requires data item selection."""
```

#### 6.2 Update Report Parser
**File:** `src/utils/report_parser.py`

```python
class DataDisplayReportParser:
    """
    Parser for Data Display HTML reports.
    Similar to DTCReportParser but for data display format.
    """

    def parse_data_report(self, report_path: str) -> dict:
        """
        Parse Data Display HTML report.

        Returns:
            {
                "module": "Engine Control Module",
                "data_item": "Engine Data" or None,
                "parameters": [
                    {"name": "Engine RPM", "value": "750", "unit": "RPM"},
                    {"name": "Coolant Temp", "value": "92", "unit": "°C"},
                    ...
                ],
                "timestamp": "2026-01-21 10:30:00"
            }
        """
```

---

### Phase 7: User Interface
**Goal:** Simple Tkinter GUI with per-vehicle registry support

#### 7.1 Module Selector UI
**File:** `src/ui/module_selector.py`

```python
"""
Tkinter GUI for Module Data Display.

Features:
- Automatic registry loading based on vehicle
- Module search/selection
- Conditional data item selection
- Discovery progress display
- Data display results
"""

class ModuleSelectorUI:
    """
    Main application window.

    Layout:
    ┌─────────────────────────────────────────────────────────────┐
    │              GDS2 Module Data Display                       │
    ├─────────────────────────────────────────────────────────────┤
    │  Vehicle: 2024 Chevrolet Corvette (1G1YY22G965104756)       │
    │  Registry: Loaded (42 modules discovered)                   │
    ├─────────────────────────────────────────────────────────────┤
    │                                                             │
    │  VCI Device: [SM2 USB          ▼]                          │
    │                                                             │
    │  Module: [Search modules...     ]                          │
    │  ┌─────────────────────────────────────────────────────┐   │
    │  │ ○ Engine Control Module                             │   │
    │  │ ● Body Control Module                       ← selected  │
    │  │ ○ Transmission Control Module                       │   │
    │  │ ○ ... (scrollable)                                  │   │
    │  └─────────────────────────────────────────────────────┘   │
    │                                                             │
    │  Data Item: [Select data item...] ← Shows if needed        │
    │  ┌─────────────────────────────────────────────────────┐   │
    │  │ ○ Door Status                                       │   │
    │  │ ○ Window Position                                   │   │
    │  │ ○ Lighting Control                                  │   │
    │  └─────────────────────────────────────────────────────┘   │
    │                                                             │
    │  [Re-discover Modules]              [Get Data Display]     │
    │                                                             │
    ├─────────────────────────────────────────────────────────────┤
    │  Status: Ready                                              │
    │  Progress: ████████████████████████ 100%                   │
    └─────────────────────────────────────────────────────────────┘

    Results Window (popup):
    ┌─────────────────────────────────────────────────────────────┐
    │  Data Display Results - Body Control Module                 │
    ├─────────────────────────────────────────────────────────────┤
    │  Parameter            │ Value      │ Unit                   │
    │  ─────────────────────┼────────────┼──────────────────────  │
    │  Door Ajar - Driver   │ Closed     │ -                      │
    │  Door Ajar - Passenger│ Open       │ -                      │
    │  Window Position - DR │ 100        │ %                      │
    │  ...                                                        │
    ├─────────────────────────────────────────────────────────────┤
    │  [Save to File]  [Copy to Clipboard]  [Close]              │
    └─────────────────────────────────────────────────────────────┘
    """

    def __init__(self):
        self.root = tk.Tk()
        self.driver = None
        self.workflow = None
        self.registry = None

    def setup_ui(self):
        """Create all UI elements."""

    def connect_to_gds2(self):
        """Connect to GDS2 and load registry."""

    def on_module_selected(self, event):
        """Handle module selection - show/hide data items."""

    def on_search_changed(self, *args):
        """Filter module list based on search text."""

    def on_get_data_clicked(self):
        """Run workflow and display results."""

    def on_rediscover_clicked(self):
        """Re-run discovery for current vehicle."""

    def show_results(self, data: dict):
        """Show results in popup window."""

    def show_progress(self, message: str, percent: int):
        """Update progress bar and status."""

    def run(self):
        """Start the application."""
        self.root.mainloop()
```

#### 7.2 Entry Point Script
**File:** `scripts/run_module_data.py`

```python
#!/usr/bin/env python
"""
Entry point for GDS2 Module Data Display application.

Usage:
    python scripts/run_module_data.py

    # Or with CLI options:
    python scripts/run_module_data.py --discover-only
    python scripts/run_module_data.py --module "Engine Control Module"
"""

import argparse
from src.ui.module_selector import ModuleSelectorUI
from src.core.image_driver import ImageDriver
from src.workflows.module_discovery import ModuleDiscoveryWorkflow


def main():
    parser = argparse.ArgumentParser(description="GDS2 Module Data Display")
    parser.add_argument("--discover-only", action="store_true",
                        help="Only run discovery, don't show UI")
    parser.add_argument("--module", type=str,
                        help="Module name for CLI mode")
    parser.add_argument("--data-item", type=str,
                        help="Data item for CLI mode")
    args = parser.parse_args()

    if args.discover_only:
        # CLI discovery mode
        with ImageDriver() as driver:
            workflow = ModuleDiscoveryWorkflow(driver)
            result = workflow.execute()
            print(f"Discovered {len(result['modules'])} modules")
    elif args.module:
        # CLI data display mode
        with ImageDriver() as driver:
            workflow = ModuleDataDisplayWorkflow(driver)
            result = workflow.execute(
                module_name=args.module,
                data_item=args.data_item
            )
            print(json.dumps(result, indent=2))
    else:
        # GUI mode
        app = ModuleSelectorUI()
        app.run()


if __name__ == "__main__":
    main()
```

---

## File Changes Summary

### New Files (13 files)

| File | Purpose |
|------|---------|
| `src/core/ocr.py` | OCR utilities with bundled Tesseract |
| `src/pages/module_list_page.py` | Module selection with scrolling |
| `src/pages/module_options_page.py` | Module options page |
| `src/pages/data_selection_page.py` | Data item selection |
| `src/pages/data_display_page.py` | Data display with report creation |
| `src/workflows/module_discovery.py` | Per-vehicle discovery |
| `src/workflows/module_data_display.py` | Data display workflow |
| `src/config/registries/` | Directory for per-vehicle registries |
| `src/ui/__init__.py` | UI module init |
| `src/ui/module_selector.py` | Tkinter GUI |
| `scripts/run_module_data.py` | Entry point |
| `scripts/setup_tesseract.py` | Tesseract setup script |
| `vendor/tesseract/` | Bundled Tesseract OCR |

### Modified Files (6 files)

| File | Changes |
|------|---------|
| `src/core/image_driver.py` | Add OCR and scroll methods |
| `src/core/image_locators.py` | Add ModuleOptions, DataDisplayPage locators |
| `src/pages/__init__.py` | Export new pages |
| `src/pages/diagnostics_menu_page.py` | Implement select_module_diagnostics() |
| `src/workflows/__init__.py` | Export new workflows |
| `src/utils/report_parser.py` | Add DataDisplayReportParser |

### New Images to Capture

| Image | Screen | Notes |
|-------|--------|-------|
| `data_display.png` | Module options | "Data Display" menu item |

---

## Dependencies

### Python Packages (add to requirements-minimal.txt)

```
pytesseract>=0.3.10    # OCR wrapper
```

### Bundled Dependencies

```
vendor/
└── tesseract/
    ├── tesseract.exe
    ├── tessdata/
    │   └── eng.traineddata
    └── *.dll
```

**Tesseract Source:** https://github.com/UB-Mannheim/tesseract/wiki
- Download: tesseract-ocr-w64-setup-5.3.x.exe (portable version)
- Extract to vendor/tesseract/

---

## Implementation Order

```
Phase 1: OCR Foundation (Day 1-2)
├── Download and bundle Tesseract
├── Create scripts/setup_tesseract.py
├── Create src/core/ocr.py
├── Add OCR methods to ImageDriver
└── Test OCR on GDS2 screenshots

Phase 2: Scroll Handling (Day 3)
├── Add scroll methods to ImageDriver
├── Implement read_all_items_with_scroll()
└── Test scrolling in GDS2

Phase 3: Image Locators (Day 4)
├── Capture data_display.png
└── Update image_locators.py

Phase 4: Page Objects (Day 5-6)
├── Create module_list_page.py
├── Create module_options_page.py
├── Create data_selection_page.py
├── Create data_display_page.py
├── Update diagnostics_menu_page.py
└── Update pages/__init__.py

Phase 5: Discovery Workflow (Day 7-8)
├── Create config/registries/ directory
├── Create module_discovery.py
├── Implement per-vehicle registry
├── Implement partial discovery saving
└── Test full discovery

Phase 6: Data Display Workflow (Day 9)
├── Create module_data_display.py
├── Update report_parser.py
├── Implement auto-rediscovery
└── Test end-to-end

Phase 7: User Interface (Day 10-12)
├── Create src/ui/__init__.py
├── Create module_selector.py
├── Create scripts/run_module_data.py
├── Test GUI with discovery
└── Test GUI with data display

Phase 8: Polish & Documentation (Day 13-14)
├── Error handling improvements
├── Update CLAUDE.md
├── Create user documentation
└── End-to-end testing
```

---

## Testing Checklist

- [x] OCR module created (`src/core/ocr.py`)
- [x] Scroll methods added to ImageDriver
- [x] Discovery workflow saves partial results
- [x] Discovery supports resume from partial results
- [x] Per-vehicle registry system implemented
- [x] Auto-discovery on missing registry
- [x] Auto-rediscovery for missing modules
- [x] Data Display report parser created
- [x] Tkinter UI created (`src/ui/module_selector.py`)
- [x] UI supports module search/filter
- [x] UI shows/hides data items based on module
- [ ] End-to-end testing with real GDS2 (requires hardware)

---

## Implementation Status

### Completed Files (Phase 1-7)

| Phase | Files Created/Modified |
|-------|------------------------|
| 1 | `scripts/setup_tesseract.py`, `src/core/ocr.py`, `vendor/tesseract/README.txt` |
| 2 | `src/core/image_driver.py` (scroll methods) |
| 3 | `src/core/image_locators.py` (ModuleOptions, DataDisplayPage) |
| 4 | `src/pages/module_list_page.py`, `module_options_page.py`, `data_selection_page.py`, `data_display_page.py` |
| 5 | `src/workflows/module_discovery.py`, `src/config/registries/` |
| 6 | `src/workflows/module_data_display.py`, `src/utils/report_parser.py` (DataDisplayReportParser) |
| 7 | `src/ui/module_selector.py`, `scripts/run_module_data.py` |

### Entry Points

```bash
# Launch the Tkinter UI
python main.py module-data

# Or directly
python scripts/run_module_data.py

# With debug logging
python scripts/run_module_data.py --debug
```

### Prerequisites Before Testing

1. **Tesseract OCR**: Run `python scripts/setup_tesseract.py` or install Tesseract manually
2. **Screenshot Images**: Capture `data_display.png` from GDS2 Module Options screen
3. **GDS2 Running**: Application must be open at Main Menu
4. **VCI Connected**: SM2 USB or other VCI device connected

---

## Approval

✅ Plan approved by user (2026-01-21)
✅ Implementation completed (2026-01-21)

All 7 phases implemented. Ready for end-to-end testing with GDS2 hardware.
