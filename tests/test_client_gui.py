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
            self.visible = False
            self.notifications: list[tuple[str, str | None]] = []

        def run(self, setup=None) -> None:
            self.ran = True
            if setup is not None:
                setup(self)
            else:
                self.visible = True

        def stop(self) -> None:
            self.stopped = True

        def notify(self, message, title=None) -> None:
            self.notifications.append((message, title))

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
    for name in (
        "VCI_PROXY_READ_AHEAD",
        "VCI_PROXY_READ_AHEAD_WINDOW_MS",
        "VCI_PROXY_READ_AHEAD_MAX_READS",
        "VCI_PROXY_READ_AHEAD_READ_TIMEOUT_MS",
        "VCI_PROXY_READ_AHEAD_MAX_MESSAGES",
        "VCI_PROXY_READ_AHEAD_TRANSACTION",
    ):
        monkeypatch.delenv(name, raising=False)
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
        "read_ahead_enabled": False,
        "read_ahead_window_ms": 200,
        "read_ahead_max_reads": 3,
        "read_ahead_read_timeout_ms": 0,
        "read_ahead_max_messages": 16,
        "read_ahead_transaction_enabled": False,
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
        "read_ahead_enabled": False,
        "read_ahead_window_ms": 200,
        "read_ahead_max_reads": 3,
        "read_ahead_read_timeout_ms": 0,
        "read_ahead_max_messages": 16,
        "read_ahead_transaction_enabled": False,
    }

    client_gui.save_config(config)

    assert json.loads(client_gui.CONFIG_FILE.read_text(encoding="utf-8")) == config


def test_config_int_preserves_explicit_zero(monkeypatch, tmp_path) -> None:
    client_gui = _import_client_gui(monkeypatch, tmp_path)

    assert client_gui.config_int({"value": 0}, "value", 200) == 0
    assert client_gui.config_int({"value": "0"}, "value", 200) == 0
    assert client_gui.config_int({"value": ""}, "value", 200) == 200
    assert client_gui.config_int({}, "value", 200) == 200


def test_apply_read_ahead_env_overrides_uses_shared_env_names(monkeypatch, tmp_path) -> None:
    client_gui = _import_client_gui(monkeypatch, tmp_path)
    monkeypatch.setenv("VCI_PROXY_READ_AHEAD", "1")
    monkeypatch.setenv("VCI_PROXY_READ_AHEAD_WINDOW_MS", "250")
    monkeypatch.setenv("VCI_PROXY_READ_AHEAD_MAX_READS", "4")
    monkeypatch.setenv("VCI_PROXY_READ_AHEAD_READ_TIMEOUT_MS", "1")
    monkeypatch.setenv("VCI_PROXY_READ_AHEAD_MAX_MESSAGES", "10")
    monkeypatch.setenv("VCI_PROXY_READ_AHEAD_TRANSACTION", "1")

    updated = client_gui.apply_read_ahead_env_overrides(
        {
            "read_ahead_enabled": False,
            "read_ahead_window_ms": 200,
            "read_ahead_max_reads": 3,
            "read_ahead_read_timeout_ms": 0,
            "read_ahead_max_messages": 16,
            "read_ahead_transaction_enabled": False,
            "host": "diag.example",
        }
    )

    assert updated == {
        "read_ahead_enabled": True,
        "read_ahead_window_ms": 250,
        "read_ahead_max_reads": 4,
        "read_ahead_read_timeout_ms": 1,
        "read_ahead_max_messages": 10,
        "read_ahead_transaction_enabled": True,
        "host": "diag.example",
    }


def test_effective_runtime_config_applies_read_ahead_env_overrides(monkeypatch, tmp_path) -> None:
    client_gui = _import_client_gui(monkeypatch, tmp_path)
    monkeypatch.setenv("VCI_PROXY_READ_AHEAD", "1")
    monkeypatch.setenv("VCI_PROXY_READ_AHEAD_MAX_MESSAGES", "8")

    app = client_gui.VCIProxyTrayApp()
    app._config = {
        "api_scheme": "http",
        "host": "diag.example",
        "port": 9000,
        "api_port": 8080,
        "api_token": "",
        "auth_token": "secret",
        "dll_path": "",
        "tls_enabled": False,
        "tls_ca_file": "",
        "tls_server_name": "",
        "read_ahead_enabled": False,
        "read_ahead_window_ms": 200,
        "read_ahead_max_reads": 3,
        "read_ahead_read_timeout_ms": 0,
        "read_ahead_max_messages": 16,
        "read_ahead_transaction_enabled": False,
    }

    cfg = app._effective_runtime_config()

    assert cfg["read_ahead_enabled"] is True
    assert cfg["read_ahead_window_ms"] == 200
    assert cfg["read_ahead_max_messages"] == 8


