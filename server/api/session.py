"""Session orchestration API blueprint (Phase G3).

Provides endpoints for session lifecycle, SSE event streaming,
user decision submission, and abort.  Operates on the in-memory
SessionOrchestrator and does not touch existing diagnostics routes.
"""

# pyright: reportMissingImports=false

import inspect
import logging
from typing import Any

from flask import Blueprint, Response, jsonify, request

from diagnostic_platform.branch_planning import BranchDecisionRequiredError
from diagnostic_platform.contracts import BackendCapability, UnsupportedCapabilityError
from diagnostic_platform.session_models import SessionContext, SessionStatus
from diagnostic_platform.runtime.session_actions import (
    ensure_session_capability,
    execute_backend_action,
    select_data_category_action,
    select_module_action,
)
from diagnostic_platform.runtime.session_decisions import (
    raise_branch_decision,
    submit_session_decision,
)
from diagnostic_platform.runtime.session_lifecycle import (
    abort_business_session,
    build_session_status_payload,
    start_business_session,
)
from diagnostic_platform.runtime.session_bootstrap import (
    bind_session_node_assignment,
    bootstrap_session_node_assignment,
    release_session_node_assignment,
)
from diagnostic_platform.runtime.worker_runtime import (
    OperationCancelledError,
    WorkerBusyError,
)
from diagnostic_platform.runtime.session_preflight import (
    run_start_diagnostics,
)
from diagnostic_platform.runtime.session_streams import (
    iter_session_events,
)
from server.api import session_ai_handlers
from server.api.http_utils import (
    RequestPayloadError,
    internal_error_payload,
    read_text_mapping_field,
    require_json_object,
)
from server.api.session_dependencies import (
    _runtime,
    get_adapter,
    get_backend as _get_backend,
    get_data_viewer as _get_data_viewer,
    get_executor,
    get_launch_spec_resolver,
    get_node_allocator,
    get_node_geo_routing_enabled,
    get_node_provisioner,
    get_node_route_resolver,
    get_orchestrator,
    get_trust_cloudfront_headers,
    get_ai_engine as _get_ai_engine,
    reset_executor,
    set_data_viewer_getter,
    set_orchestrator,
)
from server.api import session_live_data_handlers
from server.api import session_navigation_handlers
logger = logging.getLogger(__name__)

session_bp = Blueprint("session", __name__, url_prefix="/api/session")


def _read_text_field(
    data: dict[str, Any],
    field: str,
    *,
    default: str = "",
) -> str:
    return read_text_mapping_field(data, field, default=default)


def _read_query_text_arg(field: str, *, default: str = "") -> str:
    return read_text_mapping_field(request.args, field, default=default)


def _read_object_field(
    data: dict[str, Any],
    field: str,
) -> dict[str, Any]:
    value = data.get(field)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _active_session_conflict_payload(exc: RuntimeError) -> dict[str, Any]:
    """Build one machine-readable payload for an already-active session conflict."""
    payload = {
        "success": False,
        "error": str(exc),
    }
    error_text = str(exc).lower()
    if "already active" not in error_text:
        return payload

    active_session = get_orchestrator().get_active_session()
    if active_session is None:
        return payload

    payload.update(
        {
            "error_code": "active_session_exists",
            "active_session_id": active_session.session_id,
            "active_session_status": active_session.status.value,
            "active_backend_name": active_session.backend_name,
        }
    )
    if active_session.pending_decision is not None:
        payload["decision"] = active_session.pending_decision.to_dict()
    return payload


