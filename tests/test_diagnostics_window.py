from __future__ import annotations

import threading
import types
import tkinter as tk
from tkinter import messagebox

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
        self.visible = True

    def configure(self, **kwargs) -> None:
        if "state" in kwargs:
            self.state = kwargs["state"]
        if "values" in kwargs:
            self.values = kwargs["values"]

    def grid(self, *args, **kwargs) -> None:
        self.visible = True

    def grid_remove(self) -> None:
        self.visible = False


class _Tree:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str]] = []
        self.headings: dict[str, str] = {}

    def get_children(self, _item: str = "") -> list[int]:
        return list(range(len(self.rows)))

    def delete(self, item_id: int) -> None:
        if 0 <= item_id < len(self.rows):
            self.rows[item_id] = ("", "", "", "")

    def insert(self, _parent: str, _index: str, values: tuple[str, str, str, str]) -> None:
        self.rows.append(values)

    def heading(self, key: str, text: str) -> None:
        self.headings[key] = text


class _Text:
    def __init__(self) -> None:
        self.state = None
        self.content = ""

    def configure(self, **kwargs) -> None:
        if "state" in kwargs:
            self.state = kwargs["state"]

    def delete(self, _start: str, _end: str) -> None:
        self.content = ""

    def insert(self, _index: str, text: str) -> None:
        self.content += text

    def see(self, _index: str) -> None:
        return None


def _build_window(*, current_page: str = "") -> DiagnosticsWindow:
    window = DiagnosticsWindow.__new__(DiagnosticsWindow)
    window._selected_module = _Var("ECM")
    window._selected_data_category = _Var("Diagnostic Data Display")
    window._session_id = "session-123"
    window._active_branch = ""
    window._action_output_mode = "dtc"
    window._stream_active = False
    window._ai_sse_running = False
    window._ai_start_pending = False
    window._auto_ai_start_scheduled = False
    window._session_live_data_active = False
    window._session_ai_active = False
    window._session_navigation_active = False
    window._session_category_confirmed = True
    window._current_page = current_page
    window._vehicle_dtc_ready = False
    window._vehicle_dtc_status_message = ""
    window._session_abort_finalizing = False
    window._session_terminal_session_id = None
    window._session_status_refresh_inflight = False
    window._server_connected = None
    window._is_destroying = False
    window._status_message = _Var("")
    window._session_status_var = _Var("")
    window._session_hint_var = _Var("")
    window._flow_step_title = _Var("")
    window._flow_step_hint = _Var("")
    window._dtc_count_text = _Var("")
    window._dtc_tree = _Tree()
    window._module_label = _Widget()
    window._data_label = _Widget()
    window._select_module_button = _Widget()
    window._select_data_category_button = _Widget()
    window._session_start_button = _Widget()
    window._session_abort_button = _Widget()
    window._start_button = _Widget()
    window._vehicle_diagnostics_button = _Widget()
    window._read_dtc_button = _Widget()
    window._clear_dtc_button = _Widget()
    window._start_stream_button = _Widget()
    window._ai_diagnose_button = _Widget()
    window._module_combo = _Widget()
    window._data_combo = _Widget()
    window._workflow_goal = "none"
    window._last_workflow_intent = ""
    window._active_assignment = None
    window._navigate_session_id = "nav-123"
    window._agent_messages = []
    window._append_agent_message = lambda role, message: window._agent_messages.append((role, message))
    window._set_status_text = lambda message: window._status_message.set(message)
    window._set_server_connected = lambda connected: setattr(window, "_server_connected", connected)
    window._stop_sse_thread = lambda: None
    window._stop_ai_sse_thread = lambda: None
    window._stop_session_sse_thread = lambda: None
    window._stop_navigate_sse_thread = lambda: None
    window._close_decision_modal = lambda: None
    window._set_agent_prompt = lambda kind, label, options, **kwargs: None
    return window


def test_request_close_destroys_immediately_when_no_active_session(monkeypatch) -> None:
    window = _build_window(current_page="data_display")
    destroyed: list[bool] = []
    window._root = object()
    window.destroy = lambda: destroyed.append(True)
    window._guard_controller = types.SimpleNamespace(has_active_session=lambda: False)

    window.request_close()

    assert destroyed == [True]


