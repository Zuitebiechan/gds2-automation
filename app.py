"""
GDS2 Automation Web UI - Flask Version (Agent-based)

Simple web interface for GDS2 vehicle diagnostics automation.
Uses Java Agent for all navigation - no PyAutoGUI dependency.

API Endpoints:
- New Interactive API: /api/nav/* - Step-by-step navigation
- Legacy API: /api/fetch_modules, /api/fetch_categories, /api/search_data
- Streaming API: /api/stream/* - Real-time data monitoring
"""

from flask import Flask, render_template, jsonify, request, Response
from flask_cors import CORS
import logging
import time
import json
import queue
import threading
from pathlib import Path
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
# Interactive Workflow Controller
# =============================================================================

# Global workflow instance
_workflow = None


def get_workflow():
    """Get or create the global InteractiveWorkflow instance."""
    global _workflow
    if _workflow is None:
        from src.workflows import InteractiveWorkflow
        _workflow = InteractiveWorkflow()
    return _workflow


# =============================================================================
# Flask Routes - Basic
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
    workflow = get_workflow()
    available = workflow.check_agent()
    return jsonify({
        "available": available,
        "message": "Agent connected" if available else "Agent not available. Start GDS2 with agent."
    })


# =============================================================================
# New Interactive Navigation API
# =============================================================================

@app.route('/api/nav/start', methods=['POST'])
def nav_start():
    """
    Start interactive navigation workflow.

    Returns current page and available choices.
    """
    try:
        workflow = get_workflow()
        result = workflow.start()
        return jsonify(result.to_dict())

    except Exception as e:
        logger.exception("nav_start failed")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/nav/diagnostics', methods=['POST'])
def nav_diagnostics():
    """
    Click Diagnostics from Main Menu.

    If Device Explorer appears, returns device list.
    """
    try:
        workflow = get_workflow()
        result = workflow.step_diagnostics()
        return jsonify(result.to_dict())

    except Exception as e:
        logger.exception("nav_diagnostics failed")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/nav/select_device', methods=['POST'])
def nav_select_device():
    """
    Select device in Device Explorer.

    Request body: {"device": "SM2 USB"}
    """
    data = request.json or {}
    device = data.get('device')

    if not device:
        return jsonify({"success": False, "error": "Device name required"}), 400

    try:
        workflow = get_workflow()
        result = workflow.step_select_device(device)
        return jsonify(result.to_dict())

    except Exception as e:
        logger.exception("nav_select_device failed")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/nav/disconnect_device', methods=['POST'])
def nav_disconnect_device():
    """
    Disconnect from current VCI device.

    Must be at Vehicle Selection page.
    After disconnect, use /api/nav/open_device_selector to select a different device.
    """
    try:
        workflow = get_workflow()
        result = workflow.step_disconnect_device()
        return jsonify(result.to_dict())

    except Exception as e:
        logger.exception("nav_disconnect_device failed")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/nav/open_device_selector', methods=['POST'])
def nav_open_device_selector():
    """
    Open Device Explorer to select a different device.

    Must be at Vehicle Selection page after disconnecting.
    Returns list of available devices.
    """
    try:
        workflow = get_workflow()
        result = workflow.step_open_device_selector()
        return jsonify(result.to_dict())

    except Exception as e:
        logger.exception("nav_open_device_selector failed")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/nav/enter', methods=['POST'])
def nav_enter():
    """
    Click Enter button (typically at Vehicle Selection page).

    Returns next page after clicking Enter.
    """
    try:
        workflow = get_workflow()
        result = workflow.step_click_enter()
        return jsonify(result.to_dict())

    except Exception as e:
        logger.exception("nav_enter failed")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/nav/module_diagnostics', methods=['POST'])
def nav_module_diagnostics():
    """
    Select Module Diagnostics from Diagnostics Menu.

    Returns list of available modules.
    """
    try:
        workflow = get_workflow()
        result = workflow.step_module_diagnostics()
        return jsonify(result.to_dict())

    except Exception as e:
        logger.exception("nav_module_diagnostics failed")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/nav/select_module', methods=['POST'])
