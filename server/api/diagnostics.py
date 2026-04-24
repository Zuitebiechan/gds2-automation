"""Phase 3 diagnostics + Phase 3.5 AI diagnosis API blueprint."""

import json
import logging
import os
from typing import Any

from flask import Blueprint, Response, jsonify, request

from diagnostic_platform.backend_registry import get_backend_registry
from diagnostic_platform.contracts import BackendCapability, UnsupportedCapabilityError
from diagnostic_platform.runtime.diagnostics_runtime import (
    build_diagnostics_start_payload,
    clear_diagnostic_dtcs,
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
from server.api.http_utils import (
    RequestPayloadError,
    internal_error_payload,
    read_text_mapping_field,
    require_json_object,
)


# Lightweight exception kept from removed AI recovery system
class WorkflowRecoveryError(Exception):
    """Raised when workflow needs to be restarted from a different page."""

    def __init__(
        self,
        message: str,
        *,
        target_page: str = "",
        reasoning: str = "",
    ) -> None:
        super().__init__(message)
        self.target_page = target_page
        self.reasoning = reasoning


logger = logging.getLogger(__name__)

diagnostics_bp = Blueprint("diagnostics", __name__, url_prefix="/api/diagnose")


def _runtime():
    return get_worker_runtime()


def _read_text_field(
    data: dict[str, object],
    field: str,
    *,
    default: str = "",
) -> str:
    return read_text_mapping_field(data, field, default=default)


def _read_query_text_arg(field: str, *, default: str = "") -> str:
    return read_text_mapping_field(request.args, field, default=default)


def _read_int_field(
    data: dict[str, object],
    field: str,
    *,
    default: int,
) -> int:
    value = data.get(field, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be an integer") from None


def _vci_proxy_config_path() -> str:
    config_dir = os.path.join(
        os.environ.get("APPDATA", os.path.expanduser("~")),
        "VCI_Proxy",
    )
    return os.path.join(config_dir, "config.json")


def _load_vci_proxy_config() -> dict[str, object]:
    config_path = _vci_proxy_config_path()
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        return payload if isinstance(payload, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _load_openai_api_key() -> str | None:
    env_key = str(os.environ.get("OPENAI_API_KEY") or "").strip()
    if env_key:
        return env_key
    try:
        config = _load_vci_proxy_config()
        return read_text_mapping_field(config, "openai_api_key") or None
    except ValueError:
        return None


def _load_openai_base_url() -> str | None:
    env_base_url = str(
        os.environ.get("OPENAI_BASE_URL")
        or os.environ.get("OPENAI_API_BASE")
        or ""
    ).strip()
    if env_base_url:
        return env_base_url
    try:
        config = _load_vci_proxy_config()
        return read_text_mapping_field(config, "openai_base_url") or None
    except ValueError:
        return None


def _load_openai_model() -> str:
    env_model = str(os.environ.get("OPENAI_MODEL") or "").strip()
    if env_model:
        return env_model
    try:
        config = _load_vci_proxy_config()
        return read_text_mapping_field(config, "openai_model", default="gpt-5.4") or "gpt-5.4"
    except ValueError:
        return "gpt-5.4"


def _load_openai_reasoning_effort() -> str:
    env_reasoning = str(os.environ.get("OPENAI_REASONING_EFFORT") or "").strip()
    if env_reasoning:
        return env_reasoning
    try:
        config = _load_vci_proxy_config()
        return read_text_mapping_field(config, "openai_reasoning_effort", default="none") or "none"
    except ValueError:
        return "none"


def _build_ai_engine() -> AIEngine:
    """Build the worker-scoped AI engine from persisted config."""
    api_key = _load_openai_api_key()
    if not api_key:
        raise RuntimeError(
            "OpenAI API key not configured. "
            "Set OPENAI_API_KEY or %APPDATA%/VCI_Proxy/config.json under 'openai_api_key'."
        )
    return AIEngine(
        api_key=api_key,
        model=_load_openai_model(),
        base_url=_load_openai_base_url(),
        reasoning_effort=_load_openai_reasoning_effort(),
    )


def _get_ai_engine() -> AIEngine:
    """Return the shared worker-scoped AI engine."""
    return _runtime().get_ai_engine(_build_ai_engine)


def _ensure_ai_engine_ready(engine: Any) -> None:
    """Fail before backend collection when AI provider/config is not usable."""
    if bool(getattr(engine, "is_active", False)):
        raise RuntimeError("AI diagnosis already in progress")
    readiness_checker = getattr(engine, "verify_ready", None)
    if callable(readiness_checker):
        readiness_checker()


def _load_zhipu_api_key() -> str | None:
    """Legacy compatibility shim kept for older tests and tooling."""
    try:
        config = _load_vci_proxy_config()
        return read_text_mapping_field(config, "zhipu_api_key") or None
    except ValueError:
        return None


def _default_backend_name() -> str:
    registry = get_backend_registry()
    if "gds2" in registry.list_backends():
        return "gds2"
    backend_names = registry.list_backends()
    if not backend_names:
        raise RuntimeError("No diagnostic backends registered")
    return backend_names[0]


def _resolve_backend_name(data: dict[str, object] | None = None) -> str:
    payload = data or {}
    explicit = _read_text_field(payload, "backend_name")
    if explicit:
        return explicit
    active_bundle = _runtime().get_active_backend_bundle()
    if active_bundle is not None:
        return active_bundle.backend_name
    return _default_backend_name()


def _get_backend(backend_name: str | None = None):
    registry = get_backend_registry()
    resolved_name = backend_name or _default_backend_name()
    backend = registry.get_by_name(resolved_name)
    with _runtime().state_lock:
        _runtime().backend = backend
    return backend


def _ensure_backend_capability(backend: object, capability: BackendCapability) -> None:
    descriptor = getattr(backend, "descriptor", None)
    if descriptor is None or not descriptor.supports(capability):
        raise UnsupportedCapabilityError(capability, getattr(backend, "name", "unknown"))


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
    backend_name: str | None = None,
    stream_scope: str = DEFAULT_AGENT_STREAM_SCOPE,
) -> dict[str, object]:
    """Start the shared live-data collector and return the JSON payload."""
    backend = _get_backend(backend_name)
    _ensure_backend_capability(backend, BackendCapability.LIVE_DATA)
    return runtime_start_live_data_stream(
        _runtime(),
        backend=backend,
        data_category=data_category,
        interval_ms=interval_ms,
        stream_scope=stream_scope,
    )


def stop_live_data_stream(*, backend_name: str | None = None) -> dict[str, object]:
    """Stop the shared live-data collector and return the JSON payload."""
    backend = _get_backend(backend_name)
    _ensure_backend_capability(backend, BackendCapability.LIVE_DATA)
    return runtime_stop_live_data_stream(_runtime(), backend=backend)


@diagnostics_bp.route('/start', methods=['POST'])
def diagnose_start():
    """One-button start: start + auto-connect + modules."""
    try:
        data = require_json_object(request)
        payload = build_diagnostics_start_payload(
            backend=_get_backend(_resolve_backend_name(data)),
        )
        if "modules" in payload:
            logger.debug(
                "DIAG start ready modules=%s device=%s",
                len(payload["modules"]),
                payload.get("device") or "-",
            )
        elif "devices" in payload:
            logger.debug(
                "DIAG start awaiting device selection devices=%s",
                len(payload["devices"]),
            )
        else:
            logger.debug("DIAG start ready payload_keys=%s", sorted(payload.keys()))
        return jsonify(payload)

    except WorkflowRecoveryError as e:
        logger.warning("diagnose_start recovered target=%s error=%s", e.target_page, e)
        return jsonify({
            "success": False,
            "recovered": True,
            "recovery_target": e.target_page,
            "reasoning": e.reasoning,
            "error": str(e),
        }), 200
    except RequestPayloadError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    except Exception as e:
        logger.exception("diagnose_start failed")
        return jsonify(internal_error_payload()), 500


@diagnostics_bp.route('/dtcs')
def diagnose_dtcs():
    """Read DTCs directly from the diagnostics backend."""
    try:
        backend_name = _read_query_text_arg("backend_name")
        module_name = _read_query_text_arg("module")
        data_category = _read_query_text_arg("data_category")
        backend = _get_backend(backend_name or None)
        _ensure_backend_capability(backend, BackendCapability.READ_DTCS)
        payload = read_diagnostic_dtcs(
            backend=backend,
            module_name=module_name,
            data_category=data_category,
        )
        logger.debug(
            "DIAG DTC read count=%s page=%s",
            payload["dtc_count"],
            payload["page_context"],
        )
        return jsonify(payload)

    except WorkflowRecoveryError as e:
        logger.warning("diagnose_dtcs recovered target=%s error=%s", e.target_page, e)
        return jsonify({
            "success": False,
            "recovered": True,
            "recovery_target": e.target_page,
            "reasoning": e.reasoning,
            "error": str(e),
            "dtcs": [],
        }), 200

    except UnsupportedCapabilityError as e:
        return jsonify({"success": False, "error": str(e), "dtcs": []}), 501

    except RuntimeError as e:
        logger.warning("diagnose_dtcs invalid state: %s", e)
        return jsonify({"success": False, "error": str(e), "dtcs": []}), 400
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc), "dtcs": []}), 400

    except Exception as e:
        logger.exception("diagnose_dtcs failed")
        payload = internal_error_payload()
        payload["dtcs"] = []
        return jsonify(payload), 500


