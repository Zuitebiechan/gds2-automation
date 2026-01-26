# GDS2 RPA Automation

Robotic Process Automation (RPA) for GM's GDS2 (General Motors Diagnostic System 2) vehicle diagnostic software.

## Overview

This project automates vehicle diagnostic workflows in GDS2 using a **hybrid automation approach**:
- **PyAutoGUI + OpenCV** for buttons and fixed elements (fast, reliable)
- **Keyboard Navigation** for dynamic list selection (DOWN + ENTER)
- **pywinauto Discovery** for automatic list enumeration and mapping

**Current Capabilities:**
1. Read Vehicle DTCs (all modules)
2. Read Data Display from any module/category (Engine Data, Misfire Data, etc.)

## Features

- **Hybrid Automation** - Combines template matching, keyboard navigation, and discovery
- **On-Demand Discovery** - Automatically discovers and maps module/data lists using pywinauto
- **Template Matching** - Fast button/device detection (~1 second vs ~60 seconds with VLM)
- **Keyboard Navigation** - Reliable list item selection (DOWN N times + ENTER)
- **JSON Persistence** - Stores discovered mappings for reuse across sessions
- **HTML Report Parsing** - Extract structured data from GDS2-generated reports
- **Auto Dialog Handling** - Automatically dismisses warning popups
- **DPI-Aware** - Handles Windows display scaling automatically

## Demo Results

### Read Vehicle DTCs
Successfully reads **31 DTCs** from a connected vehicle with structured output.

### Read Data Display
Successfully reads data from any module and category:
- Engine Control Module → Engine Data
- Engine Control Module → Ignition Data (index 16)
- Engine Control Module → Misfire Data (index 19)
- And any other module/data combination

```python
{
    "success": True,
    "report_path": "C:/Users/.../Temp/GDS 2/Data Display_LSGXE8351HD028615_01 26 2026.html"
}
```

## Requirements

- Windows 10/11
- Python 3.10+
- GDS2 installed and running
- VCI device connected to vehicle (e.g., SM2 USB)
- PyAutoGUI, OpenCV, pywinauto (installed via requirements.txt)

## Installation

```bash
# Clone the repository
git clone https://github.com/yourusername/RPA_demo.git
cd RPA_demo

# Create virtual environment
python -m venv venv
venv\Scripts\activate

# Install dependencies
pip install -r requirements-minimal.txt
```

## Usage

### Prerequisites
1. Open GDS2 and navigate to **Main Menu**
2. Ensure VCI device is connected (e.g., SM2 USB)
3. Vehicle must be connected with data loaded

### Quick Start

```bash
# Run with default settings (Engine Control Module → Engine Data)
python main.py demo

# Run with custom module and data category
python main.py demo --module "[K20] Engine Control Module" --data "Misfire Data"

# Run with verbose logging
python main.py demo --module "[K20] Engine Control Module" --data "Ignition Data" -v

# Run discovery utility (optional - happens automatically)
python main.py discover
```

### All Available Commands

```bash
# Main workflow - read data display
python main.py demo [--vci "SM2 USB"] [--module "..."] [--data "..."] [-v]

# Discovery utility - manually discover module/data lists
python main.py discover

# Inspect GDS2 UI structure (for debugging)
python main.py inspect

# Test GDS2 connection
python main.py test-connection
```

### Python API

```python
from src.workflows import ReadDataDisplayWorkflow

# Create workflow instance
workflow = ReadDataDisplayWorkflow(vehicle_id="current_vehicle")

# Execute workflow
result = workflow.execute(
    vci_device="SM2 USB",
    target_module="[K20] Engine Control Module",
    data_category="Misfire Data",
)

if result["success"]:
    print(f"Report created: {result['report_path']}")
else:
    print(f"Failed: {result.get('error', 'Unknown error')}")
```

### How It Works

1. **Button Clicking** - PyAutoGUI finds buttons using template matching from `images/buttons/`
2. **Device Selection** - Template matching finds device in Device Explorer from `images/devices/`
3. **List Navigation** - Keyboard navigation (DOWN + ENTER) selects items by index
4. **Discovery** - pywinauto enumerates list items on first run, saves to `mappings/current_vehicle.json`
5. **Report Generation** - Creates HTML report, parses data

### Adding Support for New Elements

**New Button:**
```bash
# 1. Take screenshot of button in GDS2
# 2. Crop to button area (with padding)
# 3. Save as: images/buttons/button_name.png
# 4. Use in code: self.click_button("button_name")
```

**New Device:**
```bash
# 1. Take screenshot of device in Device Explorer
# 2. Crop to device name area
# 3. Save as: images/devices/device_name.png (lowercase, underscores)
# 4. Use in code: self.click_device("device_name")
```

**New Module/Data Category:**
```bash
# Just run with the new name - discovery happens automatically
python main.py demo --module "New Module" --data "New Category"
```

## Project Structure