def test_format_driver_label_shows_driver_architecture_without_python_warning(monkeypatch, tmp_path) -> None:
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

    assert label == "SM2 USB (Scanmatik) [x86]"


def test_format_driver_label_can_include_path_to_disambiguate_duplicate_drivers(monkeypatch, tmp_path) -> None:
    client_gui = _import_client_gui(monkeypatch, tmp_path)

    first = client_gui.format_driver_label(
        {
            "name": "SM2 USB",
            "vendor": "Scanmatik",
            "architecture": "x86",
            "dll_path": r"C:\Program Files (x86)\Scanmatik\smj2534.dll",
        },
        include_path=True,
    )
    second = client_gui.format_driver_label(
        {
            "name": "SM2 USB",
            "vendor": "Scanmatik",
            "architecture": "x86",
            "dll_path": r"D:\OEM\Scanmatik\smj2534.dll",
        },
        include_path=True,
    )

    assert first != second
    assert r"C:\Program Files (x86)\Scanmatik\smj2534.dll" in first
    assert r"D:\OEM\Scanmatik\smj2534.dll" in second


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
    monkeypatch.setattr(client_gui, "ObservabilityOutbox", lambda appdata=None: types.SimpleNamespace(
        stage_default_artifacts=lambda **kwargs: {"queued_count": 0},
        upload_pending=lambda **kwargs: {"uploaded_count": 0},
    ))
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
        "read_ahead_enabled": False,
        "read_ahead_window_ms": 200,
        "read_ahead_max_reads": 3,
        "read_ahead_read_timeout_ms": 0,
        "read_ahead_max_messages": 16,
        "read_ahead_transaction_enabled": False,
    }
    assert callable(observed["on_status_change"])
    assert app._client_thread.started is True


def test_upload_observability_once_stages_and_uploads_outbox(monkeypatch, tmp_path) -> None:
    client_gui = _import_client_gui(monkeypatch, tmp_path)
    observed: dict[str, object] = {}

    class _FakeOutbox:
        def __init__(self, appdata=None):
            observed["appdata"] = appdata

        def stage_default_artifacts(self, **kwargs):
            observed["stage_kwargs"] = kwargs
            return {"queued_count": 2}

        def upload_pending(self, **kwargs):
            observed["upload_kwargs"] = kwargs
            return {"uploaded_count": 1}

    monkeypatch.setattr(client_gui, "ObservabilityOutbox", _FakeOutbox)
    monkeypatch.setattr(client_gui.socket, "gethostname", lambda: "host-1")

    app = client_gui.VCIProxyTrayApp()
    app._config = {
        "api_scheme": "https",
        "host": "diag.example",
        "port": 9000,
        "api_port": 8080,
        "api_token": "api-secret",
        "auth_token": "secret",
        "dll_path": "",
        "tls_enabled": False,
        "tls_ca_file": "",
        "tls_server_name": "",
    }

    result = app._upload_observability_once()

    assert result == {"queued_count": 2, "uploaded_count": 1}
    assert observed["stage_kwargs"]["client_instance_id"] == "host-1-tray"
    assert observed["upload_kwargs"]["api_base_url"] == "https://diag.example:8080"
    assert observed["upload_kwargs"]["api_token"] == "api-secret"


def test_upload_observability_once_prefers_assigned_api_base_over_tunnel_host(monkeypatch, tmp_path) -> None:
    client_gui = _import_client_gui(monkeypatch, tmp_path)
    observed: dict[str, object] = {}

    class _FakeOutbox:
        def __init__(self, appdata=None):
            pass

        def stage_default_artifacts(self, **kwargs):
            return {"queued_count": 0}

        def upload_pending(self, **kwargs):
            observed["upload_kwargs"] = kwargs
            return {"uploaded_count": 0}

    monkeypatch.setattr(client_gui, "ObservabilityOutbox", _FakeOutbox)
    monkeypatch.setattr(client_gui.socket, "gethostname", lambda: "host-1")

    app = client_gui.VCIProxyTrayApp()
    app._config = {
        "api_scheme": "http",
        "host": "tunnel.example.com",
        "port": 9000,
        "api_port": 8080,
        "api_token": "api-secret",
        "auth_token": "secret",
        "dll_path": "",
        "tls_enabled": False,
        "tls_ca_file": "",
        "tls_server_name": "",
    }
    app._active_node_assignment = {
        "api_base_url": "https://api.customer-node.example.com:443",
        "tunnel_host": "tunnel.customer-node.example.com",
    }

    app._upload_observability_once()

    assert observed["upload_kwargs"]["api_base_url"] == "https://api.customer-node.example.com:443"


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