@diagnostics_bp.route('/clear_dtcs', methods=['POST'])
def diagnose_clear_dtcs():
    """Clear DTCs directly from the diagnostics backend."""
    try:
        data = require_json_object(request)
        backend = _get_backend(_resolve_backend_name(data))
        _ensure_backend_capability(backend, BackendCapability.CLEAR_DTCS)
        payload = clear_diagnostic_dtcs(
            backend=backend,
            module_name=_read_text_field(data, 'module'),
            data_category=_read_text_field(data, 'data_category'),
        )
        logger.debug(
            "DIAG clear_dtcs cleared=%s page=%s",
            payload["cleared_count"],
            payload["page_context"],
        )
        return jsonify(payload)

    except UnsupportedCapabilityError as e:
        return jsonify({"success": False, "error": str(e)}), 501
    except RuntimeError as e:
        logger.warning("diagnose_clear_dtcs invalid state: %s", e)
        return jsonify({"success": False, "error": str(e)}), 400
    except RequestPayloadError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as e:
        logger.exception("diagnose_clear_dtcs failed")
        return jsonify(internal_error_payload()), 500


@diagnostics_bp.route('/select_module', methods=['POST'])
def diagnose_select_module():
    """Select module and return data categories."""
    try:
        data = require_json_object(request)
        module = _read_text_field(data, 'module')

        if not module:
            return jsonify({"success": False, "error": "Module name required"}), 400

        backend = _get_backend(_resolve_backend_name(data))
        payload = select_diagnostic_module(backend=backend, module=module)
        logger.debug("DIAG module=%s categories=%s", module, len(payload["data_categories"]))
        return jsonify(payload)

    except WorkflowRecoveryError as e:
        logger.warning("diagnose_select_module recovered target=%s error=%s", e.target_page, e)
        return jsonify({
            "success": False,
            "recovered": True,
            "recovery_target": e.target_page,
            "reasoning": e.reasoning,
            "error": str(e),
        }), 200
    except RequestPayloadError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    except Exception as e:
        logger.exception("diagnose_select_module failed")
        return jsonify(internal_error_payload()), 500


