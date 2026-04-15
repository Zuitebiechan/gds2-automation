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


def test_handle_session_status_result_surfaces_pending_decision() -> None:
    window = _build_window(current_page="")
    prompted: list[dict[str, object]] = []
    decision = {
        "decision_id": "decision-1",
        "prompt": "Choose a backend",
        "options": [
            {"option_id": "backend:gds2", "label": "GDS2", "description": "GM"},
        ],
    }
    window._prompt_decision = lambda payload: prompted.append(payload) or True
    window._show_decision_modal = lambda payload: prompted.append({"modal": payload})

    window._handle_session_status_result(
        {
            "success": True,
            "active_ai_session_id": "",
            "active_navigation_session_id": "",
            "live_data_active": False,
            "pending_decision": decision,
            "backend_state_summary": {
                "current_page": "vehicle_selection",
            },
        }
    )

    assert prompted == [decision]


def test_request_headers_include_api_token_when_configured() -> None:
    window = DiagnosticsWindow.__new__(DiagnosticsWindow)
    window._api_token = "api-secret"

    assert window._request_headers() == {"X-API-Token": "api-secret"}


def test_request_headers_omit_api_token_when_missing() -> None:
    window = DiagnosticsWindow.__new__(DiagnosticsWindow)
    window._api_token = ""

    assert window._request_headers() == {}


def test_on_session_start_clicked_uses_bootstrap_when_enabled() -> None:
    window = DiagnosticsWindow.__new__(DiagnosticsWindow)
    calls: list[tuple[str, str, dict[str, object]]] = []
    messages: list[tuple[str, str]] = []
    window._session_brand = _Var("Chevrolet")
    window._session_start_button = _Widget()
    window._start_button = _Widget()
    window._session_status_var = _Var("")
    window._use_session_bootstrap = True
    window._append_agent_message = lambda role, message: messages.append((role, message))
    window._build_session_start_payload = lambda brand: {
        "brand": brand,
        "client_time_zone": "America/Chicago",
    }
    window._api_call = lambda method, endpoint, **kwargs: calls.append((method, endpoint, kwargs))

    window._on_session_start_clicked()

    assert window._session_status_var.get() == "Starting session..."
    assert calls == [
        (
            "POST",
            "/api/session/bootstrap",
            {
                "json_data": {
                    "brand": "Chevrolet",
                    "client_time_zone": "America/Chicago",
                },
                "callback_event": "session_bootstrap_result",
            },
        )
    ]


def test_build_session_start_payload_includes_client_time_zone_hint_when_available(monkeypatch) -> None:
    window = DiagnosticsWindow.__new__(DiagnosticsWindow)
    monkeypatch.setattr(window, "_client_time_zone_hint", lambda: "America/Chicago")

    payload = window._build_session_start_payload("Chevrolet")

    assert payload == {
        "brand": "Chevrolet",
        "client_time_zone": "America/Chicago",
    }


def test_handle_session_bootstrap_result_switches_api_base_and_starts_session() -> None:
    window = DiagnosticsWindow.__new__(DiagnosticsWindow)
    calls: list[tuple[str, str, dict[str, object]]] = []
    assignments: list[dict[str, object]] = []
    messages: list[tuple[str, str]] = []
    window._api_base = "https://entry.diag.example.com"
    window._server_display = "entry.diag.example.com"
    window._server_state_text = _Var("Server: entry.diag.example.com")
    window._session_brand = _Var("Chevrolet")
    window._session_start_button = _Widget()
    window._start_button = _Widget()
    window._session_status_var = _Var("")
    window._node_assignment_callback = lambda assignment: assignments.append(assignment)
    window._append_agent_message = lambda role, message: messages.append((role, message))
    window._api_call = lambda method, endpoint, **kwargs: calls.append((method, endpoint, kwargs))

    payload = {
        "success": True,
        "assignment": {
            "assignment_id": "assign-123",
            "node_id": "node-lax-1",
            "zone": "us-west-2-lax-1a",
            "metro": "los-angeles",
            "api_base_url": "https://lax-1.diag.example.com",
            "tunnel_host": "lax-1.diag.example.com",
            "selection_reason": "preferred_zone",
        },
        "session_context": {
            "brand": "Chevrolet",
            "model": "Malibu",
            "vin": "VIN123",
            "backend_name": "",
            "extra": {},
        },
        "next_action": "start_session_on_assigned_node",
    }

    window._handle_session_bootstrap_result(payload)

    assert window._api_base == "https://lax-1.diag.example.com"
    assert window._server_display == "lax-1.diag.example.com"
    assert window._server_state_text.get() == "Server: lax-1.diag.example.com"
    assert assignments == [payload["assignment"]]
    assert calls == [
        (
            "POST",
            "/api/session/start",
            {
                "json_data": {
                    "brand": "Chevrolet",
                    "model": "Malibu",
                    "vin": "VIN123",
                    "backend_name": "",
                },
                "callback_event": "session_start_result",
            },
        )
    ]


