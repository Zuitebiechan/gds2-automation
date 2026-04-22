import queue
import types

import pytest

import diagnostic_platform.runtime.diagnostics_runtime as diagnostics_runtime
import diagnostic_platform.runtime.navigation_runtime as navigation_runtime
import diagnostic_platform.runtime.worker_runtime as worker_runtime_module
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
    iter_navigation_session_events,
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


class _StableValue:
    def __str__(self) -> str:
        return "stable-value"


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

    session = start_navigation_session(
        runtime,
        goal="Go to Data Display",
        controller_factory=lambda: object(),
    )
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


def test_navigation_runtime_start_defaults_invalid_goal(monkeypatch):
    runtime = WorkerRuntime()

    class FakeThread:
        def __init__(self, target, args, daemon, name):
            self.target = target
            self.args = args
            self.daemon = daemon
            self.name = name

        def start(self):
            return None

    monkeypatch.setattr(navigation_runtime.threading, "Thread", FakeThread)

    session = start_navigation_session(
        runtime,
        goal=["bad-goal"],
        controller_factory=lambda: object(),
    )

    assert session.goal == "Navigate to Data Display"


def test_await_navigation_decision_ignores_non_mapping_payloads():
    session = NavSession(session_id="nav-1", goal="Navigate to Data Display")
    session.decision_queue = queue.Queue(maxsize=2)
    session.decision_queue.put(["bad-payload"])
    session.decision_queue.put({"selected_item": "ECM"})

    selected_item = navigation_runtime._await_navigation_decision(
        session,
        page="module_list",
        items=["ECM", "TCM"],
    )

    assert selected_item == "ECM"


def test_await_navigation_decision_normalizes_event_items():
    class DisplayItem:
        def __str__(self) -> str:
            return "Engine Control"

    session = NavSession(session_id="nav-1", goal="Navigate to Data Display")
    session.decision_queue.put({"selected_item": "Engine Control"})

    selected_item = navigation_runtime._await_navigation_decision(
        session,
        page="module_list",
        items=[DisplayItem(), None],
    )
    event = session.event_queue.get_nowait()

    assert selected_item == "Engine Control"
    assert event == {
        "type": "decision_required",
        "decision_id": event["decision_id"],
        "page": "module_list",
        "items": ["Engine Control"],
    }


def test_run_navigation_page_module_submenu_records_selected_item():
    class FakeController:
        def wait_for_list(self):
            return ["Data Display", "Module Information"]

        def select_list_item(self, item):
            assert item == "Data Display"
            return {"success": True, "page": "data_list"}

    session = NavSession(session_id="nav-1", goal="Navigate to Data Display")
    navigation_history: list[dict[str, object]] = []

    result = navigation_runtime._run_navigation_page(
        session,
        controller=FakeController(),
        current_page="module_submenu",
        navigation_history=navigation_history,
        selections={},
    )

    assert result == "data_list"
    assert navigation_history == [
        {
            "page": "module_submenu",
            "action": "select_data_display",
            "selected_item": "Data Display",
            "to_page": "data_list",
            "success": True,
        }
    ]


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


def test_build_navigation_status_payload_accepts_string_status():
    session = NavSession(session_id="nav-1", goal="Navigate to Data Display")
    session.status = "completed"

    payload = build_navigation_status_payload(session.session_id, session)

    assert payload["status"] == "completed"


def test_iter_navigation_session_events_serializes_non_json_values():
    class StableValue:
        def __str__(self) -> str:
            return "stable-value"

    session = NavSession(session_id="nav-1", goal="Navigate to Data Display")
    session.thread = types.SimpleNamespace(is_alive=lambda: False)
    session.status = NavSessionStatus.COMPLETED
    session.event_queue.put({"type": "progress", "page": StableValue()})

    events = list(iter_navigation_session_events(session.session_id, session))

    assert events == [
        'event: connected\ndata: {"session_id": "nav-1"}\n\n',
        'event: progress\ndata: {"type": "progress", "page": "stable-value"}\n\n',
        ": keepalive\n\n",
        'event: done\ndata: {"type": "done", "status": "completed", "error": null}\n\n',
    ]


def test_abort_navigation_session_sets_cancel_event():
    runtime = WorkerRuntime()
    session = NavSession(session_id="nav-1", goal="Navigate to Data Display")
    runtime.set_navigation_session(session.session_id, session)

    abort_navigation_session(runtime, session.session_id)

    assert session.cancel_event.is_set() is True


def test_abort_navigation_session_schedules_cleanup(monkeypatch):
    runtime = WorkerRuntime()
    session = NavSession(session_id="nav-1", goal="Navigate to Data Display")
    runtime.set_navigation_session(session.session_id, session)

    class ImmediateTimer:
        def __init__(self, interval, callback, args=None, kwargs=None):
            self.interval = interval
            self.callback = callback
            self.args = args or ()
            self.kwargs = kwargs or {}
            self.daemon = False

        def start(self):
            self.callback(*self.args, **self.kwargs)

        def cancel(self):
            return None

    monkeypatch.setattr(worker_runtime_module.threading, "Timer", ImmediateTimer)

    abort_navigation_session(runtime, session.session_id)

    with pytest.raises(KeyError):
        runtime.get_navigation_session(session.session_id)


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


def test_run_graph_thread_schedules_cleanup_after_completion(monkeypatch):
    scheduled: list[str] = []
    session = NavSession(session_id="nav-1", goal="Navigate to Data Display")
    session.cleanup_callback = lambda session_id: scheduled.append(session_id)

    monkeypatch.setattr(
        navigation_runtime,
        "_run_navigation_session",
        lambda nav_session: {
            "current_page": "data_display",
            "navigation_history": [{"action": "completed"}],
            "error": None,
        },
        raising=False,
    )

    navigation_runtime._run_graph_thread(session)

    assert session.status == NavSessionStatus.COMPLETED
    assert scheduled == ["nav-1"]


def test_run_graph_thread_uses_controller_factory_when_present(monkeypatch):
    observed = {}
    session = NavSession(session_id="nav-1", goal="Navigate to Data Display")
    controller = object()
    session.controller_factory = lambda: controller

    def fake_run_navigation_session(nav_session, *, controller=None):
        observed["session"] = nav_session
        observed["controller"] = controller
        return {
            "current_page": "data_display",
            "navigation_history": [{"action": "completed"}],
            "error": None,
        }

    monkeypatch.setattr(
        navigation_runtime,
        "_run_navigation_session",
        fake_run_navigation_session,
        raising=False,
    )

    navigation_runtime._run_graph_thread(session)

    assert observed["session"] is session
    assert observed["controller"] is controller
    assert session.status == NavSessionStatus.COMPLETED


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


def test_broadcast_agent_event_stringifies_non_json_payload_values() -> None:
    scope = "session:test-unsafe"
    client_queue = subscribe_agent_stream(scope)

    try:
        delivered = broadcast_agent_event(
            scope,
            RuntimeError("snapshot"),
            {
                "detail": _StableValue(),
                "error": RuntimeError("boom"),
            },
        )

        assert delivered == 1
        assert client_queue.get_nowait() == (
            'event: snapshot\ndata: {"detail": "stable-value", "error": "boom"}\n\n'
        )
    finally:
        unsubscribe_agent_stream(scope, client_queue)


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