@diagnostics_bp.route('/live_data/start', methods=['POST'])
def diagnose_live_data_start():
    """Navigate to Data Display and start live Agent streaming."""
    try:
        data = require_json_object(request)
        data_category = _read_text_field(data, 'data_category')
        interval_ms = _read_int_field(data, 'interval_ms', default=100)

        if not data_category:
            return jsonify({"success": False, "error": "Data category required"}), 400

        return jsonify(
            start_live_data_stream(
                data_category,
                interval_ms,
                backend_name=_resolve_backend_name(data),
            )
        )

    except WorkflowRecoveryError as e:
        logger.warning("diagnose_live_data_start recovered target=%s error=%s", e.target_page, e)
        return jsonify({
            "success": False,
            "recovered": True,
            "recovery_target": e.target_page,
            "reasoning": e.reasoning,
            "error": str(e),
        }), 200
    except RequestPayloadError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    except UnsupportedCapabilityError as e:
        return jsonify({"success": False, "error": str(e)}), 501
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    except Exception as e:
        logger.exception("diagnose_live_data_start failed")
        return jsonify(internal_error_payload()), 500


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
        data = require_json_object(request)
        return jsonify(stop_live_data_stream(backend_name=_resolve_backend_name(data)))

    except UnsupportedCapabilityError as e:
        return jsonify({"success": False, "error": str(e)}), 501
    except RequestPayloadError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as e:
        logger.exception("diagnose_live_data_stop failed")
        return jsonify(internal_error_payload()), 500


