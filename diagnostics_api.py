"""Phase 3 diagnostics + Phase 3.5 AI diagnosis API blueprint."""

import json
import logging
import os
import queue

from flask import Blueprint, Response, jsonify, request

from backends.gds2 import GDS2DiagnosticBackend
from diagnostic_platform.sse import (
    agent_clients,
    agent_lock,
    broadcast_to_agent_clients,
    on_agent_dtc_change,
    on_agent_error,
    on_agent_param_change,
    on_agent_snapshot,
)
from src.recovery.types import WorkflowRecoveryError
from src.streaming import AgentDataCollector
from src.diagnosis.ai_engine import AIEngine, get_cached_payload
from src.navigation import GDS2Page

logger = logging.getLogger(__name__)

diagnostics_bp = Blueprint("diagnostics", __name__, url_prefix="/api/diagnose")

_diag_collector = None

# Phase 3.5: AI diagnosis engine (lazy-initialized)
_ai_engine: AIEngine | None = None
_backend: GDS2DiagnosticBackend | None = None


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


def _get_backend() -> GDS2DiagnosticBackend:
    global _backend
    if _backend is None:
        _backend = GDS2DiagnosticBackend()
    return _backend


def _make_data_display_guard(backend: GDS2DiagnosticBackend, data_category: str, *, mode: str,
                              check_interval: float = 5.0):
    """Build a shared Data Display guard for live and AI collectors.

    mode:
      - 'stream': allow backtrack recovery through Data List
      - 'ai_collect': allow only in-place soft recovery; otherwise invalidate sample

    check_interval:
      Minimum seconds between actual page detection calls.  The guard is
      invoked every ~100 ms by the collector poll loop, but the underlying
      detect_current_page IPC round-trip takes ~800 ms and blocks the
      loop.  Throttling to every *check_interval* seconds keeps the poll
      loop fast (just reading latest.json) and only does an IPC check
      periodically to confirm we're still on Data Display.
    """
    if mode not in {'stream', 'ai_collect'}:
        raise ValueError(f"Unsupported guard mode: {mode}")

    import time as _time
    _last_check_ts: list[float] = [0.0]       # mutable container for nonlocal
    _last_result: list[dict | None] = [None]

    def guard() -> dict | None:
        now = _time.time()
        if now - _last_check_ts[0] < check_interval:
            return _last_result[0]  # reuse cached result

        _last_check_ts[0] = now

        page = backend.detect_current_page()
        if page == GDS2Page.DATA_DISPLAY.value:
            _last_result[0] = None
            return None

        # Page is NOT Data Display — reset timer so the next poll
        # immediately re-checks instead of waiting another 5 seconds.
        _last_check_ts[0] = 0.0

        if page == GDS2Page.LOADING.value:
            return {
                'ok': True,
                'mode': mode,
                'message': 'Waiting for GDS2 loading page to finish...',
            }

        if page == GDS2Page.J2534_DISCONNECT.value:
            # Transitional escape hatch: recovery helper still lives on the
            # underlying workflow/controller until this path is moved into the
            # DiagnosticBackend contract.
            recovery = backend._get_workflow().controller.recover_data_display_connection(
                data_category=data_category,
                allow_backtrack=True,
            )
            if recovery.success and recovery.page == GDS2Page.DATA_DISPLAY:
                recovery_method = (recovery.context or {}).get('recovery_method', 'unknown')
                if mode == 'ai_collect' and recovery_method == 'backtrack':
                    message = 'Recovered Data Display after reconnect; restarting AI collection window.'
                elif mode == 'ai_collect':
                    message = 'Recovered temporary J2534 disconnect and returned to Data Display.'
                else:
                    message = 'Recovered Data Display after J2534 disconnect.'
                return {
                    'ok': True,
                    'mode': mode,
                    'recovered': True,
                    'recovery_method': recovery_method,
                    'restart_collection': mode == 'ai_collect' and recovery_method == 'backtrack',
                    'message': message,
                }

            if mode == 'ai_collect':
                return {
                    'ok': False,
                    'mode': mode,
                    'error': (
                        'Lost communication with J2534 during AI collection and could not '
                        'restore Data Display in-place. Please reconnect and restart AI Diagnostics.'
                    ),
                }

            return {
                'ok': False,
                'mode': mode,
                'error': (
                    'Lost communication with J2534 and could not restore Data Display. '
                    'Please reconnect and restart live monitoring.'
                ),
            }

        return {
            'ok': False,
            'mode': mode,
            'error': (
                f'Data Display guard detected page drift to {page}. '
                'Please return to Data Display and retry.'
            ),
        }

    return guard


