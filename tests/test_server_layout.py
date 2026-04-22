import importlib
import ast
import json
import logging
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
            self._after_request_handlers = []
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

        def after_request(self, fn):
            self._after_request_handlers.append(fn)
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
    assert "/api/session/bootstrap/ready" in routes
    assert "/api/navigate/start" in routes


def test_server_app_factory_installs_api_failure_logger(monkeypatch):
    _install_fake_flask_stack(monkeypatch)
    server_app = importlib.import_module("server.app")

    app = server_app.create_app()

    assert len(app._after_request_handlers) == 1


def test_api_failure_logger_emits_only_for_api_error_responses(monkeypatch, caplog):
    _install_fake_flask_stack(monkeypatch)
    server_app = importlib.import_module("server.app")
    fake_flask = sys.modules["flask"]
    app = server_app.create_app()
    handler = app._after_request_handlers[0]

    fake_flask.request.path = "/api/session/start"
    fake_flask.request.method = "POST"
    fake_flask.request.endpoint = "session_start"
    fake_flask.request.remote_addr = "10.0.0.5"

    ok_response = types.SimpleNamespace(status_code=200)
    with caplog.at_level(logging.WARNING):
        returned = handler(ok_response)

    assert returned is ok_response
    assert caplog.text == ""

    error_response = types.SimpleNamespace(status_code=502)
    with caplog.at_level(logging.ERROR):
        returned = handler(error_response)

    assert returned is error_response
    assert "API POST /api/session/start -> 502 endpoint=session_start remote=10.0.0.5" in caplog.text


def test_extract_request_session_id_falls_back_to_query_args_without_touching_request_json(monkeypatch):
    _install_fake_flask_stack(monkeypatch)
    server_app = importlib.import_module("server.app")

    class _ArgsLike:
        def get(self, key, default=None):
            if key == "session_id":
                return "session-get-1"
            return default

    class _Request:
        args = _ArgsLike()

        @staticmethod
        def get_json(silent=False):
            raise RuntimeError("json parsing should fail safely")

        @property
        def json(self):
            raise AssertionError("request.json must not be accessed")

    monkeypatch.setattr(server_app, "request", _Request())

    assert server_app._extract_request_session_id() == "session-get-1"


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


def test_server_app_factory_configures_file_backed_node_allocator_from_env(monkeypatch, tmp_path):
    _install_fake_flask_stack(monkeypatch)
    monkeypatch.setenv("DIAGNOSTIC_NODE_INVENTORY_FILE", str(tmp_path / "inventory.json"))
    monkeypatch.setenv("DIAGNOSTIC_NODE_LEASE_FILE", str(tmp_path / "leases.json"))

    server_app = importlib.import_module("server.app")
    session_dependencies = importlib.import_module("server.api.session_dependencies")
    session_dependencies.set_node_allocator(None)

    server_app.create_app()

    allocator = session_dependencies.get_node_allocator()
    assert allocator is not None
    assert (tmp_path / "inventory.json").exists()
    assert (tmp_path / "leases.json").exists()


