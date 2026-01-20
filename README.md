# GDS2 RPA Automation

Robotic Process Automation (RPA) for GM's GDS2 (General Motors Diagnostic System 2) vehicle diagnostic software.

## Overview

This project automates vehicle diagnostic workflows in GDS2 using Python and pywinauto. It uses the **Page Object pattern** with **fluent navigation** for clean, maintainable automation code.

**Current Capability:** Read all DTCs (Diagnostic Trouble Codes) from a connected vehicle and generate structured reports.

## Features

- **Page Object Pattern** - Each GDS2 screen is a Python class with actions that return the next page
- **Fluent Navigation** - Chain page transitions for readable workflow code
- **Centralized Locators** - All UI elements defined in one place (from official GDS2 User Guide)
- **HTML Report Parsing** - Extract structured DTC data from GDS2-generated reports
- **Screenshot Verification** - Optional OpenCV-based UI change detection
- **DPI-Aware** - Handles Windows display scaling automatically

## Demo Result

Successfully reads **31 DTCs** from a connected vehicle with structured output:

```python
{
    "success": True,
    "dtc_list": [
        {"code": "P0300", "description": "Random/Multiple Cylinder Misfire", "status": "Current", ...},
        {"code": "P0171", "description": "System Too Lean Bank 1", "status": "History", ...},
        # ... 31 DTCs total
    ],
    "vehicle_info": {"vin": "...", "year": "2020", "make": "Chevrolet", ...},
    "module_status": [{"name": "Engine Control Module", "status": "DTCs Stored", ...}, ...]
}
```

## Requirements

- Windows 10/11
- Python 3.10+
- GDS2 installed and running
- SM2 USB VCI device connected to vehicle

## Installation

```bash
# Clone the repository
git clone https://github.com/Zuitebiechan/gds2-automation.git
cd gds2-automation

# Create virtual environment
python -m venv venv
venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

## Usage

### Prerequisites
1. Open GDS2 and navigate to **Main Menu**
2. Ensure SM2 USB VCI is connected
3. Vehicle must be connected with data loaded

### Run the Demo

```bash
# Option 1: Using main.py
python main.py demo

# Option 2: Direct script
python scripts/run_demo.py
```

### Python API

```python
from src.core.driver import GDS2Driver
from src.workflows.read_vehicle_dtc import ReadVehicleDTCWorkflow

with GDS2Driver() as driver:
    driver.connect()
    workflow = ReadVehicleDTCWorkflow(driver)
    result = workflow.execute(vci_device="SM2 USB")

    print(f"Found {len(result['dtc_list'])} DTCs")
    for dtc in result['dtc_list']:
        print(f"  [{dtc['code']}] {dtc['description']}")
```

### Fluent Navigation Example

```python
from src.pages import MainMenuPage

# Clean, readable navigation chain
dtc_page = (MainMenuPage(driver)
    .click_diagnostics()           # -> DeviceExplorerPage
    .select_device("SM2 USB")      # -> VehicleSelectionPage
    .click_enter()                 # -> DiagnosticsMenuPage
    .select_vehicle_diagnostics()  # -> VehicleDiagnosticsPage
    .select_vehicle_dtc_info())    # -> DTCPage

# Now on DTC page
dtc_page.wait_for_data_loaded()
report_path = dtc_page.create_report_and_get_path()
```

## Project Structure

```
gds2-automation/
├── src/
│   ├── core/
│   │   ├── driver.py        # GDS2Driver - pywinauto wrapper
│   │   ├── locators.py      # All UI element definitions
│   │   └── exceptions.py    # Custom exceptions
│   ├── pages/               # Page Object classes
│   │   ├── main_menu_page.py
│   │   ├── dtc_page.py
│   │   └── ...
│   ├── workflows/           # Business workflows
│   │   └── read_vehicle_dtc.py
│   ├── vision/              # Screenshot comparison (optional)
│   └── utils/               # Report parsing utilities
├── scripts/                 # Entry point scripts
├── docs/                    # Documentation
├── res/                     # Resources (GDS2 User Guide)
└── main.py                  # CLI entry point
```

## Other Commands

```bash
# Inspect GDS2 UI structure (for debugging)
python main.py inspect

# Test GDS2 connection
python main.py test-connection
```

## Architecture

```
┌─────────────────────────────────────────┐
│         Workflow Layer                  │  <- Business logic
│   ReadVehicleDTCWorkflow                │
└─────────────────┬───────────────────────┘
                  │
┌─────────────────▼───────────────────────┐
│         Page Object Layer               │  <- UI abstraction
│   MainMenuPage, DTCPage, etc.           │
└─────────────────┬───────────────────────┘
                  │
┌─────────────────▼───────────────────────┐
│         Driver Layer                    │  <- Low-level automation
│   GDS2Driver (pywinauto)                │
└─────────────────────────────────────────┘
```

## Documentation

- [CLAUDE.md](docs/CLAUDE.md) - Detailed project documentation
- [GDS2_CONTROL_MAPPING.md](docs/GDS2_CONTROL_MAPPING.md) - UI control reference
- [WORKFLOW_DIAGRAM.md](docs/WORKFLOW_DIAGRAM.md) - Navigation flow diagrams

## Roadmap

- [x] Read Vehicle DTCs
- [ ] Clear Vehicle DTCs
- [ ] Read Module-specific DTCs
- [ ] Read PID/Live Data
- [ ] Read Freeze Frame Data
- [ ] VLM fallback for element finding
- [ ] OCR text verification

## License

MIT License

## Acknowledgments

- Built with [pywinauto](https://github.com/pywinauto/pywinauto) for Windows UI automation
- Optional [OpenCV](https://opencv.org/) for screenshot comparison
- UI element names from official GM GDS2 User Guide