def test_complete_guarded_quit_hides_and_stops_tray(monkeypatch, tmp_path) -> None:
    client_gui = _import_client_gui(monkeypatch, tmp_path)
    observed: dict[str, object] = {}

    class _FakeTray:
        def __init__(self) -> None:
            self.visible = True
            self.stopped = False

        def stop(self) -> None:
            self.stopped = True

    tray = _FakeTray()
    app = client_gui.VCIProxyTrayApp()
    app._tray = tray
    app._diagnostics_guard_controller = types.SimpleNamespace(
        get_attached_window=lambda: None
    )
    app._stop_client = lambda: observed.setdefault("client_stopped", True)
    app._stop_ui_thread = lambda: observed.setdefault("ui_stopped", True)

    app._complete_guarded_quit()

    assert observed == {"client_stopped": True, "ui_stopped": True}
    assert tray.visible is False
    assert tray.stopped is True
    assert app._tray is None


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
    app._client_thread = types.SimpleNamespace(is_alive=lambda: True)
    app._stop_client = lambda: observed.setdefault("stopped", True)
    app._start_client = lambda: observed.setdefault("started", True)
    monkeypatch.setattr(client_gui, "ConfigDialog", _FakeDialog)
    monkeypatch.setattr(client_gui.threading, "Thread", _ImmediateThread)

    app._on_settings()

    assert observed["dialog_config"]["auth_token"] == ""
    assert "stopped" not in observed
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
    app._client_thread = types.SimpleNamespace(is_alive=lambda: True)
    monkeypatch.setattr(client_gui, "ConfigDialog", _FakeDialog)
    monkeypatch.setattr(client_gui, "save_config", lambda cfg: observed.setdefault("saved", dict(cfg)))
    monkeypatch.setattr(app, "_start_client", lambda: observed.setdefault("started", True))
    monkeypatch.setattr(app, "_restart_client", lambda: observed.setdefault("restarted", True))
    monkeypatch.setattr(app, "_run_on_ui_thread", lambda callback, timeout=None: callback())

    app._run_settings_dialog()

    assert observed["saved"]["tls_enabled"] is True
    assert observed["saved"]["tls_ca_file"] == "C:/certs/ca.pem"
    assert observed["saved"]["tls_server_name"] == "diag.example"
    assert observed["saved"]["api_scheme"] == "https"
    assert observed["saved"]["api_token"] == "api-secret"
    assert "started" not in observed
    assert "restarted" not in observed


def test_run_settings_dialog_restarts_client_when_tunnel_settings_change(monkeypatch, tmp_path) -> None:
    client_gui = _import_client_gui(monkeypatch, tmp_path)
    observed: dict[str, object] = {}

    class _FakeDialog:
        def __init__(self, config):
            observed["dialog_config"] = dict(config)

        def show(self):
            return {
                "api_scheme": "http",
                "host": "diag.example",
                "port": 9000,
                "api_port": 8080,
                "api_token": "",
                "auth_token": "updated-secret",
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
        "tls_enabled": False,
        "tls_ca_file": "",
        "tls_server_name": "",
    }
    app._client_thread = types.SimpleNamespace(is_alive=lambda: True)
    monkeypatch.setattr(client_gui, "ConfigDialog", _FakeDialog)
    monkeypatch.setattr(client_gui, "save_config", lambda cfg: observed.setdefault("saved", dict(cfg)))
    monkeypatch.setattr(app, "_restart_client", lambda: observed.setdefault("restarted", True))
    monkeypatch.setattr(app, "_start_client", lambda: observed.setdefault("started", True))
    monkeypatch.setattr(app, "_run_on_ui_thread", lambda callback, timeout=None: callback())

    app._run_settings_dialog()

    assert observed["saved"]["auth_token"] == "updated-secret"
    assert observed["restarted"] is True
    assert "started" not in observed


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
    monkeypatch.setattr(
        app,
        "_run_on_ui_thread",
        lambda callback, timeout=None: observed.setdefault("ui_calls", []).append(True) or callback(),
    )

    app.run()

    assert observed["ui_calls"] == [True]
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
    assert app._tray.visible is True
    assert app._tray.notifications == [
        (
            "VCI Proxy is running in the system tray. Right-click the tray icon to open Settings or Diagnostics.",
            "VCI Proxy",
        )
    ]