def test_server_app_factory_configures_aws_provisioner_and_launch_spec_resolver_from_env(
    monkeypatch,
    tmp_path,
):
    _install_fake_flask_stack(monkeypatch)
    fake_boto3 = types.ModuleType("boto3")
    observed: dict[str, object] = {}

    def _client(name, region_name=None):
        observed["service_name"] = name
        observed["region_name"] = region_name
        return {"service": name, "region_name": region_name}

    fake_boto3.client = _client
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setenv("DIAGNOSTIC_NODE_INVENTORY_FILE", str(tmp_path / "inventory.json"))
    monkeypatch.setenv("DIAGNOSTIC_NODE_LEASE_FILE", str(tmp_path / "leases.json"))
    monkeypatch.setenv("DIAGNOSTIC_AWS_REGION", "us-west-2")
    monkeypatch.setenv("DIAGNOSTIC_NODE_LAUNCH_TEMPLATE", "diag-local-zone-worker")
    monkeypatch.setenv("DIAGNOSTIC_NODE_INSTANCE_TYPE", "m6i.xlarge")
    monkeypatch.setenv("DIAGNOSTIC_NODE_SUBNET_ID", "subnet-12345")
    monkeypatch.setenv("DIAGNOSTIC_NODE_SECURITY_GROUP_IDS", "sg-12345,sg-67890")
    monkeypatch.setenv("DIAGNOSTIC_NODE_DEFAULT_ZONE", "us-west-2-lax-1a")
    monkeypatch.setenv("DIAGNOSTIC_NODE_DEFAULT_METRO", "los-angeles")
    monkeypatch.setenv("DIAGNOSTIC_NODE_DEFAULT_API_BASE", "https://lax-1.diag.example.com")
    monkeypatch.setenv("DIAGNOSTIC_NODE_DEFAULT_TUNNEL_HOST", "lax-1.diag.example.com")

    server_app = importlib.import_module("server.app")
    session_dependencies = importlib.import_module("server.api.session_dependencies")
    session_dependencies.set_node_allocator(None)
    session_dependencies.set_node_provisioner(None)
    session_dependencies.set_launch_spec_resolver(None)

    server_app.create_app()

    provisioner = session_dependencies.get_node_provisioner()
    resolver = session_dependencies.get_launch_spec_resolver()
    spec = resolver(
        context=types.SimpleNamespace(brand="Chevrolet"),
        preferred_zone="",
        preferred_metro="",
    )

    assert provisioner is not None
    assert resolver is not None
    assert observed == {
        "service_name": "ec2",
        "region_name": "us-west-2",
    }
    assert spec.zone == "us-west-2-lax-1a"
    assert spec.metro == "los-angeles"
    assert spec.launch_template_name == "diag-local-zone-worker"
    assert spec.instance_type == "m6i.xlarge"
    assert spec.subnet_id == "subnet-12345"
    assert spec.security_group_ids == ("sg-12345", "sg-67890")
    assert spec.api_base_url == "https://lax-1.diag.example.com"
    assert spec.tunnel_host == "lax-1.diag.example.com"


def test_server_app_factory_launch_spec_resolver_prefers_requested_route_over_defaults(
    monkeypatch,
    tmp_path,
):
    _install_fake_flask_stack(monkeypatch)
    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda name, region_name=None: {
        "service": name,
        "region_name": region_name,
    }
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setenv("DIAGNOSTIC_NODE_INVENTORY_FILE", str(tmp_path / "inventory.json"))
    monkeypatch.setenv("DIAGNOSTIC_NODE_LEASE_FILE", str(tmp_path / "leases.json"))
    monkeypatch.setenv("DIAGNOSTIC_AWS_REGION", "us-west-2")
    monkeypatch.setenv("DIAGNOSTIC_NODE_LAUNCH_TEMPLATE", "diag-local-zone-worker")
    monkeypatch.setenv("DIAGNOSTIC_NODE_INSTANCE_TYPE", "m6i.xlarge")
    monkeypatch.setenv("DIAGNOSTIC_NODE_SUBNET_ID", "subnet-12345")
    monkeypatch.setenv("DIAGNOSTIC_NODE_DEFAULT_ZONE", "us-west-2-lax-1a")
    monkeypatch.setenv("DIAGNOSTIC_NODE_DEFAULT_METRO", "los-angeles")
    monkeypatch.setenv("DIAGNOSTIC_NODE_DEFAULT_API_BASE", "https://lax-1.diag.example.com")
    monkeypatch.setenv("DIAGNOSTIC_NODE_DEFAULT_TUNNEL_HOST", "lax-1.diag.example.com")

    server_app = importlib.import_module("server.app")
    session_dependencies = importlib.import_module("server.api.session_dependencies")
    session_dependencies.set_node_allocator(None)
    session_dependencies.set_node_provisioner(None)
    session_dependencies.set_launch_spec_resolver(None)

    server_app.create_app()

    resolver = session_dependencies.get_launch_spec_resolver()
    spec = resolver(
        context=types.SimpleNamespace(brand="Chevrolet"),
        preferred_zone="us-east-1-atl-2a",
        preferred_metro="atlanta",
    )

    assert spec.zone == "us-east-1-atl-2a"
    assert spec.metro == "atlanta"