def test_request_close_active_session_routes_through_guard_controller(monkeypatch) -> None:
    window = _build_window(current_page="data_display")
    observed: dict[str, object] = {}
    window._root = object()

    class _FakeController:
        def has_active_session(self) -> bool:
            return True

        def request_guarded_action(self, action_name, **kwargs):
            observed["action_name"] = action_name
            observed["kwargs"] = kwargs
            return True

    window._guard_controller = _FakeController()
    monkeypatch.setattr(messagebox, "askyesno", lambda *args, **kwargs: True)

    window.request_close()

    assert observed["action_name"] == "close_diagnostics"


def test_guard_controller_headless_quit_suppresses_assignment_reconnect(monkeypatch) -> None:
    observed = {
        "assignment_events": [],
        "suppressed": [],
        "calls": [],
        "safe": [],
    }

    class _ImmediateThread:
        def __init__(self, target=None, daemon=None, name=None):
            self.target = target
            self.daemon = daemon
            self.name = name

        def start(self):
            if self.target is not None:
                self.target()

    class _NoopTimer:
        def __init__(self, interval, callback, args=None, kwargs=None):
            self.interval = interval
            self.callback = callback
            self.args = args or ()
            self.kwargs = kwargs or {}
            self.daemon = False

        def start(self):
            return None

        def cancel(self):
            return None

    class _Response:
        def __init__(self, status_code: int, payload: dict[str, object], reason: str = "OK"):
            self.status_code = status_code
            self._payload = payload
            self.reason = reason

        def json(self):
            return self._payload

    def _fake_post(url, json=None, params=None, timeout=None, headers=None):
        observed["calls"].append(("POST", url, json))
        if url.endswith("/api/session/bootstrap/release"):
            return _Response(200, {"success": True, "released": True})
        return _Response(200, {"success": True, "session_id": "session-123", "status": "aborted"})

    def _fake_get(url, params=None, timeout=None, headers=None):
        observed["calls"].append(("GET", url, params))
        return _Response(404, {"success": False, "error": "Session session-123 not found"}, reason="Not Found")

    monkeypatch.setattr(threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(threading, "Timer", _NoopTimer)
    monkeypatch.setattr("vci_proxy.diagnostics_window.requests.post", _fake_post)
    monkeypatch.setattr("vci_proxy.diagnostics_window.requests.get", _fake_get)

    from vci_proxy.diagnostics_window import DiagnosticsGuardController

    controller = DiagnosticsGuardController(
        "https://node-1.diag.example.com",
        api_token="api-secret",
        node_assignment_callback=lambda assignment: observed["assignment_events"].append(assignment),
        ui_dispatch=lambda callback: callback(),
        suppress_assignment_reconnect=lambda: observed["suppressed"].append(True),
    )
    controller.set_bootstrap_api_base("https://entry.diag.example.com")
    controller.set_active_assignment(
        {
            "assignment_id": "assign-123",
            "api_base_url": "https://node-1.diag.example.com",
            "tunnel_host": "node-1.diag.example.com",
        },
        bootstrap_api_base="https://entry.diag.example.com",
        api_base_url="https://node-1.diag.example.com",
    )
    controller.set_session_active("session-123")

    controller.request_guarded_action(
        "quit_app",
        on_safe=lambda: observed["safe"].append("safe"),
        on_force=lambda: observed["safe"].append("force"),
        on_failure=lambda reason: observed["safe"].append(f"failure:{reason}"),
    )

    assert observed["safe"] == ["safe"]
    assert observed["suppressed"] == [True]
    assert observed["assignment_events"] == [None]
    assert any(call[1].endswith("/api/session/abort") for call in observed["calls"])
    assert any(call[1].endswith("/api/session/bootstrap/release") for call in observed["calls"])


def test_guard_controller_coalesces_duplicate_guarded_actions() -> None:
    observed: list[str] = []
    from vci_proxy.diagnostics_window import DiagnosticsGuardController

    controller = DiagnosticsGuardController(
        "https://node-1.diag.example.com",
        ui_dispatch=lambda callback: callback(),
    )
    controller.attach_window(
        types.SimpleNamespace(
            _run_sync_ui_callback=lambda callback: callback(),
            _start_guarded_shutdown_from_controller=lambda action_name: observed.append(action_name),
        )
    )
    controller.set_session_active("session-123")
    controller._pending_action = {"name": "quit_app", "on_safe": lambda: None}

    result = controller.request_guarded_action(
        "quit_app",
        on_safe=lambda: observed.append("safe"),
    )

    assert result is True
    assert observed == []


def test_guard_controller_rejects_different_inflight_guarded_action() -> None:
    observed: list[str] = []
    from vci_proxy.diagnostics_window import DiagnosticsGuardController

    controller = DiagnosticsGuardController(
        "https://node-1.diag.example.com",
        ui_dispatch=lambda callback: callback(),
    )
    controller._pending_action = {"name": "close_diagnostics", "on_safe": lambda: None}

    result = controller.request_guarded_action(
        "quit_app",
        on_safe=lambda: observed.append("safe"),
        on_failure=lambda reason: observed.append(reason),
    )

    assert result is False
    assert observed == [
        "Another shutdown action is already in progress. Please wait for it to finish."
    ]


def test_offer_force_guarded_action_shows_warning_for_cross_action_conflict(monkeypatch) -> None:
    window = _build_window(current_page="data_display")
    observed: dict[str, object] = {}
    window._root = object()
    window._guard_controller = types.SimpleNamespace(
        force_pending_action=lambda: observed.setdefault("forced", True),
        cancel_pending_action=lambda: observed.setdefault("cancelled", True),
    )
    monkeypatch.setattr(
        messagebox,
        "showwarning",
        lambda title, message, parent=None: observed.setdefault("warning", (title, message, parent)),
    )
    monkeypatch.setattr(
        messagebox,
        "askyesno",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("force confirmation should not be shown for a cross-action conflict")
        ),
    )

    window._offer_force_guarded_action(
        "close_diagnostics",
        "Another shutdown action is already in progress. Please wait for it to finish.",
    )

    assert observed["warning"] == (
        "Action In Progress",
        "Another shutdown action is already in progress. Please wait for it to finish.",
        window._root,
    )
    assert "forced" not in observed
    assert "cancelled" not in observed