def test_run_shows_startup_tray_notification_when_config_is_ready(monkeypatch, tmp_path) -> None:
    client_gui = _import_client_gui(monkeypatch, tmp_path)
    observed: dict[str, object] = {}

    app = client_gui.VCIProxyTrayApp()
    app._config = {
        "api_scheme": "http",
        "host": "diag.example",
        "port": 9000,
        "api_port": 8080,
        "api_token": "",
        "auth_token": "secret",
        "dll_path": "",
        "tls_enabled": False,
        "tls_ca_file": "",
        "tls_server_name": "",
    }
    monkeypatch.setattr(app, "_start_client", lambda: observed.setdefault("started", True))

    app.run()

    assert observed["started"] is True
    assert app._tray is not None
    assert app._tray.ran is True
    assert app._tray.visible is True
    assert app._tray.notifications == [
        (
            "VCI Proxy is running in the system tray. Right-click the tray icon to open Settings or Diagnostics.",
            "VCI Proxy",
        )
    ]


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
    app._run_on_ui_thread = lambda callback, timeout=None: callback()

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
    app._run_on_ui_thread = lambda callback, timeout=None: callback()

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


def test_on_node_assignment_none_can_skip_restart_for_guarded_release(monkeypatch, tmp_path) -> None:
    client_gui = _import_client_gui(monkeypatch, tmp_path)
    restarted: list[bool] = []

    app = client_gui.VCIProxyTrayApp()
    app._active_node_assignment = {
        "tunnel_host": "lax-1.diag.example.com",
        "api_base_url": "https://lax-1.diag.example.com:443",
    }
    app._restart_client = lambda: restarted.append(True)
    app._suppress_next_assignment_reconnect()

    app._on_node_assignment(None)

    assert app._active_node_assignment is None
    assert restarted == []


def test_on_quit_defers_shutdown_when_active_session_exists(monkeypatch, tmp_path) -> None:
    client_gui = _import_client_gui(monkeypatch, tmp_path)
    observed: dict[str, object] = {}

    class _FakeController:
        def has_active_session(self) -> bool:
            return True

        def request_guarded_action(self, action_name, **kwargs):
            observed["action_name"] = action_name
            observed["kwargs"] = kwargs
            return True

    app = client_gui.VCIProxyTrayApp()
    app._tray = types.SimpleNamespace(stop=lambda: observed.setdefault("tray_stopped", True))
    app._stop_client = lambda: observed.setdefault("stopped", True)
    app._stop_ui_thread = lambda: observed.setdefault("ui_stopped", True)
    app._ensure_diagnostics_guard_controller = lambda: _FakeController()
    app._ask_threadsafe_confirmation = lambda title, message: observed.setdefault("confirmed", (title, message)) or True

    app._on_quit()

    assert observed["action_name"] == "quit_app"
    assert "stopped" not in observed
    assert "ui_stopped" not in observed
    assert "tray_stopped" not in observed


def test_run_settings_dialog_defers_restart_when_active_session_exists(monkeypatch, tmp_path) -> None:
    client_gui = _import_client_gui(monkeypatch, tmp_path)
    observed: dict[str, object] = {}

    class _FakeDialog:
        def __init__(self, config):
            observed["dialog_config"] = dict(config)

        def show(self):
            return {
                "api_scheme": "http",
                "host": "diag.example",
                "port": 9000,
                "api_port": 8080,
                "api_token": "",
                "auth_token": "updated-secret",
                "dll_path": "",
            }

    class _FakeController:
        def has_active_session(self) -> bool:
            return True

        def request_guarded_action(self, action_name, **kwargs):
            observed["action_name"] = action_name
            observed["kwargs"] = kwargs
            return True

    app = client_gui.VCIProxyTrayApp()
    app._config = {
        "api_scheme": "http",
        "host": "diag.example",
        "port": 9000,
        "api_port": 8080,
        "api_token": "",
        "auth_token": "secret",
        "dll_path": "",
        "tls_enabled": False,
        "tls_ca_file": "",
        "tls_server_name": "",
        "read_ahead_enabled": False,
        "read_ahead_window_ms": 200,
        "read_ahead_max_reads": 3,
        "read_ahead_read_timeout_ms": 0,
        "read_ahead_max_messages": 16,
    }
    app._client_thread = types.SimpleNamespace(is_alive=lambda: True)
    monkeypatch.setattr(client_gui, "ConfigDialog", _FakeDialog)
    monkeypatch.setattr(client_gui, "save_config", lambda cfg: observed.setdefault("saved", dict(cfg)))
    monkeypatch.setattr(app, "_restart_client", lambda: observed.setdefault("restarted", True))
    monkeypatch.setattr(app, "_start_client", lambda: observed.setdefault("started", True))
    monkeypatch.setattr(app, "_run_on_ui_thread", lambda callback, timeout=None: callback())
    app._ensure_diagnostics_guard_controller = lambda: _FakeController()
    app._ask_threadsafe_confirmation = lambda title, message: observed.setdefault("confirmed", (title, message)) or True

    app._run_settings_dialog()

    assert observed["action_name"] == "apply_settings_and_restart"
    assert app._pending_restart_config is not None
    assert app._pending_restart_config["auth_token"] == "updated-secret"
    assert "saved" not in observed
    assert "restarted" not in observed
    assert "started" not in observed


