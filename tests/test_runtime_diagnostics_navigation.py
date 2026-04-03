import queue
import types

import pytest

import diagnostic_platform.runtime.diagnostics_runtime as diagnostics_runtime
import diagnostic_platform.runtime.navigation_runtime as navigation_runtime
import diagnostic_platform.runtime.worker_runtime as worker_runtime_module
from src.workflows.data_viewer import DataViewerWorkflow
from src.navigation import NavigationController, NavigationResult
from diagnostic_platform.runtime.diagnostics_runtime import (
    clear_diagnostic_dtcs,
    start_live_data_stream,
    stop_live_data_stream,
)
from diagnostic_platform.runtime.navigation_runtime import (
    NavSession,
    NavSessionStatus,
    abort_navigation_session,
    build_navigation_status_payload,
    start_navigation_session,
    submit_navigation_decision,
)
from diagnostic_platform.runtime.worker_runtime import WorkerRuntime
from diagnostic_platform.sse import (
    agent_stream_client_count,
    broadcast_agent_event,
    subscribe_agent_stream,
    unsubscribe_agent_stream,
)
from src.navigation import GDS2Page


def test_live_data_stream_reuses_scope_and_stops_cleanly(monkeypatch):
    runtime = WorkerRuntime()
    class GenericLiveBackend:
        def __init__(self):
            self.selected_categories = []
            self.started = 0
            self.stopped = 0

        def select_data_category(self, category):
            self.selected_categories.append(category)

        def start_live_data(self):
            self.started += 1
            return types.SimpleNamespace(session_id="live-1", active=True)

        def stop_live_data(self):
            self.stopped += 1

    backend = GenericLiveBackend()

    payload = start_live_data_stream(
        runtime,
        backend=backend,
        data_category="Engine Data",
        interval_ms=250,
        stream_scope="session:s-1",
    )

    assert payload["success"] is True
    assert payload["interval_ms"] == 250
    assert payload["session_id"] == "live-1"
    assert backend.selected_categories == ["Engine Data"]
    assert backend.started == 1

    stop_payload = stop_live_data_stream(runtime, backend=backend)

    assert stop_payload == {"success": True, "message": "Live data stopped"}
    assert backend.stopped == 1


def test_clear_diagnostic_dtcs_selects_context_and_delegates_to_backend():
    class GenericClearBackend:
        def __init__(self):
            self.selected_modules = []
            self.selected_categories = []
            self.clear_calls = 0
            self.current_module = None
            self.current_data_category = None

        def get_state(self):
            current_page = "data_display" if self.current_data_category else "module_list"
            return types.SimpleNamespace(
                current_page=current_page,
                current_module=self.current_module,
                current_data_category=self.current_data_category,
            )

        def select_module(self, module):
            self.selected_modules.append(module)
            self.current_module = module

        def select_data_category(self, data_category):
            self.selected_categories.append(data_category)
            self.current_data_category = data_category

        def clear_dtcs(self):
            self.clear_calls += 1
            return types.SimpleNamespace(
                success=True,
                cleared_count=3,
                message="Cleared by generic backend",
            )

    backend = GenericClearBackend()

    payload = clear_diagnostic_dtcs(
        backend=backend,
        module_name="ECM",
        data_category="DTCs",
    )

    assert payload == {
        "success": True,
        "cleared_count": 3,
        "message": "Cleared by generic backend",
        "page_context": "data_display",
    }
    assert backend.selected_modules == ["ECM"]
    assert backend.selected_categories == ["DTCs"]
    assert backend.clear_calls == 1


def test_navigation_runtime_start_registers_session_and_accepts_decision(monkeypatch):
    runtime = WorkerRuntime()

    class FakeThread:
        def __init__(self, target, args, daemon, name):
            self.target = target
            self.args = args
            self.daemon = daemon
            self.name = name
            self.started = False

        def start(self):
            self.started = True

        def is_alive(self):
            return self.started

    monkeypatch.setattr(navigation_runtime.threading, "Thread", FakeThread)

    session = start_navigation_session(runtime, goal="Go to Data Display")
    session.status = NavSessionStatus.AWAITING_DECISION
    session.pending_decision_id = "decision-1"
    session.pending_items = ["ECM", "TCM"]

    payload = submit_navigation_decision(
        runtime,
        session.session_id,
        decision_id="decision-1",
        selected_item="ECM",
    )

    assert runtime.get_navigation_session(session.session_id) is session
    assert session.thread.started is True
    assert payload["success"] is True
    assert payload["selected_item"] == "ECM"
    assert session.status == NavSessionStatus.RUNNING
    assert session.pending_decision_id is None
    assert session.pending_items == []
    assert session.decision_queue.get_nowait() == {"selected_item": "ECM"}


