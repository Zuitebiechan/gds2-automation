from __future__ import annotations

import importlib
import json
import sys
import types


def _install_fake_flask_stack(monkeypatch, payload, *, path="/api/test", method="POST"):
    class FakeFlask:
        def __init__(self, import_name):
            self.import_name = import_name

    class FakeBlueprint:
        def __init__(self, name, import_name, url_prefix=""):
            self.name = name
            self.import_name = import_name
            self.url_prefix = url_prefix

        def route(self, _path, methods=None):
            def decorator(fn):
                return fn

            return decorator

    fake_flask = types.ModuleType("flask")
    fake_flask.Flask = FakeFlask
    fake_flask.Blueprint = FakeBlueprint
    fake_flask.Response = object
    fake_flask.jsonify = lambda payload=None, **kwargs: payload if payload is not None else kwargs
    fake_flask.request = types.SimpleNamespace(
        json=payload,
        args={},
        headers={},
        path=path,
        method=method,
        get_json=lambda silent=False: payload,
    )
    monkeypatch.setitem(sys.modules, "flask", fake_flask)

    fake_flask_cors = types.ModuleType("flask_cors")
    fake_flask_cors.CORS = lambda app, *args, **kwargs: app
    monkeypatch.setitem(sys.modules, "flask_cors", fake_flask_cors)


def _fresh_import(monkeypatch, module_name: str):
    monkeypatch.delitem(sys.modules, "server.api.http_utils", raising=False)
    monkeypatch.delitem(sys.modules, module_name, raising=False)
    return importlib.import_module(module_name)


def test_session_execute_rejects_non_object_json_body(monkeypatch):
    _install_fake_flask_stack(monkeypatch, ["bad-payload"])
    session_api = _fresh_import(monkeypatch, "server.api.session")

    payload, status = session_api.session_execute()

    assert status == 400
    assert payload == {
        "success": False,
        "error": "JSON request body must be an object",
    }


def test_session_start_rejects_non_string_brand(monkeypatch):
    _install_fake_flask_stack(monkeypatch, {"brand": ["GM"]})
    session_api = _fresh_import(monkeypatch, "server.api.session")
    monkeypatch.setattr(
        session_api,
        "start_business_session",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("start_business_session should not be called")
        ),
    )

    payload, status = session_api.session_start()

    assert status == 400
    assert payload == {
        "success": False,
        "error": "brand must be a string",
    }


def test_session_start_returns_active_session_metadata_on_conflict(monkeypatch):
    _install_fake_flask_stack(monkeypatch, {"brand": "GM"})
    session_api = _fresh_import(monkeypatch, "server.api.session")
    active_session = types.SimpleNamespace(
        session_id="session-123",
        status=types.SimpleNamespace(value="running"),
        backend_name="gds2",
        pending_decision=None,
    )
    monkeypatch.setattr(session_api, "_runtime", lambda: None)
    monkeypatch.setattr(
        session_api,
        "start_business_session",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError(
                "Another session is already active (session_id=session-123, status=running)"
            )
        ),
    )
    monkeypatch.setattr(
        session_api,
        "get_orchestrator",
        lambda: types.SimpleNamespace(get_active_session=lambda: active_session),
    )

    payload, status = session_api.session_start()

    assert status == 409
    assert payload == {
        "success": False,
        "error": "Another session is already active (session_id=session-123, status=running)",
        "error_code": "active_session_exists",
        "active_session_id": "session-123",
        "active_session_status": "running",
        "active_backend_name": "gds2",
    }


def test_session_execute_rejects_non_object_args(monkeypatch):
    _install_fake_flask_stack(monkeypatch, {"session_id": "sess-1", "action": "go_back", "args": ["bad"]})
    session_api = _fresh_import(monkeypatch, "server.api.session")
    monkeypatch.setattr(
        session_api,
        "get_orchestrator",
        lambda: (_ for _ in ()).throw(AssertionError("get_orchestrator should not be called")),
    )

    payload, status = session_api.session_execute()

    assert status == 400
    assert payload == {
        "success": False,
        "error": "args must be an object",
    }


