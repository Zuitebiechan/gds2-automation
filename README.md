# GDS2 RPA Automation

Robotic Process Automation (RPA) for GM's GDS2 (General Motors Diagnostic System 2) vehicle diagnostic software.

## Overview

This project automates vehicle diagnostic workflows in GDS2 using a **Java Agent-based approach**:
- **Java Agent** for direct JVM data extraction (100ms interval, ~50ms latency)
- **Windows API** for Device Explorer dialog automation
- **Navigation Controller** for GDS2 page navigation
- **Flask Web UI** for visual interaction (recommended)

**Current Capabilities:**
1. **Data Viewer** - Simplified 3-dropdown UI: Device → Module → Data Category
2. **Real-time Monitoring** - High-frequency parameter streaming via SSE
3. **DTC Extraction** - Automatic DTC detection from Agent snapshots
4. **Smart Navigation** - Automatic back-navigation when user changes any selection

## Features

- **Simplified Data Viewer** - Three dropdowns: Device, Module, Data Category
- **Automatic Navigation** - System handles all GDS2 page transitions
- **High-frequency Monitoring** - 100ms collection interval via Java Agent
- **Real-time Streaming** - Parameter changes via Server-Sent Events (SSE)
- **DTC Auto-extraction** - DTCs automatically included in every snapshot
- **Device Switching** - Change VCI device from any page
- **VIN-based Caching** - Cache discovered data per vehicle

## Demo Results

### Real-time Monitoring
Successfully monitors parameters with **100ms interval** and **~50ms latency**.

### Supported Modules
- Engine Control Module → Engine Data, Misfire Data, Ignition Data, etc.
- Body Control Module → All data categories
- Any other module/data combination discovered automatically

## Requirements

- Windows 10/11
- Python 3.10+
- GDS2 installed and running with Java Agent
- VCI device connected to vehicle (e.g., SM2 USB)
- Flask, pywinauto (installed via requirements.txt)

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
1. Open GDS2 with Java Agent attached
2. Navigate to **Main Menu** (or any page with device connected)
3. Vehicle must be connected with data loaded

### Web UI (Recommended)

```bash
# Start Web UI
python main.py web

# With custom port
python main.py web --port 8000

# With debug mode
python main.py web --debug
```

Open http://localhost:8080 in your browser.

### Data Viewer Workflow

1. **Click Start** - System scans for devices or navigates to Module List if device already connected
2. **Select Device** - Choose VCI device from dropdown (e.g., SM2 USB)
3. **Select Module** - System navigates and discovers data categories
4. **Select Data Category** - System navigates to Data Display, starts monitoring

**Change Device/Module/Data anytime** - System automatically stops monitoring, navigates back, and resumes.

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/viewer/start` | POST | Initialize, get devices or modules if connected |
| `/api/viewer/connect` | POST | Connect device, get modules |
| `/api/viewer/change_device` | POST | Navigate to Device Explorer for device switch |
| `/api/viewer/select_module` | POST | Select module, get data categories |
| `/api/viewer/select_data` | POST | Select data, start monitoring |
| `/api/viewer/stop` | POST | Stop monitoring |
| `/api/viewer/state` | GET | Get current viewer state |
| `/api/stream/events` | GET | SSE endpoint for real-time data |
| `/api/agent/status` | GET | Check Agent availability |

### CLI Mode

```bash
# Run with default settings
python main.py demo

# Run with custom module and data category
python main.py demo --module "[K20] Engine Control Module" --data "Misfire Data"

# Run with verbose logging
python main.py demo -v

# Inspect GDS2 UI structure (for debugging)
python main.py inspect
```

### Python API

```python
from src.workflows import DataViewerWorkflow

# Create workflow instance
viewer = DataViewerWorkflow()

# Start and get devices or modules
result = viewer.start()

# Connect device and get modules
result = viewer.connect_device("SM2 USB")
modules = result["modules"]

# Select module and get data categories
result = viewer.select_module("[K20] Engine Control Module")
categories = result["data_categories"]