def _resolve_bootstrap_route_preferences(
    data: dict[str, Any],
) -> dict[str, Any]:
    def _route_has_preference(route: dict[str, Any]) -> bool:
        return bool(
            _read_text_field(route, "preferred_zone")
            or _read_text_field(route, "preferred_metro")
        )

    def _apply_route_preferences(
        *,
        requested_zone: str,
        requested_metro: str,
        route: dict[str, Any],
    ) -> tuple[str, str]:
        preferred_zone = requested_zone
        preferred_metro = requested_metro
        route_source = _read_text_field(route, "source")
        route_zone = _read_text_field(route, "preferred_zone")
        route_metro = _read_text_field(route, "preferred_metro")

        if route_zone and (not preferred_zone or route_source == "preferred_zone"):
            preferred_zone = route_zone
        if route_metro and (
            not preferred_metro
            or route_source in {"preferred_zone", "preferred_metro"}
        ):
            preferred_metro = route_metro
        return preferred_zone, preferred_metro

    def _call_route_resolver(
        resolver: Any,
        *,
        payload: dict[str, Any],
        include_fallbacks: bool,
    ) -> dict[str, Any]:
        if not callable(resolver):
            return {}

        try:
            signature = inspect.signature(resolver)
            supports_include_fallbacks = "include_fallbacks" in signature.parameters
        except (TypeError, ValueError):
            supports_include_fallbacks = False

        try:
            if supports_include_fallbacks:
                resolved_route = resolver(
                    data=payload,
                    include_fallbacks=include_fallbacks,
                ) or {}
            else:
                resolved_route = resolver(data=payload) or {}
        except Exception:
            logger.exception("session bootstrap route inference failed")
            return {}

        if not isinstance(resolved_route, dict):
            return {}
        return resolved_route

    def _extract_cloudfront_route_input() -> dict[str, str]:
        if not (
            get_node_geo_routing_enabled() and get_trust_cloudfront_headers()
        ):
            return {}

        headers = getattr(request, "headers", {}) or {}
        cloudfront_city = read_text_mapping_field(
            headers,
            "CloudFront-Viewer-City",
            default="",
        )
        cloudfront_time_zone = read_text_mapping_field(
            headers,
            "CloudFront-Viewer-Time-Zone",
            default="",
        )
        route_input = {}
        if cloudfront_city:
            route_input["client_city"] = cloudfront_city
        if cloudfront_time_zone:
            route_input["client_time_zone"] = cloudfront_time_zone
        return route_input

    def _translate_cloudfront_route(route: dict[str, Any]) -> dict[str, Any]:
        if not route:
            return {}

        translated = dict(route)
        source = _read_text_field(route, "source")
        translated["source"] = {
            "client_city": "cloudfront_viewer_city",
            "client_time_zone": "cloudfront_viewer_time_zone",
        }.get(source, source)
        return translated

    preferred_zone = _read_text_field(data, "preferred_zone")
    preferred_metro = _read_text_field(data, "preferred_metro")
    route_source = "preferred_zone" if preferred_zone else "preferred_metro" if preferred_metro else ""
    fallback_used = False
    signal_conflict = False
    resolver = get_node_route_resolver()
    if not callable(resolver):
        return {
            "preferred_zone": preferred_zone,
            "preferred_metro": preferred_metro,
            "route_source": route_source,
            "fallback_used": fallback_used,
            "signal_conflict": signal_conflict,
        }

    client_signal_source = route_source
    client_signal_zone = preferred_zone
    client_signal_metro = preferred_metro
    client_route = _call_route_resolver(
        resolver,
        payload=data,
        include_fallbacks=False,
    )
    if _route_has_preference(client_route):
        client_signal_zone, client_signal_metro = _apply_route_preferences(
            requested_zone=preferred_zone,
            requested_metro=preferred_metro,
            route=client_route,
        )
        if not client_signal_source:
            client_signal_source = _read_text_field(client_route, "source")

    cloudfront_route = _translate_cloudfront_route(
        _call_route_resolver(
            resolver,
            payload=_extract_cloudfront_route_input(),
            include_fallbacks=False,
        )
    )

    if (
        client_signal_source
        and client_signal_metro
        and _route_has_preference(cloudfront_route)
        and client_signal_metro != _read_text_field(cloudfront_route, "preferred_metro")
    ):
        signal_conflict = True
        logger.warning(
            "session bootstrap route signal conflict source_client=%s source_cloudfront=%s selected_metro=%s",
            client_signal_source,
            _read_text_field(cloudfront_route, "source"),
            client_signal_metro,
        )

    if client_signal_source:
        preferred_zone = client_signal_zone
        preferred_metro = client_signal_metro
        route_source = client_signal_source
    elif _route_has_preference(cloudfront_route):
        preferred_zone, preferred_metro = _apply_route_preferences(
            requested_zone=preferred_zone,
            requested_metro=preferred_metro,
            route=cloudfront_route,
        )
        route_source = _read_text_field(cloudfront_route, "source")
        fallback_used = True
    else:
        default_route = _call_route_resolver(
            resolver,
            payload={},
            include_fallbacks=True,
        )
        if _route_has_preference(default_route):
            preferred_zone, preferred_metro = _apply_route_preferences(
                requested_zone=preferred_zone,
                requested_metro=preferred_metro,
                route=default_route,
            )
            route_source = _read_text_field(default_route, "source")
            fallback_used = True

    return {
        "preferred_zone": preferred_zone,
        "preferred_metro": preferred_metro,
        "route_source": route_source,
        "fallback_used": fallback_used,
        "signal_conflict": signal_conflict,
    }


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