def test_handle_session_start_result_binds_active_assignment_via_bootstrap_api() -> None:
    window = DiagnosticsWindow.__new__(DiagnosticsWindow)
    bootstrap_calls: list[tuple[str, str, dict[str, object]]] = []
    window._session_start_button = _Widget()
    window._session_abort_button = _Widget()
    window._start_button = _Widget()
    window._select_data_category_button = _Widget()
    window._session_status_var = _Var("")
    window._session_hint_var = _Var("")
    window._server_state_text = _Var("Server: lax-1.diag.example.com")
    window._agent_messages = []
    window._append_agent_message = lambda role, message: window._agent_messages.append((role, message))
    window._set_current_page = lambda page: setattr(window, "_current_page", page)
    window._set_agent_prompt = lambda *args, **kwargs: None
    window._refresh_action_buttons = lambda: None
    window._prompt_decision = lambda decision: False
    window._show_decision_modal = lambda decision: None
    window._start_session_sse_thread = lambda session_id: setattr(window, "_session_sse_started", session_id)
    window._request_session_status_refresh = lambda: setattr(window, "_status_refresh_requested", True)
    window._bootstrap_api_base = "https://entry.diag.example.com"
    window._active_assignment = {
        "assignment_id": "assign-123",
        "api_base_url": "https://lax-1.diag.example.com",
        "tunnel_host": "lax-1.diag.example.com",
    }
    window._api_call_to_base = lambda base_url, method, endpoint, **kwargs: bootstrap_calls.append(
        (base_url, endpoint, kwargs)
    )

    window._handle_session_start_result(
        {
            "success": True,
            "session_id": "session-123",
            "status": "running",
            "workflow": "gds2",
        }
    )

    assert bootstrap_calls == [
        (
            "https://entry.diag.example.com",
            "/api/session/bootstrap/bind",
            {
                "json_data": {
                    "assignment_id": "assign-123",
                    "session_id": "session-123",
                },
                "callback_event": "session_bootstrap_bind_result",
            },
        )
    ]


def test_handle_session_start_result_recovers_existing_active_session() -> None:
    window = DiagnosticsWindow.__new__(DiagnosticsWindow)
    bind_calls: list[str] = []
    prompted: list[dict[str, object]] = []
    messages: list[tuple[str, str]] = []
    decision = {
        "decision_id": "decision-1",
        "prompt": "Choose a backend",
        "options": [
            {"option_id": "backend:gds2", "label": "GDS2", "description": "GM"},
        ],
    }
    window._session_start_button = _Widget()
    window._session_abort_button = _Widget()
    window._start_button = _Widget()
    window._select_data_category_button = _Widget()
    window._session_status_var = _Var("")
    window._session_hint_var = _Var("")
    window._active_assignment = {"assignment_id": "assign-123"}
    window._append_agent_message = lambda role, message: messages.append((role, message))
    window._set_current_page = lambda page: setattr(window, "_current_page", page)
    window._set_agent_prompt = lambda *args, **kwargs: None
    window._refresh_action_buttons = lambda: None
    window._prompt_decision = lambda payload: prompted.append(payload) or True
    window._show_decision_modal = lambda payload: prompted.append({"modal": payload})
    window._bind_active_assignment = lambda session_id: bind_calls.append(session_id)
    window._start_session_sse_thread = lambda session_id: setattr(window, "_session_sse_started", session_id)
    window._request_session_status_refresh = lambda: setattr(window, "_status_refresh_requested", True)
    window._release_active_assignment = lambda: bind_calls.append("released")

    window._handle_session_start_result(
        {
            "success": False,
            "error": "Another session is already active (session_id=session-123, status=running)",
            "error_code": "active_session_exists",
            "active_session_id": "session-123",
            "active_session_status": "running",
            "active_backend_name": "gds2",
            "decision": decision,
        }
    )

    assert window._session_id == "session-123"
    assert window._session_start_button.state == tk.DISABLED
    assert window._session_abort_button.state == tk.NORMAL
    assert window._start_button.state == tk.NORMAL
    assert window._session_sse_started == "session-123"
    assert window._status_refresh_requested is True
    assert bind_calls == ["session-123"]
    assert prompted == [decision]
    assert "Recovered session" in window._session_status_var.get()
    assert any("Recovered existing session" in message for _, message in messages)


