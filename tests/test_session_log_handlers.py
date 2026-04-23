from __future__ import annotations

import base64
import importlib
import json
import sys
import types
from pathlib import Path


def _install_fake_flask_stack(monkeypatch, payload=None):
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
        args={},
        json=payload,
        headers={},
        path="/api/session/logs/upload",
        method="POST",
        endpoint="session_logs_upload",
        get_json=lambda silent=False: payload,
    )
    monkeypatch.setitem(sys.modules, "flask", fake_flask)

    fake_flask_cors = types.ModuleType("flask_cors")
    fake_flask_cors.CORS = lambda app, *args, **kwargs: app
    monkeypatch.setitem(sys.modules, "flask_cors", fake_flask_cors)
    for module_name in (
        "server.app",
        "server.api.diagnostics",
        "server.api.navigate",
        "server.api.session",
        "server.api.http_utils",
        "server.api.session_log_handlers",
    ):
        monkeypatch.delitem(sys.modules, module_name, raising=False)


def test_server_app_registers_session_logs_upload_route(monkeypatch) -> None:
    _install_fake_flask_stack(monkeypatch, {})
    server_app = importlib.import_module("server.app")

    app = server_app.create_app()
    routes = {rule.rule for rule in app.url_map.iter_rules()}

    assert "/api/session/logs/upload" in routes
    assert "/api/session/logs/sync" in routes


def test_session_logs_upload_handler_ingests_artifact(monkeypatch, tmp_path: Path) -> None:
    payload = {
        "client_instance_id": "client-1",
        "connection_epoch": "epoch-1",
        "artifact_id": "artifact-1",
        "artifact_name": "local.jsonl",
        "artifact_type": "raw",
        "session_id": "session-1",
        "content_base64": base64.b64encode(
            json.dumps(
                {
                    "schema_version": "observability.v1",
                    "ts": "2026-04-22T00:00:00Z",
                    "component": "reverse_client",
                    "component_instance_id": "reverse_client:pid:startup",
                    "event_type": "proxy.request.client_received",
                    "session_id": "session-1",
                    "connection_epoch": "epoch-1",
                    "dll_seq": None,
                    "proxy_seq": 20,
                    "worker_request_id": None,
                    "operation_kind": "j2534:PassThruReadMsgs",
                    "status": "ok",
                    "failure_code": None,
                    "failure_domain": "unknown",
                    "reason": None,
                    "duration_ms": None,
                    "hw_ms": None,
                    "network_ms": None,
                    "page": None,
                    "module": None,
                    "data_category": None,
                    "symptom": None,
                    "impact_scope": "proxy_request",
                    "next_checks": [],
                    "redaction_applied": [],
                }
            ).encode("utf-8")
        ).decode("ascii"),
    }
    _install_fake_flask_stack(monkeypatch, payload)
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "ProgramData"))

    session_api = importlib.import_module("server.api.session")

    response_payload, status = session_api.session_logs_upload()

    assert status == 201
    assert response_payload["success"] is True
    assert response_payload["deduped"] is False
    assert Path(response_payload["artifact_path"]).exists()


def test_session_logs_upload_handler_rejects_artifact_over_limit(monkeypatch, tmp_path: Path) -> None:
    payload = {
        "client_instance_id": "client-1",
        "connection_epoch": "epoch-1",
        "artifact_id": "artifact-big",
        "artifact_name": "big.jsonl",
        "artifact_type": "raw",
        "session_id": "session-1",
        "content_base64": base64.b64encode(b"a" * (2 * 1024 * 1024)).decode("ascii"),
    }
    _install_fake_flask_stack(monkeypatch, payload)
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "ProgramData"))
    monkeypatch.setenv("PRODUCT_LOG_MAX_ARTIFACT_MB", "1")

    session_api = importlib.import_module("server.api.session")

    response_payload, status = session_api.session_logs_upload()

    assert status == 400
    assert response_payload == {
        "success": False,
        "error": "artifact exceeds PRODUCT_LOG_MAX_ARTIFACT_MB",
    }