def test_refresh_action_buttons_shows_step_one_before_branch_selection() -> None:
    window = _build_window(current_page="module_list")

    window._refresh_action_buttons()

    assert window._start_button.state == tk.NORMAL
    assert window._vehicle_diagnostics_button.state == tk.NORMAL
    assert window._module_combo.visible is False
    assert window._data_combo.visible is False
    assert window._read_dtc_button.visible is False
    assert window._clear_dtc_button.visible is False


def test_refresh_action_buttons_enables_module_actions_only_when_module_branch_ready() -> None:
    window = _build_window(current_page="data_display")
    window._active_branch = "module"
    window._selected_module.set("ECM")
    window._selected_data_category.set("Engine Data")
    window._session_category_confirmed = True

    window._refresh_action_buttons()

    assert window._module_combo.visible is True
    assert window._data_combo.visible is True
    assert window._ai_diagnose_button.visible is True
    assert window._ai_diagnose_button.state == tk.NORMAL
    assert window._read_dtc_button.visible is True
    assert window._read_dtc_button.state == tk.NORMAL
    assert window._clear_dtc_button.visible is True
    assert window._clear_dtc_button.state == tk.NORMAL


def test_refresh_action_buttons_enables_vehicle_actions_only_on_vehicle_page() -> None:
    window = _build_window(current_page="data_display")
    window._active_branch = "vehicle"
    window._selected_data_category.set("Vehicle DTC Information")
    window._vehicle_dtc_ready = True

    window._refresh_action_buttons()

    assert window._module_combo.visible is False
    assert window._data_combo.visible is False
    assert window._ai_diagnose_button.visible is False
    assert window._read_dtc_button.visible is True
    assert window._read_dtc_button.state == tk.NORMAL
    assert window._clear_dtc_button.visible is True
    assert window._clear_dtc_button.state == tk.NORMAL


def test_refresh_action_buttons_disables_vehicle_actions_until_vehicle_dtc_ready() -> None:
    window = _build_window(current_page="data_display")
    window._active_branch = "vehicle"
    window._selected_data_category.set("Vehicle DTC Information")
    window._vehicle_dtc_ready = False
    window._vehicle_dtc_status_message = "Vehicle DTC Information is still loading."

    window._refresh_action_buttons()

    assert window._read_dtc_button.visible is True
    assert window._read_dtc_button.state == tk.DISABLED
    assert window._clear_dtc_button.visible is True
    assert window._clear_dtc_button.state == tk.DISABLED
    assert window._flow_step_hint.get() == "Vehicle DTC Information is still loading."


