"""
GDS2 automation HTTP server bootstrap.

Owns Flask initialization and blueprint registration.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hmac
import logging
import os
import socket
import sys
import time

from flask import Flask, jsonify, request
from flask_cors import CORS

from diagnostic_platform.observability import (
    LogContext,
    build_snapshot_log_context,
    emit_event,
    generate_request_id,
    get_product_log_writer,
    install_observability_log_handler,
)
from diagnostic_platform.observability_artifacts import (
    cleanup_product_observability,
    resolve_product_log_settings,
)
from server.api.diagnostics import diagnostics_bp
from server.api.navigate import navigate_bp
from server.api.session import session_bp
from server.api.session_dependencies import (
    configure_node_allocator_from_env,
    configure_node_readiness_from_env,
    configure_node_provisioning_from_env,
    start_node_readiness_monitor,
    stop_node_readiness_monitor,
)

_bootstrap_logs: list[tuple[str, str]] = []
_TRUTHY_VALUES = {"1", "true", "yes", "on"}

try:
    from dotenv import load_dotenv

    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
        _bootstrap_logs.append(("debug", f"Loaded environment variables from {env_path}"))
    else:
        _bootstrap_logs.append(("debug", f".env file not found at {env_path}"))
except ImportError:
    _bootstrap_logs.append(("debug", "python-dotenv not installed"))


def _resolve_server_log_path(
    file_name: str = "gds2_web.log",
    *,
    environ: dict[str, str] | None = None,
) -> Path:
    env = os.environ if environ is None else environ
    configured_dir = str(env.get("LOG_DIR", "") or "").strip()
    if configured_dir:
        log_dir = Path(configured_dir)
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            return log_dir / file_name
        except OSError as exc:
            _bootstrap_logs.append(
                (
                    "warning",
                    f"Failed to create LOG_DIR {log_dir}: {exc}; falling back to {file_name}",
                )
            )
    return Path(file_name)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(_resolve_server_log_path(), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

for level, message in _bootstrap_logs:
    getattr(logger, level)(message)


def _install_runtime_log_observability() -> None:
    install_observability_log_handler(
        logging.getLogger(),
        component="server.runtime",
        writer=get_product_log_writer("server.runtime"),
        context_provider=lambda _record: build_snapshot_log_context(
            operation_kind="server_runtime"
        ),
    )


def _emit_server_runtime_event(
    event_type: str,
    *,
    status: str = "ok",
    failure_code: str | None = None,
    reason: str | None = None,
    **extra: object,
) -> dict[str, object]:
    return emit_event(
        get_product_log_writer("server.runtime"),
        component="server.runtime",
        event_type=event_type,
        context=build_snapshot_log_context(operation_kind="server_runtime"),
        status=status,
        failure_code=failure_code,
        failure_domain="unknown",
        reason=reason,
        impact_scope="server_runtime",
        **extra,
    )


@dataclass(frozen=True)
class ServerRuntimeSettings:
    host: str = "127.0.0.1"
    port: int = 8080
    debug: bool = False
    enable_cors: bool = False
    cors_origins: tuple[str, ...] = ()
    api_token: str | None = None


def _is_truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUTHY_VALUES


def _parse_cors_origins(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(
        origin.strip()
        for origin in value.split(",")
        if origin.strip()
    )


def resolve_server_settings(
    argv: list[str] | None = None,
    *,
    environ: dict[str, str] | None = None,
) -> ServerRuntimeSettings:
    env = os.environ if environ is None else environ
    args = list(sys.argv[1:] if argv is None else argv)

    host = (env.get("DIAGNOSTIC_API_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    port = int((env.get("DIAGNOSTIC_API_PORT") or "8080").strip())
    debug = _is_truthy(env.get("DIAGNOSTIC_API_DEBUG"))
    enable_cors = _is_truthy(env.get("DIAGNOSTIC_API_ENABLE_CORS"))
    cors_origins = _parse_cors_origins(env.get("DIAGNOSTIC_API_CORS_ORIGINS"))
    api_token = (env.get("DIAGNOSTIC_API_TOKEN") or "").strip() or None

    if "--port" in args:
        idx = args.index("--port")
        if idx + 1 < len(args):
            port = int(args[idx + 1])

    if "--public" in args:
        host = "0.0.0.0"
    if "--local" in args:
        host = "127.0.0.1"
    if "--debug" in args:
        debug = True
    if "--cors" in args:
        enable_cors = True

    return ServerRuntimeSettings(
        host=host,
        port=port,
        debug=debug,
        enable_cors=enable_cors,
        cors_origins=cors_origins,
        api_token=api_token,
    )


def _apply_cors(app: Flask, settings: ServerRuntimeSettings) -> None:
    if not settings.enable_cors:
        return

    if settings.cors_origins:
        CORS(
            app,
            resources={
                r"/api/*": {
                    "origins": list(settings.cors_origins),
                }
            },
        )
        return

    CORS(app)


def _extract_request_token() -> str:
    headers = getattr(request, "headers", {}) or {}
    auth_header = str(headers.get("Authorization") or "").strip()
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return str(headers.get("X-API-Token") or "").strip()


def _install_api_request_observability(app: Flask) -> None:
    @app.before_request
    def _bind_api_request_id():
        request_path = str(getattr(request, "path", "") or "")
        if not request_path.startswith("/api/"):
            return None
        if not getattr(request, "request_id", None):
            setattr(request, "request_id", generate_request_id())
        setattr(request, "_request_started_at", time.perf_counter())
        return None


def _extract_request_session_id() -> str | None:
    request_json = None
    get_json = getattr(request, "get_json", None)
    if callable(get_json):
        try:
            request_json = get_json(silent=True)
        except Exception:
            request_json = None
    if isinstance(request_json, dict):
        session_id = request_json.get("session_id")
        if isinstance(session_id, str) and session_id.strip():
            return session_id.strip()
    request_args = getattr(request, "args", None)
    session_id = None
    get_value = getattr(request_args, "get", None)
    if callable(get_value):
        session_id = get_value("session_id")
    if isinstance(session_id, str) and session_id.strip():
        return session_id.strip()
    return None


def _install_api_token_guard(app: Flask, settings: ServerRuntimeSettings) -> None:
    if not settings.api_token:
        return

    @app.before_request
    def _require_api_token():
        if getattr(request, "method", "GET") == "OPTIONS":
            return None
        if not str(getattr(request, "path", "") or "").startswith("/api/"):
            return None

        provided_token = _extract_request_token()
        if not provided_token or not hmac.compare_digest(provided_token, settings.api_token):
            return jsonify({"success": False, "error": "Unauthorized"}), 401
        return None


def _install_api_failure_logger(app: Flask) -> None:
    writer = get_product_log_writer("server.api")

    @app.after_request
    def _log_api_failure(response):
        status_code = int(getattr(response, "status_code", 200) or 200)
        request_path = str(getattr(request, "path", "") or "")
        if not request_path.startswith("/api/"):
            return response

        request_started_at = getattr(request, "_request_started_at", None)
        duration_ms = None
        if isinstance(request_started_at, (float, int)):
            duration_ms = round((time.perf_counter() - float(request_started_at)) * 1000.0, 3)

        emit_event(
            writer,
            component="server.api",
            event_type="api.request.completed",
            context=LogContext(
                session_id=_extract_request_session_id(),
                operation_kind=f"http:{str(getattr(request, 'method', 'GET') or 'GET')} {request_path}",
                request_id=str(getattr(request, "request_id", "") or generate_request_id()),
            ),
            status="error" if status_code >= 400 else "ok",
            failure_code=f"http_{status_code}" if status_code >= 400 else None,
            reason=f"http_status_{status_code}" if status_code >= 400 else None,
            duration_ms=duration_ms,
            impact_scope=f"http:{request_path}",
            http_status=status_code,
            http_method=str(getattr(request, "method", "GET") or "GET"),
            endpoint=str(getattr(request, "endpoint", "") or "-"),
            remote_addr=str(getattr(request, "remote_addr", "") or "-"),
            request_id=str(getattr(request, "request_id", "") or ""),
        )

        if status_code < 400:
            return response

        level = logging.ERROR if status_code >= 500 else logging.WARNING
        logger.log(
            level,
            "API %s %s -> %s endpoint=%s remote=%s",
            str(getattr(request, "method", "GET") or "GET"),
            request_path,
            status_code,
            str(getattr(request, "endpoint", "") or "-"),
            str(getattr(request, "remote_addr", "") or "-"),
        )
        return response


def create_app(settings: ServerRuntimeSettings | None = None) -> Flask:
    resolved_settings = settings or resolve_server_settings([])
    app = Flask(__name__)
    log_settings = resolve_product_log_settings()
    if log_settings.enabled:
        cleanup_product_observability(
            programdata=os.environ.get("PROGRAMDATA"),
            retention_days_raw=log_settings.retention_days_raw,
            retention_days_session_trace=log_settings.retention_days_session_trace,
            retention_days_incident=log_settings.retention_days_incident,
        )
    configure_node_allocator_from_env()
    configure_node_provisioning_from_env()
    configure_node_readiness_from_env()
    _apply_cors(app, resolved_settings)
    _install_api_request_observability(app)
    _install_api_token_guard(app, resolved_settings)
    _install_api_failure_logger(app)
    app.register_blueprint(diagnostics_bp)
    app.register_blueprint(session_bp)
    app.register_blueprint(navigate_bp)
    return app


app = create_app()


def _disable_windows_quick_edit() -> None:
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-10)
        mode = ctypes.c_ulong()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        new_mode = (mode.value | 0x0080) & ~0x0040
        kernel32.SetConsoleMode(handle, new_mode)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    _disable_windows_quick_edit()
    _install_runtime_log_observability()
    try:
        settings = resolve_server_settings(argv)
    except Exception as exc:
        _emit_server_runtime_event(
            "process.lifecycle.failed",
            status="error",
            failure_code=type(exc).__name__,
            reason=str(exc),
            stage="resolve_server_settings",
            argv=list(sys.argv[1:] if argv is None else argv),
        )
        raise
    readiness_started = False
    run_completed = False

    try:
        runtime_app = create_app(settings)
        readiness_started = start_node_readiness_monitor()

        local_ip = "127.0.0.1"
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect(("8.8.8.8", 80))
            local_ip = sock.getsockname()[0]
            sock.close()
        except Exception:
            pass

        logger.info("API starting host=%s port=%s debug=%s", settings.host, settings.port, settings.debug)
        if settings.enable_cors:
            logger.info("API CORS enabled origins=%s", list(settings.cors_origins) or ["*"])
        if settings.api_token:
            logger.info("API token authentication enabled")
        if settings.host == "0.0.0.0":
            logger.info(
                "API endpoints local=http://localhost:%s remote=http://%s:%s",
                settings.port,
                local_ip,
                settings.port,
            )
        else:
            logger.info("API endpoint http://localhost:%s", settings.port)
        logger.info("API routes /api/diagnose/* /api/navigate/* /api/session/*")
        if readiness_started:
            logger.info("Booting-node readiness monitor started")

        _emit_server_runtime_event(
            "process.lifecycle.starting",
            reason="server_starting",
            host=settings.host,
            port=settings.port,
            debug=settings.debug,
            cors_enabled=settings.enable_cors,
            api_token_enabled=bool(settings.api_token),
        )
        runtime_app.run(
            debug=settings.debug,
            host=settings.host,
            port=settings.port,
            use_reloader=False,
            threaded=True,
        )
        run_completed = True
    except Exception as exc:
        _emit_server_runtime_event(
            "process.lifecycle.failed",
            status="error",
            failure_code=type(exc).__name__,
            reason=str(exc),
            host=settings.host,
            port=settings.port,
        )
        raise
    finally:
        if run_completed:
            _emit_server_runtime_event(
                "process.lifecycle.shutdown_started",
                reason="server_stopping",
                host=settings.host,
                port=settings.port,
            )
            _emit_server_runtime_event(
                "process.lifecycle.shutdown_finished",
                reason="server_stopped",
                host=settings.host,
                port=settings.port,
            )
        stop_node_readiness_monitor()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