def test_show_threadsafe_error_uses_single_ui_executor(monkeypatch, tmp_path) -> None:
    client_gui = _import_client_gui(monkeypatch, tmp_path)
    app = client_gui.VCIProxyTrayApp()
    observed: dict[str, object] = {"ui_calls": 0}

    def _run_on_ui_thread(callback, timeout=None):
        observed["ui_calls"] = int(observed["ui_calls"]) + 1
        return callback()

    monkeypatch.setattr(app, "_run_on_ui_thread", _run_on_ui_thread)
    monkeypatch.setattr(client_gui.messagebox, "showerror", lambda title, message: observed.setdefault("dialog", (title, message)))
    monkeypatch.setattr(
        client_gui.tk,
        "Tk",
        lambda: types.SimpleNamespace(withdraw=lambda: None, destroy=lambda: None),
    )

    app._show_threadsafe_error("Diagnostics Error", "boom")

    assert observed["ui_calls"] == 1
    assert observed["dialog"] == ("Diagnostics Error", "boom")


def test_ask_threadsafe_confirmation_prefers_attached_window_executor(monkeypatch, tmp_path) -> None:
    client_gui = _import_client_gui(monkeypatch, tmp_path)
    app = client_gui.VCIProxyTrayApp()
    observed: dict[str, object] = {}

    def _run_window_callback(callback):
        observed["window_executor"] = True
        return callback()

    fake_window = types.SimpleNamespace(
        _root=object(),
        _run_sync_ui_callback=_run_window_callback,
    )
    app._diagnostics_guard_controller = types.SimpleNamespace(
        get_attached_window=lambda: fake_window
    )
    monkeypatch.setattr(
        app,
        "_run_on_ui_thread",
        lambda callback, timeout=None: (_ for _ in ()).throw(
            AssertionError("tray UI executor should not be used while diagnostics window is attached")
        ),
    )
    monkeypatch.setattr(
        client_gui.messagebox,
        "askyesno",
        lambda title, message, parent=None: observed.setdefault("parent", parent) or True,
    )

    assert app._ask_threadsafe_confirmation("Quit", "Continue?") is True
    assert observed["window_executor"] is True
    assert observed["parent"] is fake_window._root


def test_handle_guarded_action_failure_shows_warning_for_cross_action_conflict(monkeypatch, tmp_path) -> None:
    client_gui = _import_client_gui(monkeypatch, tmp_path)
    app = client_gui.VCIProxyTrayApp()
    observed: dict[str, object] = {}

    app._show_threadsafe_error = lambda title, message: observed.setdefault("error", (title, message))
    app._ask_threadsafe_confirmation = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("force confirmation should not be shown for a cross-action conflict")
    )
    app._diagnostics_guard_controller = types.SimpleNamespace(
        force_pending_action=lambda: observed.setdefault("forced", True),
        cancel_pending_action=lambda: observed.setdefault("cancelled", True),
    )

    app._handle_guarded_action_failure(
        "quit_app",
        "Another shutdown action is already in progress. Please wait for it to finish.",
    )

    assert observed["error"] == (
        "Action In Progress",
        "Another shutdown action is already in progress. Please wait for it to finish.",
    )
    assert "forced" not in observed
    assert "cancelled" not in observed


def test_main_forces_logging_configuration(monkeypatch, tmp_path) -> None:
    client_gui = _import_client_gui(monkeypatch, tmp_path)
    observed: dict[str, object] = {}

    monkeypatch.setattr(client_gui.logging, "basicConfig", lambda **kwargs: observed.update(kwargs))
    monkeypatch.setattr(
        client_gui,
        "VCIProxyTrayApp",
        lambda: types.SimpleNamespace(run=lambda: observed.setdefault("ran", True)),
    )

    client_gui.main()

    assert observed["force"] is True
    assert observed["ran"] is True
    assert len(observed["handlers"]) == 2