@diagnostics_bp.route('/start', methods=['POST'])
def diagnose_start():
    """One-button start: start + auto-connect + modules."""
    try:
        backend = _get_backend()
        backend.start()
        state = backend.get_state()
        modules = backend.get_modules()
        logger.info("DIAG start ready modules=%s device=%s", len(modules), state.extra.get("device") or "-")
        return jsonify({
            "success": True,
            "modules": modules,
            "vin": state.extra.get("vin"),
            "device": state.extra.get("device"),
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

        backend = _get_backend()

        # Keep navigation minimal for UI flow:
        # User already clicked "Select module", so we should avoid jumping
        # back to module list unless module context is missing.
        current_page = backend.detect_current_page()
        state = backend.get_state()

        if module_name and not state.current_module:
            backend.select_module(module_name)
            current_page = backend.detect_current_page()

        if data_category and current_page != GDS2Page.DATA_DISPLAY.value:
            backend.select_data_category(data_category)

        dtcs = backend.read_dtcs()
        page_context = backend.detect_current_page()
        logger.info("DIAG DTC read count=%s page=%s", len(dtcs), page_context)
        return jsonify({
            "success": True,
            "dtcs": [
                {
                    "code": dtc.code,
                    "control_module": dtc.module,
                    "module": dtc.module,
                    "status": dtc.status,
                    "description": dtc.description,
                    "source_backend": dtc.source_backend,
                }
                for dtc in dtcs
            ],
            "dtc_count": len(dtcs),
            "page_context": page_context,
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
        backend = _get_backend()
        backend.select_module(module)
        data_categories = backend.get_data_categories()
        logger.info("DIAG module=%s categories=%s", module, len(data_categories))
        return jsonify({
            "success": True,
            "data_categories": data_categories,
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

        backend = _get_backend()
        backend.select_data_category(data_category)
        page_guard = _make_data_display_guard(backend, data_category, mode='stream')

        def on_guard_event(event: dict[str, object]) -> None:
            message = event.get('message')
            if message:
                broadcast_to_agent_clients('guard', {'message': message})

        _diag_collector = AgentDataCollector(
            on_snapshot=on_agent_snapshot,
            on_param_change=on_agent_param_change,
            on_dtc_change=on_agent_dtc_change,
            on_error=on_agent_error,
            page_guard=page_guard,
            on_guard_event=on_guard_event,
            interval_ms=interval_ms,
        )
        _diag_collector.start()

        logger.info("DIAG live start category=%s interval=%sms", data_category, interval_ms)
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
    def generate():
        client_queue = queue.Queue(maxsize=200)

        with agent_lock:
            agent_clients.append(client_queue)

        logger.debug("Diagnostics SSE client connected. total=%s", len(agent_clients))

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
            logger.debug("Diagnostics SSE client disconnected. total=%s", len(agent_clients))

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

        backend = _get_backend()
        current_page = backend.detect_current_page()
        if current_page == GDS2Page.DATA_DISPLAY.value:
            backend.go_back()

        logger.info("DIAG live stopped")
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

        # Skip redundant navigation if GDS2 is already on Data Display
        # (e.g. agentic navigation already landed here)
        backend = _get_backend()
        current_page = backend.detect_current_page()
        if current_page == GDS2Page.DATA_DISPLAY.value:
            logger.debug("AI-DIAG request already on data_display")
        else:
            logger.debug("AI-DIAG navigating to data_display from %s", current_page)
            backend.select_data_category(data_category)

        page_guard = _make_data_display_guard(backend, data_category, mode='ai_collect')
        session_id = engine.start_session(vehicle_context, collection_guard=page_guard)
        logger.info("AI-DIAG %s requested module=%s category=%s", session_id, vehicle_context.get('module') or '-', data_category)
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
                # SSE format: 'event: <type>\ndata: <json>\n\n'
                # Check by line prefix, not substring match (avoid false positives)
                if message.startswith('event: done\n'):
                    break
                if message.startswith('event: error\n'):
                    break
                if message.startswith('event: result\n'):
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
        'data_category': data.get('data_category', ''),
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
        logger.info("AI-DIAG %s retry requested payload=%s", session_id, cached_payload_id)
        return jsonify({
            "success": True,
            "session_id": session_id,
            "message": "Retry started. Subscribe to /ai_diagnose/events for progress.",
        })

    except Exception as e:
        logger.exception("diagnose_ai_retry failed")
        return jsonify({"success": False, "error": str(e)}), 500