def test_session_select_module_rejects_non_string_module(monkeypatch):
    _install_fake_flask_stack(monkeypatch, {"session_id": "sess-1", "module": {"name": "ECM"}})
    session_api = _fresh_import(monkeypatch, "server.api.session")
    monkeypatch.setattr(
        session_api,
        "select_module_action",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("select_module_action should not be called")
        ),
    )

    payload, status = session_api.session_select_module()

    assert status == 400
    assert payload == {
        "success": False,
        "error": "module must be a string",
    }


def test_navigate_start_rejects_non_string_goal(monkeypatch):
    _install_fake_flask_stack(monkeypatch, {"goal": {"screen": "data_display"}})
    navigate_api = _fresh_import(monkeypatch, "server.api.navigate")
    monkeypatch.setattr(
        navigate_api,
        "start_navigation_session",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("start_navigation_session should not be called")
        ),
    )

    payload, status = navigate_api.navigate_start()

    assert status == 400
    assert payload == {
        "success": False,
        "error": "goal must be a string",
    }


def test_navigate_start_requires_active_backend_navigation_runtime(monkeypatch):
    _install_fake_flask_stack(monkeypatch, {"goal": "Go to Data Display"})
    navigate_api = _fresh_import(monkeypatch, "server.api.navigate")

    def _missing_navigation_session(session_id):
        raise KeyError(f"Navigation session {session_id} not found")

    runtime = types.SimpleNamespace(
        get_active_backend_bundle=lambda: None,
        get_navigation_session=_missing_navigation_session,
    )
    monkeypatch.setattr(
        navigate_api,
        "_runtime",
        lambda: runtime,
    )

    payload, status = navigate_api.navigate_start()

    assert status == 409
    assert payload == {
        "success": False,
        "error": "No active backend navigation runtime is available",
    }

    navigate_api.request.args = {"session_id": "missing-nav"}
    status_payload, status_code = navigate_api.navigate_status()

    assert status_code == 404
    assert status_payload == {
        "success": False,
        "error": "'Navigation session missing-nav not found'",
    }


def test_navigate_start_uses_active_backend_navigation_handle(monkeypatch):
    _install_fake_flask_stack(monkeypatch, {"goal": "Go to Data Display"})
    navigate_api = _fresh_import(monkeypatch, "server.api.navigate")

    fake_session = types.SimpleNamespace(
        session_id="nav-1",
        status=types.SimpleNamespace(value="running"),
        goal="Go to Data Display",
        current_page="module_list",
        pending_decision_id=None,
        pending_items=[],
        error=None,
    )
    runtime_state = {
        "bundle": None,
        "sessions": {},
    }

    class _FakeNavigationHandle:
        def __init__(self):
            self.calls = []

        def start_navigation_session(self, runtime, goal):
            self.calls.append((runtime, goal))
            runtime_state["sessions"][fake_session.session_id] = fake_session
            return fake_session

    class _FakeRuntime:
        def get_active_backend_bundle(self):
            return runtime_state["bundle"]

        def get_navigation_session(self, session_id):
            try:
                return runtime_state["sessions"][session_id]
            except KeyError:
                raise KeyError(f"Navigation session {session_id} not found") from None

    navigation_handle = _FakeNavigationHandle()
    runtime = _FakeRuntime()
    runtime_state["bundle"] = types.SimpleNamespace(navigation_handle=navigation_handle)
    monkeypatch.setattr(navigate_api, "_runtime", lambda: runtime)

    payload = navigate_api.navigate_start()

    assert payload == {
        "success": True,
        "session_id": "nav-1",
        "status": "running",
    }
    assert navigation_handle.calls == [(runtime, "Go to Data Display")]

    runtime_state["bundle"] = None
    navigate_api.request.args = {"session_id": "nav-1"}
    status_payload = navigate_api.navigate_status()

    assert status_payload == {
        "success": True,
        "session_id": "nav-1",
        "status": "running",
        "goal": "Go to Data Display",
        "current_page": "module_list",
    }