# ---------------------------------------------------------------------------
# POST /api/session/bootstrap
# ---------------------------------------------------------------------------

@session_bp.route("/bootstrap/ready")
def session_bootstrap_ready():
    """Return one lightweight readiness signal for booting-node probes."""
    return jsonify(
        {
            "success": True,
            "ready": True,
            "status": "worker_ready",
        }
    )


@session_bp.route("/bootstrap", methods=["POST"])
def session_bootstrap():
    """Assign a new diagnostics session to a remote worker node."""
    try:
        allocator = get_node_allocator()
        if allocator is None:
            return jsonify(
                {
                    "success": False,
                    "error": "Node allocator is not configured",
                }
            ), 503

        data = require_json_object(request)
        brand = _read_text_field(data, "brand")

        if not brand:
            return jsonify({"success": False, "error": "brand is required"}), 400

        route_decision = _resolve_bootstrap_route_preferences(data)
        preferred_zone = _read_text_field(route_decision, "preferred_zone")
        preferred_metro = _read_text_field(route_decision, "preferred_metro")

        ctx = SessionContext(
            brand=brand,
            model=_read_text_field(data, "model"),
            vin=_read_text_field(data, "vin"),
            backend_name=_read_text_field(data, "backend_name"),
            extra={
                k: v
                for k, v in data.items()
                if k
                not in (
                    "brand",
                    "model",
                    "vin",
                    "backend_name",
                    "preferred_zone",
                    "preferred_metro",
                )
            },
        )
        payload = bootstrap_session_node_assignment(
            allocator=allocator,
            context=ctx,
            preferred_zone=preferred_zone,
            preferred_metro=preferred_metro,
            provisioner=get_node_provisioner(),
            launch_spec_resolver=get_launch_spec_resolver(),
        )
        selected_zone = _read_text_field(payload.get("assignment", {}), "zone") or _read_text_field(
            payload.get("provisioning", {}),
            "zone",
        ) or preferred_zone
        selected_metro = _read_text_field(payload.get("assignment", {}), "metro") or _read_text_field(
            payload.get("provisioning", {}),
            "metro",
        ) or preferred_metro
        logger.info(
            "session bootstrap route decision selected_zone=%s selected_metro=%s route_source=%s fallback_used=%s signal_conflict=%s",
            selected_zone,
            selected_metro,
            _read_text_field(route_decision, "route_source") or "none",
            str(bool(route_decision.get("fallback_used"))).lower(),
            str(bool(route_decision.get("signal_conflict"))).lower(),
        )
        status = 202 if payload.get("pending_capacity") else 200
        return jsonify(payload), status

    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except RequestPayloadError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"success": False, "error": str(exc)}), 503
    except Exception:
        logger.exception("session_bootstrap failed")
        return jsonify(internal_error_payload()), 500


# ---------------------------------------------------------------------------
# POST /api/session/bootstrap/bind
# ---------------------------------------------------------------------------