def test_handle_session_status_result_updates_current_page_and_button_state() -> None:
    window = _build_window(current_page="")
    window._active_branch = "module"
    window._selected_module.set("ECM")
    window._selected_data_category.set("Engine Data")
    window._session_category_confirmed = True

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


def test_handle_session_abort_result_enters_finalization_and_requests_status_refresh() -> None:
    window = _build_window(current_page="data_display")
    refresh_calls: list[str] = []
    window._request_session_status_refresh = lambda: refresh_calls.append("refresh")

    window._handle_session_abort_result(
        {
            "success": True,
            "session_id": "session-123",
            "status": "aborted",
        }
    )

    assert window._session_abort_finalizing is True
    assert window._session_terminal_session_id == "session-123"
    assert window._session_start_button.state == tk.DISABLED
    assert window._session_abort_button.state == tk.DISABLED
    assert window._start_button.state == tk.DISABLED
    assert window._session_status_var.get() == "Abort accepted. Finalizing session cleanup..."
    assert "Start Session stays disabled" in window._session_hint_var.get()
    assert refresh_calls == ["refresh"]


def test_on_session_abort_clicked_disables_restart_and_sets_waiting_hint() -> None:
    window = _build_window(current_page="data_display")
    calls: list[tuple[str, str, dict[str, object]]] = []
    window._api_call = lambda method, endpoint, **kwargs: calls.append((method, endpoint, kwargs))

    window._on_session_abort_clicked()

    assert window._session_start_button.state == tk.DISABLED
    assert window._session_abort_button.state == tk.DISABLED
    assert window._start_button.state == tk.DISABLED
    assert window._vehicle_diagnostics_button.state == tk.DISABLED
    assert window._session_status_var.get() == "Aborting..."
    assert window._session_hint_var.get() == (
        "Abort requested. Start Session will re-enable after session cleanup completes."
    )
    assert calls == [
        (
            "POST",
            "/api/session/abort",
            {
                "json_data": {"session_id": "session-123"},
                "callback_event": "session_abort_result",
            },
        )
    ]


def test_handle_session_status_result_finalizes_aborted_session_without_session_done_event() -> None:
    window = _build_window(current_page="data_display")
    window._active_branch = "module"
    window._session_abort_finalizing = True
    window._session_terminal_session_id = "session-123"

    window._handle_session_status_result(
        {
            "success": True,
            "session_id": "session-123",
            "status": "aborted",
            "reason": "user_cancelled",
        }
    )

    assert window._session_abort_finalizing is False
    assert window._session_terminal_session_id is None
    assert window._session_id is None
    assert window._session_start_button.state == tk.NORMAL
    assert window._session_abort_button.state == tk.DISABLED
    assert window._start_button.state == tk.DISABLED
    assert window._session_status_var.get() == "Session aborted. user_cancelled"
    assert window._session_hint_var.get() == "Abort completed. You can start a new session."


def test_handle_session_status_result_resets_stale_missing_session() -> None:
    window = _build_window(current_page="data_display")

    window._handle_session_status_result(
        {
            "success": False,
            "error": "'Session session-123 not found'",
        }
    )

    assert window._session_id is None
    assert window._selected_module.get() == ""
    assert window._selected_data_category.get() == ""
    assert window._session_start_button.state == tk.NORMAL
    assert window._session_abort_button.state == tk.DISABLED
    assert window._status_message.get() == "Session refresh failed: session expired on the server. Please start a new session."


def test_handle_dtcs_result_resets_stale_missing_session() -> None:
    window = _build_window(current_page="data_display")

    window._handle_dtcs_result(
        {
            "success": False,
            "error": "'Session session-123 not found'",
        }
    )

    assert window._session_id is None
    assert window._selected_module.get() == ""
    assert window._selected_data_category.get() == ""
    assert window._session_start_button.state == tk.NORMAL
    assert window._session_abort_button.state == tk.DISABLED
    assert window._status_message.get() == "Read DTCs failed: session expired on the server. Please start a new session."


def test_handle_dtcs_result_uses_vehicle_summary_mode_labels_and_count() -> None:
    window = _build_window(current_page="data_display")

    window._handle_dtcs_result(
        {
            "success": True,
            "result": {
                "dtcs": [
                    {
                        "code": "30",
                        "module": "Engine Control Module",
                        "status": "DTCs Stored",
                        "description": "DLC Pin: 6,14",
                    }
                ],
                "dtc_count": 30,
                "dtc_display_mode": "vehicle_summary",
                "page_context": "data_display",
            },
        }
    )

    assert window._dtc_tree.headings["code"] == "DTC Count"
    assert window._dtc_tree.headings["module"] == "Control Module"
    assert window._dtc_tree.headings["status"] == "Module Status"
    assert window._dtc_tree.headings["description"] == "DLC Pin"
    assert window._dtc_count_text.get() == "Vehicle summary: 30 DTC(s) across 1 module row(s)"


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


