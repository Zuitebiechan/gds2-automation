"""
GDS2 Automation Web UI - Flask Version (Agent-based)

Simple web interface for GDS2 vehicle diagnostics automation.
Uses Java Agent for all navigation - no PyAutoGUI dependency.

API Endpoints:
- Data Viewer API: /api/viewer/* - Simplified 3-dropdown workflow (Device → Module → Data)
- Streaming API: /api/stream/* - Real-time data monitoring via SSE
- Agent API: /api/agent/* - Direct Agent communication
"""

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
    from pathlib import Path
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
        print(f"[OK] Loaded environment variables from {env_path}")
    else:
        print(f"[WARN] .env file not found at {env_path}")
except ImportError:
    print("[WARN] python-dotenv not installed. Run: pip install python-dotenv")

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
from src.recovery.types import WorkflowRecoveryError

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

from diagnostics_api import diagnostics_bp
app.register_blueprint(diagnostics_bp)

from session_api import session_bp
app.register_blueprint(session_bp)

from navigate_api import navigate_bp
app.register_blueprint(navigate_bp)


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
    viewer = get_data_viewer()
    available = viewer.controller.nav.check_agent()
    return jsonify({
        "available": available,
        "message": "Agent connected" if available else "Agent not available. Start GDS2 with agent."
    })


# =============================================================================
# Data Viewer API (Simplified 3-dropdown workflow)
# =============================================================================

_data_viewer = None


def get_data_viewer():
    """Get or create the global DataViewerWorkflow instance."""
    global _data_viewer
    if _data_viewer is None:
        from src.workflows.data_viewer import DataViewerWorkflow
        # Enable AI recovery (reads from .env: ENABLE_AI_RECOVERY=true)
        _data_viewer = DataViewerWorkflow(enable_ai_recovery=True)

        # Log AI recovery status
        if _data_viewer.recovery and _data_viewer.recovery.enabled:
            logger.info("[OK] AI recovery enabled")
            logger.info(f"   Provider: {_data_viewer.recovery.config.provider}")
            logger.info(f"   Model: {_data_viewer.recovery.config.model}")
        else:
            logger.warning("[WARN] AI recovery not enabled (check .env configuration)")
    return _data_viewer


@app.route('/api/viewer/start', methods=['POST'])
def viewer_start():
    """
    Navigate to Device Explorer and get available devices.

    This should be called first to discover available VCI devices.
    Returns: {"devices": [...], "at_device_explorer": true/false, "device_connected": true/false}
    """
    try:
        viewer = get_data_viewer()
        result = viewer.start()
        return jsonify({
            "success": True,
            **result
        })

    except Exception as e:
        logger.exception("viewer_start failed")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/viewer/connect', methods=['POST'])
def viewer_connect():
    """Connect device, navigate to Module List, return modules."""
    data = request.json or {}
    device = data.get('device')

    if not device:
        return jsonify({"success": False, "error": "Device name required"}), 400

    try:
        viewer = get_data_viewer()
        result = viewer.connect_device(device)
        return jsonify({
            "success": True,
            "modules": result["modules"],
            "vin": result.get("vin"),
            "device": result.get("device"),
        })

    except WorkflowRecoveryError as e:
        logger.info(f"viewer_connect recovered: {e}")
        return jsonify({
            "success": False,
            "recovered": True,
            "recovery_target": e.target_page,
            "reasoning": e.reasoning,
            "error": str(e),
        }), 200

    except Exception as e:
        logger.exception("viewer_connect failed")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/viewer/change_device', methods=['POST'])
def viewer_change_device():
    """
    Navigate to Device Explorer and get available devices for switching.

    Use this when user wants to change the connected device.
    Returns: {"devices": [...], "at_device_explorer": true}
    """
    try:
        viewer = get_data_viewer()
        result = viewer.get_available_devices()
        return jsonify({
            "success": True,
            **result
        })

    except Exception as e:
        logger.exception("viewer_change_device failed")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/viewer/select_module', methods=['POST'])