def nav_select_module():
    """
    Select a module from Module List.

    Request body: {"module": "[K20] Engine Control Module"}
    """
    data = request.json or {}
    module = data.get('module')

    if not module:
        return jsonify({"success": False, "error": "Module name required"}), 400

    try:
        workflow = get_workflow()
        result = workflow.step_select_module(module)
        return jsonify(result.to_dict())

    except Exception as e:
        logger.exception("nav_select_module failed")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/nav/data_display', methods=['POST'])
def nav_data_display():
    """
    Select Data Display from Module Submenu.

    Returns list of available data categories.
    """
    try:
        workflow = get_workflow()
        result = workflow.step_data_display()
        return jsonify(result.to_dict())

    except Exception as e:
        logger.exception("nav_data_display failed")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/nav/select_data_category', methods=['POST'])
def nav_select_data_category():
    """
    Select a data category from Data List.

    May return DATA_DISPLAY or SUB_DATA_LIST if category has sub-categories.

    Request body: {"data_category": "Engine Data"}
    """
    data = request.json or {}
    data_category = data.get('data_category')

    if not data_category:
        return jsonify({"success": False, "error": "Data category required"}), 400

    try:
        workflow = get_workflow()
        result = workflow.step_select_data_category(data_category)
        return jsonify(result.to_dict())

    except Exception as e:
        logger.exception("nav_select_data_category failed")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/nav/select_sub_category', methods=['POST'])
def nav_select_sub_category():
    """
    Select a sub-category from Sub Data List.

    Request body: {"sub_category": "Fuel Injector Data"}
    """
    data = request.json or {}
    sub_category = data.get('sub_category')

    if not sub_category:
        return jsonify({"success": False, "error": "Sub-category required"}), 400

    try:
        workflow = get_workflow()
        result = workflow.step_select_sub_category(sub_category)
        return jsonify(result.to_dict())

    except Exception as e:
        logger.exception("nav_select_sub_category failed")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/nav/back', methods=['POST'])
def nav_back():
    """Go back one page."""
    try:
        workflow = get_workflow()
        result = workflow.step_go_back()
        return jsonify(result.to_dict())

    except Exception as e:
        logger.exception("nav_back failed")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/nav/home', methods=['POST'])
def nav_home():
    """Go to Main Menu."""
    try:
        workflow = get_workflow()
        result = workflow.step_go_home()
        return jsonify(result.to_dict())

    except Exception as e:
        logger.exception("nav_home failed")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/nav/change_module', methods=['POST'])
def nav_change_module():
    """
    Navigate back to Module List and select a different module.

    Request body: {"module": "[T42] Body Control Module"}
    """
    data = request.json or {}
    module = data.get('module')

    if not module:
        return jsonify({"success": False, "error": "Module name required"}), 400

    try:
        workflow = get_workflow()
        result = workflow.step_change_module(module)
        return jsonify(result.to_dict())

    except Exception as e:
        logger.exception("nav_change_module failed")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/nav/change_data_category', methods=['POST'])
def nav_change_data_category():
    """
    Navigate back to Data List and select a different data category.

    Request body: {"data_category": "Misfire Data"}
    """
    data = request.json or {}
    data_category = data.get('data_category')

    if not data_category:
        return jsonify({"success": False, "error": "Data category required"}), 400

    try:
        workflow = get_workflow()
        result = workflow.step_change_data_category(data_category)
        return jsonify(result.to_dict())

    except Exception as e:
        logger.exception("nav_change_data_category failed")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/nav/choices')
def nav_choices():
    """Get available choices at current page without navigation."""
    try:
        workflow = get_workflow()
        result = workflow.get_current_choices()
        return jsonify(result.to_dict())

    except Exception as e:
        logger.exception("nav_choices failed")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/nav/create_report', methods=['POST'])