def test_on_clear_dtcs_clicked_uses_session_only_payload_for_vehicle_branch() -> None:
    window = _build_window(current_page="data_display")
    window._active_branch = "vehicle"
    window._selected_module.set("")
    window._selected_data_category.set("Vehicle DTC Information")
    window._vehicle_dtc_ready = True
    calls: list[tuple[str, str, dict[str, object]]] = []
    window._api_call = lambda method, endpoint, **kwargs: calls.append((method, endpoint, kwargs))

    window._on_clear_dtcs_clicked()

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


def test_on_read_dtcs_clicked_uses_session_only_payload_for_vehicle_branch() -> None:
    window = _build_window(current_page="data_display")
    window._active_branch = "vehicle"
    window._selected_module.set("")
    window._selected_data_category.set("Vehicle DTC Information")
    window._vehicle_dtc_ready = True
    calls: list[tuple[str, str, dict[str, object]]] = []
    window._api_call = lambda method, endpoint, **kwargs: calls.append((method, endpoint, kwargs))

    window._on_read_dtcs_clicked()

    assert calls == [
        (
            "POST",
            "/api/session/dtcs",
            {
                "json_data": {
                    "session_id": "session-123",
                },
                "callback_event": "dtcs_result",
            },
        )
    ]


def test_on_read_dtcs_clicked_blocks_vehicle_branch_while_dtc_table_loading() -> None:
    window = _build_window(current_page="data_display")
    window._active_branch = "vehicle"
    window._selected_module.set("")
    window._selected_data_category.set("Vehicle DTC Information")
    window._vehicle_dtc_ready = False
    window._vehicle_dtc_status_message = "Vehicle DTC Information is still loading."
    calls: list[tuple[str, str, dict[str, object]]] = []
    warnings: list[tuple[str, str]] = []
    window._api_call = lambda method, endpoint, **kwargs: calls.append((method, endpoint, kwargs))

    original = messagebox.showwarning
    messagebox.showwarning = lambda title, message: warnings.append((title, message))
    try:
        window._on_read_dtcs_clicked()
    finally:
        messagebox.showwarning = original

    assert calls == []
    assert warnings == [("Vehicle DTC Loading", "Vehicle DTC Information is still loading.")]


def test_refresh_action_buttons_disables_clear_dtcs_while_ai_pending() -> None:
    window = _build_window(current_page="data_display")
    window._active_branch = "module"
    window._selected_module.set("ECM")
    window._selected_data_category.set("Engine Data")
    window._session_category_confirmed = True
    window._ai_start_pending = True

    window._refresh_action_buttons()

    assert window._clear_dtc_button.state == tk.DISABLED


def test_schedule_auto_ai_start_only_surfaces_guidance() -> None:
    window = _build_window(current_page="data_display")
    window._on_ai_diagnose_clicked = lambda: (_ for _ in ()).throw(AssertionError("must not auto-run"))

    window._schedule_auto_ai_start("AI Diagnosis is ready when you choose it.")

    assert window._session_hint_var.get() == "AI Diagnosis is ready when you choose it."
    assert window._agent_messages[-1] == ("agent", "AI Diagnosis is ready when you choose it.")


def test_on_start_clicked_routes_through_module_guided_intent() -> None:
    window = _build_window(current_page="module_list")
    observed: list[str] = []
    window._start_module_diagnostics_flow = lambda: observed.append("module")

    window._on_start_clicked()

    assert observed == ["module"]
    assert window._workflow_goal == "module_guided"
    assert window._last_workflow_intent == "start_module_guided"


def test_on_vehicle_diagnostics_clicked_routes_through_vehicle_guided_intent() -> None:
    window = _build_window(current_page="module_list")
    calls: list[tuple[str, str, str]] = []
    window._start_workflow_navigation = lambda goal, *, status_text, hint, message: calls.append(
        (goal, status_text, hint)
    )

    window._on_vehicle_diagnostics_clicked()

    assert window._workflow_goal == "vehicle_guided"
    assert window._last_workflow_intent == "start_vehicle_guided"
    assert calls == [
        (
            "Vehicle Diagnostics",
            "Starting vehicle diagnostics navigation...",
            "Navigating to the Vehicle Diagnostics branch...",
        )
    ]