def test_server_app_factory_configures_node_readiness_monitor_from_env(
    monkeypatch,
    tmp_path,
):
    _install_fake_flask_stack(monkeypatch)
    monkeypatch.setenv("DIAGNOSTIC_NODE_INVENTORY_FILE", str(tmp_path / "inventory.json"))
    monkeypatch.setenv("DIAGNOSTIC_NODE_LEASE_FILE", str(tmp_path / "leases.json"))
    monkeypatch.setenv("DIAGNOSTIC_NODE_READINESS_ENABLED", "1")
    monkeypatch.setenv("DIAGNOSTIC_NODE_READINESS_INTERVAL_SEC", "7")
    monkeypatch.setenv("DIAGNOSTIC_NODE_READINESS_TIMEOUT_SEC", "4")
    monkeypatch.setenv("DIAGNOSTIC_NODE_READINESS_PATH", "/api/session/bootstrap/ready")
    monkeypatch.setenv("DIAGNOSTIC_NODE_READINESS_API_TOKEN", "secret-token")

    server_app = importlib.import_module("server.app")
    session_dependencies = importlib.import_module("server.api.session_dependencies")
    session_dependencies.set_node_allocator(None)
    session_dependencies.set_node_readiness_monitor(None)

    server_app.create_app()

    monitor = session_dependencies.get_node_readiness_monitor()

    assert monitor is not None
    assert monitor.poll_interval_sec == 7.0


def test_server_app_factory_zone_catalog_routes_to_requested_metro_with_zone_specific_subnet_and_hosts(
    monkeypatch,
    tmp_path,
):
    _install_fake_flask_stack(monkeypatch)
    fake_boto3 = types.ModuleType("boto3")
    observed: dict[str, object] = {}

    def _client(name, region_name=None):
        observed["service_name"] = name
        observed["region_name"] = region_name
        return {"service": name, "region_name": region_name}

    fake_boto3.client = _client
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setenv("DIAGNOSTIC_NODE_INVENTORY_FILE", str(tmp_path / "inventory.json"))
    monkeypatch.setenv("DIAGNOSTIC_NODE_LEASE_FILE", str(tmp_path / "leases.json"))
    monkeypatch.setenv("DIAGNOSTIC_AWS_REGION", "us-east-1")
    monkeypatch.setenv("DIAGNOSTIC_NODE_LAUNCH_TEMPLATE", "diag-local-zone-worker")
    monkeypatch.setenv("DIAGNOSTIC_NODE_INSTANCE_TYPE", "c6i.xlarge")
    monkeypatch.setenv("DIAGNOSTIC_NODE_SECURITY_GROUP_IDS", "sg-default")
    monkeypatch.setenv("DIAGNOSTIC_NODE_DEFAULT_ZONE", "us-east-1-dfw-2a")
    monkeypatch.setenv(
        "DIAGNOSTIC_NODE_ZONE_CATALOG_JSON",
        json.dumps(
            {
                "zones": [
                    {
                        "zone": "us-east-1-dfw-2a",
                        "metro": "dallas",
                        "subnet_id": "subnet-dfw",
                        "api_base_url": "https://dfw.diag.example.com",
                        "tunnel_host": "dfw.diag.example.com",
                    },
                    {
                        "zone": "us-east-1-atl-2a",
                        "metro": "atlanta",
                        "subnet_id": "subnet-atl",
                        "api_base_url": "https://atl.diag.example.com",
                        "tunnel_host": "atl.diag.example.com",
                        "instance_type": "m6i.xlarge",
                        "security_group_ids": ["sg-atl-1", "sg-atl-2"],
                    },
                ]
            }
        ),
    )

    server_app = importlib.import_module("server.app")
    session_dependencies = importlib.import_module("server.api.session_dependencies")
    session_dependencies.set_node_allocator(None)
    session_dependencies.set_node_provisioner(None)
    session_dependencies.set_launch_spec_resolver(None)

    server_app.create_app()

    provisioner = session_dependencies.get_node_provisioner()
    resolver = session_dependencies.get_launch_spec_resolver()
    spec = resolver(
        context=types.SimpleNamespace(brand="Chevrolet"),
        preferred_zone="",
        preferred_metro="atlanta",
    )

    assert provisioner is not None
    assert observed == {
        "service_name": "ec2",
        "region_name": "us-east-1",
    }
    assert spec.zone == "us-east-1-atl-2a"
    assert spec.metro == "atlanta"
    assert spec.subnet_id == "subnet-atl"
    assert spec.api_base_url == "https://atl.diag.example.com"
    assert spec.tunnel_host == "atl.diag.example.com"
    assert spec.instance_type == "m6i.xlarge"
    assert spec.launch_template_name == "diag-local-zone-worker"
    assert spec.security_group_ids == ("sg-atl-1", "sg-atl-2")


