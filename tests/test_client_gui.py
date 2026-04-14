from __future__ import annotations

import importlib
import json
import sys
import types

import pytest


def _import_client_gui(monkeypatch, tmp_path):
    class _FakeMenu(tuple):
        SEPARATOR = object()

        def __new__(cls, *items):
            return tuple.__new__(cls, items)

    class _FakeIcon:
        def __init__(self, name=None, icon=None, title=None, menu=None):
            self.name = name
            self.icon = icon
            self.title = title
            self.menu = menu
            self.ran = False
            self.stopped = False

        def run(self) -> None:
            self.ran = True

        def stop(self) -> None:
            self.stopped = True

    fake_pystray = types.SimpleNamespace(
        Menu=_FakeMenu,
        MenuItem=lambda text, action, enabled=True: {
            "text": text,
            "action": action,
            "enabled": enabled,
        },
        Icon=_FakeIcon,
    )

    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setitem(sys.modules, "pystray", fake_pystray)
    sys.modules.pop("vci_proxy.client_gui", None)
    return importlib.import_module("vci_proxy.client_gui")


def test_load_config_returns_defaults_when_config_is_missing(monkeypatch, tmp_path) -> None:
    client_gui = _import_client_gui(monkeypatch, tmp_path)

    assert client_gui.load_config() == client_gui.DEFAULT_CONFIG


def test_load_config_merges_saved_values_with_defaults(monkeypatch, tmp_path) -> None:
    client_gui = _import_client_gui(monkeypatch, tmp_path)
    client_gui.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    client_gui.CONFIG_FILE.write_text(json.dumps({"host": "diag.example", "api_port": 9090}), encoding="utf-8")

    loaded = client_gui.load_config()

    assert loaded == {
        "api_scheme": "http",
        "host": "diag.example",
        "port": 9000,
        "api_port": 9090,
        "api_token": "",
        "auth_token": "",
        "dll_path": "",
        "tls_enabled": False,
        "tls_ca_file": "",
        "tls_server_name": "",
    }


def test_save_config_persists_json(monkeypatch, tmp_path) -> None:
    client_gui = _import_client_gui(monkeypatch, tmp_path)
    config = {
        "api_scheme": "https",
        "host": "diag.example",
        "port": 9100,
        "api_port": 8081,
        "api_token": "api-secret",
        "auth_token": "secret",
        "dll_path": "C:/drivers/demo.dll",
        "tls_enabled": True,
        "tls_ca_file": "C:/certs/ca.pem",
        "tls_server_name": "diag.example",
    }

    client_gui.save_config(config)

    assert json.loads(client_gui.CONFIG_FILE.read_text(encoding="utf-8")) == config


def test_format_driver_label_marks_incompatible_architecture(monkeypatch, tmp_path) -> None:
    client_gui = _import_client_gui(monkeypatch, tmp_path)

    label = client_gui.format_driver_label(
        {
            "name": "SM2 USB",
            "vendor": "Scanmatik",
            "architecture": "x86",
            "compatible": False,
        },
        python_arch="x64",
    )

    assert label == "SM2 USB (Scanmatik) [x86] - incompatible with Python x64"


def test_update_tray_sets_icon_and_title(monkeypatch, tmp_path) -> None:
    client_gui = _import_client_gui(monkeypatch, tmp_path)
    app = client_gui.VCIProxyTrayApp()
    app._tray = types.SimpleNamespace(icon=None, title="")
    app._status = "connected"
    app._status_detail = "diag.example:9000"

    app._update_tray()

    assert app._tray.icon is client_gui.ICONS["connected"]
    assert "Connected" in app._tray.title
    assert "diag.example:9000" in app._tray.title