def nav_create_report():
    """
    Create report at Data Display page.

    Returns report path and parsed data.
    """
    try:
        workflow = get_workflow()

        report_path = workflow.create_report()
        if not report_path:
            return jsonify({"success": False, "error": "Failed to create report"}), 500

        report_data = workflow.parse_report(report_path)

        # Also parse for DTCs
        from src.utils.report_parser import GDS2ReportParser
        dtc_parser = GDS2ReportParser()
        dtc_data = dtc_parser.parse_dtc_report(report_path)

        return jsonify({
            "success": True,
            "report_path": report_path,
            "report_data": report_data,
            "vehicle_info": dtc_data.get("vehicle_info", {}),
            "dtc_list": dtc_data.get("dtc_list", []),
            "module_status": dtc_data.get("module_status", []),
        })

    except Exception as e:
        logger.exception("nav_create_report failed")
        return jsonify({"success": False, "error": str(e)}), 500


# =============================================================================
# Cache Access API
# =============================================================================

@app.route('/api/modules')
def get_modules():
    """Get list of modules from cache."""
    workflow = get_workflow()
    modules = workflow.get_cached_modules()
    return jsonify({"modules": modules})


@app.route('/api/data_categories')
def get_data_categories():
    """Get data categories for a module from cache."""
    module_name = request.args.get('module')
    if not module_name:
        return jsonify({"error": "Module name required"}), 400

    workflow = get_workflow()
    categories = workflow.get_cached_data_categories(module_name)
    return jsonify({"data_categories": categories})


@app.route('/api/sub_categories')
def get_sub_categories():
    """Get sub-categories for a data category from cache."""
    module_name = request.args.get('module')
    data_category = request.args.get('data_category')

    if not module_name or not data_category:
        return jsonify({"error": "Module and data category required"}), 400

    workflow = get_workflow()
    sub_cats = workflow.get_cached_sub_categories(module_name, data_category)
    return jsonify({"sub_categories": sub_cats})


@app.route('/api/state')
def get_state():
    """Get current navigation state with button info."""
    workflow = get_workflow()
    result = workflow.get_current_choices()
    buttons = workflow.get_available_buttons()
    return jsonify({
        "page": result.page.value,
        "context": result.context,
        "choices": result.choices,
        "buttons": buttons,
    })


# =============================================================================
# Legacy API (Backward Compatibility)
# =============================================================================

@app.route('/api/fetch_modules', methods=['POST'])
def fetch_modules():
    """
    Legacy Step 1: Fetch all modules.
    Assumes GDS2 is at Main Menu.
    """
    try:
        logger.info("=== Legacy Step 1: Fetch Modules ===")
        workflow = get_workflow()

        # Check agent
        if not workflow.check_agent():
            return jsonify({"error": "Java Agent not available. Start GDS2 with agent."}), 400

        # Check cache
        cached_modules = workflow.get_cached_modules()

        # Start workflow
        result = workflow.start()
        if not result.success:
            return jsonify({"error": result.error}), 500

        # Click Diagnostics
        result = workflow.step_diagnostics()
        if not result.success:
            return jsonify({"error": result.error}), 500

        # Handle Device Explorer if present
        if result.page.value == "device_explorer":
            result = workflow.step_select_device("SM2 USB")
            if not result.success:
                return jsonify({"error": result.error}), 500

        # Select Module Diagnostics
        result = workflow.step_module_diagnostics()
        if not result.success:
            return jsonify({"error": result.error}), 500

        modules = result.choices or []

        return jsonify({
            "success": True,
            "modules": modules,
            "from_cache": len(cached_modules) > 0,
            "state": {
                "gds2_state": result.page.value,
                "current_module": result.context.get("module"),
                "current_data_category": result.context.get("data_category"),
            }
        })

    except Exception as e:
        logger.exception("Fetch modules failed")
        return jsonify({"error": str(e)}), 500