# Select data category and start monitoring
result = viewer.select_data_category("Engine Data")
```

## Project Structure

```
RPA_demo/
├── src/
│   ├── core/                # Core framework
│   │   ├── driver.py        # GDS2Driver - pywinauto wrapper
│   │   ├── locators.py      # UI element definitions
│   │   └── template_matcher.py  # Template matching utilities
│   ├── discovery/           # Discovery system
│   │   └── vehicle_mapping.py  # VehicleDiscovery & VehicleMapping
│   ├── native/              # Windows API integration
│   │   └── device_explorer.py  # Device Explorer automation
│   ├── navigation/          # Navigation system
│   │   └── controller.py    # NavigationController
│   ├── streaming/           # Real-time data streaming
│   │   ├── agent_navigator.py    # Java Agent communication
│   │   └── agent_data_collector.py  # Agent-based data collection
│   ├── workflows/           # Business workflows
│   │   ├── data_viewer.py   # PRIMARY - Simplified Data Viewer
│   │   ├── interactive_workflow.py  # Step-by-step navigation
│   │   └── read_data_display_agent.py  # CLI workflow
│   └── utils/               # Utilities
│       └── report_parser.py # HTML report parsing
├── templates/               # Web UI templates
│   └── index.html           # Main Web UI page
├── images/                  # Template images
│   ├── buttons/             # Button templates
│   └── devices/             # Device templates
├── mappings/                # Auto-generated discovery data
│   └── current_vehicle.json # Module/data category indices
├── scripts/                 # Utility scripts
├── docs/                    # Documentation
├── main.py                  # CLI entry point
└── app.py                   # Flask Web UI backend
```

## Architecture

```
┌─────────────────────────────────────────┐
│         Workflow Layer                  │  <- Business logic
│   DataViewerWorkflow (PRIMARY)          │
│   InteractiveWorkflow                   │
│   ReadDataDisplayAgentWorkflow          │
└─────────────────┬───────────────────────┘
                  │
┌─────────────────▼───────────────────────┐
│    Navigation Layer                     │  <- GDS2 page transitions
│   - NavigationController                │
│   - AgentNavigator (Java Agent comms)   │
│   - DeviceExplorerController (Win32)    │
└─────────────────┬───────────────────────┘
                  │
┌─────────────────▼───────────────────────┐
│      Data Collection Layer              │  <- Real-time monitoring
│   - AgentDataCollector (100ms)          │
│   - SSE Broadcasting                    │
│   - Parameter change detection          │
└─────────────────┬───────────────────────┘
                  │
┌─────────────────▼───────────────────────┐
│      Discovery & Mapping Layer          │  <- List enumeration
│   - VehicleDiscovery: enumerate lists   │
│   - VehicleMapping: JSON persistence    │
│   - On-demand module/data discovery     │
└─────────────────────────────────────────┘
```

### Key Design Decisions

1. **Java Agent** - Direct JVM data extraction
   - 100ms collection interval (vs 3000ms with HTML parsing)
   - ~50ms latency (vs ~1500ms waiting for file writes)
   - No UI interaction required

2. **Simplified 3-Dropdown UI** - User-friendly interface
   - Device → Module → Data Category
   - Automatic navigation in background
   - Smart back-navigation on selection change

3. **Windows API for Device Explorer** - Native dialog handling
   - Device Explorer is Win32, not JavaFX
   - DeviceExplorerController for reliable automation

## Performance

| Metric | Value |
|--------|-------|
| Collection Interval | 100ms |
| Latency | ~50ms |
| Device Selection | ~2 seconds |
| Module Discovery | ~3 seconds |
| Navigation | ~2-5 seconds per page |

## Documentation

- [CLAUDE.md](CLAUDE.md) - Comprehensive project documentation and architecture
- [docs/GDS2_CONTROL_MAPPING.md](docs/GDS2_CONTROL_MAPPING.md) - UI control reference
- [docs/WORKFLOW_DIAGRAM.md](docs/WORKFLOW_DIAGRAM.md) - Navigation flow diagrams

## Troubleshooting

**Agent not available:**
- Ensure GDS2 is started with Java Agent attached
- Check `%USERPROFILE%\gds2-data\latest.json` exists and is being updated

**Device Explorer not responding:**
- Device Explorer is a Win32 dialog, not JavaFX
- Ensure GDS2 window is in focus

**Navigation timeout:**
- Check GDS2 is not showing a popup dialog
- Ensure page has fully loaded before proceeding

## Roadmap

- [x] Java Agent integration (100ms monitoring)
- [x] Data Viewer with 3-dropdown UI
- [x] Device switching from any page
- [x] Real-time SSE streaming
- [x] DTC auto-extraction
- [x] VIN-based caching
- [ ] Clear Vehicle DTCs
- [ ] Multi-vehicle session handling
- [ ] Offline report generation

## License

MIT License

## Acknowledgments

- Built with [pywinauto](https://github.com/pywinauto/pywinauto) for Windows UI automation
- [Flask](https://flask.palletsprojects.com/) for Web UI backend
- UI element names from official GM GDS2 User Guide

---

**Last Updated:** 2026-02-03
**Status:** Production Ready - Simplified Data Viewer