def test_handle_session_status_result_disables_clear_dtcs_while_session_ai_active() -> None:
    window = _build_window(current_page="")
    window._active_branch = "module"
    window._selected_module.set("ECM")
    window._selected_data_category.set("Engine Data")
    window._session_category_confirmed = True

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


def test_handle_session_status_result_enables_vehicle_actions_when_vehicle_dtc_ready() -> None:
    window = _build_window(current_page="")
    window._active_branch = "vehicle"
    window._selected_data_category.set("Vehicle DTC Information")

    window._handle_session_status_result(
        {
            "success": True,
            "active_ai_session_id": "",
            "active_navigation_session_id": "",
            "live_data_active": False,
            "backend_state_summary": {
                "current_page": "data_display",
                "vehicle_dtc_status": {
                    "applicable": True,
                    "ready": True,
                    "message": "Vehicle DTC Information is ready.",
                },
            },
        }
    )

    assert window._current_page == "data_display"
    assert window._read_dtc_button.state == tk.NORMAL
    assert window._clear_dtc_button.state == tk.NORMAL


def test_handle_session_status_result_keeps_vehicle_actions_disabled_while_dtc_loading() -> None:
    window = _build_window(current_page="")
    window._active_branch = "vehicle"
    window._selected_data_category.set("Vehicle DTC Information")

    window._handle_session_status_result(
        {
            "success": True,
            "active_ai_session_id": "",
            "active_navigation_session_id": "",
            "live_data_active": False,
            "backend_state_summary": {
                "current_page": "data_display",
                "vehicle_dtc_status": {
                    "applicable": True,
                    "ready": False,
                    "message": "Vehicle DTC Information is still loading.",
                },
            },
        }
    )

    assert window._current_page == "data_display"
    assert window._read_dtc_button.state == tk.DISABLED
    assert window._clear_dtc_button.state == tk.DISABLED
    assert window._flow_step_hint.get() == "Vehicle DTC Information is still loading."


def test_handle_navigate_decision_required_auto_submits_vehicle_dtc_information() -> None:
    window = _build_window(current_page="data_list")
    window._active_branch = "vehicle"
    submitted: list[tuple[str, str]] = []
    window._navigate_submit_decision = lambda decision_id, selected_item: submitted.append(
        (decision_id, selected_item)
    )

    window._handle_navigate_decision_required(
        {
            "decision_id": "decision-vehicle",
            "page": "vehicle_diagnostics_menu",
            "items": ["Vehicle DTC Information", "Vehicle DTC and ID Information"],
        }
    )

    assert submitted == [("decision-vehicle", "Vehicle DTC Information")]


def test_handle_navigate_decision_required_uses_modal_for_manual_choices() -> None:
    window = _build_window(current_page="module_list")
    window._active_branch = "module"
    shown: list[dict[str, object]] = []
    window._show_navigation_decision_modal = lambda payload: shown.append(payload)

    window._handle_navigate_decision_required(
        {
            "decision_id": "decision-module",
            "page": "module_list",
            "items": ["ECM", "TCM"],
            "prompt": "Choose a module",
        }
    )

    assert shown == [
        {
            "decision_id": "decision-module",
            "page": "module_list",
            "items": ["ECM", "TCM"],
            "prompt": "Choose a module",
        }
    ]


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


