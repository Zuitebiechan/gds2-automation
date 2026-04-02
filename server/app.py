"""
GDS2 automation HTTP server bootstrap.

Owns Flask initialization and blueprint registration.
"""

from pathlib import Path
import logging
import socket
import sys

from flask import Flask
from flask_cors import CORS

from server.api.diagnostics import diagnostics_bp
from server.api.navigate import navigate_bp
from server.api.session import session_bp

_bootstrap_logs: list[tuple[str, str]] = []

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


def create_app() -> Flask:
    app = Flask(__name__)
    CORS(app)
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
    args = list(sys.argv[1:] if argv is None else argv)

    _disable_windows_quick_edit()

    port = 8080
    host = "0.0.0.0"

    if "--port" in args:
        idx = args.index("--port")
        if idx + 1 < len(args):
            port = int(args[idx + 1])

    if "--local" in args:
        host = "127.0.0.1"

    local_ip = "127.0.0.1"
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        local_ip = sock.getsockname()[0]
        sock.close()
    except Exception:
        pass

    logger.info("API starting host=%s port=%s", host, port)
    if host == "0.0.0.0":
        logger.info("API endpoints local=http://localhost:%s remote=http://%s:%s", port, local_ip, port)
    else:
        logger.info("API endpoint http://localhost:%s", port)
    logger.info("API routes /api/diagnose/* /api/navigate/* /api/session/*")

    app.run(debug=True, host=host, port=port, use_reloader=False, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