def test_start_client_builds_reverse_proxy_client_and_starts_thread(monkeypatch, tmp_path) -> None:
    client_gui = _import_client_gui(monkeypatch, tmp_path)
    observed: dict[str, object] = {}

    class _FakeClient:
        def __init__(self, server_host, server_port, dll_path, config, on_status_change):
            observed["server_host"] = server_host
            observed["server_port"] = server_port
            observed["dll_path"] = dll_path
            observed["config"] = config
            observed["on_status_change"] = on_status_change

        async def connect_and_serve(self):
            raise AssertionError("background target should not execute in this test")

    class _FakeThread:
        def __init__(self, target=None, daemon=None, name=None):
            self.target = target
            self.daemon = daemon
            self.name = name
            self.started = False

        def start(self):
            self.started = True

        def is_alive(self):
            return self.started

        def join(self, timeout=None):
            observed["join_timeout"] = timeout

    monkeypatch.setattr(client_gui, "ReverseProxyClient", _FakeClient)
    monkeypatch.setattr(client_gui.threading, "Thread", _FakeThread)
    monkeypatch.setattr(client_gui.ProxyConfig, "from_args", lambda **kwargs: kwargs)

    app = client_gui.VCIProxyTrayApp()
    app._config = {
        "api_scheme": "http",
        "host": "diag.example",
        "port": 9000,
        "api_port": 8080,
        "api_token": "",
        "auth_token": "secret",
        "dll_path": "C:/drivers/demo.dll",
        "tls_enabled": True,
        "tls_ca_file": "C:/certs/ca.pem",
        "tls_server_name": "diag.example",
    }

    app._start_client()

    assert observed["server_host"] == "diag.example"
    assert observed["server_port"] == 9000
    assert observed["dll_path"] == "C:/drivers/demo.dll"
    assert observed["config"] == {
        "auth_token": "secret",
        "tls_enabled": True,
        "tls_ca_file": "C:/certs/ca.pem",
        "tls_server_name": "diag.example",
    }
    assert callable(observed["on_status_change"])
    assert app._client_thread.started is True


def test_stop_client_waits_for_graceful_shutdown(monkeypatch, tmp_path) -> None:
    client_gui = _import_client_gui(monkeypatch, tmp_path)
    observed: dict[str, object] = {}

    class _FakeFuture:
        def result(self, timeout=None):
            observed["timeout"] = timeout
            return None

    app = client_gui.VCIProxyTrayApp()
    app._client = types.SimpleNamespace(stop=lambda: _FakeFuture())
    app._loop = types.SimpleNamespace(call_soon_threadsafe=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("loop.stop should not be called")))

    app._stop_client()

    assert observed["timeout"] == 5
    assert app._status == "idle"


def test_on_settings_dispatches_dialog_work_to_background_thread(monkeypatch, tmp_path) -> None:
    client_gui = _import_client_gui(monkeypatch, tmp_path)
    observed: dict[str, object] = {}

    class _FakeDialog:
        def __init__(self, config):
            observed["dialog_config"] = dict(config)

        def show(self):
            observed["show_called"] = True
            return None

    class _FakeThread:
        instances: list["_FakeThread"] = []

        def __init__(self, target=None, daemon=None, name=None):
            self.target = target
            self.daemon = daemon
            self.name = name
            self.started = False
            self.__class__.instances.append(self)

        def start(self):
            self.started = True

        def is_alive(self):
            return self.started

    app = client_gui.VCIProxyTrayApp()
    app._config = {
        "api_scheme": "http",
        "host": "diag.example",
        "port": 9000,
        "api_port": 8080,
        "api_token": "",
        "auth_token": "secret",
        "dll_path": "",
    }
    app._stop_client = lambda: observed.setdefault("stopped", True)
    app._start_client = lambda: observed.setdefault("started", True)
    monkeypatch.setattr(client_gui, "ConfigDialog", _FakeDialog)
    monkeypatch.setattr(client_gui.threading, "Thread", _FakeThread)

    app._on_settings()

    assert len(_FakeThread.instances) == 1
    assert _FakeThread.instances[0].started is True
    assert _FakeThread.instances[0].name == "vci-proxy-settings"
    assert "stopped" not in observed
    assert "dialog_config" not in observed
    assert "show_called" not in observed


