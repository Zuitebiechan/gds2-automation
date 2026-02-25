"""Phase 3 diagnostics + Phase 3.5 AI diagnosis API blueprint."""

import json
import logging
import queue
import os
import time

from flask import Blueprint, Response, jsonify, request

from src.recovery.types import WorkflowRecoveryError
from src.streaming import AgentDataCollector
from src.diagnosis.ai_engine import AIEngine, get_cached_payload

logger = logging.getLogger(__name__)

diagnostics_bp = Blueprint("diagnostics", __name__, url_prefix="/api/diagnose")

_diag_collector = None

# Phase 3.5: AI diagnosis engine (lazy-initialized)
_ai_engine: AIEngine | None = None


def _get_ai_engine() -> AIEngine:
    """Lazy-init the AI engine with API key from config."""
    global _ai_engine
    if _ai_engine is None:
        api_key = _load_zhipu_api_key()
        if not api_key:
            raise RuntimeError(
                "ZhipuAI API key not configured. "
                "Set it in %APPDATA%/VCI_Proxy/config.json under 'zhipu_api_key'."
            )
        _ai_engine = AIEngine(api_key=api_key)
    return _ai_engine


def _load_zhipu_api_key() -> str | None:
    """Load ZhipuAI API key from config file."""
    config_dir = os.path.join(
        os.environ.get('APPDATA', os.path.expanduser('~')),
        'VCI_Proxy'
    )
    config_path = os.path.join(config_dir, 'config.json')
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
        return config.get('zhipu_api_key', '').strip() or None
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _app_bindings():
    """Get shared app-level viewer + SSE bindings."""
    from app import (
        get_data_viewer,
        agent_clients,
        agent_lock,
        broadcast_to_agent_clients,
        on_agent_snapshot,
        on_agent_param_change,
        on_agent_dtc_change,
        on_agent_error,
    )

    return {
        "get_data_viewer": get_data_viewer,
        "agent_clients": agent_clients,
        "agent_lock": agent_lock,
        "broadcast_to_agent_clients": broadcast_to_agent_clients,
        "on_agent_snapshot": on_agent_snapshot,
        "on_agent_param_change": on_agent_param_change,
        "on_agent_dtc_change": on_agent_dtc_change,
        "on_agent_error": on_agent_error,
    }


@diagnostics_bp.route('/start', methods=['POST'])
def diagnose_start():
    """One-button start: start + auto-connect + modules."""
    try:
        viewer = _app_bindings()["get_data_viewer"]()
        result = viewer.auto_start()
        return jsonify({
            "success": True,
            "modules": result["modules"],
            "vin": result.get("vin"),
            "device": result.get("device"),
        })

    except WorkflowRecoveryError as e:
        logger.info(f"diagnose_start recovered: {e}")
        return jsonify({
            "success": False,
            "recovered": True,
            "recovery_target": e.target_page,
            "reasoning": e.reasoning,
            "error": str(e),
        }), 200

    except Exception as e:
        logger.exception("diagnose_start failed")
        return jsonify({"success": False, "error": str(e)}), 500


@diagnostics_bp.route('/dtcs')
def diagnose_dtcs():
    """Read DTCs directly from Agent snapshot JSON."""
    try:
        module_name = request.args.get('module', '').strip()
        data_category = request.args.get('data_category', '').strip()

        viewer = _app_bindings()["get_data_viewer"]()

        # Keep navigation minimal for UI flow:
        # User already clicked "Select module", so we should avoid jumping
        # back to module list unless module context is missing.
        current_page = viewer.controller.detect_current_page()
        state = viewer.get_state()

        if module_name and not state.get("module"):
            viewer.select_module(module_name)
            current_page = viewer.controller.detect_current_page()

        if data_category and current_page.value != "data_display":
            viewer.select_data_category(data_category)

        result = viewer.read_all_dtcs()
        return jsonify({
            "success": True,
            "dtcs": result["dtcs"],
            "dtc_count": result["dtc_count"],
            "page_context": result.get("page_context"),
        })

    except WorkflowRecoveryError as e:
        logger.info(f"diagnose_dtcs recovered: {e}")
        return jsonify({
            "success": False,
            "recovered": True,
            "recovery_target": e.target_page,
            "reasoning": e.reasoning,
            "error": str(e),
            "dtcs": [],
        }), 200

    except RuntimeError as e:
        logger.info(f"diagnose_dtcs invalid state: {e}")
        return jsonify({"success": False, "error": str(e), "dtcs": []}), 400

    except Exception as e:
        logger.exception("diagnose_dtcs failed")
        return jsonify({"success": False, "error": str(e), "dtcs": []}), 500