def test_handle_session_start_result_does_not_recover_aborted_active_session() -> None:
    window = DiagnosticsWindow.__new__(DiagnosticsWindow)
    messages: list[tuple[str, str]] = []
    refresh_calls: list[str] = []
    window._session_id = None
    window._session_abort_finalizing = False
    window._session_terminal_session_id = None
    window._session_start_button = _Widget()
    window._session_abort_button = _Widget()
    window._start_button = _Widget()
    window._vehicle_diagnostics_button = _Widget()
    window._select_module_button = _Widget()
    window._select_data_category_button = _Widget()
    window._read_dtc_button = _Widget()
    window._clear_dtc_button = _Widget()
    window._start_stream_button = _Widget()
    window._ai_diagnose_button = _Widget()
    window._session_status_var = _Var("")
    window._session_hint_var = _Var("")
    window._append_agent_message = lambda role, message: messages.append((role, message))
    window._request_session_status_refresh = lambda: refresh_calls.append("refresh")
    window._close_decision_modal = lambda: None
    window._set_agent_prompt = lambda *args, **kwargs: None
    window._active_assignment = None
    window._recover_existing_session = DiagnosticsWindow._recover_existing_session.__get__(window, DiagnosticsWindow)
    window._handle_aborted_session_conflict = DiagnosticsWindow._handle_aborted_session_conflict.__get__(window, DiagnosticsWindow)
    window._active_session_id_from_payload = DiagnosticsWindow._active_session_id_from_payload.__get__(window, DiagnosticsWindow)
    window._is_aborted_status = DiagnosticsWindow._is_aborted_status.__get__(window, DiagnosticsWindow)
    window._begin_abort_finalization = DiagnosticsWindow._begin_abort_finalization.__get__(window, DiagnosticsWindow)
    window._retry_bootstrap_after_assignment_failure = lambda payload: False
    window._error_message = DiagnosticsWindow._error_message.__get__(window, DiagnosticsWindow)

    window._handle_session_start_result(
        {
            "success": False,
            "error": "Another session is already active (session_id=session-123, status=aborted)",
            "error_code": "active_session_exists",
            "active_session_id": "session-123",
            "active_session_status": "aborted",
            "active_backend_name": "gds2",
        }
    )

    assert window._session_abort_finalizing is True
    assert window._session_id == "session-123"
    assert window._session_terminal_session_id == "session-123"
    assert window._session_start_button.state == tk.DISABLED
    assert window._session_abort_button.state == tk.DISABLED
    assert window._start_button.state == tk.DISABLED
    assert window._session_status_var.get() == "Previous abort is still finalizing..."
    assert "Start Session stays disabled" in window._session_hint_var.get()
    assert refresh_calls == ["refresh"]
    assert not any("Recovered existing session" in message for _, message in messages)


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
    window._stop_sse_thread = lambda: None
    window._stop_ai_sse_thread = lambda: None
    window._stop_session_sse_thread = lambda: None
    window._stop_navigate_sse_thread = lambda: None
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
                "json_data": {
                    "assignment_id": "assign-123",
                    "recovery_action": "idle",
                },
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


def test_handle_session_start_result_retries_bootstrap_when_assigned_node_returns_gateway_error() -> None:
    window = DiagnosticsWindow.__new__(DiagnosticsWindow)
    release_calls: list[tuple[str, str, dict[str, object]]] = []
    api_calls: list[tuple[str, str, dict[str, object]]] = []
    assignment_events: list[object] = []
    messages: list[tuple[str, str]] = []
    window._session_brand = _Var("gds2")
    window._session_start_button = _Widget()
    window._session_abort_button = _Widget()
    window._start_button = _Widget()
    window._session_status_var = _Var("")
    window._session_hint_var = _Var("")
    window._server_state_text = _Var("Server: node-1.diag.example.com")
    window._api_base = "https://node-1.diag.example.com"
    window._server_display = "node-1.diag.example.com"
    window._bootstrap_api_base = "https://entry.diag.example.com"
    window._active_assignment = {
        "assignment_id": "assign-123",
        "api_base_url": "https://node-1.diag.example.com",
        "tunnel_host": "node-1.diag.example.com",
    }
    window._append_agent_message = lambda role, message: messages.append((role, message))
    window._node_assignment_callback = lambda assignment: assignment_events.append(assignment)
    window._api_call = lambda method, endpoint, **kwargs: api_calls.append((method, endpoint, kwargs))
    window._api_call_to_base = lambda base_url, method, endpoint, **kwargs: release_calls.append(
        (base_url, endpoint, kwargs)
    )
    window._build_session_start_payload = lambda brand: {
        "brand": brand,
        "client_time_zone": "Asia/Shanghai",
    }
    window._set_session_hint = lambda message: window._session_hint_var.set(message)
    window._bootstrap_assignment_retry_count = 0

    window._handle_session_start_result(
        {
            "success": False,
            "error": "Server returned non-JSON response (HTTP 502 Bad Gateway).",
            "http_status": 502,
        }
    )

    assert release_calls == [
        (
            "https://entry.diag.example.com",
            "/api/session/bootstrap/release",
            {
                "json_data": {
                    "assignment_id": "assign-123",
                    "recovery_action": "reprobe",
                },
                "callback_event": "session_bootstrap_release_result",
            },
        )
    ]
    assert api_calls == [
        (
            "POST",
            "/api/session/bootstrap",
            {
                "json_data": {
                    "brand": "gds2",
                    "client_time_zone": "Asia/Shanghai",
                },
                "callback_event": "session_bootstrap_result",
            },
        )
    ]
    assert assignment_events == [None]
    assert window._api_base == "https://entry.diag.example.com"
    assert window._session_start_button.state == tk.DISABLED
    assert window._start_button.state == tk.DISABLED
    assert "Retrying session bootstrap" in window._session_status_var.get()
    assert any("Retrying session bootstrap automatically" in message for _, message in messages)