@app.route('/api/fetch_categories', methods=['POST'])
def fetch_categories():
    """
    Legacy Step 2: Fetch data categories for selected module.
    Assumes GDS2 is at Module List.
    """
    data = request.json or {}
    module_name = data.get('module')

    if not module_name:
        return jsonify({"error": "Module name required"}), 400

    try:
        logger.info(f"=== Legacy Step 2: Fetch Categories for {module_name} ===")
        workflow = get_workflow()

        # Check cache
        cached_categories = workflow.get_cached_data_categories(module_name)

        # Select module
        result = workflow.step_select_module(module_name)
        if not result.success:
            return jsonify({"error": result.error}), 500

        # Select Data Display
        result = workflow.step_data_display()
        if not result.success:
            return jsonify({"error": result.error}), 500

        categories = result.choices or []

        return jsonify({
            "success": True,
            "data_categories": categories,
            "from_cache": len(cached_categories) > 0,
            "state": {
                "gds2_state": result.page.value,
                "current_module": result.context.get("module"),
                "current_data_category": result.context.get("data_category"),
            }
        })

    except Exception as e:
        logger.exception("Fetch categories failed")
        return jsonify({"error": str(e)}), 500


@app.route('/api/search_data', methods=['POST'])
def search_data():
    """
    Legacy Step 3: Search data for selected category.
    Assumes GDS2 is at Data List.
    """
    data = request.json or {}
    module_name = data.get('module')
    data_category = data.get('data_category')

    if not module_name or not data_category:
        return jsonify({"error": "Module and data category required"}), 400

    try:
        logger.info(f"=== Legacy Search: {data_category} ===")
        workflow = get_workflow()

        # Select data category
        result = workflow.step_select_data_category(data_category)
        if not result.success:
            return jsonify({"error": result.error}), 500

        # Handle sub-categories if present
        if result.page.value == "sub_data_list":
            # For legacy API, auto-select first sub-category
            if result.choices:
                result = workflow.step_select_sub_category(result.choices[0])
                if not result.success:
                    return jsonify({"error": result.error}), 500

        # Create report
        report_path = workflow.create_report()
        if not report_path:
            return jsonify({"error": "Failed to create report"}), 500

        report_data = workflow.parse_report(report_path)

        # Parse for DTCs
        from src.utils.report_parser import GDS2ReportParser
        dtc_parser = GDS2ReportParser()
        dtc_data = dtc_parser.parse_dtc_report(report_path)

        # Go back to Data List
        workflow.step_go_back()

        return jsonify({
            "success": True,
            "report_path": report_path,
            "report_data": report_data,
            "vehicle_info": dtc_data.get("vehicle_info", {}),
            "dtc_list": dtc_data.get("dtc_list", []),
            "module_status": dtc_data.get("module_status", []),
            "state": {
                "gds2_state": "data_list",
                "current_module": module_name,
                "current_data_category": data_category,
            }
        })

    except Exception as e:
        logger.exception("Search data failed")
        return jsonify({"error": str(e)}), 500


@app.route('/api/get_dtcs', methods=['POST'])
def get_dtcs():
    """Legacy: Get DTCs from Vehicle DTC Information category."""
    try:
        logger.info("=== Legacy Get DTCs ===")
        workflow = get_workflow()

        module = workflow.current_module
        if not module:
            return jsonify({"error": "No module selected"}), 400

        # Try to find DTC category
        categories = workflow.get_cached_data_categories(module)
        dtc_category = None
        for cat in categories:
            if "DTC" in cat:
                dtc_category = cat
                break

        if not dtc_category:
            return jsonify({"error": "DTC category not found"}), 400

        # Navigate and create report
        result = workflow.step_select_data_category(dtc_category)
        if not result.success:
            return jsonify({"error": result.error}), 500

        report_path = workflow.create_report()
        if not report_path:
            return jsonify({"error": "Failed to create DTC report"}), 500

        from src.utils.report_parser import GDS2ReportParser
        parser = GDS2ReportParser()
        dtc_data = parser.parse_dtc_report(report_path)

        workflow.step_go_back()

        return jsonify({
            "success": True,
            "vehicle_info": dtc_data.get("vehicle_info", {}),
            "dtc_list": dtc_data.get("dtc_list", []),
            "module_status": dtc_data.get("module_status", []),
        })

    except Exception as e:
        logger.exception("Get DTCs failed")
        return jsonify({"error": str(e)}), 500