def test_navigate_decision_rejects_non_string_selected_item(monkeypatch):
    _install_fake_flask_stack(
        monkeypatch,
        {"session_id": "nav-1", "decision_id": "decision-1", "selected_item": ["ECM"]},
    )
    navigate_api = _fresh_import(monkeypatch, "server.api.navigate")
    monkeypatch.setattr(
        navigate_api,
        "submit_navigation_decision",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("submit_navigation_decision should not be called")
        ),
    )

    payload, status = navigate_api.navigate_decision()

    assert status == 400
    assert payload == {
        "success": False,
        "error": "selected_item must be a string",
    }


def test_session_status_rejects_non_string_session_id(monkeypatch):
    _install_fake_flask_stack(monkeypatch, None, path="/api/session/status", method="GET")
    session_api = _fresh_import(monkeypatch, "server.api.session")
    session_api.request.args = {"session_id": ["bad"]}
    monkeypatch.setattr(
        session_api,
        "build_session_status_payload",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("build_session_status_payload should not be called")
        ),
    )

    payload, status = session_api.session_status()

    assert status == 400
    assert payload == {
        "success": False,
        "error": "session_id must be a string",
    }


def test_session_ai_diagnose_events_rejects_non_string_session_id(monkeypatch):
    _install_fake_flask_stack(
        monkeypatch,
        None,
        path="/api/session/ai_diagnose/events",
        method="GET",
    )
    session_api = _fresh_import(monkeypatch, "server.api.session")
    session_api.request.args = {"session_id": ["bad"]}
    monkeypatch.setattr(
        session_api.session_ai_handlers,
        "stream_ai_diagnose_events",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("stream_ai_diagnose_events should not be called")
        ),
    )

    payload, status = session_api.session_ai_diagnose_events()

    assert status == 400
    assert payload == {
        "success": False,
        "error": "session_id must be a string",
    }


def test_navigate_events_rejects_non_string_session_id(monkeypatch):
    _install_fake_flask_stack(monkeypatch, None, path="/api/navigate/events", method="GET")
    navigate_api = _fresh_import(monkeypatch, "server.api.navigate")
    navigate_api.request.args = {"session_id": ["bad"]}
    monkeypatch.setattr(
        navigate_api,
        "_get_session",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("_get_session should not be called")
        ),
    )

    payload, status = navigate_api.navigate_events()

    assert status == 400
    assert payload == {
        "success": False,
        "error": "session_id must be a string",
    }


def test_diagnose_select_module_rejects_non_object_json_body(monkeypatch):
    _install_fake_flask_stack(monkeypatch, ["bad-payload"])
    diagnostics_api = _fresh_import(monkeypatch, "server.api.diagnostics")

    payload, status = diagnostics_api.diagnose_select_module()

    assert status == 400
    assert payload == {
        "success": False,
        "error": "JSON request body must be an object",
    }


def test_diagnose_select_module_rejects_non_string_module(monkeypatch):
    _install_fake_flask_stack(monkeypatch, {"module": {"name": "ECM"}})
    diagnostics_api = _fresh_import(monkeypatch, "server.api.diagnostics")
    monkeypatch.setattr(
        diagnostics_api,
        "select_diagnostic_module",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("select_diagnostic_module should not be called")
        ),
    )

    payload, status = diagnostics_api.diagnose_select_module()

    assert status == 400
    assert payload == {
        "success": False,
        "error": "module must be a string",
    }


def test_session_events_rejects_non_string_session_id(monkeypatch):
    _install_fake_flask_stack(monkeypatch, None, path="/api/session/events", method="GET")
    session_api = _fresh_import(monkeypatch, "server.api.session")
    session_api.request.args = {"session_id": ["bad"]}

    payload, status = session_api.session_events()

    assert status == 400
    assert payload == {
        "success": False,
        "error": "session_id must be a string",
    }