def test_handle_session_start_result_does_not_loop_after_retry_limit_is_hit() -> None:
    window = DiagnosticsWindow.__new__(DiagnosticsWindow)
    release_calls: list[tuple[str, str, dict[str, object]]] = []
    messages: list[tuple[str, str]] = []
    window._session_start_button = _Widget()
    window._session_abort_button = _Widget()
    window._start_button = _Widget()
    window._session_status_var = _Var("")
    window._session_hint_var = _Var("")
    window._active_assignment = {"assignment_id": "assign-123"}
    window._append_agent_message = lambda role, message: messages.append((role, message))
    window._set_session_hint = lambda message: window._session_hint_var.set(message)
    window._recover_existing_session = lambda payload: False
    window._api_call = lambda *args, **kwargs: release_calls.append(("retry", "", {}))
    window._api_call_to_base = lambda *args, **kwargs: release_calls.append(("release", "", {}))
    window._bootstrap_assignment_retry_count = DiagnosticsWindow.BOOTSTRAP_ASSIGNMENT_RETRY_LIMIT

    window._handle_session_start_result(
        {
            "success": False,
            "error": "Server returned non-JSON response (HTTP 502 Bad Gateway).",
            "http_status": 502,
        }
    )

    assert window._session_start_button.state == tk.NORMAL
    assert window._start_button.state == tk.DISABLED
    assert "Failed:" in window._session_status_var.get()
    assert any("Session start failed" in message for _, message in messages)


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


def test_handle_ai_done_surfaces_missing_terminal_payload_as_error() -> None:
    window = DiagnosticsWindow.__new__(DiagnosticsWindow)
    messages: list[tuple[str, str]] = []
    window._ai_status_text = _Var("Sending to AI for analysis...")
    window._ai_result_text = _Text()
    window._ai_terminal_event_seen = False
    window._ai_start_pending = True
    window._session_ai_active = True
    window._ai_diagnose_button = _Widget()
    window._start_stream_button = _Widget()
    window._read_dtc_button = _Widget()
    window._append_agent_message = lambda role, message: messages.append((role, message))
    window._stop_ai_sse_thread = lambda: None
    window._refresh_action_buttons = lambda: None

    window._handle_ai_done({})

    assert window._ai_status_text.get() == (
        "Error: AI analysis ended before any result or error was received. Check cloud logs for provider or SSE failures."
    )
    assert "AI analysis ended before any result or error was received." in window._ai_result_text.content
    assert window._ai_diagnose_button.state == tk.NORMAL
    assert window._start_stream_button.state == tk.NORMAL
    assert window._read_dtc_button.state == tk.NORMAL
    assert messages[-1] == (
        "agent",
        "AI Diagnostics ended without a terminal payload. AI analysis ended before any result or error was received. Check cloud logs for provider or SSE failures.",
    )


def test_handle_ai_done_preserves_existing_terminal_error_state() -> None:
    window = DiagnosticsWindow.__new__(DiagnosticsWindow)
    messages: list[tuple[str, str]] = []
    window._ai_status_text = _Var("Error: AI analysis failed: provider denied access")
    window._ai_result_text = _Text()
    window._ai_terminal_event_seen = True
    window._ai_start_pending = True
    window._session_ai_active = True
    window._ai_diagnose_button = _Widget()
    window._start_stream_button = _Widget()
    window._read_dtc_button = _Widget()
    window._append_agent_message = lambda role, message: messages.append((role, message))
    window._stop_ai_sse_thread = lambda: None
    window._refresh_action_buttons = lambda: None

    window._handle_ai_done({})

    assert window._ai_status_text.get() == "Error: AI analysis failed: provider denied access"
    assert window._ai_result_text.content == ""
    assert messages == []


class _QueueForSse:
    def put(self, item) -> None:
        return None