@session_bp.route("/bootstrap/bind", methods=["POST"])
def session_bootstrap_bind():
    """Bind an existing bootstrap assignment to a real session id."""
    try:
        allocator = get_node_allocator()
        if allocator is None:
            return jsonify({"success": False, "error": "Node allocator is not configured"}), 503

        data = require_json_object(request)
        assignment_id = _read_text_field(data, "assignment_id")
        session_id = _read_text_field(data, "session_id")
        if not assignment_id:
            return jsonify({"success": False, "error": "assignment_id is required"}), 400
        if not session_id:
            return jsonify({"success": False, "error": "session_id is required"}), 400

        return jsonify(
            bind_session_node_assignment(
                allocator=allocator,
                assignment_id=assignment_id,
                session_id=session_id,
            )
        )
    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except RequestPayloadError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"success": False, "error": str(exc)}), 409
    except Exception:
        logger.exception("session_bootstrap_bind failed")
        return jsonify(internal_error_payload()), 500


# ---------------------------------------------------------------------------
# POST /api/session/bootstrap/release
# ---------------------------------------------------------------------------

@session_bp.route("/bootstrap/release", methods=["POST"])
def session_bootstrap_release():
    """Release one bootstrap assignment after session failure or completion."""
    try:
        allocator = get_node_allocator()
        if allocator is None:
            return jsonify({"success": False, "error": "Node allocator is not configured"}), 503

        data = require_json_object(request)
        assignment_id = _read_text_field(data, "assignment_id")
        recovery_action = _read_text_field(data, "recovery_action") or "idle"
        if not assignment_id:
            return jsonify({"success": False, "error": "assignment_id is required"}), 400
        if recovery_action not in {"idle", "reprobe"}:
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "recovery_action must be one of: idle, reprobe",
                    }
                ),
                400,
            )

        return jsonify(
            release_session_node_assignment(
                allocator=allocator,
                assignment_id=assignment_id,
                recovery_action=recovery_action,
            )
        )
    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except RequestPayloadError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"success": False, "error": str(exc)}), 409
    except Exception:
        logger.exception("session_bootstrap_release failed")
        return jsonify(internal_error_payload()), 500


# ---------------------------------------------------------------------------
# POST /api/session/start
# ---------------------------------------------------------------------------

@session_bp.route("/start", methods=["POST"])
def session_start():
    """Start a new diagnostics session.

    Request body (JSON)::

        {
            "brand": "Chevrolet",      # required
            "model": "Malibu",         # optional
            "vin": "1G1ZD5...",        # optional
        }

    Response::

        {
            "success": true,
            "session_id": "abc123...",
            "status": "running",       # or "awaiting_decision"
            "backend_name": "gds2",    # primary field
            "workflow": "gds2",        # deprecated alias
        }
    """
    try:
        data = require_json_object(request)
        brand = _read_text_field(data, "brand")

        if not brand:
            return jsonify({"success": False, "error": "brand is required"}), 400

        ctx = SessionContext(
            brand=brand,
            model=_read_text_field(data, "model"),
            vin=_read_text_field(data, "vin"),
            backend_name=_read_text_field(data, "backend_name"),
            extra={
                k: v
                for k, v in data.items()
                if k not in ("brand", "model", "vin", "backend_name")
            },
        )
        return jsonify(
            start_business_session(
                _runtime(),
                orchestrator=get_orchestrator(),
                context=ctx,
            )
        )

    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify(_active_session_conflict_payload(exc)), 409
    except RequestPayloadError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    except Exception as exc:
        logger.exception("session_start failed")
        return jsonify(internal_error_payload()), 500


# ---------------------------------------------------------------------------
# POST /api/session/start_diagnostics
# ---------------------------------------------------------------------------

