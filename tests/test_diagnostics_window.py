from __future__ import annotations

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
                    "module": "ECM",
                    "data_category": "Diagnostic Data Display",
                },
                "callback_event": "clear_dtcs_result",
            },
        )
    ]
