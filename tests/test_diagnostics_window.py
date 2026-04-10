from __future__ import annotations

import threading
import tkinter as tk

from vci_proxy.diagnostics_window import DiagnosticsWindow


class _Var:
    def __init__(self, value: str = "") -> None:
        self._value = value

    def get(self) -> str:
        return self._value

    def set(self, value: str) -> None:
        self._value = value


class _Widget:
    def __init__(self) -> None:
        self.state = None
        self.values = None

    def configure(self, **kwargs) -> None:
        if "state" in kwargs:
            self.state = kwargs["state"]
        if "values" in kwargs:
            self.values = kwargs["values"]


def _build_window(*, current_page: str = "") -> DiagnosticsWindow:
    window = DiagnosticsWindow.__new__(DiagnosticsWindow)
    window._selected_module = _Var("ECM")
    window._selected_data_category = _Var("Diagnostic Data Display")
    window._session_id = "session-123"
    window._stream_active = False
    window._ai_sse_running = False
    window._ai_start_pending = False
    window._auto_ai_start_scheduled = False
    window._session_live_data_active = False
    window._session_ai_active = False
    window._session_navigation_active = False
    window._session_category_confirmed = True
    window._current_page = current_page
    window._session_status_refresh_inflight = False
    window._server_connected = None
    window._status_message = _Var("")
    window._session_hint_var = _Var("")
    window._dtc_count_text = _Var("")
    window._select_module_button = _Widget()
    window._select_data_category_button = _Widget()
    window._read_dtc_button = _Widget()
    window._clear_dtc_button = _Widget()
    window._start_stream_button = _Widget()
    window._ai_diagnose_button = _Widget()
    window._module_combo = _Widget()
    window._data_combo = _Widget()
    window._agent_messages = []
    window._append_agent_message = lambda role, message: window._agent_messages.append((role, message))
    window._set_status_text = lambda message: window._status_message.set(message)
    window._set_server_connected = lambda connected: setattr(window, "_server_connected", connected)
    return window


def test_refresh_action_buttons_keeps_clear_dtcs_disabled_off_data_display() -> None:
    window = _build_window(current_page="module_list")

    window._refresh_action_buttons()

    assert window._read_dtc_button.state == tk.NORMAL
    assert window._clear_dtc_button.state == tk.DISABLED


def test_refresh_action_buttons_enables_clear_dtcs_on_data_display() -> None:
    window = _build_window(current_page="data_display")

    window._refresh_action_buttons()

    assert window._clear_dtc_button.state == tk.NORMAL


def test_handle_session_status_result_updates_current_page_and_button_state() -> None:
    window = _build_window(current_page="")

    window._handle_session_status_result(
        {
            "success": True,
            "active_ai_session_id": "",
            "active_navigation_session_id": "",
            "live_data_active": False,
            "backend_state_summary": {
                "current_page": "data_display",
            },
        }
    )

    assert window._current_page == "data_display"
    assert window._clear_dtc_button.state == tk.NORMAL


def test_on_clear_dtcs_clicked_posts_session_clear_request() -> None:
    window = _build_window(current_page="data_display")
    calls: list[tuple[str, str, dict[str, object]]] = []
    window._api_call = lambda method, endpoint, **kwargs: calls.append((method, endpoint, kwargs))

    window._on_clear_dtcs_clicked()

    assert window._clear_dtc_button.state == tk.DISABLED
    assert calls == [
        (
            "POST",
            "/api/session/clear_dtcs",
            {
                "json_data": {
                    "session_id": "session-123",
                },
                "callback_event": "clear_dtcs_result",
            },
        )
    ]


def test_refresh_action_buttons_disables_clear_dtcs_while_ai_pending() -> None:
    window = _build_window(current_page="data_display")
    window._ai_start_pending = True

    window._refresh_action_buttons()

    assert window._clear_dtc_button.state == tk.DISABLED