@session_bp.route("/start_diagnostics", methods=["POST"])
def session_start_diagnostics():
    """Start GDS2 diagnostics via the deterministic executor.

    Executes START_DIAGNOSTICS through the DeterministicExecutor,
    which dispatches to the real DataViewerWorkflow.start().

    Request body (JSON)::

        {
            "session_id": "abc123..."
        }

    Response::

        {
            "success": true,
            "session_id": "abc123...",
            "result": { ... }  // modules or devices from GDS2
        }
    """
    try:
        data = require_json_object(request)
        session_id = _read_text_field(data, "session_id")

        if not session_id:
            return jsonify({"success": False, "error": "session_id required"}), 400

        payload = run_start_diagnostics(
            _runtime(),
            orchestrator=get_orchestrator(),
            backend=_get_backend(session_id),
            session_id=session_id,
        )
        if payload.get("result"):
            result = payload["result"]
            if "modules" in result:
                logger.debug(
                    "SESSION %s diagnostics started modules=%s device=%s",
                    session_id,
                    len(result["modules"]),
                    result.get("device") or '-',
                )
            elif "devices" in result:
                logger.debug(
                    "SESSION %s diagnostics awaiting device selection devices=%s",
                    session_id,
                    len(result["devices"]),
                )
            else:
                logger.debug(
                    "SESSION %s diagnostics started result_keys=%s",
                    session_id,
                    sorted(result.keys()),
                )
        return jsonify(payload)

    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 409
    except (OperationCancelledError, WorkerBusyError) as exc:
        return jsonify({"success": False, "error": str(exc)}), 409
    except RequestPayloadError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        logger.exception("session_start_diagnostics failed")
        return jsonify(internal_error_payload()), 500


# ---------------------------------------------------------------------------
# POST /api/session/execute
# ---------------------------------------------------------------------------

@session_bp.route("/execute", methods=["POST"])
def session_execute():
    """Execute a single backend action through the active backend contract.

    Generic endpoint for one backend-owned action. The active backend
    validates and executes the action behind the shared session facade.

    Request body (JSON)::

        {
            "session_id": "abc123...",
            "action": "select_module",    // backend-specific action id
            "args": {"module_name": "ECM"},  // action-specific arguments
            "timeout_sec": 30.0             // optional
        }
    """
    try:
        data = require_json_object(request)
        session_id = _read_text_field(data, "session_id")
        action_name = _read_text_field(data, "action")
        action_args = _read_object_field(data, "args")

        if not session_id:
            return jsonify({"success": False, "error": "session_id required"}), 400
        if not action_name:
            return jsonify({"success": False, "error": "action required"}), 400

        orch = get_orchestrator()
        session = orch.get_session(session_id)
        ensure_session_capability(session, BackendCapability.GENERIC_ACTIONS)

        logger.debug("SESSION %s action=%s start", session_id, action_name)

        try:
            try:
                backend = _get_backend(session_id)
            except TypeError:
                backend = _get_backend()
            outcome = execute_backend_action(
                session_id,
                backend=backend,
                action_name=action_name,
                action_args=action_args,
                timeout_sec=float(data.get("timeout_sec", 30.0)),
                emit_progress=lambda message: orch.emit_progress(session_id, message),
            )
        except BranchDecisionRequiredError as exc:
            payload = raise_branch_decision(
                orchestrator=orch,
                session_id=session_id,
                exc=exc,
                default_action=action_name,
            )
            logger.info(
                "SESSION %s action=%s awaiting decision=%s domain=%s options=%s",
                session_id,
                action_name,
                payload["decision"]["decision_id"],
                exc.decision.domain.value,
                len(exc.choices),
            )
            return jsonify(payload)

        if not outcome["success"]:
            logger.warning(
                "SESSION %s action=%s failed error=%s",
                session_id,
                action_name,
                outcome["error"],
            )
            return jsonify({
                "success": False,
                "session_id": session_id,
                "action": action_name,
                "error": outcome["error"],
                "attempts": outcome["attempts"],
                "elapsed_time": outcome["elapsed_time"],
            }), 500

        logger.debug(
            "SESSION %s action=%s completed attempts=%s elapsed=%.1fs",
            session_id,
            action_name,
            outcome["attempts"],
            outcome["elapsed_time"],
        )
        return jsonify({
            "success": True,
            "session_id": session_id,
            "action": action_name,
            "result": outcome["result"],
            "attempts": outcome["attempts"],
            "elapsed_time": outcome["elapsed_time"],
        })

    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404
    except UnsupportedCapabilityError as exc:
        return jsonify({"success": False, "error": str(exc)}), 501
    except ValueError as exc:
        status = 409 if "Session not running" in str(exc) else 400
        return jsonify({"success": False, "error": str(exc)}), status
    except RequestPayloadError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        logger.exception("session_execute failed")
        return jsonify(internal_error_payload()), 500


