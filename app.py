"""
GDS2 Automation Flask Backend - API Service Layer

Shared infrastructure for mechanic-facing client APIs.
Provides foundation for diagnostics, navigation, and session management.

API Endpoints:
- Diagnostics API: /api/diagnose/* - Remote vehicle diagnostics workflow
- Navigate API: /api/navigate/* - LangGraph-based agentic navigation
- Session API: /api/session/* - Agentic diagnostic session management
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

from flask import Flask
from flask_cors import CORS
import logging

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
    print(f"  GDS2 Automation Flask Backend")
    print(f"{'='*60}")
    if host == '0.0.0.0':
        print(f"\n  Local:  http://localhost:{port}")
        print(f"  Remote: http://{local_ip}:{port}")
    else:
        print(f"\n  http://localhost:{port}")
    print(f"\n  Diagnostics: /api/diagnose/*")
    print(f"  Navigate:    /api/navigate/*")
    print(f"  Session:     /api/session/*")
    print(f"{'='*60}\n")

    app.run(debug=True, host=host, port=port, use_reloader=False, threaded=True)
