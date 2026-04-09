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

from flask import Flask, jsonify, request
from flask_cors import CORS

from server.api.diagnostics import diagnostics_bp
from server.api.navigate import navigate_bp
from server.api.session import session_bp

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("gds2_web.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

for level, message in _bootstrap_logs:
    getattr(logger, level)(message)


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


def create_app(settings: ServerRuntimeSettings | None = None) -> Flask:
    resolved_settings = settings or resolve_server_settings([])
    app = Flask(__name__)
    _apply_cors(app, resolved_settings)
    _install_api_token_guard(app, resolved_settings)
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
    settings = resolve_server_settings(argv)
    runtime_app = create_app(settings)

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

    runtime_app.run(
        debug=settings.debug,
        host=settings.host,
        port=settings.port,
        use_reloader=False,
        threaded=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