# ---------------------------------------------------------------------------
# GET /api/session/events?session_id=...
# ---------------------------------------------------------------------------

@session_bp.route("/events")
def session_events():
    """SSE stream for session events.

    Query params:
        session_id (str): required
    """
    try:
        session_id = read_text_mapping_field(request.args, "session_id")
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    if not session_id:
        return jsonify({"success": False, "error": "session_id required"}), 400

    try:
        return _sse_response(
            iter_session_events(
                orchestrator=get_orchestrator(),
                backend=_get_backend(session_id, required=False),
                session_id=session_id,
            )
        )
    except LookupError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404


# ---------------------------------------------------------------------------
# POST /api/session/decision
# ---------------------------------------------------------------------------

@session_bp.route("/decision", methods=["POST"])
def session_decision():
    """Submit a decision for a pending gate.

    Request body (JSON)::

        {
            "session_id": "abc123...",
            "decision_id": "def456...",
            "option_id": "gds2"
        }
    """
    try:
        data = require_json_object(request)
        session_id = _read_text_field(data, "session_id")
        decision_id = _read_text_field(data, "decision_id")
        option_id = _read_text_field(data, "option_id")

        if not session_id:
            return jsonify({"success": False, "error": "session_id required"}), 400
        if not decision_id:
            return jsonify({"success": False, "error": "decision_id required"}), 400
        if not option_id:
            return jsonify({"success": False, "error": "option_id required"}), 400

        return jsonify(
            submit_session_decision(
                _runtime(),
                orchestrator=get_orchestrator(),
                backend=_get_backend(session_id, required=False),
                session_id=session_id,
                decision_id=decision_id,
                option_id=option_id,
                get_data_viewer=_get_data_viewer,
                get_backend=lambda: _get_backend(session_id),
            )
        )

    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404

    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except RequestPayloadError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    except Exception as exc:
        logger.exception("session_decision failed")
        return jsonify(internal_error_payload()), 500


@session_bp.route("/select_module", methods=["POST"])
def session_select_module():
    """Select module within a running session.

    If ambiguous, emits decision_required via SessionOrchestrator and returns
    awaiting_decision payload.
    """
    try:
        data = require_json_object(request)
        session_id = _read_text_field(data, "session_id")
        module = _read_text_field(data, "module")

        if not session_id:
            return jsonify({"success": False, "error": "session_id required"}), 400
        if not module:
            return jsonify({"success": False, "error": "module required"}), 400

        orch = get_orchestrator()
        session = orch.get_session(session_id)
        if session.status != SessionStatus.RUNNING:
            return jsonify({
                "success": False,
                "error": f"Session not running (status={session.status.value})",
            }), 409

        try:
            outcome = select_module_action(
                _runtime(),
                session,
                module=module,
                backend=_get_backend(session_id),
                get_data_viewer=_get_data_viewer,
                get_executor=get_executor,
                get_adapter=get_adapter,
                emit_progress=lambda message: orch.emit_progress(session_id, message),
            )
        except BranchDecisionRequiredError as exc:
            payload = raise_branch_decision(
                orchestrator=orch,
                session_id=session_id,
                exc=exc,
                default_action="select_module",
            )
            logger.info(
                "SESSION %s select_module awaiting decision=%s options=%s",
                session_id,
                payload["decision"]["decision_id"],
                len(exc.choices),
            )
            return jsonify(payload)

        if not outcome["success"]:
            logger.warning("SESSION %s select_module failed error=%s", session_id, outcome["error"])
            return jsonify({
                "success": False,
                "session_id": session_id,
                "error": outcome["error"],
            }), 500

        logger.debug("SESSION %s module=%s selected", session_id, module)
        return jsonify({
            "success": True,
            "session_id": session_id,
            "result": outcome["result"],
        })

    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except RequestPayloadError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        logger.exception("session_select_module failed")
        return jsonify(internal_error_payload()), 500