def test_on_settings_cancel_does_not_restart_client_when_auth_token_is_missing(monkeypatch, tmp_path) -> None:
    client_gui = _import_client_gui(monkeypatch, tmp_path)
    observed: dict[str, object] = {}

    class _FakeDialog:
        def __init__(self, config):
            observed["dialog_config"] = dict(config)

        def show(self):
            return None

    class _ImmediateThread:
        def __init__(self, target=None, daemon=None, name=None):
            self.target = target
            self.daemon = daemon
            self.name = name
            self.started = False

        def start(self):
            self.started = True
            if self.target is not None:
                self.target()

        def is_alive(self):
            return False

    app = client_gui.VCIProxyTrayApp()
    app._config = {
        "api_scheme": "http",
        "host": "diag.example",
        "port": 9000,
        "api_port": 8080,
        "api_token": "",
        "auth_token": "",
        "dll_path": "",
    }
    app._stop_client = lambda: observed.setdefault("stopped", True)
    app._start_client = lambda: observed.setdefault("started", True)
    monkeypatch.setattr(client_gui, "ConfigDialog", _FakeDialog)
    monkeypatch.setattr(client_gui.threading, "Thread", _ImmediateThread)

    app._on_settings()

    assert observed["stopped"] is True
    assert observed["dialog_config"]["auth_token"] == ""
    assert "started" not in observed


def test_run_settings_dialog_preserves_existing_tls_config(monkeypatch, tmp_path) -> None:
    client_gui = _import_client_gui(monkeypatch, tmp_path)
    observed: dict[str, object] = {}

    class _FakeDialog:
        def __init__(self, config):
            observed["dialog_config"] = dict(config)

        def show(self):
            return {
                "api_scheme": "https",
                "host": "diag.example",
                "port": 9000,
                "api_port": 8080,
                "api_token": "api-secret",
                "auth_token": "secret",
                "dll_path": "",
            }

    app = client_gui.VCIProxyTrayApp()
    app._config = {
        "api_scheme": "http",
        "host": "diag.example",
        "port": 9000,
        "api_port": 8080,
        "api_token": "",
        "auth_token": "secret",
        "dll_path": "",
        "tls_enabled": True,
        "tls_ca_file": "C:/certs/ca.pem",
        "tls_server_name": "diag.example",
    }
    monkeypatch.setattr(client_gui, "ConfigDialog", _FakeDialog)
    monkeypatch.setattr(client_gui, "save_config", lambda cfg: observed.setdefault("saved", dict(cfg)))
    monkeypatch.setattr(app, "_start_client", lambda: observed.setdefault("started", True))
    monkeypatch.setattr(app, "_stop_client", lambda: observed.setdefault("stopped", True))

    app._run_settings_dialog()

    assert observed["stopped"] is True
    assert observed["saved"]["tls_enabled"] is True
    assert observed["saved"]["tls_ca_file"] == "C:/certs/ca.pem"
    assert observed["saved"]["tls_server_name"] == "diag.example"
    assert observed["saved"]["api_scheme"] == "https"
    assert observed["saved"]["api_token"] == "api-secret"
    assert observed["started"] is True