@app.route('/api/back', methods=['POST'])
def back():
    """Legacy: Click back button."""
    try:
        workflow = get_workflow()
        result = workflow.step_go_back()
        return jsonify({
            "success": result.success,
            "state": {
                "gds2_state": result.page.value,
                "current_module": result.context.get("module"),
                "current_data_category": result.context.get("data_category"),
            }
        })

    except Exception as e:
        logger.exception("Back button failed")
        return jsonify({"error": str(e)}), 500


@app.route('/api/home', methods=['POST'])
def home():
    """Legacy: Click Home button to return to Main Menu."""
    try:
        workflow = get_workflow()
        result = workflow.step_go_home()
        return jsonify({
            "success": result.success,
            "state": {
                "gds2_state": result.page.value,
                "current_module": result.context.get("module"),
                "current_data_category": result.context.get("data_category"),
            }
        })

    except Exception as e:
        logger.exception("Home button failed")
        return jsonify({"error": str(e)}), 500


@app.route('/api/reset', methods=['POST'])
def reset_state():
    """Reset workflow state."""
    global _workflow
    try:
        _workflow = None
        logger.info("Workflow reset")
        return jsonify({"success": True})

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


def on_agent_snapshot(snapshot, param_changes=None):
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
        "param_changes": param_changes or [],
        "timestamp": time.time(),
    })


def on_agent_param_change(changes):
    """Callback when Agent detects parameter changes."""
    logger.info(f"Agent detected {len(changes)} parameter change(s)")


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
    global agent_collector

    from src.streaming import AgentDataCollector

    checker = AgentDataCollector()
    availability = checker.check_agent_available()

    # Check if streaming is actually running
    is_running = False
    collection_count = 0

    if agent_collector is not None:
        try:
            # Check both the flag and if the thread is actually alive
            is_running = agent_collector._running and agent_collector._thread and agent_collector._thread.is_alive()
            collection_count = agent_collector.collection_count
        except Exception as e:
            logger.warning(f"Error checking agent_collector status: {e}")
            is_running = False
            collection_count = 0

    client_count = len(agent_clients)

    logger.debug(f"Agent status: available={availability.get('available')}, running={is_running}, collector={agent_collector is not None}")

    return jsonify({
        "agent": availability,
        "streaming": {
            "running": is_running,
            "collection_count": collection_count,
            "connected_clients": client_count,
        }
    })


@app.route('/api/agent/reset', methods=['POST'])
def reset_agent():
    """Reset the agent collector state."""
    global agent_collector

    try:
        if agent_collector:
            try:
                agent_collector.stop()
            except Exception as e:
                logger.warning(f"Error stopping agent_collector: {e}")
            agent_collector = None

        # Clear clients
        with agent_lock:
            agent_clients.clear()

        logger.info("Agent collector reset")
        return jsonify({"success": True, "message": "Agent reset"})

    except Exception as e:
        logger.exception("Failed to reset agent")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/agent/dtcs')
