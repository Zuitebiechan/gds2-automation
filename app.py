"""
GDS2 Automation Web UI - Flask Version (Agent-based)

Simple web interface for GDS2 vehicle diagnostics automation.
Uses Java Agent for all navigation - no PyAutoGUI dependency.
"""

from flask import Flask, render_template, jsonify, request, Response
from flask_cors import CORS
import logging
import re
import time
import json
import queue
import threading
from pathlib import Path
from enum import Enum
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, asdict

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler('gds2_web.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)


# =============================================================================
# State Machine
# =============================================================================

class GDS2State(Enum):
    """GDS2 application state."""
    UNKNOWN = "unknown"
    MAIN_MENU = "main_menu"
    DIAGNOSTICS_MENU = "diagnostics_menu"
    MODULE_LIST = "module_list"
    MODULE_SUBMENU = "module_submenu"
    DATA_LIST = "data_list"
    DATA_DISPLAY = "data_display"


@dataclass
class AppState:
    """Application state container."""
    gds2_state: str = GDS2State.MAIN_MENU.value
    current_module: Optional[str] = None
    current_data_category: Optional[str] = None
    data_list_focus_index: int = 0


# Global state
app_state = AppState()


# =============================================================================
# GDS2 Controller (Agent-based)
# =============================================================================

class GDS2Controller:
    """Controller for GDS2 automation using Java Agent."""

    def __init__(self):
        """Initialize controller."""
        self._mapping = None
        self._nav = None

    @property
    def mapping(self):
        """Lazy-load vehicle mapping."""
        if self._mapping is None:
            from src.discovery import VehicleMapping
            self._mapping = VehicleMapping()
        return self._mapping

    @property
    def nav(self):
        """Lazy-load Agent Navigator."""
        if self._nav is None:
            from src.streaming import AgentNavigator
            self._nav = AgentNavigator(timeout_sec=15.0)
        return self._nav

    def check_agent(self) -> bool:
        """Check if Java Agent is available."""
        return self.nav.check_agent()

    def get_module_list(self, vehicle_id: str = "current_vehicle") -> List[str]:
        """Get list of available modules from cache."""
        mapping = self.mapping.load_mapping(vehicle_id)
        if mapping and "modules" in mapping:
            return list(mapping["modules"].keys())
        return []

    def get_data_categories(self, vehicle_id: str, module_name: str) -> List[str]:
        """Get data categories for a module from cache."""
        mapping = self.mapping.load_mapping(vehicle_id)
        if not mapping:
            return []

        modules = mapping.get("modules", {})
        module_info = modules.get(module_name, {})
        data_categories = module_info.get("data_categories", {})
        return list(data_categories.keys())

    def _wait_for_list(self, list_index: int = 0, max_attempts: int = 15) -> List[str]:
        """Wait for list items to load."""
        for attempt in range(max_attempts):
            items = self.nav.get_list_items(list_index)
            if items:
                return items
            logger.info(f"  Waiting for list to load... ({attempt+1})")
            time.sleep(1)
        return []

    def _dismiss_warning_dialog(self):
        """Dismiss warning dialog if present."""
        for _ in range(3):
            buttons = self.nav.get_buttons()
            button_texts = [b.get('text') for b in buttons]
            if "OK" in button_texts:
                logger.info("  Warning dialog detected, clicking OK...")
                result = self.nav.click_button("OK")
                if result.get('success'):
                    time.sleep(1)
                    return
            time.sleep(0.3)

    def discover_modules(self, vehicle_id: str = "current_vehicle") -> List[str]:
        """
        Discover all modules by navigating to Module List page.
        Uses Java Agent for navigation.
        """
        from src.native import handle_device_explorer

        try:
            logger.info("=== Discovering Modules (Agent) ===")

            # Step 1: Click Diagnostics
            logger.info("Step 1: Clicking Diagnostics...")
            result = self.nav.click_button("Diagnostics")
            if not result.get('success'):
                logger.error(f"Failed to click Diagnostics: {result.get('message')}")
                return []
            time.sleep(3)

            # Step 2: Handle Device Explorer (Windows API)
            logger.info("Step 2: Handling Device Explorer...")
            handle_device_explorer(device_name="SM2 USB", timeout=5.0)
            time.sleep(3)

            # Step 3: Click Enter for vehicle selection
            logger.info("Step 3: Clicking Enter...")
            for _ in range(10):
                buttons = self.nav.get_buttons()
                if any(b.get('text') == 'Enter' for b in buttons):
                    result = self.nav.click_button("Enter")
                    if result.get('success'):
                        break
                time.sleep(0.5)
            time.sleep(3)

            # Step 4: Handle warning dialog
            self._dismiss_warning_dialog()

            # Step 5: Select Module Diagnostics
            logger.info("Step 4: Selecting Module Diagnostics...")
            items = self._wait_for_list(0)
            logger.info(f"  Menu items: {items}")

            for i, item in enumerate(items):
                if "Module Diagnostics" in item:
                    result = self.nav.select_list_item(0, i, double_click=True)
                    if result.get('success'):
                        logger.info(f"  [OK] Selected Module Diagnostics at index {i}")
                        break
            time.sleep(3)

            # Step 6: Discover modules using Agent
            logger.info("Step 5: Enumerating modules...")
            items = self._wait_for_list(0)
            logger.info(f"  Found {len(items)} modules")

            # Save to mapping
            module_indices = {name: idx for idx, name in enumerate(items)}
            self.mapping.update_module_list(vehicle_id, module_indices)

            logger.info("=== Module Discovery Complete ===")
            return items

        except Exception as e:
            logger.exception(f"Module discovery failed: {e}")
            return []

    def discover_data_categories(self, vehicle_id: str, module_name: str) -> List[str]:
        """Discover data categories using Agent."""
        logger.info(f"Discovering data categories for {module_name}...")

        items = self._wait_for_list(0)
        logger.info(f"  Found {len(items)} data categories")

        # Save to mapping
        data_categories = {name: idx for idx, name in enumerate(items)}
        self.mapping.update_data_categories(vehicle_id, module_name, data_categories)

        return items

    def navigate_to_module_list_from_main_menu(self) -> bool:
        """Navigate from Main Menu to Module List page using Agent."""
        from src.native import handle_device_explorer

        try:
            logger.info("=== Navigating to Module List (Agent) ===")

            # Step 1: Click Diagnostics
            logger.info("Step 1: Clicking Diagnostics...")
            result = self.nav.click_button("Diagnostics")
            if not result.get('success'):
                logger.error(f"Failed: {result.get('message')}")
                return False
            time.sleep(3)

            # Step 2: Handle Device Explorer
            logger.info("Step 2: Handling Device Explorer...")
            handle_device_explorer(device_name="SM2 USB", timeout=5.0)
            time.sleep(3)

            # Step 3: Click Enter
            logger.info("Step 3: Clicking Enter...")
            for _ in range(10):
                buttons = self.nav.get_buttons()
                if any(b.get('text') == 'Enter' for b in buttons):
                    result = self.nav.click_button("Enter")
                    if result.get('success'):
                        break
                time.sleep(0.5)
            time.sleep(3)

            # Handle warning dialog
            self._dismiss_warning_dialog()

            # Step 4: Select Module Diagnostics
            logger.info("Step 4: Selecting Module Diagnostics...")
            items = self._wait_for_list(0)
            for i, item in enumerate(items):
                if "Module Diagnostics" in item:
                    result = self.nav.select_list_item(0, i, double_click=True)
                    if result.get('success'):
                        logger.info(f"  [OK] Selected Module Diagnostics")
                        break
            time.sleep(3)

            logger.info("=== Navigation to Module List Complete ===")
            return True

        except Exception as e:
            logger.exception(f"Navigation failed: {e}")
            return False

    def navigate_to_data_list_from_module_list(self, module_name: str) -> bool:
        """Navigate from Module List to Data List using Agent."""
        try:
            logger.info(f"=== Navigating to Data List (Agent) ===")
            logger.info(f"Target module: {module_name}")

            # Step 1: Select module
            items = self._wait_for_list(0)
            logger.info(f"  Found {len(items)} modules")

            module_index = None
            for i, item in enumerate(items):
                if module_name in item or item in module_name:
                    module_index = i
                    break

            if module_index is None:
                # Try partial match
                module_key = module_name.split("]")[-1].strip() if "]" in module_name else module_name
                for i, item in enumerate(items):
                    if module_key in item:
                        module_index = i
                        break

            if module_index is None:
                logger.error(f"Module '{module_name}' not found")
                return False

            logger.info(f"Step 1: Selecting module at index {module_index}...")
            result = self.nav.select_list_item(0, module_index, double_click=True)
            if not result.get('success'):
                logger.error(f"Failed to select module: {result.get('message')}")
                return False
            time.sleep(3)

            # Step 2: Select Data Display from submenu
            logger.info("Step 2: Selecting Data Display...")
            items = self._wait_for_list(0)
            logger.info(f"  Submenu items: {items}")

            for i, item in enumerate(items):
                if "Data Display" in item:
                    result = self.nav.select_list_item(0, i, double_click=True)
                    if result.get('success'):
                        logger.info(f"  [OK] Selected Data Display at index {i}")
                        break
            time.sleep(3)

            # Handle warning dialog
            self._dismiss_warning_dialog()

            logger.info("=== Navigation to Data List Complete ===")
            return True

        except Exception as e:
            logger.exception(f"Navigation failed: {e}")
            return False

    def navigate_to_data_list(self, module_name: str) -> bool:
        """Navigate from Main Menu to Data List using Agent."""
        if not self.navigate_to_module_list_from_main_menu():
            return False
        return self.navigate_to_data_list_from_module_list(module_name)

    def navigate_to_data_display(self, module_name: str, data_category: str, current_focus: int) -> Optional[str]:
        """Navigate from Data List to Data Display and create report using Agent."""
        try:
            # Get target index
            target_index = self.mapping.get_data_category_index("current_vehicle", module_name, data_category)
            if target_index is None:
                # Try to find it in current list
                items = self.nav.get_list_items(0)
                for i, item in enumerate(items):
                    if data_category in item or item in data_category:
                        target_index = i
                        break

            if target_index is None:
                logger.error(f"Data category '{data_category}' not found")
                return None

            logger.info(f"Selecting data category '{data_category}' at index {target_index}...")
            result = self.nav.select_list_item(0, target_index, double_click=True)
            if not result.get('success'):
                logger.error(f"Failed to select data category: {result.get('message')}")
                return None
            time.sleep(5)

            # Click Create Report
            logger.info("Clicking Create Report...")
            for _ in range(30):
                buttons = self.nav.get_buttons()
                if any(b.get('text') == 'Create Report' for b in buttons):
                    result = self.nav.click_button("Create Report")
                    if result.get('success'):
                        logger.info("  [OK] Clicked Create Report")
                        break
                time.sleep(1)
            time.sleep(2)

            # Find latest report
            report_dir = Path.home() / "AppData" / "Local" / "Temp" / "GDS 2"
            if report_dir.exists():
                reports = list(report_dir.glob("Data Display_*.html"))
                if reports:
                    latest_report = max(reports, key=lambda p: p.stat().st_mtime)
                    return str(latest_report)

            return None

        except Exception as e:
            logger.exception(f"Navigation failed: {e}")
            return None

    def click_back_button(self) -> bool:
        """Click Back button using Agent."""
        try:
            result = self.nav.click_button("Back")
            if result.get('success'):
                time.sleep(1.5)
                return True
            logger.error(f"Failed to click Back: {result.get('message')}")
            return False
        except Exception as e:
            logger.exception(f"Back button failed: {e}")
            return False

    def click_home_button(self) -> bool:
        """Click Home button using Agent to return to Main Menu."""
        try:
            result = self.nav.click_button("Home")
            if result.get('success'):
                time.sleep(2)
                return True
            return False
        except Exception as e:
            logger.exception(f"Home button failed: {e}")
            return False

    def parse_report(self, report_path: str) -> Dict[str, Any]:
        """Parse HTML report."""
        result = {"vehicle_info": {}, "data_items": []}

        try:
            with open(report_path, 'r', encoding='iso-8859-1') as f:
                html_content = f.read()

            vin_match = re.search(r'Vehicle Identification Number \(VIN\)</td><td>([^<]+)</td>', html_content)
            if vin_match:
                result["vehicle_info"]["vin"] = vin_match.group(1)

            make_match = re.search(r'<td>Make</td><td>([^<]+)</td>', html_content)
            if make_match:
                result["vehicle_info"]["make"] = make_match.group(1)

            model_match = re.search(r'<td>Model</td><td>([^<]+)</td>', html_content)
            if model_match:
                result["vehicle_info"]["model"] = model_match.group(1)

            year_match = re.search(r'<td>Model Year</td><td>([^<]+)</td>', html_content)
            if year_match:
                result["vehicle_info"]["year"] = year_match.group(1)

            data_pattern = r'<tr><td>([^<]+)</td><td>([^<]*)</td><td>([^<]*)</td></tr>'
            matches = re.findall(data_pattern, html_content)

            for match in matches:
                parameter, value, units = match
                if parameter not in ["Parameter", "Control Module", "DTC Type"]:
                    result["data_items"].append({
                        "parameter": parameter.strip(),
                        "value": value.strip(),
                        "units": units.strip(),
                    })

        except Exception as e:
            logger.error(f"Error parsing report: {e}")

        return result


# Global controller
controller = GDS2Controller()


# =============================================================================
# Flask Routes
# =============================================================================

@app.route('/test')
def test():
    """Simple test endpoint."""
    return "<h1>Flask is working!</h1><p>Agent-based GDS2 Automation</p>"


@app.route('/')
def index():
    """Render main page."""
    return render_template('index.html')


@app.route('/api/agent/check')
def check_agent():
    """Check if Java Agent is available."""
    available = controller.check_agent()
    return jsonify({
        "available": available,
        "message": "Agent connected" if available else "Agent not available. Start GDS2 with agent."
    })


@app.route('/api/fetch_modules', methods=['POST'])
def fetch_modules():
    """
    Step 1: Fetch all modules.
    Assumes GDS2 is at Main Menu.
    """
    try:
        logger.info("=== Step 1: Fetch Modules ===")

        # Check agent first
        if not controller.check_agent():
            return jsonify({"error": "Java Agent not available. Start GDS2 with agent."}), 400

        # Check if we have cached modules
        cached_modules = controller.get_module_list()

        if cached_modules:
            logger.info(f"Found {len(cached_modules)} cached modules. Navigating...")
            success = controller.navigate_to_module_list_from_main_menu()
            if not success:
                return jsonify({"error": "Failed to navigate to Module List"}), 500

            app_state.gds2_state = GDS2State.MODULE_LIST.value
            app_state.current_module = None
            app_state.current_data_category = None

            return jsonify({
                "success": True,
                "modules": cached_modules,
                "from_cache": True,
                "state": asdict(app_state)
            })
        else:
            logger.info("No cached modules. Discovering...")
            modules = controller.discover_modules("current_vehicle")

            if modules:
                app_state.gds2_state = GDS2State.MODULE_LIST.value
                app_state.current_module = None
                app_state.current_data_category = None

                return jsonify({
                    "success": True,
                    "modules": modules,
                    "from_cache": False,
                    "state": asdict(app_state)
                })
            else:
                return jsonify({"error": "Failed to discover modules"}), 500

    except Exception as e:
        logger.exception("Fetch modules failed")
        return jsonify({"error": str(e)}), 500


@app.route('/api/fetch_categories', methods=['POST'])
def fetch_categories():
    """
    Step 2: Fetch data categories for selected module.
    Assumes GDS2 is at Module List.
    """
    data = request.json
    module_name = data.get('module')

    if not module_name:
        return jsonify({"error": "Module name required"}), 400

    try:
        logger.info(f"=== Step 2: Fetch Categories for {module_name} ===")

        cached_categories = controller.get_data_categories("current_vehicle", module_name)

        # Navigate from Module List to Data List
        success = controller.navigate_to_data_list_from_module_list(module_name)
        if not success:
            return jsonify({"error": "Failed to navigate to data list"}), 500

        if cached_categories:
            logger.info(f"Using {len(cached_categories)} cached categories")
            app_state.gds2_state = GDS2State.DATA_LIST.value
            app_state.current_module = module_name
            app_state.data_list_focus_index = 0

            return jsonify({
                "success": True,
                "data_categories": cached_categories,
                "from_cache": True,
                "state": asdict(app_state)
            })
        else:
            logger.info("Discovering data categories...")
            categories = controller.discover_data_categories("current_vehicle", module_name)

            if not categories:
                return jsonify({"error": "Failed to discover data categories"}), 500

            app_state.gds2_state = GDS2State.DATA_LIST.value
            app_state.current_module = module_name
            app_state.data_list_focus_index = 0

            return jsonify({
                "success": True,
                "data_categories": categories,
                "from_cache": False,
                "state": asdict(app_state)
            })

    except Exception as e:
        logger.exception("Fetch categories failed")
        return jsonify({"error": str(e)}), 500


@app.route('/api/search_data', methods=['POST'])
def search_data():
    """
    Step 3: Search data for selected category.
    Assumes GDS2 is at Data List.
    """
    data = request.json
    module_name = data.get('module')
    data_category = data.get('data_category')

    if not module_name or not data_category:
        return jsonify({"error": "Module and data category required"}), 400

    try:
        logger.info(f"=== Search: {data_category} ===")

        # Navigate to data display
        report_path = controller.navigate_to_data_display(
            module_name, data_category, app_state.data_list_focus_index
        )

        if not report_path:
            return jsonify({"error": "Failed to create report"}), 500

        # Parse report
        report_data = controller.parse_report(report_path)

        # Parse for DTCs
        from src.utils.report_parser import GDS2ReportParser
        dtc_parser = GDS2ReportParser()
        dtc_data = dtc_parser.parse_dtc_report(report_path)

        # Click Back to return to Data List
        logger.info("Clicking Back...")
        controller.click_back_button()

        # Update state
        target_index = controller.mapping.get_data_category_index("current_vehicle", module_name, data_category) or 0
        app_state.gds2_state = GDS2State.DATA_LIST.value
        app_state.current_data_category = data_category
        app_state.data_list_focus_index = target_index

        return jsonify({
            "success": True,
            "report_path": report_path,
            "report_data": report_data,
            "vehicle_info": dtc_data.get("vehicle_info", {}),
            "dtc_list": dtc_data.get("dtc_list", []),
            "module_status": dtc_data.get("module_status", []),
            "state": asdict(app_state)
        })

    except Exception as e:
        logger.exception("Search data failed")
        return jsonify({"error": str(e)}), 500


@app.route('/api/get_dtcs', methods=['POST'])
def get_dtcs():
    """Get DTCs from Vehicle DTC Information category."""
    try:
        logger.info("=== Get DTCs ===")

        if not app_state.current_module:
            return jsonify({"error": "No module selected"}), 400

        module_name = app_state.current_module

        # Find DTC category
        dtc_category = "Vehicle DTC Information"
        target_index = controller.mapping.get_data_category_index("current_vehicle", module_name, dtc_category)

        if target_index is None:
            for alt in ["Vehicle DTC and ID Information", "DTC Information"]:
                target_index = controller.mapping.get_data_category_index("current_vehicle", module_name, alt)
                if target_index is not None:
                    dtc_category = alt
                    break

        if target_index is None:
            return jsonify({"error": "DTC category not found"}), 400

        report_path = controller.navigate_to_data_display(
            module_name, dtc_category, app_state.data_list_focus_index
        )

        if not report_path:
            return jsonify({"error": "Failed to create DTC report"}), 500

        from src.utils.report_parser import GDS2ReportParser
        parser = GDS2ReportParser()
        dtc_data = parser.parse_dtc_report(report_path)

        controller.click_back_button()

        app_state.gds2_state = GDS2State.DATA_LIST.value
        app_state.data_list_focus_index = target_index

        return jsonify({
            "success": True,
            "vehicle_info": dtc_data.get("vehicle_info", {}),
            "dtc_list": dtc_data.get("dtc_list", []),
            "module_status": dtc_data.get("module_status", []),
            "state": asdict(app_state)
        })

    except Exception as e:
        logger.exception("Get DTCs failed")
        return jsonify({"error": str(e)}), 500


@app.route('/api/modules')
def get_modules():
    """Get list of modules from cache."""
    modules = controller.get_module_list()
    return jsonify({"modules": modules})


@app.route('/api/data_categories')
def get_data_categories():
    """Get data categories for a module from cache."""
    module_name = request.args.get('module')
    if not module_name:
        return jsonify({"error": "Module name required"}), 400

    categories = controller.get_data_categories("current_vehicle", module_name)
    return jsonify({"data_categories": categories})


@app.route('/api/back', methods=['POST'])
def back():
    """Click back button."""
    try:
        success = controller.click_back_button()
        if success:
            # Update state based on current state
            if app_state.gds2_state == GDS2State.DATA_DISPLAY.value:
                app_state.gds2_state = GDS2State.DATA_LIST.value
            elif app_state.gds2_state == GDS2State.DATA_LIST.value:
                app_state.gds2_state = GDS2State.MODULE_SUBMENU.value
            elif app_state.gds2_state == GDS2State.MODULE_SUBMENU.value:
                app_state.gds2_state = GDS2State.MODULE_LIST.value

            return jsonify({"success": True, "state": asdict(app_state)})
        return jsonify({"error": "Failed to click back"}), 500

    except Exception as e:
        logger.exception("Back button failed")
        return jsonify({"error": str(e)}), 500


@app.route('/api/home', methods=['POST'])
def home():
    """Click Home button to return to Main Menu."""
    try:
        success = controller.click_home_button()
        if success:
            app_state.gds2_state = GDS2State.MAIN_MENU.value
            app_state.current_module = None
            app_state.current_data_category = None
            app_state.data_list_focus_index = 0
            return jsonify({"success": True, "state": asdict(app_state)})
        return jsonify({"error": "Failed to click Home"}), 500

    except Exception as e:
        logger.exception("Home button failed")
        return jsonify({"error": str(e)}), 500


@app.route('/api/state')
def get_state():
    """Get current application state."""
    return jsonify(asdict(app_state))


@app.route('/api/reset', methods=['POST'])
def reset_state():
    """Reset state to Main Menu."""
    try:
        app_state.gds2_state = GDS2State.MAIN_MENU.value
        app_state.current_module = None
        app_state.current_data_category = None
        app_state.data_list_focus_index = 0

        logger.info("State reset to MAIN_MENU")
        return jsonify({"success": True, "state": asdict(app_state)})

    except Exception as e:
        logger.exception("Reset failed")
        return jsonify({"error": str(e)}), 500


# =============================================================================
# Agent-based Data Streaming (High-frequency)
# =============================================================================

agent_collector = None
agent_clients: List[queue.Queue] = []
agent_lock = threading.Lock()


def broadcast_to_agent_clients(event_type: str, data: dict):
    """Broadcast data to all connected Agent SSE clients."""
    message = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
    with agent_lock:
        dead_clients = []
        for client_queue in agent_clients:
            try:
                client_queue.put_nowait(message)
            except queue.Full:
                dead_clients.append(client_queue)
        for dead in dead_clients:
            agent_clients.remove(dead)


def on_agent_snapshot(snapshot):
    """Callback when Agent produces a new snapshot."""
    broadcast_to_agent_clients("snapshot", {
        "type": "snapshot",
        "extraction_count": snapshot.extraction_count,
        "extraction_duration_ms": snapshot.extraction_duration_ms,
        "page_context": snapshot.page_context,
        "param_count": len(snapshot.parameters),
        "dtc_count": len(snapshot.dtcs),
        "parameters": snapshot.parameters,
        "dtcs": [d.to_dict() for d in snapshot.dtcs],
        "timestamp": time.time(),
    })


def on_agent_param_change(changes):
    """Callback when Agent detects parameter changes."""
    broadcast_to_agent_clients("param_changes", {
        "type": "param_changes",
        "count": len(changes),
        "changes": changes,
        "timestamp": time.time(),
    })


def on_agent_dtc_change(added, removed):
    """Callback when Agent detects DTC changes."""
    broadcast_to_agent_clients("dtc_changes", {
        "type": "dtc_changes",
        "added": [d.to_dict() for d in added],
        "removed": [d.to_dict() for d in removed],
        "timestamp": time.time(),
    })


def on_agent_error(error):
    """Callback when Agent encounters an error."""
    broadcast_to_agent_clients("error", {
        "type": "error",
        "message": error,
        "timestamp": time.time(),
    })
    logger.error(f"Agent streaming error: {error}")


@app.route('/api/agent/status')
def agent_status():
    """Check Agent availability and status."""
    from src.streaming import AgentDataCollector

    checker = AgentDataCollector()
    availability = checker.check_agent_available()

    is_running = agent_collector is not None and agent_collector.is_running
    collection_count = agent_collector.collection_count if agent_collector else 0
    client_count = len(agent_clients)

    return jsonify({
        "agent": availability,
        "streaming": {
            "running": is_running,
            "collection_count": collection_count,
            "connected_clients": client_count,
        }
    })


@app.route('/api/stream/start', methods=['POST'])
def start_streaming():
    """Start Agent-based data streaming with navigation."""
    global agent_collector

    data = request.json or {}
    interval_ms = data.get('interval_ms', 100)
    data_category = data.get('data_category')

    try:
        if agent_collector and agent_collector.is_running:
            return jsonify({"error": "Streaming already running"}), 400

        if not data_category:
            return jsonify({"error": "Please select a data category"}), 400

        if not app_state.current_module:
            return jsonify({"error": "Please complete Step 2 first"}), 400

        if app_state.gds2_state != GDS2State.DATA_LIST.value:
            return jsonify({"error": f"GDS2 must be at Data List. Current: {app_state.gds2_state}"}), 400

        logger.info(f"=== Starting Monitoring for {data_category} ===")

        # Navigate to Data Display using Agent
        target_index = controller.mapping.get_data_category_index(
            "current_vehicle", app_state.current_module, data_category
        )
        if target_index is None:
            return jsonify({"error": f"Data category '{data_category}' not found"}), 400

        result = controller.nav.select_list_item(0, target_index, double_click=True)
        if not result.get('success'):
            return jsonify({"error": "Failed to select data category"}), 500
        time.sleep(3)

        app_state.gds2_state = GDS2State.DATA_DISPLAY.value
        app_state.current_data_category = data_category
        app_state.data_list_focus_index = target_index

        # Start Agent collector
        from src.streaming import AgentDataCollector

        agent_collector = AgentDataCollector(
            on_snapshot=on_agent_snapshot,
            on_param_change=on_agent_param_change,
            on_dtc_change=on_agent_dtc_change,
            on_error=on_agent_error,
            interval_ms=interval_ms,
        )
        agent_collector.start()

        logger.info(f"Started Agent streaming ({interval_ms}ms interval)")
        return jsonify({
            "success": True,
            "message": f"Monitoring started for {data_category}",
            "interval_ms": interval_ms,
            "state": asdict(app_state)
        })

    except Exception as e:
        logger.exception("Failed to start streaming")
        return jsonify({"error": str(e)}), 500


@app.route('/api/stream/stop', methods=['POST'])
def stop_streaming():
    """Stop Agent-based streaming and return to Data List."""
    global agent_collector

    try:
        if agent_collector:
            agent_collector.stop()
            agent_collector = None
            logger.info("Stopped Agent streaming")

            if app_state.gds2_state == GDS2State.DATA_DISPLAY.value:
                controller.click_back_button()
                app_state.gds2_state = GDS2State.DATA_LIST.value

            return jsonify({
                "success": True,
                "message": "Streaming stopped",
                "state": asdict(app_state)
            })
        return jsonify({"message": "Streaming was not running"})

    except Exception as e:
        logger.exception("Failed to stop streaming")
        return jsonify({"error": str(e)}), 500


@app.route('/api/stream/status')
def streaming_status():
    """Get streaming status."""
    is_running = agent_collector is not None and agent_collector.is_running
    collection_count = agent_collector.collection_count if agent_collector else 0

    return jsonify({
        "running": is_running,
        "collection_count": collection_count,
        "connected_clients": len(agent_clients)
    })


@app.route('/api/stream/events')
def stream_events():
    """SSE endpoint for real-time data streaming."""
    def generate():
        client_queue = queue.Queue(maxsize=200)

        with agent_lock:
            agent_clients.append(client_queue)

        logger.info(f"SSE client connected. Total: {len(agent_clients)}")

        try:
            yield f"event: connected\ndata: {json.dumps({'message': 'Connected to stream'})}\n\n"

            while True:
                try:
                    message = client_queue.get(timeout=30)
                    yield message
                except queue.Empty:
                    yield f": keepalive\n\n"

        except GeneratorExit:
            pass
        finally:
            with agent_lock:
                if client_queue in agent_clients:
                    agent_clients.remove(client_queue)
            logger.info(f"SSE client disconnected. Total: {len(agent_clients)}")

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        }
    )