def test_run_prompts_for_settings_when_auth_token_is_missing(monkeypatch, tmp_path) -> None:
    client_gui = _import_client_gui(monkeypatch, tmp_path)
    observed: dict[str, object] = {}

    class _FakeDialog:
        def __init__(self, config):
            observed["dialog_config"] = dict(config)

        def show(self):
            return {
                "api_scheme": "https",
                "host": "diag.example",
                "port": 9000,
                "api_port": 8080,
                "api_token": "api-secret",
                "auth_token": "secret",
                "dll_path": "",
            }

    app = client_gui.VCIProxyTrayApp()
    app._config = {
        "api_scheme": "http",
        "host": "diag.example",
        "port": 9000,
        "api_port": 8080,
        "api_token": "",
        "auth_token": "",
        "dll_path": "",
        "tls_enabled": True,
        "tls_ca_file": "C:/certs/ca.pem",
        "tls_server_name": "diag.example",
    }
    monkeypatch.setattr(client_gui, "ConfigDialog", _FakeDialog)
    monkeypatch.setattr(client_gui, "save_config", lambda cfg: observed.setdefault("saved", dict(cfg)))
    monkeypatch.setattr(app, "_start_client", lambda: observed.setdefault("started", True))

    app.run()

    assert observed["dialog_config"]["auth_token"] == ""
    assert observed["saved"]["auth_token"] == "secret"
    assert observed["saved"]["tls_enabled"] is True
    assert observed["saved"]["tls_ca_file"] == "C:/certs/ca.pem"
    assert observed["saved"]["tls_server_name"] == "diag.example"
    assert observed["saved"]["api_scheme"] == "https"
    assert observed["saved"]["api_token"] == "api-secret"
    assert observed["started"] is True
    assert app._tray is not None
    assert app._tray.ran is True


def test_on_diagnostics_without_host_shows_error(monkeypatch, tmp_path) -> None:
    client_gui = _import_client_gui(monkeypatch, tmp_path)
    app = client_gui.VCIProxyTrayApp()
    app._config = {"host": "", "api_port": 8080}
    errors: list[tuple[str, str]] = []
    app._show_threadsafe_error = lambda title, message: errors.append((title, message))

    app._on_diagnostics()

    assert errors == [
        ("Diagnostics", "Server address is not configured. Open Settings first.")
    ]


def test_on_diagnostics_opens_window_with_expected_api_base(monkeypatch, tmp_path) -> None:
    client_gui = _import_client_gui(monkeypatch, tmp_path)
    opened: dict[str, object] = {}

    fake_diagnostics_module = types.ModuleType("vci_proxy.diagnostics_window")

    class _FakeDiagnosticsWindow:
        def __init__(
            self,
            api_base: str,
            *,
            api_token: str = "",
            use_session_bootstrap: bool = False,
            node_assignment_callback=None,
        ):
            opened["api_base"] = api_base
            opened["api_token"] = api_token
            opened["use_session_bootstrap"] = use_session_bootstrap
            opened["has_node_assignment_callback"] = callable(node_assignment_callback)

        def show(self) -> None:
            opened["shown"] = True

    class _ImmediateThread:
        def __init__(self, target=None, daemon=None, name=None):
            self.target = target
            self.daemon = daemon
            self.name = name
            self.started = False

        def start(self):
            self.started = True
            if self.target is not None:
                self.target()

    fake_diagnostics_module.DiagnosticsWindow = _FakeDiagnosticsWindow
    monkeypatch.setitem(sys.modules, "vci_proxy.diagnostics_window", fake_diagnostics_module)
    monkeypatch.setattr(client_gui.threading, "Thread", _ImmediateThread)

    app = client_gui.VCIProxyTrayApp()
    app._config = {"api_scheme": "http", "host": "127.0.0.1", "api_port": 8080, "api_token": ""}
    app._show_threadsafe_error = lambda title, message: pytest.fail(f"unexpected error dialog: {title}: {message}")

    app._on_diagnostics()

    assert opened == {
        "api_base": "http://127.0.0.1:8080",
        "api_token": "",
        "use_session_bootstrap": True,
        "has_node_assignment_callback": True,
        "shown": True,
    }


