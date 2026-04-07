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
        "host": "diag.example",
        "port": 9000,
        "api_port": 9090,
        "auth_token": "",
        "dll_path": "",
    }


def test_save_config_persists_json(monkeypatch, tmp_path) -> None:
    client_gui = _import_client_gui(monkeypatch, tmp_path)
    config = {
        "host": "diag.example",
        "port": 9100,
        "api_port": 8081,
        "auth_token": "secret",
        "dll_path": "C:/drivers/demo.dll",
    }

    client_gui.save_config(config)

    assert json.loads(client_gui.CONFIG_FILE.read_text(encoding="utf-8")) == config


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
    monkeypatch.setattr(client_gui.ProxyConfig, "from_args", lambda auth_token=None: {"auth_token": auth_token})

    app = client_gui.VCIProxyTrayApp()
    app._config = {
        "host": "diag.example",
        "port": 9000,
        "api_port": 8080,
        "auth_token": "secret",
        "dll_path": "C:/drivers/demo.dll",
    }

    app._start_client()

    assert observed["server_host"] == "diag.example"
    assert observed["server_port"] == 9000
    assert observed["dll_path"] == "C:/drivers/demo.dll"
    assert observed["config"] == {"auth_token": "secret"}
    assert callable(observed["on_status_change"])
    assert app._client_thread.started is True


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
        def __init__(self, api_base: str):
            opened["api_base"] = api_base

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
    app._config = {"host": "127.0.0.1", "api_port": 8080}
    app._show_threadsafe_error = lambda title, message: pytest.fail(f"unexpected error dialog: {title}: {message}")

    app._on_diagnostics()

    assert opened == {
        "api_base": "http://127.0.0.1:8080",
        "shown": True,
    }