@app.route('/api/agent/snapshot')
def agent_latest_snapshot():
    """Get the latest Agent snapshot."""
    global agent_collector

    if agent_collector and agent_collector.last_snapshot:
        return jsonify(agent_collector.last_snapshot.to_dict())

    # Try to read directly from file
    from src.streaming import AgentDataCollector
    from src.streaming.agent_data_collector import _parse_agent_json

    temp = AgentDataCollector()
    status = temp.check_agent_available()
    if not status['available']:
        return jsonify({"error": "Agent not available"}), 400

    try:
        with open(temp.json_path, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        snapshot = _parse_agent_json(raw)
        return jsonify(snapshot.to_dict())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    import sys
    import socket

    port = 8080
    host = '0.0.0.0'

    if '--port' in sys.argv:
        idx = sys.argv.index('--port')
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])

    if '--local' in sys.argv:
        host = '127.0.0.1'

    local_ip = '127.0.0.1'
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass

    print(f"\n{'='*60}")
    print(f"  GDS2 Automation Web UI (Agent-based)")
    print(f"{'='*60}")
    if host == '0.0.0.0':
        print(f"\n  Local:  http://localhost:{port}")
        print(f"  Remote: http://{local_ip}:{port}")
    else:
        print(f"\n  http://localhost:{port}")
    print(f"{'='*60}\n")

    app.run(debug=True, host=host, port=port, use_reloader=False)