def viewer_select_module():
    """Select module, navigate to Data List, return data categories."""
    data = request.json or {}
    module = data.get('module')

    if not module:
        return jsonify({"success": False, "error": "Module name required"}), 400

    try:
        viewer = get_data_viewer()
        result = viewer.select_module(module)
        return jsonify({
            "success": True,
            "data_categories": result["data_categories"],
        })

    except WorkflowRecoveryError as e:
        logger.info(f"viewer_select_module recovered: {e}")
        return jsonify({
            "success": False,
            "recovered": True,
            "recovery_target": e.target_page,
            "reasoning": e.reasoning,
            "error": str(e),
        }), 200

    except Exception as e:
        logger.exception("viewer_select_module failed")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/viewer/select_data', methods=['POST'])
def viewer_select_data():
    """Select data category, navigate to Data Display, start monitoring."""
    data = request.json or {}
    data_category = data.get('data_category')

    if not data_category:
        return jsonify({"success": False, "error": "Data category required"}), 400

    try:
        viewer = get_data_viewer()
        result = viewer.select_data_category(data_category)
        return jsonify({
            "success": True,
            "monitoring": result["monitoring"],
            "sub_categories": result.get("sub_categories"),
        })

    except WorkflowRecoveryError as e:
        logger.info(f"viewer_select_data recovered: {e}")
        return jsonify({
            "success": False,
            "recovered": True,
            "recovery_target": e.target_page,
            "reasoning": e.reasoning,
            "error": str(e),
        }), 200

    except Exception as e:
        logger.exception("viewer_select_data failed")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/viewer/stop', methods=['POST'])
def viewer_stop():
    """Stop monitoring."""
    global agent_collector
    try:
        viewer = get_data_viewer()
        viewer.stop_monitoring()

        # Also stop the global agent collector
        if agent_collector is not None:
            try:
                agent_collector.stop()
            except Exception:
                pass
            agent_collector = None

        return jsonify({"success": True})

    except Exception as e:
        logger.exception("viewer_stop failed")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/viewer/state')
def viewer_state():
    """Get current viewer state."""
    try:
        viewer = get_data_viewer()
        return jsonify(viewer.get_state())

    except Exception as e:
        logger.exception("viewer_state failed")
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

        # Stop any stale collector
        if agent_collector is not None:
            try:
                agent_collector.stop()
            except Exception:
                pass
            agent_collector = None

        # Check if we're at Data Display page
        viewer = get_data_viewer()
        current_page = viewer.controller.detect_current_page()

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

        viewer = get_data_viewer()
        current_page = viewer.controller.detect_current_page()

        # Check if at Data Display page
        if current_page.value != "data_display":
            # If at data_list and data_category provided, navigate to data_display
            if current_page.value == "data_list" and data_category:
                logger.info(f"=== Navigating to Data Display for {data_category} ===")
                result = viewer.select_data_category(data_category)
                if not result.get("monitoring"):
                    return jsonify({"error": "Failed to navigate to Data Display"}), 500
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

        state = viewer.get_state()
        return jsonify({
            "success": True,
            "message": f"Monitoring started",
            "interval_ms": interval_ms,
            "state": state,
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

            viewer = get_data_viewer()
            current_page = viewer.controller.detect_current_page()

            if current_page.value == "data_display":
                viewer.controller.go_back()

            state = viewer.get_state()

            return jsonify({
                "success": True,
                "message": "Streaming stopped",
                "state": state,
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
    print(f"\n  Data Viewer:  /api/viewer/*")
    print(f"  Diagnostics: /api/diagnose/*")
    print(f"  Navigate:    /api/navigate/*")
    print(f"  Streaming:   /api/stream/*")
    print(f"  Agent:       /api/agent/*")
    print(f"{'='*60}\n")

    app.run(debug=True, host=host, port=port, use_reloader=False, threaded=True)