@session_bp.route("/select_data_category", methods=["POST"])
def session_select_data_category():
    """Select data category within a running session.

    If ambiguous, emits decision_required and waits for /decision.
    """
    try:
        data = require_json_object(request)
        session_id = _read_text_field(data, "session_id")
        data_category = _read_text_field(data, "data_category")

        if not session_id:
            return jsonify({"success": False, "error": "session_id required"}), 400
        if not data_category:
            return jsonify({"success": False, "error": "data_category required"}), 400

        orch = get_orchestrator()
        session = orch.get_session(session_id)
        if session.status != SessionStatus.RUNNING:
            return jsonify({
                "success": False,
                "error": f"Session not running (status={session.status.value})",
            }), 409

        try:
            outcome = select_data_category_action(
                _runtime(),
                session,
                data_category=data_category,
                backend=_get_backend(session_id),
                get_data_viewer=_get_data_viewer,
                get_executor=get_executor,
                get_adapter=get_adapter,
                emit_progress=lambda message: orch.emit_progress(session_id, message),
            )
        except BranchDecisionRequiredError as exc:
            payload = raise_branch_decision(
                orchestrator=orch,
                session_id=session_id,
                exc=exc,
                default_action="select_data_category",
            )
            logger.info(
                "SESSION %s select_data_category awaiting decision=%s options=%s",
                session_id,
                payload["decision"]["decision_id"],
                len(exc.choices),
            )
            return jsonify(payload)

        if not outcome["success"]:
            logger.warning("SESSION %s select_data_category failed error=%s", session_id, outcome["error"])
            return jsonify({
                "success": False,
                "session_id": session_id,
                "error": outcome["error"],
            }), 500

        logger.debug("SESSION %s data_category=%s selected", session_id, data_category)
        return jsonify({
            "success": True,
            "session_id": session_id,
            "result": outcome["result"],
        })

    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except RequestPayloadError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        logger.exception("session_select_data_category failed")
        return jsonify(internal_error_payload()), 500


@session_bp.route("/ai_diagnose", methods=["POST"])
def session_ai_diagnose():
    """Start AI diagnosis through the public session facade."""
    try:
        data = require_json_object(request)
    except RequestPayloadError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    payload, status = session_ai_handlers.start_ai_diagnose(data)
    return jsonify(payload), status


@session_bp.route("/ai_diagnose/events")
def session_ai_diagnose_events():
    """Stream AI diagnosis SSE events through the business session id."""
    try:
        session_id = read_text_mapping_field(request.args, "session_id")
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    result = session_ai_handlers.stream_ai_diagnose_events(
        session_id,
        sse_response=_sse_response,
    )
    if isinstance(result, tuple):
        payload, status = result
        return jsonify(payload), status
    return result


@session_bp.route("/ai_diagnose/retry", methods=["POST"])
def session_ai_diagnose_retry():
    """Retry AI diagnosis through the public session facade."""
    try:
        data = require_json_object(request)
    except RequestPayloadError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    payload, status = session_ai_handlers.retry_ai_diagnose(data)
    return jsonify(payload), status


@session_bp.route("/dtcs", methods=["POST"])
def session_dtcs():
    """Read DTCs through the public session facade."""
    try:
        data = require_json_object(request)
    except RequestPayloadError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    payload, status = session_live_data_handlers.read_session_dtcs(data)
    return jsonify(payload), status


@session_bp.route("/clear_dtcs", methods=["POST"])
def session_clear_dtcs():
    """Clear DTCs through the public session facade."""
    try:
        data = require_json_object(request)
    except RequestPayloadError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    payload, status = session_live_data_handlers.clear_session_dtcs(data)
    return jsonify(payload), status


@session_bp.route("/live_data/start", methods=["POST"])
def session_live_data_start():
    """Start live data streaming through the public session facade."""
    try:
        data = require_json_object(request)
    except RequestPayloadError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    payload, status = session_live_data_handlers.start_live_data_session(data)
    return jsonify(payload), status


