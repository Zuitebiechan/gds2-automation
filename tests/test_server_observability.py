from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path

import pytest

from diagnostic_platform.observability import flush_product_log_writers


def _install_fake_flask_stack(
    monkeypatch,
    *,
    path="/api/session/start",
    method="POST",
    payload=None,
    args=None,
):
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
    fake_flask.request = types.SimpleNamespace(
        args=args or {},
        json=payload,
        headers={},
        path=path,
        method=method,
        endpoint="session_start",
        remote_addr="10.0.0.5",
        get_json=lambda silent=False: payload,
    )
    monkeypatch.setitem(sys.modules, "flask", fake_flask)

    fake_flask_cors = types.ModuleType("flask_cors")
    fake_flask_cors.CORS = lambda app, *args, **kwargs: app
    monkeypatch.setitem(sys.modules, "flask_cors", fake_flask_cors)
    monkeypatch.delitem(sys.modules, "server.app", raising=False)
    monkeypatch.delitem(sys.modules, "server.api.diagnostics", raising=False)
    monkeypatch.delitem(sys.modules, "server.api.navigate", raising=False)
    monkeypatch.delitem(sys.modules, "server.api.session", raising=False)
    monkeypatch.delitem(sys.modules, "server.api.http_utils", raising=False)
    return fake_flask


def _raw_event_files(root: Path) -> list[Path]:
    return sorted((root / "RPA_Diagnostic" / "observability" / "cloud" / "raw").glob("*.jsonl"))


class _JsonResponse:
    is_json = True
    mimetype = "application/json"
    content_type = "application/json"

    def __init__(self, payload: dict[str, object], status_code: int) -> None:
        self.status_code = status_code
        self._payload = dict(payload)
        self.data = json.dumps(self._payload).encode("utf-8")

    def get_json(self, silent=False):
        return dict(self._payload)

    def set_data(self, data):
        text = data.decode("utf-8") if isinstance(data, bytes) else str(data)
        self.data = text.encode("utf-8")
        self._payload = json.loads(text)


def test_resolve_server_log_path_prefers_log_dir(tmp_path: Path, monkeypatch) -> None:
    _install_fake_flask_stack(monkeypatch)
    server_app = importlib.import_module("server.app")

    path = server_app._resolve_server_log_path(
        environ={"LOG_DIR": str(tmp_path / "logs")}
    )

    assert path == tmp_path / "logs" / "gds2_web.log"
    assert path.parent.is_dir()