def test_navigate_status_rejects_non_string_session_id(monkeypatch):
    _install_fake_flask_stack(monkeypatch, None, path="/api/navigate/status", method="GET")
    navigate_api = _fresh_import(monkeypatch, "server.api.navigate")
    navigate_api.request.args = {"session_id": ["bad"]}
    monkeypatch.setattr(
        navigate_api,
        "_get_session",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("_get_session should not be called")
        ),
    )

    payload, status = navigate_api.navigate_status()

    assert status == 400
    assert payload == {
        "success": False,
        "error": "session_id must be a string",
    }


def test_session_start_hides_internal_error_details(monkeypatch):
    _install_fake_flask_stack(monkeypatch, {"brand": "GM"})
    session_api = _fresh_import(monkeypatch, "server.api.session")
    monkeypatch.setattr(
        session_api,
        "start_business_session",
        lambda *args, **kwargs: (_ for _ in ()).throw(Exception("secret file path")),
    )

    payload, status = session_api.session_start()

    assert status == 500
    assert payload == {
        "success": False,
        "error": "Internal server error",
    }


def test_session_ai_handler_hides_internal_error_details(monkeypatch):
    _install_fake_flask_stack(monkeypatch, None)
    session_ai_handlers = _fresh_import(monkeypatch, "server.api.session_ai_handlers")
    monkeypatch.setattr(
        session_ai_handlers,
        "get_orchestrator",
        lambda: (_ for _ in ()).throw(Exception("sensitive orchestrator failure")),
    )

    payload, status = session_ai_handlers.start_ai_diagnose({"session_id": "session-ai"})

    assert status == 500
    assert payload == {
        "success": False,
        "error": "Internal server error",
    }


def test_workflow_recovery_error_exposes_optional_metadata(monkeypatch):
    _install_fake_flask_stack(monkeypatch, {})
    diagnostics_api = _fresh_import(monkeypatch, "server.api.diagnostics")
    monkeypatch.setattr(
        diagnostics_api,
        "build_diagnostics_start_payload",
        lambda **kwargs: (_ for _ in ()).throw(
            diagnostics_api.WorkflowRecoveryError(
                "Need to restart workflow",
                target_page="main_menu",
                reasoning="Recover from stale page state",
            )
        ),
    )

    payload, status = diagnostics_api.diagnose_start()

    assert status == 200
    assert payload == {
        "success": False,
        "recovered": True,
        "recovery_target": "main_menu",
        "reasoning": "Recover from stale page state",
        "error": "Need to restart workflow",
    }


def test_diagnose_live_data_start_rejects_invalid_interval_ms(monkeypatch):
    _install_fake_flask_stack(monkeypatch, {"data_category": "Live Data", "interval_ms": "fast"})
    diagnostics_api = _fresh_import(monkeypatch, "server.api.diagnostics")
    monkeypatch.setattr(
        diagnostics_api,
        "start_live_data_stream",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("start_live_data_stream should not be called")
        ),
    )

    payload, status = diagnostics_api.diagnose_live_data_start()

    assert status == 400
    assert payload == {
        "success": False,
        "error": "interval_ms must be an integer",
    }


def test_diagnose_ai_retry_rejects_non_string_cached_payload_id(monkeypatch):
    _install_fake_flask_stack(monkeypatch, {"cached_payload_id": ["bad"]})
    diagnostics_api = _fresh_import(monkeypatch, "server.api.diagnostics")
    monkeypatch.setattr(
        diagnostics_api,
        "retry_public_ai_diagnosis",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("retry_public_ai_diagnosis should not be called")
        ),
    )

    payload, status = diagnostics_api.diagnose_ai_retry()

    assert status == 400
    assert payload == {
        "success": False,
        "error": "cached_payload_id must be a string",
    }