def test_server_app_factory_zone_catalog_uses_default_zone_entry_when_request_has_no_preference(
    monkeypatch,
    tmp_path,
):
    _install_fake_flask_stack(monkeypatch)
    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda name, region_name=None: {
        "service": name,
        "region_name": region_name,
    }
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setenv("DIAGNOSTIC_NODE_INVENTORY_FILE", str(tmp_path / "inventory.json"))
    monkeypatch.setenv("DIAGNOSTIC_NODE_LEASE_FILE", str(tmp_path / "leases.json"))
    monkeypatch.setenv("DIAGNOSTIC_AWS_REGION", "us-east-1")
    monkeypatch.setenv("DIAGNOSTIC_NODE_LAUNCH_TEMPLATE", "diag-local-zone-worker")
    monkeypatch.setenv("DIAGNOSTIC_NODE_INSTANCE_TYPE", "c6i.xlarge")
    monkeypatch.setenv("DIAGNOSTIC_NODE_DEFAULT_ZONE", "us-east-1-dfw-2a")
    monkeypatch.setenv(
        "DIAGNOSTIC_NODE_ZONE_CATALOG_JSON",
        json.dumps(
            {
                "zones": [
                    {
                        "zone": "us-east-1-atl-2a",
                        "metro": "atlanta",
                        "subnet_id": "subnet-atl",
                        "api_base_url": "https://atl.diag.example.com",
                        "tunnel_host": "atl.diag.example.com",
                    },
                    {
                        "zone": "us-east-1-dfw-2a",
                        "metro": "dallas",
                        "subnet_id": "subnet-dfw",
                        "api_base_url": "https://dfw.diag.example.com",
                        "tunnel_host": "dfw.diag.example.com",
                    },
                ]
            }
        ),
    )

    server_app = importlib.import_module("server.app")
    session_dependencies = importlib.import_module("server.api.session_dependencies")
    session_dependencies.set_node_allocator(None)
    session_dependencies.set_node_provisioner(None)
    session_dependencies.set_launch_spec_resolver(None)

    server_app.create_app()

    resolver = session_dependencies.get_launch_spec_resolver()
    spec = resolver(
        context=types.SimpleNamespace(brand="Chevrolet"),
        preferred_zone="",
        preferred_metro="",
    )

    assert spec.zone == "us-east-1-dfw-2a"
    assert spec.metro == "dallas"
    assert spec.subnet_id == "subnet-dfw"
    assert spec.api_base_url == "https://dfw.diag.example.com"
    assert spec.tunnel_host == "dfw.diag.example.com"