@diagnostics_bp.route('/select_module', methods=['POST'])
def diagnose_select_module():
    """Select module and return data categories."""
    data = request.json or {}
    module = data.get('module')

    if not module:
        return jsonify({"success": False, "error": "Module name required"}), 400

    try:
        viewer = _app_bindings()["get_data_viewer"]()
        result = viewer.select_module(module)
        return jsonify({
            "success": True,
            "data_categories": result["data_categories"],
        })

    except WorkflowRecoveryError as e:
        logger.info(f"diagnose_select_module recovered: {e}")
        return jsonify({
            "success": False,
            "recovered": True,
            "recovery_target": e.target_page,
            "reasoning": e.reasoning,
            "error": str(e),
        }), 200

    except Exception as e:
        logger.exception("diagnose_select_module failed")
        return jsonify({"success": False, "error": str(e)}), 500


@diagnostics_bp.route('/live_data/start', methods=['POST'])
def diagnose_live_data_start():
    """Navigate to Data Display and start live Agent streaming."""
    global _diag_collector

    data = request.json or {}
    data_category = data.get('data_category')
    interval_ms = data.get('interval_ms', 100)

    if not data_category:
        return jsonify({"success": False, "error": "Data category required"}), 400

    try:
        if _diag_collector and _diag_collector.is_running:
            return jsonify({"success": True, "message": "Streaming already running"})

        if _diag_collector is not None:
            try:
                _diag_collector.stop()
            except Exception:
                pass
            _diag_collector = None

        app_shared = _app_bindings()
        viewer = app_shared["get_data_viewer"]()
        viewer.select_data_category(data_category)

        _diag_collector = AgentDataCollector(
            on_snapshot=app_shared["on_agent_snapshot"],
            on_param_change=app_shared["on_agent_param_change"],
            on_dtc_change=app_shared["on_agent_dtc_change"],
            on_error=app_shared["on_agent_error"],
            interval_ms=interval_ms,
        )
        _diag_collector.start()

        logger.info(f"Started diagnostics live data streaming ({interval_ms}ms interval)")
        return jsonify({
            "success": True,
            "message": "Live data streaming started",
            "interval_ms": interval_ms,
        })

    except WorkflowRecoveryError as e:
        logger.info(f"diagnose_live_data_start recovered: {e}")
        return jsonify({
            "success": False,
            "recovered": True,
            "recovery_target": e.target_page,
            "reasoning": e.reasoning,
            "error": str(e),
        }), 200

    except Exception as e:
        logger.exception("diagnose_live_data_start failed")
        return jsonify({"success": False, "error": str(e)}), 500


@diagnostics_bp.route('/live_data/events')
def diagnose_live_data_events():
    """SSE endpoint for diagnostics live data events."""
    app_shared = _app_bindings()
    agent_clients = app_shared["agent_clients"]
    agent_lock = app_shared["agent_lock"]

    def generate():
        client_queue = queue.Queue(maxsize=200)

        with agent_lock:
            agent_clients.append(client_queue)

        logger.info(f"Diagnostics SSE client connected. Total: {len(agent_clients)}")

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
            logger.info(f"Diagnostics SSE client disconnected. Total: {len(agent_clients)}")

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        }
    )