def get_agent_dtcs():
    """Get DTCs directly from Agent (single read, no streaming)."""
    try:
        from src.streaming import AgentDataCollector
        from src.streaming.agent_data_collector import _parse_agent_json

        collector = AgentDataCollector()
        status = collector.check_agent_available()

        if not status.get('available'):
            return jsonify({
                "success": False,
                "error": "Agent not available",
                "dtcs": []
            })

        # Read latest data from Agent - try multiple encodings
        try:
            raw = None
            for encoding in ['utf-8', 'utf-8-sig', 'latin-1', 'cp1252']:
                try:
                    with open(collector.json_path, 'r', encoding=encoding) as f:
                        raw = json.load(f)
                    break
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue

            if raw is None:
                return jsonify({
                    "success": False,
                    "error": "Failed to decode Agent JSON file",
                    "dtcs": []
                })

            snapshot = _parse_agent_json(raw)

            return jsonify({
                "success": True,
                "dtcs": [d.to_dict() for d in snapshot.dtcs],
                "dtc_count": len(snapshot.dtcs),
                "page_context": snapshot.page_context,
            })
        except Exception as e:
            logger.error(f"Failed to read Agent data: {e}")
            return jsonify({
                "success": False,
                "error": str(e),
                "dtcs": []
            })

    except Exception as e:
        logger.exception("get_agent_dtcs failed")
        return jsonify({"success": False, "error": str(e), "dtcs": []}), 500


@app.route('/api/agent/monitor/start', methods=['POST'])
def start_agent_monitor():
    """Start Agent monitoring without navigation (when already at Data Display)."""
    global agent_collector

    data = request.json or {}
    interval_ms = data.get('interval_ms', 100)

    try:
        if agent_collector and agent_collector.is_running:
            return jsonify({"success": True, "message": "Monitoring already running"})

        # Check if we're at Data Display page
        workflow = get_workflow()
        current_page = workflow.controller.detect_current_page()

        if current_page.value != "data_display":
            return jsonify({
                "success": False,
                "error": f"Must be at Data Display page. Current: {current_page.value}"
            }), 400

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

        logger.info(f"Started Agent monitoring at Data Display ({interval_ms}ms interval)")
        return jsonify({
            "success": True,
            "message": "Monitoring started",
            "interval_ms": interval_ms,
        })

    except Exception as e:
        logger.exception("Failed to start agent monitor")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/stream/start', methods=['POST'])
def start_streaming():
    """Start Agent-based data streaming (requires being at Data Display page)."""
    global agent_collector

    data = request.json or {}
    interval_ms = data.get('interval_ms', 100)
    data_category = data.get('data_category')

    try:
        if agent_collector and agent_collector.is_running:
            return jsonify({"error": "Streaming already running"}), 400

        workflow = get_workflow()
        current_page = workflow.controller.detect_current_page()

        # Check if at Data Display page
        if current_page.value != "data_display":
            # If at data_list and data_category provided, navigate to data_display
            if current_page.value == "data_list" and data_category:
                logger.info(f"=== Navigating to Data Display for {data_category} ===")
                result = workflow.step_select_data_category(data_category)
                if not result.success:
                    return jsonify({"error": result.error}), 500

                # Handle sub-categories
                if result.page.value == "sub_data_list" and result.choices:
                    result = workflow.step_select_sub_category(result.choices[0])
                    if not result.success:
                        return jsonify({"error": result.error}), 500
            else:
                return jsonify({
                    "error": f"Must be at Data Display page. Current: {current_page.value}"
                }), 400

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

        result = workflow.get_current_choices()
        return jsonify({
            "success": True,
            "message": f"Monitoring started",
            "interval_ms": interval_ms,
            "state": {
                "gds2_state": result.page.value,
                "current_module": result.context.get("module"),
                "current_data_category": result.context.get("data_category"),
            }
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

            workflow = get_workflow()
            current_page = workflow.controller.detect_current_page()

            if current_page.value == "data_display":
                workflow.step_go_back()

            result = workflow.get_current_choices()

            return jsonify({
                "success": True,
                "message": "Streaming stopped",
                "state": {
                    "gds2_state": result.page.value,
                    "current_module": result.context.get("module"),
                    "current_data_category": result.context.get("data_category"),
                }
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
    print(f"\n  New API: /api/nav/*")
    print(f"  Legacy:  /api/fetch_modules, /api/fetch_categories, etc.")
    print(f"{'='*60}\n")

    app.run(debug=True, host=host, port=port, use_reloader=False)