def test_server_app_factory_zone_catalog_can_be_loaded_from_json_file(
    monkeypatch,
    tmp_path,
):
    _install_fake_flask_stack(monkeypatch)
    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda name, region_name=None: {
        "service": name,
        "region_name": region_name,
    }
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setenv("DIAGNOSTIC_NODE_INVENTORY_FILE", str(tmp_path / "inventory.json"))
    monkeypatch.setenv("DIAGNOSTIC_NODE_LEASE_FILE", str(tmp_path / "leases.json"))
    monkeypatch.setenv("DIAGNOSTIC_AWS_REGION", "us-east-1")
    monkeypatch.setenv("DIAGNOSTIC_NODE_LAUNCH_TEMPLATE", "diag-local-zone-worker")
    monkeypatch.setenv("DIAGNOSTIC_NODE_INSTANCE_TYPE", "c6i.xlarge")
    monkeypatch.setenv("DIAGNOSTIC_NODE_DEFAULT_ZONE", "us-east-1-dfw-2a")
    catalog_path = tmp_path / "zone_catalog.json"
    catalog_path.write_text(
        json.dumps(
            {
                "zones": [
                    {
                        "zone": "us-east-1-dfw-2a",
                        "metro": "dallas",
                        "subnet_id": "subnet-dfw",
                        "api_base_url": "https://dfw.diag.example.com",
                        "tunnel_host": "dfw.diag.example.com",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DIAGNOSTIC_NODE_ZONE_CATALOG_FILE", str(catalog_path))

    server_app = importlib.import_module("server.app")
    session_dependencies = importlib.import_module("server.api.session_dependencies")
    session_dependencies.set_node_allocator(None)
    session_dependencies.set_node_provisioner(None)
    session_dependencies.set_launch_spec_resolver(None)

    server_app.create_app()

    resolver = session_dependencies.get_launch_spec_resolver()
    spec = resolver(
        context=types.SimpleNamespace(brand="Chevrolet"),
        preferred_zone="",
        preferred_metro="",
    )

    assert spec.zone == "us-east-1-dfw-2a"
    assert spec.subnet_id == "subnet-dfw"


def test_server_app_factory_zone_catalog_configures_route_resolver_for_client_time_zone(
    monkeypatch,
    tmp_path,
):
    _install_fake_flask_stack(monkeypatch)
    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda name, region_name=None: {
        "service": name,
        "region_name": region_name,
    }
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setenv("DIAGNOSTIC_NODE_INVENTORY_FILE", str(tmp_path / "inventory.json"))
    monkeypatch.setenv("DIAGNOSTIC_NODE_LEASE_FILE", str(tmp_path / "leases.json"))
    monkeypatch.setenv("DIAGNOSTIC_AWS_REGION", "us-east-1")
    monkeypatch.setenv("DIAGNOSTIC_NODE_LAUNCH_TEMPLATE", "diag-local-zone-worker")
    monkeypatch.setenv("DIAGNOSTIC_NODE_INSTANCE_TYPE", "c6i.xlarge")
    monkeypatch.setenv("DIAGNOSTIC_NODE_DEFAULT_ZONE", "us-east-1-dfw-2a")
    monkeypatch.setenv(
        "DIAGNOSTIC_NODE_ZONE_CATALOG_JSON",
        json.dumps(
            {
                "zones": [
                    {
                        "zone": "us-east-1-dfw-2a",
                        "metro": "dallas",
                        "subnet_id": "subnet-dfw",
                        "api_base_url": "https://dfw.diag.example.com",
                        "tunnel_host": "dfw.diag.example.com",
                        "time_zones": ["America/Chicago"],
                        "cities": ["Dallas"],
                    },
                    {
                        "zone": "us-east-1-atl-2a",
                        "metro": "atlanta",
                        "subnet_id": "subnet-atl",
                        "api_base_url": "https://atl.diag.example.com",
                        "tunnel_host": "atl.diag.example.com",
                        "time_zones": ["America/New_York"],
                        "cities": ["Atlanta"],
                    },
                ]
            }
        ),
    )

    server_app = importlib.import_module("server.app")
    session_dependencies = importlib.import_module("server.api.session_dependencies")
    session_dependencies.set_node_allocator(None)
    session_dependencies.set_node_provisioner(None)
    session_dependencies.set_launch_spec_resolver(None)
    session_dependencies.set_node_route_resolver(None)

    server_app.create_app()

    resolver = session_dependencies.get_node_route_resolver()
    route = resolver(
        data={
            "brand": "Chevrolet",
            "client_time_zone": "America/Chicago",
        }
    )

    assert route == {
        "preferred_zone": "",
        "preferred_metro": "dallas",
        "source": "client_time_zone",
    }


def test_server_app_factory_zone_catalog_route_resolver_prefers_city_over_time_zone(
    monkeypatch,
    tmp_path,
):
    _install_fake_flask_stack(monkeypatch)
    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda name, region_name=None: {
        "service": name,
        "region_name": region_name,
    }
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setenv("DIAGNOSTIC_NODE_INVENTORY_FILE", str(tmp_path / "inventory.json"))
    monkeypatch.setenv("DIAGNOSTIC_NODE_LEASE_FILE", str(tmp_path / "leases.json"))
    monkeypatch.setenv("DIAGNOSTIC_AWS_REGION", "us-east-1")
    monkeypatch.setenv("DIAGNOSTIC_NODE_LAUNCH_TEMPLATE", "diag-local-zone-worker")
    monkeypatch.setenv("DIAGNOSTIC_NODE_INSTANCE_TYPE", "c6i.xlarge")
    monkeypatch.setenv("DIAGNOSTIC_NODE_DEFAULT_ZONE", "us-east-1-dfw-2a")
    monkeypatch.setenv(
        "DIAGNOSTIC_NODE_ZONE_CATALOG_JSON",
        json.dumps(
            {
                "zones": [
                    {
                        "zone": "us-east-1-dfw-2a",
                        "metro": "dallas",
                        "subnet_id": "subnet-dfw",
                        "api_base_url": "https://dfw.diag.example.com",
                        "tunnel_host": "dfw.diag.example.com",
                        "time_zones": ["America/Chicago"],
                    },
                    {
                        "zone": "us-east-1-atl-2a",
                        "metro": "atlanta",
                        "subnet_id": "subnet-atl",
                        "api_base_url": "https://atl.diag.example.com",
                        "tunnel_host": "atl.diag.example.com",
                        "cities": ["Atlanta"],
                    },
                ]
            }
        ),
    )

    server_app = importlib.import_module("server.app")
    session_dependencies = importlib.import_module("server.api.session_dependencies")
    session_dependencies.set_node_allocator(None)
    session_dependencies.set_node_provisioner(None)
    session_dependencies.set_launch_spec_resolver(None)
    session_dependencies.set_node_route_resolver(None)

    server_app.create_app()

    resolver = session_dependencies.get_node_route_resolver()
    route = resolver(
        data={
            "brand": "Chevrolet",
            "client_city": "Atlanta",
            "client_time_zone": "America/Chicago",
        }
    )

    assert route == {
        "preferred_zone": "",
        "preferred_metro": "atlanta",
        "source": "client_city",
    }


def test_server_app_factory_route_resolver_accepts_windows_time_zone_aliases(
    monkeypatch,
    tmp_path,
):
    _install_fake_flask_stack(monkeypatch)
    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda name, region_name=None: {
        "service": name,
        "region_name": region_name,
    }
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setenv("DIAGNOSTIC_NODE_INVENTORY_FILE", str(tmp_path / "inventory.json"))
    monkeypatch.setenv("DIAGNOSTIC_NODE_LEASE_FILE", str(tmp_path / "leases.json"))
    monkeypatch.setenv("DIAGNOSTIC_AWS_REGION", "us-east-1")
    monkeypatch.setenv("DIAGNOSTIC_NODE_LAUNCH_TEMPLATE", "diag-local-zone-worker")
    monkeypatch.setenv("DIAGNOSTIC_NODE_INSTANCE_TYPE", "c6i.xlarge")
    monkeypatch.setenv("DIAGNOSTIC_NODE_DEFAULT_ZONE", "us-east-1-dfw-2a")
    monkeypatch.setenv(
        "DIAGNOSTIC_NODE_ZONE_CATALOG_JSON",
        json.dumps(
            {
                "zones": [
                    {
                        "zone": "us-east-1-dfw-2a",
                        "metro": "dallas",
                        "subnet_id": "subnet-dfw",
                        "api_base_url": "https://dfw.diag.example.com",
                        "tunnel_host": "dfw.diag.example.com",
                        "time_zones": ["America/Chicago"],
                    }
                ]
            }
        ),
    )

    server_app = importlib.import_module("server.app")
    session_dependencies = importlib.import_module("server.api.session_dependencies")
    session_dependencies.set_node_allocator(None)
    session_dependencies.set_node_provisioner(None)
    session_dependencies.set_launch_spec_resolver(None)
    session_dependencies.set_node_route_resolver(None)

    server_app.create_app()

    resolver = session_dependencies.get_node_route_resolver()
    route = resolver(
        data={
            "brand": "Chevrolet",
            "client_time_zone": "Central Standard Time",
        }
    )

    assert route == {
        "preferred_zone": "",
        "preferred_metro": "dallas",
        "source": "client_time_zone",
    }


def test_server_app_factory_configures_cloudfront_geo_flags_from_env(
    monkeypatch,
    tmp_path,
):
    _install_fake_flask_stack(monkeypatch)
    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda name, region_name=None: {
        "service": name,
        "region_name": region_name,
    }
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setenv("DIAGNOSTIC_NODE_INVENTORY_FILE", str(tmp_path / "inventory.json"))
    monkeypatch.setenv("DIAGNOSTIC_NODE_LEASE_FILE", str(tmp_path / "leases.json"))
    monkeypatch.setenv("DIAGNOSTIC_AWS_REGION", "us-east-1")
    monkeypatch.setenv("DIAGNOSTIC_NODE_LAUNCH_TEMPLATE", "diag-local-zone-worker")
    monkeypatch.setenv("DIAGNOSTIC_NODE_INSTANCE_TYPE", "c6i.xlarge")
    monkeypatch.setenv("DIAGNOSTIC_NODE_DEFAULT_ZONE", "us-east-1-dfw-2a")
    monkeypatch.setenv("DIAGNOSTIC_NODE_DEFAULT_METRO", "dallas")
    monkeypatch.setenv("DIAGNOSTIC_NODE_DEFAULT_API_BASE", "https://dfw.diag.example.com")
    monkeypatch.setenv("DIAGNOSTIC_NODE_DEFAULT_TUNNEL_HOST", "dfw.diag.example.com")
    monkeypatch.setenv("DIAGNOSTIC_NODE_SUBNET_ID", "subnet-dfw")
    monkeypatch.setenv("DIAGNOSTIC_NODE_GEO_ROUTING_ENABLED", "1")
    monkeypatch.setenv("DIAGNOSTIC_NODE_TRUST_CLOUDFRONT_HEADERS", "1")

    server_app = importlib.import_module("server.app")
    session_dependencies = importlib.import_module("server.api.session_dependencies")

    server_app.create_app()

    assert session_dependencies.get_node_geo_routing_enabled() is True
    assert session_dependencies.get_trust_cloudfront_headers() is True


def test_server_app_factory_warns_when_zone_catalog_contains_duplicate_route_hints(
    monkeypatch,
    tmp_path,
    caplog,
):
    _install_fake_flask_stack(monkeypatch)
    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda name, region_name=None: {
        "service": name,
        "region_name": region_name,
    }
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setenv("DIAGNOSTIC_NODE_INVENTORY_FILE", str(tmp_path / "inventory.json"))
    monkeypatch.setenv("DIAGNOSTIC_NODE_LEASE_FILE", str(tmp_path / "leases.json"))
    monkeypatch.setenv("DIAGNOSTIC_AWS_REGION", "us-east-1")
    monkeypatch.setenv("DIAGNOSTIC_NODE_LAUNCH_TEMPLATE", "diag-local-zone-worker")
    monkeypatch.setenv("DIAGNOSTIC_NODE_INSTANCE_TYPE", "c6i.xlarge")
    monkeypatch.setenv("DIAGNOSTIC_NODE_DEFAULT_ZONE", "us-east-1-dfw-2a")
    monkeypatch.setenv(
        "DIAGNOSTIC_NODE_ZONE_CATALOG_JSON",
        json.dumps(
            {
                "zones": [
                    {
                        "zone": "us-east-1-dfw-2a",
                        "metro": "dallas",
                        "subnet_id": "subnet-dfw",
                        "api_base_url": "https://dfw.diag.example.com",
                        "tunnel_host": "dfw.diag.example.com",
                        "time_zones": ["America/Chicago"],
                        "cities": ["Springfield"],
                    },
                    {
                        "zone": "us-east-1-atl-2a",
                        "metro": "atlanta",
                        "subnet_id": "subnet-atl",
                        "api_base_url": "https://atl.diag.example.com",
                        "tunnel_host": "atl.diag.example.com",
                        "time_zones": ["America/Chicago"],
                        "cities": ["Springfield"],
                    },
                ]
            }
        ),
    )

    server_app = importlib.import_module("server.app")
    session_dependencies = importlib.import_module("server.api.session_dependencies")
    session_dependencies.set_node_allocator(None)
    session_dependencies.set_node_provisioner(None)
    session_dependencies.set_launch_spec_resolver(None)
    session_dependencies.set_node_route_resolver(None)

    with caplog.at_level(logging.WARNING):
        server_app.create_app()

    assert "Duplicate node zone catalog city hint" in caplog.text
    assert "Duplicate node zone catalog time-zone hint" in caplog.text


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
