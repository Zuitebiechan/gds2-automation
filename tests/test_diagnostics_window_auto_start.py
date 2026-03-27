from types import SimpleNamespace

from vci_proxy.diagnostics_window import DiagnosticsWindow
import vci_proxy.diagnostics_window as diagnostics_window_module


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
        self.visible = True

    def configure(self, **kwargs):
        if "state" in kwargs:
            self.state = kwargs["state"]
        if "values" in kwargs:
            self.values = kwargs["values"]

    def grid_remove(self):
        self.visible = False

    def grid(self):
        self.visible = True


class _FakeRoot:
    def __init__(self):
        self.after_calls: list[tuple[int, object]] = []
        self.auto_run = True

    def after(self, delay_ms: int, callback):
        self.after_calls.append((delay_ms, callback))
        if self.auto_run:
            callback()


class _FakeThread:
    def __init__(self, target=None, daemon=None, name=None):
        self._target = target
        self.daemon = daemon
        self.name = name

    def start(self):
        if self._target is not None:
            self._target()

    def is_alive(self):
        return False

    def join(self, timeout=None):
        return None


class _FakeResponse:
    def __init__(self, url: str, lines=None):
        self.url = url
        self._lines = lines or []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        return None

    def iter_lines(self, decode_unicode=True):
        return iter(self._lines)

    def close(self):
        return None


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
    window._api_base = "http://127.0.0.1:8080"
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
    window._ai_retry_button = _FakeWidget()
    window._module_combo = _FakeWidget()
    window._data_combo = _FakeWidget()
    window._session_status_var = _FakeVar("")
    window._ai_status_text = _FakeVar("")
    window._session_category_confirmed = True
    window._auto_ai_start_scheduled = False
    window._cached_payload_id = ""
    window._vin = ""
    window._is_destroying = False
    window._navigate_session_id = None
    window._sse_running = False
    window._sse_thread = None
    window._sse_response = None
    window._ai_sse_running = False
    window._ai_sse_thread = None
    window._ai_sse_response = None
    window._navigate_sse_running = False
    window._navigate_sse_thread = None
    window._navigate_sse_response = None
    window._queue = SimpleNamespace(put=lambda *_args, **_kwargs: None)
    window._set_status_text = lambda message: setattr(window, "_last_status_text", message)
    window._set_server_connected = lambda connected: setattr(window, "_server_connected", connected)
    window._refresh_action_buttons = lambda: setattr(window, "_refreshed", True)
    window._set_session_hint = lambda message: setattr(window, "_last_hint", message)
    window._set_agent_prompt = lambda *args, **kwargs: setattr(window, "_prompt_cleared", True)
    window._set_ai_result_text = lambda message: setattr(window, "_last_ai_result_text", message)
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
            "/api/session/navigate/start",
            {"session_id": "session-1", "goal": "Navigate to Data Display"},
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
            "/api/session/navigate/start",
            {"session_id": "session-1", "goal": "Navigate to Data Display"},
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


def test_on_ai_diagnose_clicked_uses_session_facade_when_session_active() -> None:
    window = _make_start_window()
    calls: list[tuple[str, str, dict, str]] = []
    window._api_call = lambda method, path, json_data=None, callback_event="": calls.append(
        (method, path, json_data or {}, callback_event)
    )

    window._on_ai_diagnose_clicked()

    assert calls == [
        (
            "POST",
            "/api/session/ai_diagnose",
            {
                "session_id": "session-1",
                "module": "ECM",
                "data_category": "Engine Data",
                "vin": "",
            },
            "ai_start_result",
        )
    ]


def test_on_ai_retry_clicked_uses_session_facade_when_session_active() -> None:
    window = _make_start_window()
    window._cached_payload_id = "payload-1"
    calls: list[tuple[str, str, dict, str]] = []
    window._api_call = lambda method, path, json_data=None, callback_event="": calls.append(
        (method, path, json_data or {}, callback_event)
    )

    window._on_ai_retry_clicked()

    assert calls == [
        (
            "POST",
            "/api/session/ai_diagnose/retry",
            {
                "session_id": "session-1",
                "cached_payload_id": "payload-1",
                "vin": "",
                "module": "ECM",
                "data_category": "Engine Data",
            },
            "ai_start_result",
        )
    ]


def test_on_read_dtcs_clicked_uses_session_dtcs_facade() -> None:
    window = _make_start_window()
    calls: list[tuple[str, str, dict, str]] = []
    window._api_call = lambda method, path, json_data=None, callback_event="": calls.append(
        (method, path, json_data or {}, callback_event)
    )

    window._on_read_dtcs_clicked()

    assert calls == [
        (
            "POST",
            "/api/session/dtcs",
            {
                "session_id": "session-1",
                "module": "ECM",
                "data_category": "Engine Data",
            },
            "dtcs_result",
        )
    ]


def test_on_start_stream_clicked_uses_session_live_data_facade() -> None:
    window = _make_start_window()
    calls: list[tuple[str, str, dict, str]] = []
    window._api_call = lambda method, path, json_data=None, callback_event="": calls.append(
        (method, path, json_data or {}, callback_event)
    )

    window._on_start_stream_clicked()

    assert calls == [
        (
            "POST",
            "/api/session/live_data/start",
            {
                "session_id": "session-1",
                "module": "ECM",
                "data_category": "Engine Data",
            },
            "live_start_result",
        )
    ]


def test_on_stop_stream_clicked_uses_session_live_data_facade() -> None:
    window = _make_start_window()
    calls: list[tuple[str, str, dict, str]] = []
    window._api_call = lambda method, path, json_data=None, callback_event="": calls.append(
        (method, path, json_data or {}, callback_event)
    )

    window._on_stop_stream_clicked()

    assert calls == [
        (
            "POST",
            "/api/session/live_data/stop",
            {"session_id": "session-1"},
            "live_stop_result",
        )
    ]


def test_navigate_submit_decision_uses_session_facade() -> None:
    window = _make_start_window()
    window._navigate_session_id = "session-1"
    calls: list[tuple[str, str, dict, str]] = []
    window._api_call = lambda method, path, json_data=None, callback_event="": calls.append(
        (method, path, json_data or {}, callback_event)
    )

    window._navigate_submit_decision("decision-1", "Engine Data")

    assert calls == [
        (
            "POST",
            "/api/session/navigate/decision",
            {
                "session_id": "session-1",
                "decision_id": "decision-1",
                "selected_item": "Engine Data",
            },
            "navigate_decision_submit_result",
        )
    ]


def test_start_sse_thread_uses_session_live_data_events_when_session_active(monkeypatch) -> None:
    window = _make_start_window()
    captured: list[str] = []

    monkeypatch.setattr(diagnostics_window_module.threading, "Thread", _FakeThread)
    monkeypatch.setattr(
        diagnostics_window_module.requests,
        "get",
        lambda url, stream=True, timeout=None: captured.append(url) or _FakeResponse(url),
    )

    window._start_sse_thread()

    assert captured == ["http://127.0.0.1:8080/api/session/live_data/events?session_id=session-1"]


def test_start_navigate_sse_thread_uses_session_facade(monkeypatch) -> None:
    window = _make_start_window()
    captured: list[str] = []

    monkeypatch.setattr(diagnostics_window_module.threading, "Thread", _FakeThread)
    monkeypatch.setattr(
        diagnostics_window_module.requests,
        "get",
        lambda url, stream=True, timeout=None: captured.append(url) or _FakeResponse(url),
    )

    window._start_navigate_sse_thread("session-1")

    assert captured == ["http://127.0.0.1:8080/api/session/navigate/events?session_id=session-1"]