@diagnostics_bp.route('/live_data/stop', methods=['POST'])
def diagnose_live_data_stop():
    """Stop diagnostics live streaming and go back from Data Display."""
    global _diag_collector

    try:
        if _diag_collector:
            _diag_collector.stop()
            _diag_collector = None

        viewer = _app_bindings()["get_data_viewer"]()
        current_page = viewer.controller.detect_current_page()
        if current_page.value == "data_display":
            viewer.controller.go_back()

        return jsonify({"success": True, "message": "Live data stopped"})

    except Exception as e:
        logger.exception("diagnose_live_data_stop failed")
        return jsonify({"success": False, "error": str(e)}), 500


# =========================================================================
# Phase 3.5: AI Diagnosis endpoints
# =========================================================================


@diagnostics_bp.route('/ai_diagnose', methods=['POST'])
def diagnose_ai_start():
    """Start AI diagnosis: navigate to Data Display, 30s data collection + LLM analysis."""
    data = request.json or {}
    data_category = data.get('data_category', '')
    vehicle_context = {
        'vin': data.get('vin', ''),
        'module': data.get('module', ''),
        'data_category': data_category,
    }

    if not data_category:
        return jsonify({"success": False, "error": "data_category required"}), 400

    try:
        engine = _get_ai_engine()
        if engine.is_active:
            return jsonify({
                "success": False,
                "error": "AI diagnosis already in progress",
            }), 409

        # Navigate GDS2 to Data Display page for the selected category
        app_shared = _app_bindings()
        viewer = app_shared["get_data_viewer"]()
        viewer.select_data_category(data_category)

        session_id = engine.start_session(vehicle_context)
        return jsonify({
            "success": True,
            "session_id": session_id,
            "message": "AI diagnosis started. Subscribe to /ai_diagnose/events for progress.",
        })

    except WorkflowRecoveryError as e:
        logger.info(f"diagnose_ai_start recovered: {e}")
        return jsonify({
            "success": False,
            "recovered": True,
            "recovery_target": e.target_page,
            "reasoning": e.reasoning,
            "error": str(e),
        }), 409

    except Exception as e:
        logger.exception("diagnose_ai_start failed")
        return jsonify({"success": False, "error": str(e)}), 500


@diagnostics_bp.route('/ai_diagnose/events')
def diagnose_ai_events():
    """SSE endpoint for AI diagnosis progress + streamed LLM result."""
    session_id = request.args.get('session_id', '').strip()
    if not session_id:
        return jsonify({"success": False, "error": "session_id required"}), 400

    try:
        engine = _get_ai_engine()
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

    event_queue = engine.get_event_queue(session_id)
    if event_queue is None:
        return jsonify({
            "success": False,
            "error": f"Session {session_id} not found",
        }), 404

    def generate():
        yield f"event: connected\ndata: {json.dumps({'session_id': session_id})}\n\n"

        while True:
            try:
                message = event_queue.get(timeout=60)
                yield message

                # Check if this was the final event
                if '"event": "done"' in message or 'event: done' in message:
                    break
                if 'event: error' in message:
                    break
                if 'event: result' in message:
                    # Result sent, done event follows
                    continue

            except queue.Empty:
                yield ": keepalive\n\n"

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        }
    )


@diagnostics_bp.route('/ai_diagnose/retry', methods=['POST'])
def diagnose_ai_retry():
    """Retry LLM analysis with cached payload (skip re-collection)."""
    data = request.json or {}
    cached_payload_id = data.get('cached_payload_id', '').strip()
    vehicle_context = {
        'vin': data.get('vin', ''),
        'module': data.get('module', ''),
    }

    if not cached_payload_id:
        return jsonify({
            "success": False,
            "error": "cached_payload_id required",
        }), 400

    try:
        engine = _get_ai_engine()
        if engine.is_active:
            return jsonify({
                "success": False,
                "error": "AI diagnosis already in progress",
            }), 409

        session_id = engine.retry_with_cached(cached_payload_id, vehicle_context)
        return jsonify({
            "success": True,
            "session_id": session_id,
            "message": "Retry started. Subscribe to /ai_diagnose/events for progress.",
        })

    except Exception as e:
        logger.exception("diagnose_ai_retry failed")
        return jsonify({"success": False, "error": str(e)}), 500