def test_diagnose_dtcs_rejects_non_string_module_query(monkeypatch):
    _install_fake_flask_stack(monkeypatch, None, path="/api/diagnose/dtcs", method="GET")
    diagnostics_api = _fresh_import(monkeypatch, "server.api.diagnostics")
    diagnostics_api.request.args = {
        "backend_name": "fakecore",
        "module": ["bad"],
        "data_category": "",
    }
    monkeypatch.setattr(
        diagnostics_api,
        "_get_backend",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("_get_backend should not be called")
        ),
    )

    payload, status = diagnostics_api.diagnose_dtcs()

    assert status == 400
    assert payload == {
        "success": False,
        "error": "module must be a string",
        "dtcs": [],
    }


def test_diagnose_dtcs_hides_internal_error_details(monkeypatch):
    _install_fake_flask_stack(monkeypatch, None, path="/api/diagnose/dtcs", method="GET")
    diagnostics_api = _fresh_import(monkeypatch, "server.api.diagnostics")
    diagnostics_api.request.args = {"backend_name": "fakecore", "module": "", "data_category": ""}
    monkeypatch.setattr(diagnostics_api, "_get_backend", lambda backend_name=None: object())
    monkeypatch.setattr(diagnostics_api, "_ensure_backend_capability", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        diagnostics_api,
        "read_diagnostic_dtcs",
        lambda **kwargs: (_ for _ in ()).throw(Exception("secret stack path")),
    )

    payload, status = diagnostics_api.diagnose_dtcs()

    assert status == 500
    assert payload == {
        "success": False,
        "error": "Internal server error",
        "dtcs": [],
    }


def test_diagnose_ai_events_rejects_non_string_session_id(monkeypatch):
    _install_fake_flask_stack(
        monkeypatch,
        None,
        path="/api/diagnose/ai_diagnose/events",
        method="GET",
    )
    diagnostics_api = _fresh_import(monkeypatch, "server.api.diagnostics")
    diagnostics_api.request.args = {"session_id": ["bad"]}

    payload, status = diagnostics_api.diagnose_ai_events()

    assert status == 400
    assert payload == {
        "success": False,
        "error": "session_id must be a string",
    }


def test_load_zhipu_api_key_ignores_non_string_config_value(monkeypatch, tmp_path):
    _install_fake_flask_stack(monkeypatch, None)
    config_dir = tmp_path / "VCI_Proxy"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps({"zhipu_api_key": ["bad"]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("APPDATA", str(tmp_path))
    diagnostics_api = _fresh_import(monkeypatch, "server.api.diagnostics")

    assert diagnostics_api._load_zhipu_api_key() is None


def test_load_openai_config_reads_appdata_config(monkeypatch, tmp_path):
    _install_fake_flask_stack(monkeypatch, None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_REASONING_EFFORT", raising=False)
    config_dir = tmp_path / "VCI_Proxy"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "openai_api_key": "team-key",
                "openai_base_url": "https://moacode.org/team/v1",
                "openai_model": "gpt-5.4",
                "openai_reasoning_effort": "none",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("APPDATA", str(tmp_path))
    diagnostics_api = _fresh_import(monkeypatch, "server.api.diagnostics")

    assert diagnostics_api._load_openai_api_key() == "team-key"
    assert diagnostics_api._load_openai_base_url() == "https://moacode.org/team/v1"
    assert diagnostics_api._load_openai_model() == "gpt-5.4"
    assert diagnostics_api._load_openai_reasoning_effort() == "none"


def test_build_ai_engine_prefers_openai_env_over_config(monkeypatch, tmp_path):
    _install_fake_flask_stack(monkeypatch, None)
    config_dir = tmp_path / "VCI_Proxy"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "openai_api_key": "config-key",
                "openai_base_url": "https://config.example/v1",
                "openai_model": "gpt-4.1",
                "openai_reasoning_effort": "low",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://env.example/v1")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.4")
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "none")
    diagnostics_api = _fresh_import(monkeypatch, "server.api.diagnostics")

    captured = {}

    class _FakeAIEngine:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(diagnostics_api, "AIEngine", _FakeAIEngine)

    diagnostics_api._build_ai_engine()

    assert captured == {
        "api_key": "env-key",
        "model": "gpt-5.4",
        "base_url": "https://env.example/v1",
        "reasoning_effort": "none",
    }