def test_session_logs_upload_handler_streams_uploaded_jsonl_without_read_text(monkeypatch, tmp_path: Path) -> None:
    payload_bytes = (
        json.dumps(
            {
                "schema_version": "observability.v1",
                "ts": "2026-04-22T00:00:00Z",
                "component": "reverse_client",
                "component_instance_id": "reverse_client:pid:startup",
                "event_type": "proxy.request.client_received",
                "session_id": "session-stream",
                "connection_epoch": "epoch-stream",
                "proxy_seq": 20,
                "status": "ok",
            }
        )
        + "\n"
        + json.dumps(
            {
                "schema_version": "observability.v1",
                "ts": "2026-04-22T00:00:01Z",
                "component": "session_runtime",
                "component_instance_id": "session_runtime:pid:startup",
                "event_type": "session.lifecycle.completed",
                "session_id": "session-stream",
                "connection_epoch": "epoch-stream",
                "status": "ok",
            }
        )
    ).encode("utf-8")
    payload = {
        "client_instance_id": "client-stream",
        "connection_epoch": "epoch-stream",
        "artifact_id": "artifact-stream",
        "artifact_name": "stream.jsonl",
        "artifact_type": "raw",
        "content_base64": base64.b64encode(payload_bytes).decode("ascii"),
    }
    _install_fake_flask_stack(monkeypatch, payload)
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "ProgramData"))

    session_api = importlib.import_module("server.api.session")

    original_read_text = Path.read_text

    def _guarded_read_text(self: Path, *args, **kwargs):
        if self.suffix in {".jsonl", ".gz"}:
            raise AssertionError("jsonl artifacts should be streamed, not read_text() into memory")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _guarded_read_text)

    response_payload, status = session_api.session_logs_upload()

    assert status == 201
    assert response_payload["success"] is True
    assert response_payload["deduped"] is False


def test_session_logs_sync_handler_returns_sync_payload(monkeypatch, tmp_path: Path) -> None:
    payload = {
        "cursor_mtime_ns": 0,
        "cursor_path": "",
        "max_files": 10,
    }
    _install_fake_flask_stack(monkeypatch, payload)
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "ProgramData"))
    monkeypatch.setenv("DIAGNOSTIC_API_TOKEN", "api-secret")

    session_api = importlib.import_module("server.api.session")
    fake_flask = sys.modules["flask"]
    fake_flask.request.headers = {"X-API-Token": "api-secret"}
    monkeypatch.setattr(
        session_api.session_log_handlers,
        "sync_cloud_logs",
        lambda data: (
            {
                "success": True,
                "files": [{"relative_path": "observability/cloud/raw/server.jsonl"}],
                "skipped_files": [],
                "has_more": False,
                "next_cursor_mtime_ns": 123,
                "next_cursor_path": "observability/cloud/raw/server.jsonl",
            },
            200,
        ),
    )

    response_payload, status = session_api.session_logs_sync()

    assert status == 200
    assert response_payload["success"] is True
    assert response_payload["files"] == [{"relative_path": "observability/cloud/raw/server.jsonl"}]


def test_session_logs_sync_handler_requires_configured_api_token(monkeypatch, tmp_path: Path) -> None:
    payload = {
        "cursor_mtime_ns": 0,
        "cursor_path": "",
        "max_files": 10,
    }
    _install_fake_flask_stack(monkeypatch, payload)
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "ProgramData"))
    monkeypatch.delenv("DIAGNOSTIC_API_TOKEN", raising=False)

    session_api = importlib.import_module("server.api.session")

    response_payload, status = session_api.session_logs_sync()

    assert status == 503
    assert response_payload == {
        "success": False,
        "error": "Cloud log sync export requires DIAGNOSTIC_API_TOKEN",
    }


def test_session_logs_sync_handler_rejects_missing_request_token(monkeypatch, tmp_path: Path) -> None:
    payload = {
        "cursor_mtime_ns": 0,
        "cursor_path": "",
        "max_files": 10,
    }
    _install_fake_flask_stack(monkeypatch, payload)
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "ProgramData"))
    monkeypatch.setenv("DIAGNOSTIC_API_TOKEN", "api-secret")

    session_api = importlib.import_module("server.api.session")

    response_payload, status = session_api.session_logs_sync()

    assert status == 401
    assert response_payload == {
        "success": False,
        "error": "Unauthorized",
    }
