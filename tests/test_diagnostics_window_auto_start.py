from types import SimpleNamespace

from vci_proxy.diagnostics_window import DiagnosticsWindow


class _FakeVar:
    def __init__(self, value: str):
        self._value = value

    def get(self) -> str:
        return self._value

    def set(self, value: str) -> None:
        self._value = value


class _FakeWidget:
    def __init__(self):
        self.state = None
        self.values = None

    def configure(self, **kwargs):
        if "state" in kwargs:
            self.state = kwargs["state"]
        if "values" in kwargs:
            self.values = kwargs["values"]


class _FakeRoot:
    def __init__(self):
        self.after_calls: list[tuple[int, object]] = []
        self.auto_run = True

    def after(self, delay_ms: int, callback):
        self.after_calls.append((delay_ms, callback))
        if self.auto_run:
            callback()


def _make_window() -> DiagnosticsWindow:
    window = DiagnosticsWindow.__new__(DiagnosticsWindow)
    window._selected_module = _FakeVar("ECM")
    window._selected_data_category = _FakeVar("Engine Data")
    window._session_id = "session-1"
    window._session_category_confirmed = True
    window._stream_active = False
    window._ai_sse_running = False
    window._auto_ai_start_scheduled = False
    window._root = _FakeRoot()
    window._set_session_hint = lambda _message: None
    window._append_agent_message = lambda _role, _message: None
    return window


def _make_start_window() -> DiagnosticsWindow:
    window = DiagnosticsWindow.__new__(DiagnosticsWindow)
    window._session_id = "session-1"
    window._selected_module = _FakeVar("ECM")
    window._selected_data_category = _FakeVar("Engine Data")
    window._start_button = _FakeWidget()
    window._read_dtc_button = _FakeWidget()
    window._select_module_button = _FakeWidget()
    window._select_data_category_button = _FakeWidget()
    window._start_stream_button = _FakeWidget()
    window._stop_stream_button = _FakeWidget()
    window._ai_diagnose_button = _FakeWidget()
    window._module_combo = _FakeWidget()
    window._data_combo = _FakeWidget()
    window._session_status_var = _FakeVar("")
    window._session_category_confirmed = True
    window._auto_ai_start_scheduled = False
    window._vin = ""
    window._is_destroying = False
    window._navigate_session_id = None
    window._set_status_text = lambda message: setattr(window, "_last_status_text", message)
    window._set_server_connected = lambda connected: setattr(window, "_server_connected", connected)
    window._refresh_action_buttons = lambda: setattr(window, "_refreshed", True)
    window._set_session_hint = lambda message: setattr(window, "_last_hint", message)
    window._set_agent_prompt = lambda *args, **kwargs: setattr(window, "_prompt_cleared", True)
    window._append_agent_message = lambda role, message: setattr(
        window, "_last_agent_message", (role, message)
    )
    return window


def test_can_auto_start_ai_when_main_path_ready() -> None:
    window = _make_window()

    assert window._can_auto_start_ai() is True


def test_schedule_auto_ai_start_avoids_duplicate_pending_schedule() -> None:
    window = _make_window()
    calls: list[str] = []
    window._on_ai_diagnose_clicked = lambda: calls.append("started")
    window._root.auto_run = False

    window._schedule_auto_ai_start("Auto start AI")
    window._schedule_auto_ai_start("Auto start AI again")

    assert calls == []
    assert window._auto_ai_start_scheduled is True
    assert len(window._root.after_calls) == 1


def test_can_auto_start_ai_false_while_ai_already_running() -> None:
    window = _make_window()
    window._ai_sse_running = True

    assert window._can_auto_start_ai() is False


def test_on_start_clicked_checks_session_gate_before_navigation() -> None:
    window = _make_start_window()
    calls: list[tuple[str, str, dict, str]] = []
    window._api_call = lambda method, path, json_data=None, callback_event="": calls.append(
        (method, path, json_data or {}, callback_event)
    )

    window._on_start_clicked()

    assert calls == [
        (
            "POST",
            "/api/session/start_diagnostics",
            {"session_id": "session-1"},
            "session_start_exec_result",
        )
    ]


def test_session_start_exec_result_success_continues_navigation() -> None:
    window = _make_start_window()
    calls: list[tuple[str, str, dict, str]] = []
    window._api_call = lambda method, path, json_data=None, callback_event="": calls.append(
        (method, path, json_data or {}, callback_event)
    )

    window._handle_session_start_exec_result(
        {
            "success": True,
            "session_id": "session-1",
            "network_quality": {"grade": "good"},
            "result": {"modules": ["ECM"]},
        }
    )

    assert calls == [
        (
            "POST",
            "/api/navigate/start",
            {"goal": "Navigate to Data Display"},
            "navigate_start_result",
        )
    ]


def test_session_start_exec_result_decision_required_prompts_without_navigation() -> None:
    window = _make_start_window()
    calls: list[tuple[str, str, dict, str]] = []
    prompted: list[dict[str, object]] = []
    window._api_call = lambda method, path, json_data=None, callback_event="": calls.append(
        (method, path, json_data or {}, callback_event)
    )
    window._prompt_decision = lambda decision: prompted.append(decision) or True
    window._show_decision_modal = lambda decision: prompted.append(decision)

    window._handle_session_start_exec_result(
        {
            "success": True,
            "decision_required": True,
            "status": "awaiting_decision",
            "decision": {"decision_id": "d1", "options": [{"option_id": "continue_anyway"}]},
            "network_quality": {"grade": "block"},
        }
    )

    assert calls == []
    assert prompted == [{"decision_id": "d1", "options": [{"option_id": "continue_anyway"}]}]


def test_session_decision_submit_result_resumed_start_diagnostics_continues_navigation() -> None:
    window = _make_start_window()
    calls: list[tuple[str, str, dict, str]] = []
    window._api_call = lambda method, path, json_data=None, callback_event="": calls.append(
        (method, path, json_data or {}, callback_event)
    )

    window._handle_session_decision_submit_result(
        {
            "success": True,
            "status": "running",
            "resumed": True,
            "resume_action": "start_diagnostics",
            "network_quality": {"grade": "block"},
            "result": {"modules": ["ECM"]},
        }
    )

    assert calls == [
        (
            "POST",
            "/api/navigate/start",
            {"goal": "Navigate to Data Display"},
            "navigate_start_result",
        )
    ]


def test_session_decision_submit_result_cancelled_does_not_continue_navigation() -> None:
    window = _make_start_window()
    calls: list[tuple[str, str, dict, str]] = []
    window._api_call = lambda method, path, json_data=None, callback_event="": calls.append(
        (method, path, json_data or {}, callback_event)
    )

    window._handle_session_decision_submit_result(
        {
            "success": True,
            "status": "running",
            "cancelled": True,
            "network_quality": {"grade": "block"},
        }
    )

    assert calls == []