@session_bp.route("/live_data/events")
def session_live_data_events():
    """SSE endpoint for session-scoped live data events."""
    try:
        session_id = read_text_mapping_field(request.args, "session_id")
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    result = session_live_data_handlers.stream_live_data_events(
        session_id,
        sse_response=_sse_response,
    )
    if isinstance(result, tuple):
        payload, status = result
        return jsonify(payload), status
    return result


@session_bp.route("/live_data/stop", methods=["POST"])
def session_live_data_stop():
    """Stop live data streaming through the public session facade."""
    try:
        data = require_json_object(request)
    except RequestPayloadError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    payload, status = session_live_data_handlers.stop_live_data_session(data)
    return jsonify(payload), status


@session_bp.route("/navigate/start", methods=["POST"])
def session_navigate_start():
    """Start guided navigation through the public session facade."""
    try:
        data = require_json_object(request)
    except RequestPayloadError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    payload, status = session_navigation_handlers.start_navigation_session_for_business(data)
    return jsonify(payload), status


@session_bp.route("/navigate/events")
def session_navigate_events():
    """SSE endpoint for session-scoped navigation events."""
    try:
        session_id = read_text_mapping_field(request.args, "session_id")
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    result = session_navigation_handlers.stream_navigation_events(
        session_id,
        sse_response=_sse_response,
    )
    if isinstance(result, tuple):
        payload, status = result
        return jsonify(payload), status
    return result


@session_bp.route("/navigate/decision", methods=["POST"])
def session_navigate_decision():
    """Submit a navigation decision through the public session facade."""
    try:
        data = require_json_object(request)
    except RequestPayloadError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    payload, status = session_navigation_handlers.submit_navigation_decision_for_business(data)
    return jsonify(payload), status


@session_bp.route("/navigate/abort", methods=["POST"])
def session_navigate_abort():
    """Abort the active navigation sub-session for a business session."""
    try:
        data = require_json_object(request)
    except RequestPayloadError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    payload, status = session_navigation_handlers.abort_navigation_session_for_business(data)
    return jsonify(payload), status


@session_bp.route("/navigate/status")
def session_navigate_status():
    """Return navigation sub-session status by business session id."""
    try:
        session_id = read_text_mapping_field(request.args, "session_id")
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    payload, status = session_navigation_handlers.build_navigation_status_for_business(
        session_id
    )
    return jsonify(payload), status


# ---------------------------------------------------------------------------
# POST /api/session/abort
# ---------------------------------------------------------------------------

@session_bp.route("/abort", methods=["POST"])
def session_abort():
    """Abort a running or awaiting session.

    Request body (JSON)::

        {
            "session_id": "abc123...",
            "reason": "User cancelled"     # optional
        }
    """
    try:
        data = require_json_object(request)
        session_id = _read_text_field(data, "session_id")

        if not session_id:
            return jsonify({"success": False, "error": "session_id required"}), 400

        reason = _read_text_field(data, "reason")
        return jsonify(
            abort_business_session(
                _runtime(),
                orchestrator=get_orchestrator(),
                session_id=session_id,
                reason=reason,
                get_ai_engine=_get_ai_engine,
            )
        )

    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404

    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except RequestPayloadError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    except Exception as exc:
        logger.exception("session_abort failed")
        return jsonify(internal_error_payload()), 500


# ---------------------------------------------------------------------------
# GET /api/session/status?session_id=...
# ---------------------------------------------------------------------------

@session_bp.route("/status")
def session_status():
    """Return current session state.

    Query params:
        session_id (str): required
    """
    try:
        session_id = read_text_mapping_field(request.args, "session_id")
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    if not session_id:
        return jsonify({"success": False, "error": "session_id required"}), 400

    try:
        return jsonify(
            build_session_status_payload(
                _runtime(),
                orchestrator=get_orchestrator(),
                backend=_get_backend(session_id, required=False),
                session_id=session_id,
            )
        )

    except KeyError as exc:
        return jsonify({"success": False, "error": str(exc)}), 404

    except Exception as exc:
        logger.exception("session_status failed")
        return jsonify(internal_error_payload()), 500