```
RPA_demo/
├── src/
│   ├── core/                # Core framework
│   │   ├── driver.py        # GDS2Driver - pywinauto wrapper
│   │   ├── locators.py      # UI element definitions (legacy)
│   │   └── exceptions.py    # Custom exceptions
│   ├── discovery/           # Discovery system
│   │   └── vehicle_mapping.py  # VehicleDiscovery & VehicleMapping
│   ├── pages/               # Page Object classes (legacy DTC workflow)
│   │   ├── main_menu_page.py
│   │   ├── dtc_page.py
│   │   └── ...
│   ├── workflows/           # Business workflows
│   │   ├── base_workflow.py       # PyAutoGUI+OpenCV base
│   │   ├── read_data_display.py   # Main workflow (keyboard+discovery)
│   │   └── read_vehicle_dtc.py    # Legacy DTC workflow
│   ├── vision/              # Vision features
│   │   ├── vlm_finder.py         # VLM fallback (optional)
│   │   └── screenshot_comparator.py
│   └── utils/               # Utilities
│       └── report_parser.py # HTML report parsing
├── images/                  # Template images
│   ├── buttons/             # Button templates (PyAutoGUI)
│   └── devices/             # Device templates (PyAutoGUI)
├── mappings/                # Auto-generated discovery data
│   └── current_vehicle.json # Module/data category indices
├── scripts/                 # Utility scripts
│   ├── run_demo.py          # Legacy demo script
│   ├── run_discovery.py     # Discovery utility
│   └── inspect_gds2.py      # UI inspection
├── docs/                    # Documentation
├── res/                     # Resources (GDS2 User Guide)
└── main.py                  # CLI entry point
```

## Architecture

```
┌─────────────────────────────────────────┐
│         Workflow Layer                  │  <- Business logic
│   ReadDataDisplayWorkflow               │
│   ReadVehicleDTCWorkflow (legacy)       │
└─────────────────┬───────────────────────┘
                  │
┌─────────────────▼───────────────────────┐
│    Hybrid Automation Layer              │  <- PyAutoGUI + Keyboard
│   - PyAutoGUI+OpenCV: buttons/devices   │
│   - Keyboard: DOWN+ENTER for lists      │
│   - Template matching: fast detection   │
└─────────────────┬───────────────────────┘
                  │
┌─────────────────▼───────────────────────┐
│      Discovery & Mapping Layer          │  <- pywinauto discovery
│   - VehicleDiscovery: enumerate lists   │
│   - VehicleMapping: JSON persistence    │
│   - On-demand module/data discovery     │
└─────────────────┬───────────────────────┘
                  │
┌─────────────────▼───────────────────────┐
│         Driver Layer                    │  <- Low-level automation
│   - pywinauto: list enumeration         │
│   - PyAutoGUI: mouse/keyboard control   │
│   - OpenCV: template matching           │
└─────────────────────────────────────────┘
```

### Key Design Decisions

1. **Hybrid Approach** - Different tools for different tasks
   - PyAutoGUI+OpenCV: Fast button/device detection (~1 second)
   - Keyboard navigation: Reliable list selection (DOWN+ENTER)
   - pywinauto: List item enumeration and discovery

2. **On-Demand Discovery** - Discover as needed
   - Module list: discovered once, reused forever
   - Data categories: discovered per-module as needed
   - JSON persistence: saved to `mappings/current_vehicle.json`

3. **Template Matching** - Fast and reliable
   - Device selection: ~1 second (vs ~60 seconds with VLM)
   - Button detection: <1 second
   - Grayscale matching with confidence threshold

## Documentation

- [CLAUDE.md](CLAUDE.md) - Comprehensive project documentation and architecture
- [SCROLLING_SUPPORT.md](docs/SCROLLING_SUPPORT.md) - Scrolling implementation notes
- [GDS2_CONTROL_MAPPING.md](docs/GDS2_CONTROL_MAPPING.md) - UI control reference
- [WORKFLOW_DIAGRAM.md](docs/WORKFLOW_DIAGRAM.md) - Navigation flow diagrams

## Performance

- Device selection: ~1 second (template matching)
- Module/data discovery: ~2-3 seconds per list (first time only)
- Button clicking: <1 second (PyAutoGUI)
- List navigation: ~50ms per item (keyboard)
- Total workflow: ~15-30 seconds depending on discovery needs

## Troubleshooting

**Template matching not working:**
- Check image exists in `images/buttons/` or `images/devices/`
- Try lower confidence: `self.click_button("name", confidence=0.7)`
- Ensure GDS2 window is in focus and fully visible

**Discovery not finding items:**
- Ensure GDS2 window title contains "GDS 2"
- Check that list page is fully loaded
- Delete `mappings/current_vehicle.json` and re-discover

**Keyboard navigation selecting wrong item:**
- Check `mappings/current_vehicle.json` for correct indices
- Delete JSON file if vehicle changed
- Ensure focus is on list (may need to click list area first)

## Roadmap

- [x] Read Vehicle DTCs (all modules)
- [x] Read Data Display (any module/category)
- [x] On-demand discovery system
- [x] Template matching for performance
- [x] Keyboard navigation for lists
- [x] Automatic dialog handling
- [ ] Clear Vehicle DTCs
- [ ] Read Module-specific DTCs
- [ ] VLM fallback for unknown elements
- [ ] OCR text verification
- [ ] Multi-vehicle session handling

## License

MIT License

## Acknowledgments

- Built with [pywinauto](https://github.com/pywinauto/pywinauto) for Windows UI automation and list discovery
- [PyAutoGUI](https://pyautogui.readthedocs.io/) for mouse/keyboard control
- [OpenCV](https://opencv.org/) for template matching and image processing
- UI element names from official GM GDS2 User Guide

---

**Last Updated:** 2026-01-26
**Status:** Production Ready
