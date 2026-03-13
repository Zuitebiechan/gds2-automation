from types import SimpleNamespace

from vci_proxy.diagnostics_window import DiagnosticsWindow


class _FakeVar:
    def __init__(self, value: str):
        self._value = value

    def get(self) -> str:
        return self._value


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