def test_navigation_status_and_abort_surface_pending_decision_and_terminal_state():
    runtime = WorkerRuntime()
    session = NavSession(session_id="nav-1", goal="Navigate to Data Display")
    session.status = NavSessionStatus.AWAITING_DECISION
    session.pending_decision_id = "decision-1"
    session.pending_items = ["ECM", "TCM"]
    runtime.set_navigation_session(session.session_id, session)

    status = build_navigation_status_payload(session.session_id, session)
    payload = abort_navigation_session(runtime, session.session_id)
    error_event = session.event_queue.get_nowait()

    assert status["pending_decision"]["decision_id"] == "decision-1"
    assert status["pending_decision"]["items"] == ["ECM", "TCM"]
    assert payload["success"] is True
    assert payload["status"] == "aborted"
    assert session.error == "Aborted by user"
    assert error_event["type"] == "error"
    assert error_event["error"] == "Aborted by user"


def test_abort_navigation_session_sets_cancel_event():
    runtime = WorkerRuntime()
    session = NavSession(session_id="nav-1", goal="Navigate to Data Display")
    runtime.set_navigation_session(session.session_id, session)

    abort_navigation_session(runtime, session.session_id)

    assert session.cancel_event.is_set() is True


def test_run_graph_thread_delegates_to_local_navigation_runner(monkeypatch):
    observed = {}
    session = NavSession(session_id="nav-1", goal="Navigate to Data Display")

    def fake_run_navigation_session(nav_session):
        observed["session"] = nav_session
        nav_session.check_cancelled()
        return {
            "current_page": "data_display",
            "navigation_history": [{"action": "completed"}],
        }

    monkeypatch.setattr(
        navigation_runtime,
        "_run_navigation_session",
        fake_run_navigation_session,
        raising=False,
    )

    navigation_runtime._run_graph_thread(session)

    assert observed["session"] is session


def test_run_navigation_session_emits_decision_required_events_and_completes():
    session = NavSession(session_id="nav-1", goal="Navigate to Data Display")
    session.decision_queue = queue.Queue(maxsize=4)
    session.decision_queue.put({"selected_item": "ECM"})
    session.decision_queue.put({"selected_item": "Engine Data"})

    class FakeController:
        def __init__(self):
            self.page = GDS2Page.MODULE_LIST
            self.context = {}

        def detect_current_page(self):
            return self.page

        def get_snapshot(self):
            return {
                "page": self.page.value,
                "buttons": [],
                "lists": self.get_list_items(0),
                "context": self.context.copy(),
            }

        def get_list_items(self, list_index=0):
            mapping = {
                GDS2Page.MODULE_LIST: ["ECM", "TCM"],
                GDS2Page.MODULE_SUBMENU: ["Data Display", "Module Information"],
                GDS2Page.DATA_LIST: ["Engine Data", "Transmission Data"],
            }
            return mapping.get(self.page, [])

        def select_list_item(self, item_text, list_index=0, double_click=True):
            if self.page == GDS2Page.MODULE_LIST:
                assert item_text == "ECM"
                self.page = GDS2Page.MODULE_SUBMENU
                self.context["module"] = item_text
                return NavigationResult(True, self.page, selected=item_text, context=self.context.copy())
            if self.page == GDS2Page.MODULE_SUBMENU:
                assert item_text == "Data Display"
                self.page = GDS2Page.DATA_LIST
                return NavigationResult(True, self.page, selected=item_text, context=self.context.copy())
            if self.page == GDS2Page.DATA_LIST:
                assert item_text == "Engine Data"
                self.page = GDS2Page.DATA_DISPLAY
                self.context["data_category"] = item_text
                return NavigationResult(True, self.page, selected=item_text, context=self.context.copy())
            raise AssertionError(f"Unexpected selection on {self.page.value}")

    final = navigation_runtime._run_navigation_session(session, controller=FakeController())

    events = []
    while not session.event_queue.empty():
        events.append(session.event_queue.get_nowait())

    assert [event["type"] for event in events].count("decision_required") == 2
    assert any(
        event["type"] == "decision_required"
        and event["page"] == "module_list"
        and event["items"] == ["ECM", "TCM"]
        for event in events
    )
    assert any(
        event["type"] == "decision_required"
        and event["page"] == "data_list"
        and event["items"] == ["Engine Data", "Transmission Data"]
        for event in events
    )
    assert events[-1]["type"] == "done"
    assert events[-1]["final_page"] == "data_display"
    assert events[-1]["selections"] == {"module": "ECM", "data_category": "Engine Data"}
    assert final["current_page"] == "data_display"
    assert final["selections"] == {"module": "ECM", "data_category": "Engine Data"}
    assert final["navigation_history"]