def test_handle_session_done_releases_assignment_and_restores_bootstrap_base() -> None:
    window = DiagnosticsWindow.__new__(DiagnosticsWindow)
    release_calls: list[tuple[str, str, dict[str, object]]] = []
    assignment_events: list[object] = []
    window._session_start_button = _Widget()
    window._session_abort_button = _Widget()
    window._start_button = _Widget()
    window._select_data_category_button = _Widget()
    window._session_status_var = _Var("")
    window._session_hint_var = _Var("")
    window._server_state_text = _Var("Server: lax-1.diag.example.com")
    window._append_agent_message = lambda role, message: None
    window._stop_session_sse_thread = lambda: None
    window._close_decision_modal = lambda: None
    window._set_agent_prompt = lambda *args, **kwargs: None
    window._set_current_page = lambda page: setattr(window, "_current_page", page)
    window._refresh_action_buttons = lambda: None
    window._node_assignment_callback = lambda assignment: assignment_events.append(assignment)
    window._api_base = "https://lax-1.diag.example.com"
    window._server_display = "lax-1.diag.example.com"
    window._bootstrap_api_base = "https://entry.diag.example.com"
    window._active_assignment = {
        "assignment_id": "assign-123",
        "api_base_url": "https://lax-1.diag.example.com",
        "tunnel_host": "lax-1.diag.example.com",
    }
    window._api_call_to_base = lambda base_url, method, endpoint, **kwargs: release_calls.append(
        (base_url, endpoint, kwargs)
    )

    window._handle_session_done({"success": True})

    assert release_calls == [
        (
            "https://entry.diag.example.com",
            "/api/session/bootstrap/release",
            {
                "json_data": {"assignment_id": "assign-123"},
                "callback_event": "session_bootstrap_release_result",
            },
        )
    ]
    assert window._api_base == "https://entry.diag.example.com"
    assert window._server_display == "entry.diag.example.com"
    assert window._server_state_text.get() == "Server: entry.diag.example.com"
    assert assignment_events == [None]


def test_handle_session_bootstrap_result_falls_back_to_direct_start_when_bootstrap_is_unavailable() -> None:
    window = DiagnosticsWindow.__new__(DiagnosticsWindow)
    calls: list[tuple[str, str, dict[str, object]]] = []
    messages: list[tuple[str, str]] = []
    window._api_base = "https://direct-node.diag.example.com"
    window._session_brand = _Var("Chevrolet")
    window._session_start_button = _Widget()
    window._start_button = _Widget()
    window._session_status_var = _Var("")
    window._use_session_bootstrap = True
    window._append_agent_message = lambda role, message: messages.append((role, message))
    window._api_call = lambda method, endpoint, **kwargs: calls.append((method, endpoint, kwargs))
    window._handle_session_start_result = lambda payload: calls.append(("HANDLER", "session_start_result", {"payload": payload}))

    window._handle_session_bootstrap_result(
        {
            "success": False,
            "error": "Node allocator is not configured",
        }
    )

    assert window._use_session_bootstrap is False
    assert calls == [
        (
            "POST",
            "/api/session/start",
            {
                "json_data": {"brand": "Chevrolet"},
                "callback_event": "session_start_result",
            },
        )
    ]


def test_handle_session_bootstrap_result_handles_capacity_pending_without_fallback() -> None:
    window = DiagnosticsWindow.__new__(DiagnosticsWindow)
    calls: list[tuple[str, str, dict[str, object]]] = []
    messages: list[tuple[str, str]] = []
    window._session_start_button = _Widget()
    window._start_button = _Widget()
    window._session_status_var = _Var("")
    window._session_hint_var = _Var("")
    window._use_session_bootstrap = True
    window._append_agent_message = lambda role, message: messages.append((role, message))
    window._api_call = lambda method, endpoint, **kwargs: calls.append((method, endpoint, kwargs))
    window._set_session_hint = lambda message: window._session_hint_var.set(message)

    window._handle_session_bootstrap_result(
        {
            "success": True,
            "pending_capacity": True,
            "status": "capacity_pending",
            "retry_after_sec": 30,
            "next_action": "retry_session_bootstrap",
            "provisioning": {
                "node_id": "i-0abc123",
                "zone": "us-west-2-lax-1a",
                "metro": "los-angeles",
                "state": "booting",
            },
        }
    )

    assert window._session_start_button.state == tk.NORMAL
    assert window._start_button.state == tk.DISABLED
    assert window._session_status_var.get() == "Capacity is starting in the target zone. Please retry shortly."
    assert "retry" in window._session_hint_var.get().lower()
    assert calls == []


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