def test_handle_session_status_result_disables_clear_dtcs_while_session_ai_active() -> None:
    window = _build_window(current_page="")

    window._handle_session_status_result(
        {
            "success": True,
            "active_ai_session_id": "ai-123",
            "active_navigation_session_id": "",
            "live_data_active": False,
            "backend_state_summary": {
                "current_page": "data_display",
            },
        }
    )

    assert window._current_page == "data_display"
    assert window._clear_dtc_button.state == tk.DISABLED


def test_request_headers_include_api_token_when_configured() -> None:
    window = DiagnosticsWindow.__new__(DiagnosticsWindow)
    window._api_token = "api-secret"

    assert window._request_headers() == {"X-API-Token": "api-secret"}


def test_request_headers_omit_api_token_when_missing() -> None:
    window = DiagnosticsWindow.__new__(DiagnosticsWindow)
    window._api_token = ""

    assert window._request_headers() == {}


def test_api_call_sends_api_token_header(monkeypatch) -> None:
    captured: dict[str, object] = {}
    window = DiagnosticsWindow.__new__(DiagnosticsWindow)
    window._api_base = "https://cust001.diag.example.com"
    window._api_token = "api-secret"
    window._queue = []

    class _ImmediateThread:
        def __init__(self, target=None, daemon=None, name=None):
            self.target = target
            self.daemon = daemon
            self.name = name

        def start(self):
            if self.target is not None:
                self.target()

    class _Queue:
        def put(self, item) -> None:
            captured["queue_item"] = item

    class _Response:
        ok = True
        status_code = 200
        reason = "OK"

        @staticmethod
        def json() -> dict[str, object]:
            return {"success": True}

    def _fake_post(url, json=None, params=None, timeout=None, headers=None):
        captured["url"] = url
        captured["json"] = json
        captured["params"] = params
        captured["timeout"] = timeout
        captured["headers"] = headers
        return _Response()

    window._queue = _Queue()
    monkeypatch.setattr(threading, "Thread", _ImmediateThread)
    monkeypatch.setattr("vci_proxy.diagnostics_window.requests.post", _fake_post)

    window._api_call(
        "POST",
        "/api/session/start",
        json_data={"brand": "gds2"},
        callback_event="session_start_result",
    )

    assert captured["url"] == "https://cust001.diag.example.com/api/session/start"
    assert captured["headers"] == {"X-API-Token": "api-secret"}
    assert captured["queue_item"] == ("session_start_result", {"success": True})


def test_start_session_sse_thread_sends_api_token_header(monkeypatch) -> None:
    captured: dict[str, object] = {}
    window = DiagnosticsWindow.__new__(DiagnosticsWindow)
    window._api_base = "https://cust001.diag.example.com"
    window._api_token = "api-secret"
    window._session_sse_running = False
    window._session_sse_thread = None
    window._session_sse_response = None
    window._queue = _QueueForSse()

    class _ImmediateThread:
        def __init__(self, target=None, daemon=None, name=None):
            self.target = target
            self.daemon = daemon
            self.name = name

        def start(self):
            if self.target is not None:
                self.target()

        def is_alive(self) -> bool:
            return False

        def join(self, timeout=None) -> None:
            return None

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self) -> None:
            return None

        def iter_lines(self, decode_unicode=True):
            return iter([])

        def close(self) -> None:
            return None

    def _fake_get(url, stream=None, timeout=None, headers=None):
        captured["url"] = url
        captured["stream"] = stream
        captured["timeout"] = timeout
        captured["headers"] = headers
        return _Response()

    monkeypatch.setattr(threading, "Thread", _ImmediateThread)
    monkeypatch.setattr("vci_proxy.diagnostics_window.requests.get", _fake_get)

    window._start_session_sse_thread("session-123")

    assert captured["url"] == "https://cust001.diag.example.com/api/session/events?session_id=session-123"
    assert captured["headers"] == {"X-API-Token": "api-secret"}


class _QueueForSse:
    def put(self, item) -> None:
        return None