def test_run_graph_thread_marks_aborted_when_local_runner_is_cancelled(monkeypatch):
    session = NavSession(session_id="nav-1", goal="Navigate to Data Display")

    def fake_run_navigation_session(nav_session):
        raise worker_runtime_module.OperationCancelledError("cancelled")

    monkeypatch.setattr(
        navigation_runtime,
        "_run_navigation_session",
        fake_run_navigation_session,
        raising=False,
    )

    navigation_runtime._run_graph_thread(session)

    assert session.status == NavSessionStatus.ABORTED
    assert session.error == "Aborted by user"


def test_scoped_agent_streams_do_not_cross_talk():
    scope_a = "session:test-a"
    scope_b = "session:test-b"
    queue_a = subscribe_agent_stream(scope_a)
    queue_b = subscribe_agent_stream(scope_b)

    try:
        assert agent_stream_client_count(scope_a) == 1
        assert agent_stream_client_count(scope_b) == 1
        delivered = broadcast_agent_event(scope_a, "snapshot", {"ok": True})

        assert delivered == 1
        assert queue_a.get_nowait().startswith("event: snapshot\n")
        with pytest.raises(queue.Empty):
            queue_b.get_nowait()
    finally:
        unsubscribe_agent_stream(scope_a, queue_a)
        unsubscribe_agent_stream(scope_b, queue_b)

    assert agent_stream_client_count(scope_a) == 0
    assert agent_stream_client_count(scope_b) == 0


def test_connect_device_retries_empty_module_list_before_failing(monkeypatch):
    class FakeController:
        def __init__(self):
            self.current_page = GDS2Page.MODULE_LIST
            self.current_data_category = None
            self.nav = object()

        def detect_current_page(self):
            return GDS2Page.MODULE_LIST

        def wait_for_list(self, previous_items=None):
            if not hasattr(self, "_calls"):
                self._calls = 0
            self._calls += 1
            if self._calls == 1:
                return []
            return ["[K20] Engine Control Module"]

        def set_context(self, **kwargs):
            return None

    class FakeMapping:
        def update_module_list(self, vehicle_id, module_indices):
            return None

    workflow = DataViewerWorkflow()
    workflow.controller = FakeController()
    workflow._mapping = FakeMapping()
    monkeypatch.setattr(workflow, "_extract_vin", lambda: None)

    result = workflow.connect_device("default")

    assert result["modules"] == ["[K20] Engine Control Module"]


def test_connect_device_checks_cancel_before_enter_click(monkeypatch):
    class FakeController:
        def __init__(self):
            self.current_page = GDS2Page.VEHICLE_SELECTION
            self.current_data_category = None
            self.nav = object()
            self.click_enter_called = False

        def detect_current_page(self):
            return GDS2Page.VEHICLE_SELECTION

        def click_enter(self):
            self.click_enter_called = True
            raise AssertionError("click_enter should not be called after cancellation")

    workflow = DataViewerWorkflow()
    workflow.controller = FakeController()
    monkeypatch.setattr(workflow, "_wait_for_button_enabled", lambda *args, **kwargs: True)
    workflow.set_cancel_checker(lambda: (_ for _ in ()).throw(worker_runtime_module.OperationCancelledError("cancelled")))

    with pytest.raises(worker_runtime_module.OperationCancelledError):
        workflow.connect_device("default")

    assert workflow.controller.click_enter_called is False