# =========================================================================
# Phase 3.5: AI Diagnosis endpoints
# =========================================================================


@diagnostics_bp.route('/ai_diagnose', methods=['POST'])
def diagnose_ai_start():
    """Start AI diagnosis: navigate to Data Display, 30s data collection + LLM analysis."""
    try:
        data = require_json_object(request)
        data_category = _read_text_field(data, 'data_category')
        vehicle_context = {
            'vin': _read_text_field(data, 'vin'),
            'module': _read_text_field(data, 'module'),
            'data_category': data_category,
            'brand': _read_text_field(data, 'brand'),
            'model': _read_text_field(data, 'model'),
        }

        if not data_category:
            return jsonify({"success": False, "error": "data_category required"}), 400

        engine = _get_ai_engine()
        backend = _get_backend(_resolve_backend_name(data))
        _ensure_backend_capability(backend, BackendCapability.AI_DATA_COLLECTION)
        _ensure_ai_engine_ready(engine)
        session_id = start_public_ai_diagnosis(
            backend=backend,
            engine=engine,
            vehicle_context=vehicle_context,
            data_category=data_category,
        )
        logger.debug(
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
        logger.warning("diagnose_ai_start recovered target=%s error=%s", e.target_page, e)
        return jsonify({
            "success": False,
            "recovered": True,
            "recovery_target": e.target_page,
            "reasoning": e.reasoning,
            "error": str(e),
        }), 409
    except RequestPayloadError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    except UnsupportedCapabilityError as e:
        return jsonify({"success": False, "error": str(e)}), 501

    except RuntimeError as e:
        return jsonify({"success": False, "error": str(e)}), 409

    except Exception as e:
        logger.exception("diagnose_ai_start failed")
        return jsonify(internal_error_payload()), 500


@diagnostics_bp.route('/ai_diagnose/events')
def diagnose_ai_events():
    """SSE endpoint for AI diagnosis progress + streamed LLM result."""
    try:
        session_id = _read_query_text_arg("session_id")
        if not session_id:
            return jsonify({"success": False, "error": "session_id required"}), 400
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    try:
        engine = _get_ai_engine()
    except Exception as e:
        logger.exception("diagnose_ai_events failed to build engine")
        return jsonify(internal_error_payload()), 500

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
    try:
        data = require_json_object(request)
        cached_payload_id = _read_text_field(data, 'cached_payload_id')
        vehicle_context = {
            'vin': _read_text_field(data, 'vin'),
            'module': _read_text_field(data, 'module'),
            'data_category': _read_text_field(data, 'data_category'),
        }

        if not cached_payload_id:
            return jsonify({
                "success": False,
                "error": "cached_payload_id required",
            }), 400

        engine = _get_ai_engine()
        _ensure_ai_engine_ready(engine)
        session_id = retry_public_ai_diagnosis(
            engine=engine,
            cached_payload_id=cached_payload_id,
            vehicle_context=vehicle_context,
        )
        logger.debug("AI-DIAG %s retry requested payload=%s", session_id, cached_payload_id)
        return jsonify({
            "success": True,
            "session_id": session_id,
            "message": "Retry started. Subscribe to /ai_diagnose/events for progress.",
        })

    except RuntimeError as e:
        return jsonify({"success": False, "error": str(e)}), 409
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except RequestPayloadError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    except Exception as e:
        logger.exception("diagnose_ai_retry failed")
        return jsonify(internal_error_payload()), 500