def test_api_request_observability_assigns_request_id_and_writes_success_event(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_flask = _install_fake_flask_stack(
        monkeypatch,
        payload={"session_id": "session-1"},
    )
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    server_app = importlib.import_module("server.app")

    app = server_app.create_app()
    for handler in app._before_request_handlers:
        result = handler()
        assert result is None

    assert getattr(fake_flask.request, "request_id", "")

    response = types.SimpleNamespace(status_code=200)
    returned = app._after_request_handlers[0](response)
    flush_product_log_writers()

    assert returned is response
    event_files = _raw_event_files(tmp_path)
    assert event_files
    records = [
        json.loads(line)
        for line in event_files[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert records[-1]["event_type"] == "api.request.completed"
    assert records[-1]["status"] == "ok"
    assert records[-1]["session_id"] == "session-1"
    assert records[-1]["request_id"] == fake_flask.request.request_id


def test_api_error_responses_receive_bound_request_id(monkeypatch) -> None:
    fake_flask = _install_fake_flask_stack(monkeypatch)
    server_app = importlib.import_module("server.app")

    app = server_app.create_app()
    for handler in app._before_request_handlers:
        result = handler()
        assert result is None

    response = _JsonResponse(
        {"success": False, "error": "Internal server error"},
        500,
    )
    returned = app._after_request_handlers[0](response)

    assert returned is response
    assert response.get_json()["request_id"] == fake_flask.request.request_id


def test_api_token_guard_error_receives_request_id(monkeypatch) -> None:
    fake_flask = _install_fake_flask_stack(monkeypatch)
    server_app = importlib.import_module("server.app")

    app = server_app.create_app(
        server_app.ServerRuntimeSettings(api_token="secret-token")
    )
    guard_result = None
    for handler in app._before_request_handlers:
        guard_result = handler()
        if guard_result is not None:
            break

    assert guard_result == ({"success": False, "error": "Unauthorized"}, 401)

    response = _JsonResponse(guard_result[0], guard_result[1])
    app._after_request_handlers[0](response)

    assert response.get_json() == {
        "success": False,
        "error": "Unauthorized",
        "request_id": fake_flask.request.request_id,
    }


def test_api_success_responses_do_not_receive_request_id(monkeypatch) -> None:
    _install_fake_flask_stack(monkeypatch)
    server_app = importlib.import_module("server.app")

    app = server_app.create_app()
    for handler in app._before_request_handlers:
        result = handler()
        assert result is None

    response = _JsonResponse({"success": True}, 200)
    app._after_request_handlers[0](response)

    assert response.get_json() == {"success": True}


def test_api_request_observability_writes_error_event_for_failed_response(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_flask = _install_fake_flask_stack(
        monkeypatch,
        path="/api/session/start_diagnostics",
        payload={"session_id": "session-1"},
    )
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    server_app = importlib.import_module("server.app")

    app = server_app.create_app()
    for handler in app._before_request_handlers:
        handler()

    response = types.SimpleNamespace(status_code=503)
    app._after_request_handlers[0](response)
    flush_product_log_writers()

    records = [
        json.loads(line)
        for line in _raw_event_files(tmp_path)[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert records[-1]["event_type"] == "api.request.completed"
    assert records[-1]["status"] == "error"
    assert records[-1]["failure_code"] == "http_503"
    assert records[-1]["impact_scope"] == "http:/api/session/start_diagnostics"


def test_api_request_observability_reads_session_id_from_multidict_like_args(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fake_flask = _install_fake_flask_stack(
        monkeypatch,
        path="/api/session/status",
        method="GET",
        payload=None,
        args={},
    )
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    server_app = importlib.import_module("server.app")

    class _ArgsLike:
        def get(self, key, default=None):
            if key == "session_id":
                return "session-get-1"
            return default

    fake_flask.request.args = _ArgsLike()

    app = server_app.create_app()
    for handler in app._before_request_handlers:
        handler()

    app._after_request_handlers[0](types.SimpleNamespace(status_code=200))
    flush_product_log_writers()

    records = [
        json.loads(line)
        for line in _raw_event_files(tmp_path)[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert records[-1]["session_id"] == "session-get-1"


def test_install_runtime_log_observability_adds_deduplicated_handler(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_fake_flask_stack(monkeypatch)
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    server_app = importlib.import_module("server.app")

    root_logger = server_app.logging.getLogger()
    before = [
        handler
        for handler in root_logger.handlers
        if getattr(handler, "component", None) == "server.runtime"
    ]
    server_app._install_runtime_log_observability()
    server_app._install_runtime_log_observability()
    after = [
        handler
        for handler in root_logger.handlers
        if getattr(handler, "component", None) == "server.runtime"
    ]

    assert len(after) == max(1, len(before) or 1)


def test_main_emits_failed_lifecycle_event_when_create_app_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_fake_flask_stack(monkeypatch)
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    server_app = importlib.import_module("server.app")
    monkeypatch.setattr(server_app, "_disable_windows_quick_edit", lambda: None)
    monkeypatch.setattr(server_app, "create_app", lambda _settings: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(server_app, "start_node_readiness_monitor", lambda: False)
    monkeypatch.setattr(server_app, "stop_node_readiness_monitor", lambda: None)

    with pytest.raises(RuntimeError, match="boom"):
        server_app.main([])
    flush_product_log_writers()

    records = []
    for path in _raw_event_files(tmp_path):
        records.extend(
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    failed = [
        record
        for record in records
        if record["component"] == "server.runtime"
        and record["event_type"] == "process.lifecycle.failed"
    ]
    assert failed
    assert failed[-1]["failure_code"] == "RuntimeError"


def test_main_emits_failed_lifecycle_event_when_settings_parse_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _install_fake_flask_stack(monkeypatch)
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    server_app = importlib.import_module("server.app")
    monkeypatch.setattr(server_app, "_disable_windows_quick_edit", lambda: None)

    with pytest.raises(ValueError):
        server_app.main(["--port", "bad"])
    flush_product_log_writers()

    records = []
    for path in _raw_event_files(tmp_path):
        records.extend(
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    failed = [
        record
        for record in records
        if record["component"] == "server.runtime"
        and record["event_type"] == "process.lifecycle.failed"
    ]
    assert failed
    assert failed[-1]["failure_code"] == "ValueError"
    assert failed[-1]["stage"] == "resolve_server_settings"