def test_connect_device_checks_cancel_after_enter_becomes_enabled(monkeypatch):
    class FakeController:
        def __init__(self):
            self.current_page = GDS2Page.VEHICLE_SELECTION
            self.current_data_category = None
            self.nav = object()
            self.click_enter_called = False

        def detect_current_page(self):
            return GDS2Page.VEHICLE_SELECTION

        def click_enter(self):
            self.click_enter_called = True
            raise AssertionError("click_enter should not be called after cancellation")

    workflow = DataViewerWorkflow()
    workflow.controller = FakeController()
    monkeypatch.setattr(workflow, "_wait_for_button_enabled", lambda *args, **kwargs: True)

    checks = {"count": 0}

    def cancel_after_wait():
        checks["count"] += 1
        if checks["count"] >= 2:
            raise worker_runtime_module.OperationCancelledError("cancelled")

    workflow.set_cancel_checker(cancel_after_wait)

    with pytest.raises(worker_runtime_module.OperationCancelledError):
        workflow.connect_device("default")

    assert workflow.controller.click_enter_called is False


def test_navigate_to_module_list_checks_cancel_after_enter_becomes_enabled(monkeypatch):
    class FakeController:
        def __init__(self):
            self.current_page = GDS2Page.VEHICLE_SELECTION
            self.current_data_category = None
            self.nav = object()
            self.click_enter_called = False

        def click_enter(self):
            self.click_enter_called = True
            raise AssertionError("click_enter should not be called after cancellation")

    workflow = DataViewerWorkflow()
    workflow.controller = FakeController()
    monkeypatch.setattr(workflow, "_wait_for_button_enabled", lambda *args, **kwargs: True)

    checks = {"count": 0}

    def cancel_after_wait():
        checks["count"] += 1
        if checks["count"] >= 2:
            raise worker_runtime_module.OperationCancelledError("cancelled")

    workflow.set_cancel_checker(cancel_after_wait)

    with pytest.raises(worker_runtime_module.OperationCancelledError):
        workflow._navigate_to_module_list_from(GDS2Page.VEHICLE_SELECTION, lambda msg: None)

    assert workflow.controller.click_enter_called is False


def test_go_home_returns_actual_transition_page(monkeypatch):
    class FakeNav:
        def click_button(self, button_text):
            assert button_text == "Home"
            return {"success": True}

    controller = NavigationController(nav=FakeNav())
    controller._current_page = GDS2Page.DIAGNOSTICS_MENU
    monkeypatch.setattr(controller, "wait_for_page_transition", lambda *args, **kwargs: GDS2Page.VEHICLE_SELECTION)

    result = controller.go_home()

    assert result.success is True
    assert result.page == GDS2Page.VEHICLE_SELECTION
    assert controller.current_page == GDS2Page.VEHICLE_SELECTION


def test_navigate_to_main_menu_keeps_going_after_transient_vehicle_selection(monkeypatch):
    class FakeNav:
        def __init__(self):
            self.calls = 0

        def get_buttons(self):
            self.calls += 1
            return [{"text": "Home", "enabled": True}]

    class FakeController:
        def __init__(self):
            self.nav = FakeNav()
            self.detect_calls = 0

        def detect_current_page(self):
            self.detect_calls += 1
            if self.detect_calls == 1:
                return GDS2Page.MODULE_SUBMENU
            return GDS2Page.MAIN_MENU

        def go_home(self):
            return NavigationResult(
                success=True,
                page=GDS2Page.VEHICLE_SELECTION,
                context={},
            )

        def get_visible_buttons(self):
            return ["Home"]

    workflow = DataViewerWorkflow()
    workflow.controller = FakeController()
    status_messages = []
    monkeypatch.setattr(workflow, "_sleep", lambda *args, **kwargs: None)

    workflow._navigate_to_main_menu(status_messages.append)

    assert workflow.controller.detect_calls >= 2
    assert "At Main Menu" in status_messages