def test_on_diagnostics_supports_https_and_api_token(monkeypatch, tmp_path) -> None:
    client_gui = _import_client_gui(monkeypatch, tmp_path)
    opened: dict[str, object] = {}

    fake_diagnostics_module = types.ModuleType("vci_proxy.diagnostics_window")

    class _FakeDiagnosticsWindow:
        def __init__(
            self,
            api_base: str,
            *,
            api_token: str = "",
            use_session_bootstrap: bool = False,
            node_assignment_callback=None,
        ):
            opened["api_base"] = api_base
            opened["api_token"] = api_token
            opened["use_session_bootstrap"] = use_session_bootstrap
            opened["has_node_assignment_callback"] = callable(node_assignment_callback)

        def show(self) -> None:
            opened["shown"] = True

    class _ImmediateThread:
        def __init__(self, target=None, daemon=None, name=None):
            self.target = target
            self.daemon = daemon
            self.name = name
            self.started = False

        def start(self):
            self.started = True
            if self.target is not None:
                self.target()

    fake_diagnostics_module.DiagnosticsWindow = _FakeDiagnosticsWindow
    monkeypatch.setitem(sys.modules, "vci_proxy.diagnostics_window", fake_diagnostics_module)
    monkeypatch.setattr(client_gui.threading, "Thread", _ImmediateThread)

    app = client_gui.VCIProxyTrayApp()
    app._config = {
        "api_scheme": "https",
        "host": "cust001.diag.example.com",
        "api_port": 443,
        "api_token": "api-secret",
    }
    app._show_threadsafe_error = lambda title, message: pytest.fail(f"unexpected error dialog: {title}: {message}")

    app._on_diagnostics()

    assert opened == {
        "api_base": "https://cust001.diag.example.com:443",
        "api_token": "api-secret",
        "use_session_bootstrap": True,
        "has_node_assignment_callback": True,
        "shown": True,
    }


def test_start_client_prefers_active_assigned_host(monkeypatch, tmp_path) -> None:
    client_gui = _import_client_gui(monkeypatch, tmp_path)
    observed: dict[str, object] = {}

    class _FakeClient:
        def __init__(self, server_host, server_port, dll_path, config, on_status_change):
            observed["server_host"] = server_host
            observed["server_port"] = server_port
            observed["dll_path"] = dll_path
            observed["config"] = config
            observed["on_status_change"] = on_status_change

        async def connect_and_serve(self):
            observed["served"] = True

        def stop(self):
            return None

    class _FakeProxyConfig:
        @classmethod
        def from_args(cls, **kwargs):
            observed["proxy_config_kwargs"] = kwargs
            return {"proxy_config": kwargs}

    class _ImmediateThread:
        def __init__(self, target=None, daemon=None, name=None):
            self.target = target
            self.daemon = daemon
            self.name = name

        def is_alive(self) -> bool:
            return False

        def start(self):
            if self.target is not None:
                self.target()

    monkeypatch.setattr(client_gui, "ReverseProxyClient", _FakeClient)
    monkeypatch.setattr(client_gui, "ProxyConfig", _FakeProxyConfig)
    monkeypatch.setattr(client_gui.threading, "Thread", _ImmediateThread)

    app = client_gui.VCIProxyTrayApp()
    app._config = {
        "host": "bootstrap.diag.example.com",
        "port": 9000,
        "api_scheme": "https",
        "api_port": 443,
        "api_token": "api-secret",
        "auth_token": "shared-secret",
        "dll_path": "",
        "tls_enabled": False,
        "tls_ca_file": "",
        "tls_server_name": "",
    }
    app._active_node_assignment = {
        "tunnel_host": "lax-1.diag.example.com",
        "api_base_url": "https://lax-1.diag.example.com:443",
    }

    app._start_client()

    assert observed["server_host"] == "lax-1.diag.example.com"
    assert observed["server_port"] == 9000


def test_on_node_assignment_none_clears_active_assignment_and_restarts(monkeypatch, tmp_path) -> None:
    client_gui = _import_client_gui(monkeypatch, tmp_path)
    restarted: list[bool] = []

    app = client_gui.VCIProxyTrayApp()
    app._active_node_assignment = {
        "tunnel_host": "lax-1.diag.example.com",
        "api_base_url": "https://lax-1.diag.example.com:443",
    }
    app._restart_client = lambda: restarted.append(True)

    app._on_node_assignment(None)

    assert app._active_node_assignment is None
    assert restarted == [True]
