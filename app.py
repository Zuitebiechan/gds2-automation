"""
GDS2 Automation Flask Backend - API Service Layer

Shared infrastructure for mechanic-facing client APIs.
Provides foundation for diagnostics, navigation, and session management.

API Endpoints:
- Diagnostics API: /api/diagnose/* - Remote vehicle diagnostics workflow
- Navigate API: /api/navigate/* - LangGraph-based agentic navigation
- Session API: /api/session/* - Agentic diagnostic session management
"""

from flask import Flask
from flask_cors import CORS
import logging

_bootstrap_logs: list[tuple[str, str]] = []

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
    from pathlib import Path
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
        _bootstrap_logs.append(("debug", f"Loaded environment variables from {env_path}"))
    else:
        _bootstrap_logs.append(("debug", f".env file not found at {env_path}"))
except ImportError:
    _bootstrap_logs.append(("debug", "python-dotenv not installed"))

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler('gds2_web.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

for level, message in _bootstrap_logs:
    getattr(logger, level)(message)

app = Flask(__name__)
CORS(app)

from diagnostics_api import diagnostics_bp
app.register_blueprint(diagnostics_bp)

from session_api import session_bp
app.register_blueprint(session_bp)

from navigate_api import navigate_bp
app.register_blueprint(navigate_bp)


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

    logger.info("API starting host=%s port=%s", host, port)
    if host == '0.0.0.0':
        logger.info("API endpoints local=http://localhost:%s remote=http://%s:%s", port, local_ip, port)
    else:
        logger.info("API endpoint http://localhost:%s", port)
    logger.info("API routes /api/diagnose/* /api/navigate/* /api/session/*")

    app.run(debug=True, host=host, port=port, use_reloader=False, threaded=True)
