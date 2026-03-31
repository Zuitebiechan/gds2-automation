"""Phase 3 diagnostics + Phase 3.5 AI diagnosis API blueprint."""

import json
import logging
import os

from flask import Blueprint, Response, jsonify, request

from backends.gds2 import GDS2DiagnosticBackend
from diagnostic_platform.runtime.diagnostics_runtime import (
    build_diagnostics_start_payload,
    make_data_display_guard,
    read_diagnostic_dtcs,
    retry_public_ai_diagnosis,
    select_diagnostic_module,
    start_live_data_stream as runtime_start_live_data_stream,
    start_public_ai_diagnosis,
    stop_live_data_stream as runtime_stop_live_data_stream,
)
from diagnostic_platform.runtime.session_streams import (
    iter_engine_events,
    iter_scoped_agent_events,
)
from diagnostic_platform.runtime.worker_runtime import get_worker_runtime
from diagnostic_platform.sse import (
    DEFAULT_AGENT_STREAM_SCOPE,
)
from src.diagnosis.ai_engine import AIEngine


# Lightweight exception kept from removed AI recovery system
class WorkflowRecoveryError(Exception):
    """Raised when workflow needs to be restarted from a different page."""
    pass


logger = logging.getLogger(__name__)

diagnostics_bp = Blueprint("diagnostics", __name__, url_prefix="/api/diagnose")


def _runtime():
    return get_worker_runtime()


def _build_ai_engine() -> AIEngine:
    """Build the worker-scoped AI engine from persisted config."""
    api_key = _load_zhipu_api_key()
    if not api_key:
        raise RuntimeError(
            "ZhipuAI API key not configured. "
            "Set it in %APPDATA%/VCI_Proxy/config.json under 'zhipu_api_key'."
        )
    return AIEngine(api_key=api_key)


def _get_ai_engine() -> AIEngine:
    """Return the shared worker-scoped AI engine."""
    return _runtime().get_ai_engine(_build_ai_engine)


def _load_zhipu_api_key() -> str | None:
    """Load ZhipuAI API key from config file."""
    config_dir = os.path.join(
        os.environ.get("APPDATA", os.path.expanduser("~")),
        "VCI_Proxy",
    )
    config_path = os.path.join(config_dir, "config.json")
    try:
        with open(config_path, "r") as f:
            config = json.load(f)
        return config.get("zhipu_api_key", "").strip() or None
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _get_backend() -> GDS2DiagnosticBackend:
    return _runtime().get_backend(GDS2DiagnosticBackend)


def _make_data_display_guard(
    backend: GDS2DiagnosticBackend,
    data_category: str,
    *,
    mode: str,
    check_interval: float = 5.0,
):
    return make_data_display_guard(
        backend,
        data_category,
        mode=mode,
        check_interval=check_interval,
    )


def _sse_response(stream) -> Response:
    return Response(
        stream,
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def start_live_data_stream(
    data_category: str,
    interval_ms: int = 100,
    *,
    stream_scope: str = DEFAULT_AGENT_STREAM_SCOPE,
) -> dict[str, object]:
    """Start the shared live-data collector and return the JSON payload."""
    return runtime_start_live_data_stream(
        _runtime(),
        backend=_get_backend(),
        data_category=data_category,
        interval_ms=interval_ms,
        stream_scope=stream_scope,
    )


def stop_live_data_stream() -> dict[str, object]:
    """Stop the shared live-data collector and return the JSON payload."""
    return runtime_stop_live_data_stream(_runtime(), backend=_get_backend())


@diagnostics_bp.route('/start', methods=['POST'])
def diagnose_start():
    """One-button start: start + auto-connect + modules."""
    try:
        payload = build_diagnostics_start_payload(backend=_get_backend())
        logger.info(
            "DIAG start ready modules=%s device=%s",
            len(payload["modules"]),
            payload.get("device") or "-",
        )
        return jsonify(payload)

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
    """Read DTCs directly from the diagnostics backend."""
    try:
        payload = read_diagnostic_dtcs(
            backend=_get_backend(),
            module_name=request.args.get('module', '').strip(),
            data_category=request.args.get('data_category', '').strip(),
        )
        logger.info(
            "DIAG DTC read count=%s page=%s",
            payload["dtc_count"],
            payload["page_context"],
        )
        return jsonify(payload)

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
        payload = select_diagnostic_module(backend=_get_backend(), module=module)
        logger.info("DIAG module=%s categories=%s", module, len(payload["data_categories"]))
        return jsonify(payload)

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
    data = request.json or {}
    data_category = data.get('data_category')
    interval_ms = data.get('interval_ms', 100)

    if not data_category:
        return jsonify({"success": False, "error": "Data category required"}), 400

    try:
        return jsonify(start_live_data_stream(data_category, interval_ms))

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
    return _sse_response(
        iter_scoped_agent_events(
            scope=DEFAULT_AGENT_STREAM_SCOPE,
            session_id="diagnostics",
        )
    )


@diagnostics_bp.route('/live_data/stop', methods=['POST'])
def diagnose_live_data_stop():
    """Stop diagnostics live streaming and go back from Data Display."""
    try:
        return jsonify(stop_live_data_stream())

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
        session_id = start_public_ai_diagnosis(
            backend=_get_backend(),
            engine=engine,
            vehicle_context=vehicle_context,
            data_category=data_category,
        )
        logger.info(
            "AI-DIAG %s requested module=%s category=%s",
            session_id,
            vehicle_context.get('module') or '-',
            data_category,
        )
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

    except RuntimeError as e:
        return jsonify({"success": False, "error": str(e)}), 409

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

    return _sse_response(
        iter_engine_events(
            session_id=session_id,
            event_queue=event_queue,
        )
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
        session_id = retry_public_ai_diagnosis(
            engine=_get_ai_engine(),
            cached_payload_id=cached_payload_id,
            vehicle_context=vehicle_context,
        )
        logger.info("AI-DIAG %s retry requested payload=%s", session_id, cached_payload_id)
        return jsonify({
            "success": True,
            "session_id": session_id,
            "message": "Retry started. Subscribe to /ai_diagnose/events for progress.",
        })

    except RuntimeError as e:
        return jsonify({"success": False, "error": str(e)}), 409

    except Exception as e:
        logger.exception("diagnose_ai_retry failed")
        return jsonify({"success": False, "error": str(e)}), 500
