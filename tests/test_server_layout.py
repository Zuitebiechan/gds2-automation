import importlib
import sys
import types


def _install_fake_flask_stack(monkeypatch):
    class FakeFlask:
        def __init__(self, import_name):
            self.import_name = import_name
            self.blueprints = {}
            self._rules = []
            self.url_map = types.SimpleNamespace(iter_rules=lambda: list(self._rules))

        def register_blueprint(self, blueprint):
            self.blueprints[blueprint.name] = blueprint
            self._rules.extend(
                types.SimpleNamespace(rule=f"{blueprint.url_prefix}{route}")
                for route in getattr(blueprint, "_registered_routes", [])
            )

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
    fake_flask.request = types.SimpleNamespace(args={}, json=None)
    monkeypatch.setitem(sys.modules, "flask", fake_flask)

    fake_flask_cors = types.ModuleType("flask_cors")
    fake_flask_cors.CORS = lambda app: app
    monkeypatch.setitem(sys.modules, "flask_cors", fake_flask_cors)


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
