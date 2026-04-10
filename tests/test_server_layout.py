import importlib
import ast
import sys
import types
from pathlib import Path


def _install_fake_flask_stack(monkeypatch):
    class FakeFlask:
        def __init__(self, import_name):
            self.import_name = import_name
            self.blueprints = {}
            self._rules = []
            self._before_request_handlers = []
            self.url_map = types.SimpleNamespace(iter_rules=lambda: list(self._rules))

        def register_blueprint(self, blueprint):
            self.blueprints[blueprint.name] = blueprint
            self._rules.extend(
                types.SimpleNamespace(rule=f"{blueprint.url_prefix}{route}")
                for route in getattr(blueprint, "_registered_routes", [])
            )

        def before_request(self, fn):
            self._before_request_handlers.append(fn)
            return fn

    class FakeBlueprint:
        def __init__(self, name, import_name, url_prefix=""):
            self.name = name
            self.import_name = import_name
            self.url_prefix = url_prefix
            self._registered_routes = []

        def route(self, path, methods=None):
            def decorator(fn):
                self._registered_routes.append(path)
                return fn

            return decorator

    fake_flask = types.ModuleType("flask")
    fake_flask.Flask = FakeFlask
    fake_flask.Blueprint = FakeBlueprint
    fake_flask.Response = object
    fake_flask.jsonify = lambda payload=None, **kwargs: payload if payload is not None else kwargs
    fake_flask.request = types.SimpleNamespace(args={}, json=None, headers={}, path="/", method="GET")
    monkeypatch.setitem(sys.modules, "flask", fake_flask)

    fake_flask_cors = types.ModuleType("flask_cors")
    fake_flask_cors.CORS = lambda app, *args, **kwargs: app
    monkeypatch.setitem(sys.modules, "flask_cors", fake_flask_cors)
    monkeypatch.delitem(sys.modules, "server.app", raising=False)
    monkeypatch.delitem(sys.modules, "server.api.diagnostics", raising=False)
    monkeypatch.delitem(sys.modules, "server.api.navigate", raising=False)
    monkeypatch.delitem(sys.modules, "server.api.session", raising=False)
    monkeypatch.delitem(sys.modules, "server.api.http_utils", raising=False)


def test_server_app_factory_registers_supported_blueprints(monkeypatch):
    _install_fake_flask_stack(monkeypatch)
    server_app = importlib.import_module("server.app")

    app = server_app.create_app()
    routes = {rule.rule for rule in app.url_map.iter_rules()}

    assert {"diagnostics", "session", "navigate"} <= set(app.blueprints)
    assert "/api/diagnose/start" in routes
    assert "/api/diagnose/clear_dtcs" in routes
    assert "/api/session/start" in routes
    assert "/api/session/clear_dtcs" in routes
    assert "/api/navigate/start" in routes


def test_root_app_delegates_to_server_package(monkeypatch):
    _install_fake_flask_stack(monkeypatch)
    fake_server_pkg = types.ModuleType("server")
    fake_server_app = types.ModuleType("server.app")
    fake_server_app.app = object()
    fake_server_app.create_app = lambda: fake_server_app.app
    fake_server_app.main = lambda argv=None: 0
    monkeypatch.setitem(sys.modules, "server", fake_server_pkg)
    monkeypatch.setitem(sys.modules, "server.app", fake_server_app)
    root_app = importlib.import_module("app")

    assert root_app.app is fake_server_app.app


def test_server_runtime_settings_default_to_local_and_safe(monkeypatch):
    _install_fake_flask_stack(monkeypatch)
    server_app = importlib.import_module("server.app")

    settings = server_app.resolve_server_settings([])

    assert settings.host == "127.0.0.1"
    assert settings.port == 8080
    assert settings.debug is False
    assert settings.enable_cors is False


def test_server_runtime_settings_support_opt_in_public_bindings(monkeypatch):
    _install_fake_flask_stack(monkeypatch)
    server_app = importlib.import_module("server.app")

    settings = server_app.resolve_server_settings(
        ["--public", "--debug", "--port", "9090"],
        environ={
            "DIAGNOSTIC_API_ENABLE_CORS": "1",
            "DIAGNOSTIC_API_CORS_ORIGINS": "http://localhost:3000,https://example.com",
            "DIAGNOSTIC_API_TOKEN": "secret-token",
        },
    )

    assert settings.host == "0.0.0.0"
    assert settings.port == 9090
    assert settings.debug is True
    assert settings.enable_cors is True
    assert settings.cors_origins == ("http://localhost:3000", "https://example.com")
    assert settings.api_token == "secret-token"


def _imported_modules(path: str) -> set[str]:
    source = Path(path).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=path)
    imported: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    return imported


def test_gds2_modules_use_shared_runtime_errors_module_for_cancellation() -> None:
    controller_imports = _imported_modules("src/navigation/controller.py")
    backend_imports = _imported_modules("backends/gds2/backend.py")

    assert "diagnostic_platform.runtime.errors" in controller_imports
    assert "diagnostic_platform.runtime.errors" in backend_imports
    assert "diagnostic_platform.runtime.worker_runtime" not in controller_imports
    assert "diagnostic_platform.runtime.worker_runtime" not in backend_imports
