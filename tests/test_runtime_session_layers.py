import json
import threading
import time
import types
from unittest.mock import MagicMock

import pytest

import diagnostic_platform.session_orchestrator as platform_session_orchestrator_module
import src.gds2_orchestration.session_orchestrator as session_orchestrator_module
import diagnostic_platform.runtime.worker_runtime as worker_runtime_module
from diagnostic_platform.runtime.navigation_runtime import NavSession, NavSessionStatus
from diagnostic_platform.runtime.session_actions import (
    apply_navigation_event,
    abort_navigation,
    clear_dtcs,
    handle_ai_stream_terminal_event,
    handle_live_data_stream_terminal_event,
    navigation_status_payload,
    read_dtcs,
    resolve_session_vehicle_context,
    start_navigation,
    submit_navigation_decision as submit_business_navigation_decision,
)
from diagnostic_platform.runtime.session_decisions import build_branch_gate, submit_session_decision
from diagnostic_platform.runtime.session_lifecycle import (
    abort_business_session,
    build_session_status_payload,
    start_business_session,
)
from diagnostic_platform.runtime.session_preflight import (
    build_network_quality_gate,
    get_session_network_snapshot,
    run_start_diagnostics,
)
from diagnostic_platform.runtime.worker_runtime import WorkerRuntime
from src.gds2_orchestration.session_orchestrator import SessionContext, SessionOrchestrator


class _StableValue:
    def __str__(self) -> str:
        return "stable-value"


def _start_gds2_session(monkeypatch):
    runtime = WorkerRuntime()
    orchestrator = SessionOrchestrator()
    monkeypatch.setattr(platform_session_orchestrator_module, "route_backend", lambda brand: "gds2")
    payload = start_business_session(
        runtime,
        orchestrator=orchestrator,
        context=SessionContext(brand="Chevrolet", model="Malibu", vin="VIN123"),
    )
    session = orchestrator.get_session(payload["session_id"])
    return runtime, orchestrator, session, payload


def test_start_business_session_binds_worker_and_keeps_execution_state_out_of_session(monkeypatch):
    runtime, orchestrator, session, payload = _start_gds2_session(monkeypatch)
    runtime.bind_navigation_session(session.session_id, "nav-1")
    runtime.bind_ai_session(session.session_id, "ai-1")
    runtime.set_live_data_active(session.session_id, True)

    backend = MagicMock()
    backend.preflight.return_value = {
        "network_quality": {
            "grade": "good",
            "status": "healthy",
            "reason": "ok",
            "connection_epoch": "epoch-1",
            "sample_count": 5,
            "fresh": True,
            "connected": True,
        },
        "connection_epoch": "epoch-1",
    }

    status = build_session_status_payload(
        runtime,
        orchestrator=orchestrator,
        backend=backend,
        session_id=session.session_id,
    )

    assert payload["success"] is True
    assert payload["status"] == "running"
    assert runtime.get_business_session_binding(session.session_id).session_id == session.session_id
    assert status["active_navigation_session_id"] == "nav-1"
    assert status["active_ai_session_id"] == "ai-1"
    assert status["live_data_active"] is True
    assert status["connection_epoch"] == "epoch-1"
    assert status["network_quality"]["grade"] == "good"
    assert "active_navigation_session_id" not in session.to_dict()
    assert "active_ai_session_id" not in session.to_dict()
    assert "live_data_active" not in session.to_dict()


def test_build_session_status_payload_is_read_only(monkeypatch):
    runtime, orchestrator, session, _ = _start_gds2_session(monkeypatch)
    backend = types.SimpleNamespace(
        get_state=lambda: types.SimpleNamespace(
            current_page="module_list",
            is_connected=False,
            current_module=None,
            current_data_category=None,
            extra={},
        ),
        preflight=lambda: {
            "network_quality": {
                "grade": "good",
                "status": "healthy",
                "reason": "ok",
                "connection_epoch": "epoch-1",
                "sample_count": 5,
                "fresh": True,
                "connected": True,
            },
            "connection_epoch": "epoch-1",
        },
    )

    assert getattr(session, "current_page", None) is None
    assert runtime.get_connection_epoch(session.session_id) is None

    status = build_session_status_payload(
        runtime,
        orchestrator=orchestrator,
        backend=backend,
        session_id=session.session_id,
    )

    assert status["backend_state_summary"]["current_page"] == "module_list"
    assert status["connection_epoch"] == "epoch-1"
    assert getattr(session, "current_page", None) is None
    assert runtime.get_connection_epoch(session.session_id) is None


def test_submit_session_decision_cancels_network_gate_and_clears_override(monkeypatch):
    runtime, orchestrator, session, _ = _start_gds2_session(monkeypatch)
    network_quality = {
        "grade": "block",
        "status": "blocked",
        "reason": "p95 above threshold",
        "connection_epoch": "epoch-1",
        "sample_count": 5,
        "fresh": True,
        "connected": True,
    }
    gate = build_network_quality_gate(network_quality)
    orchestrator.raise_decision(session.session_id, gate)
    session.network_override = {
        "allowed": True,
        "connection_epoch": "epoch-1",
        "reason": "stale",
    }

    backend = MagicMock()
    backend.preflight.return_value = {
        "network_quality": network_quality,
        "connection_epoch": "epoch-1",
    }

    result = submit_session_decision(
        runtime,
        orchestrator=orchestrator,
        backend=backend,
        session_id=session.session_id,
        decision_id=gate.decision_id,
        option_id="cancel",
        get_navigation_runtime=None,
        get_backend=lambda: MagicMock(),
    )

    updated_session = orchestrator.get_session(session.session_id)
    assert result["success"] is True
    assert result["cancelled"] is True
    assert result["network_override"] is None
    assert updated_session.status.value == "running"
    assert updated_session.network_override is None


def test_submit_session_decision_resumes_branch_selection_and_updates_business_state(monkeypatch):
    runtime, orchestrator, session, _ = _start_gds2_session(monkeypatch)
    backend = MagicMock()
    backend.select_module.return_value = {"selected_module": "ECM-B"}

    gate = build_branch_gate(
        domain="module",
        target="ECM",
        choices=["ECM-A", "ECM-B"],
        reason="ambiguous module",
        resume_action="select_module",
    )
    orchestrator.raise_decision(session.session_id, gate)

    result = submit_session_decision(
        runtime,
        orchestrator=orchestrator,
        backend=backend,
        session_id=session.session_id,
        decision_id=gate.decision_id,
        option_id="branch_1",
        get_navigation_runtime=None,
        get_backend=lambda: backend,
    )

    assert result["success"] is True
    assert result["resumed"] is True
    assert result["resume_action"] == "select_module"
    assert result["selected_choice"] == "ECM-B"
    assert session.selected_module == "ECM-B"
    assert session.selected_data_category == ""
    backend.select_module.assert_called_once_with("ECM-B")


def test_submit_session_decision_prefers_navigation_runtime_for_module_resume(monkeypatch):
    runtime, orchestrator, session, _ = _start_gds2_session(monkeypatch)
    navigation_runtime = MagicMock()
    navigation_runtime.select_module.return_value = {"selected_module": "ECM-B"}

    gate = build_branch_gate(
        domain="module",
        target="ECM",
        choices=["ECM-A", "ECM-B"],
        reason="ambiguous module",
        resume_action="select_module",
    )
    orchestrator.raise_decision(session.session_id, gate)

    result = submit_session_decision(
        runtime,
        orchestrator=orchestrator,
        backend=MagicMock(),
        session_id=session.session_id,
        decision_id=gate.decision_id,
        option_id="branch_1",
        get_navigation_runtime=lambda: navigation_runtime,
        get_backend=lambda: MagicMock(),
    )

    assert result["success"] is True
    assert result["resume_action"] == "select_module"
    navigation_runtime.select_module.assert_called_once_with("ECM-B")


def test_submit_session_decision_falls_back_to_backend_when_navigation_runtime_unavailable(monkeypatch):
    runtime, orchestrator, session, _ = _start_gds2_session(monkeypatch)
    backend = MagicMock()
    backend.select_module.return_value = {"selected_module": "ECM-B"}

    gate = build_branch_gate(
        domain="module",
        target="ECM",
        choices=["ECM-A", "ECM-B"],
        reason="ambiguous module",
        resume_action="select_module",
    )
    orchestrator.raise_decision(session.session_id, gate)

    result = submit_session_decision(
        runtime,
        orchestrator=orchestrator,
        backend=backend,
        session_id=session.session_id,
        decision_id=gate.decision_id,
        option_id="branch_1",
        get_navigation_runtime=lambda: (_ for _ in ()).throw(RuntimeError("no navigation runtime")),
        get_backend=lambda: backend,
    )

    assert result["success"] is True
    assert result["resume_action"] == "select_module"
    backend.select_module.assert_called_once_with("ECM-B")


def test_clear_dtcs_uses_current_context_without_hidden_reselection(monkeypatch):
    runtime, _, session, _ = _start_gds2_session(monkeypatch)
    session.selected_module = "[K20] Engine Control Module"
    session.selected_data_category = "Engine Data"

    backend = MagicMock()
    backend.get_state.return_value = types.SimpleNamespace(
        current_page="data_display",
        current_module="",
        current_data_category="",
    )
    backend.detect_current_page.return_value = "data_display"
    backend.clear_dtcs.return_value = types.SimpleNamespace(
        success=True,
        cleared_count=2,
        message="Clear DTCs completed",
    )

    payload = clear_dtcs(
        runtime,
        session,
        {"session_id": session.session_id},
        backend=backend,
        emit_progress=lambda _message: None,
    )

    assert payload["success"] is True
    assert payload["cleared_count"] == 2
    assert payload["page_context"] == "data_display"
    backend.select_module.assert_not_called()
    backend.select_data_category.assert_not_called()
    backend.clear_dtcs.assert_called_once_with()


def test_read_dtcs_uses_current_data_display_without_hidden_reselection(monkeypatch):
    _runtime, _, session, _ = _start_gds2_session(monkeypatch)
    session.selected_module = "[K20] Engine Control Module"
    session.selected_data_category = "Engine Data"

    backend = MagicMock()
    backend.get_state.return_value = types.SimpleNamespace(
        current_page="data_display",
        current_module="",
        current_data_category="",
    )
    backend.detect_current_page.return_value = "data_display"
    backend.read_dtcs.return_value = [
        types.SimpleNamespace(
            code="P0001",
            module="[K20] Engine Control Module",
            status="current",
            description="Example fault",
            source_backend="gds2",
        )
    ]

    payload = read_dtcs(
        session,
        {
            "session_id": session.session_id,
            "module": session.selected_module,
            "data_category": session.selected_data_category,
        },
        backend=backend,
        emit_progress=lambda _message: None,
    )

    assert payload["dtc_count"] == 1
    assert payload["page_context"] == "data_display"
    assert payload["dtcs"] == [
        {
            "code": "P0001",
            "control_module": "[K20] Engine Control Module",
            "module": "[K20] Engine Control Module",
            "status": "current",
            "description": "Example fault",
            "source_backend": "gds2",
        }
    ]
    backend.select_module.assert_not_called()
    backend.select_data_category.assert_not_called()
    backend.read_dtcs.assert_called_once_with()


def test_clear_dtcs_explicit_context_selects_module_and_category(monkeypatch):
    runtime, _, session, _ = _start_gds2_session(monkeypatch)

    class BackendWithExplicitSelection:
        def __init__(self):
            self.current_page = "module_list"
            self.current_module = ""
            self.current_data_category = ""
            self.select_module_calls: list[str] = []
            self.select_data_category_calls: list[str] = []

        def get_state(self):
            return types.SimpleNamespace(
                current_page=self.current_page,
                current_module=self.current_module,
                current_data_category=self.current_data_category,
            )

        def detect_current_page(self):
            return self.current_page

        def select_module(self, module_name):
            self.select_module_calls.append(module_name)
            self.current_module = module_name
            self.current_page = "data_list"

        def select_data_category(self, data_category):
            self.select_data_category_calls.append(data_category)
            self.current_data_category = data_category
            self.current_page = "data_display"

        def clear_dtcs(self):
            return types.SimpleNamespace(
                success=True,
                cleared_count=1,
                message="Clear DTCs completed",
            )

    backend = BackendWithExplicitSelection()

    payload = clear_dtcs(
        runtime,
        session,
        {
            "session_id": session.session_id,
            "module": "ECM",
            "data_category": "Engine Data",
        },
        backend=backend,
        emit_progress=lambda _message: None,
    )

    assert payload == {
        "success": True,
        "cleared_count": 1,
        "message": "Clear DTCs completed",
        "page_context": "data_display",
    }
    assert backend.select_module_calls == ["ECM"]
    assert backend.select_data_category_calls == ["Engine Data"]
    assert session.selected_module == "ECM"
    assert session.selected_data_category == "Engine Data"


def test_clear_dtcs_uses_remembered_context_when_not_on_data_display(monkeypatch):
    runtime, _, session, _ = _start_gds2_session(monkeypatch)
    session.selected_module = "ECM"
    session.selected_data_category = "Engine Data"

    class BackendWithRememberedSelection:
        def __init__(self):
            self.current_page = "module_list"
            self.current_module = ""
            self.current_data_category = ""
            self.select_module_calls: list[str] = []
            self.select_data_category_calls: list[str] = []

        def get_state(self):
            return types.SimpleNamespace(
                current_page=self.current_page,
                current_module=self.current_module,
                current_data_category=self.current_data_category,
            )

        def detect_current_page(self):
            return self.current_page

        def select_module(self, module_name):
            self.select_module_calls.append(module_name)
            self.current_module = module_name
            self.current_page = "data_list"

        def select_data_category(self, data_category):
            self.select_data_category_calls.append(data_category)
            self.current_data_category = data_category
            self.current_page = "data_display"

        def clear_dtcs(self):
            return types.SimpleNamespace(
                success=True,
                cleared_count=2,
                message="Clear DTCs completed",
            )

    backend = BackendWithRememberedSelection()

    payload = clear_dtcs(
        runtime,
        session,
        {"session_id": session.session_id},
        backend=backend,
        emit_progress=lambda _message: None,
    )

    assert payload == {
        "success": True,
        "cleared_count": 2,
        "message": "Clear DTCs completed",
        "page_context": "data_display",
    }
    assert backend.select_module_calls == ["ECM"]
    assert backend.select_data_category_calls == ["Engine Data"]
    assert session.selected_module == "ECM"
    assert session.selected_data_category == "Engine Data"


def test_clear_dtcs_reselects_remembered_category_outside_data_display(monkeypatch):
    runtime, _, session, _ = _start_gds2_session(monkeypatch)
    session.selected_module = "ECM"
    session.selected_data_category = "Engine Data"

    class BackendRequiringCategoryReselection:
        def __init__(self):
            self.current_page = "data_list"
            self.current_module = "ECM"
            self.current_data_category = "Engine Data"
            self.select_data_category_calls: list[str] = []

        def get_state(self):
            return types.SimpleNamespace(
                current_page=self.current_page,
                current_module=self.current_module,
                current_data_category=self.current_data_category,
            )

        def detect_current_page(self):
            return self.current_page

        def select_data_category(self, data_category):
            self.select_data_category_calls.append(data_category)
            self.current_page = "data_display"

        def clear_dtcs(self):
            return types.SimpleNamespace(
                success=True,
                cleared_count=1,
                message="Clear DTCs completed",
            )

    backend = BackendRequiringCategoryReselection()

    payload = clear_dtcs(
        runtime,
        session,
        {"session_id": session.session_id},
        backend=backend,
        emit_progress=lambda _message: None,
    )

    assert payload == {
        "success": True,
        "cleared_count": 1,
        "message": "Clear DTCs completed",
        "page_context": "data_display",
    }
    assert backend.select_data_category_calls == ["Engine Data"]


def test_resolve_session_vehicle_context_ignores_non_string_state_values() -> None:
    session = types.SimpleNamespace(
        context=types.SimpleNamespace(vin=["VIN123"]),
        selected_module={"name": "ECM"},
        selected_data_category=None,
    )
    backend = types.SimpleNamespace(
        get_state=lambda: types.SimpleNamespace(
            current_module=["Current Module"],
            current_data_category={"name": "Engine Data"},
            extra={
                "vin": 12345,
                "module": ["Extra Module"],
                "data_category": object(),
            },
        )
    )

    context = resolve_session_vehicle_context(
        session,
        {
            "vin": {"value": "bad"},
            "module": ["bad"],
            "data_category": 99,
        },
        backend=backend,
    )

    assert context == {
        "vin": "",
        "module": "",
        "data_category": "",
    }


def test_get_session_network_snapshot_ignores_non_mapping_preflight_payload(monkeypatch):
    _, orchestrator, session, _ = _start_gds2_session(monkeypatch)
    session.network_override = {
        "allowed": True,
        "connection_epoch": "epoch-1",
        "reason": "stale",
    }
    backend = types.SimpleNamespace(preflight=lambda: ["bad"])

    snapshot = get_session_network_snapshot(
        orchestrator=orchestrator,
        backend=backend,
        session_id=session.session_id,
    )

    assert snapshot == {
        "network_quality": None,
        "network_override": None,
        "connection_epoch": None,
    }
    assert session.network_override is None


def test_apply_navigation_event_ignores_non_mapping_event() -> None:
    runtime = WorkerRuntime()
    session = types.SimpleNamespace(
        session_id="session-1",
        selected_module="",
        selected_data_category="",
    )
    nav_session = types.SimpleNamespace(
        current_page="module_list",
        status=NavSessionStatus.RUNNING,
        pending_decision_id=None,
        pending_items=[],
    )

    event_type = apply_navigation_event(runtime, session, nav_session, ["bad-event"])

    assert event_type == "progress"
    assert nav_session.current_page == "module_list"
    assert nav_session.pending_decision_id is None
    assert nav_session.pending_items == []


def test_handle_ai_stream_terminal_event_ignores_non_string_message() -> None:
    runtime = WorkerRuntime()
    session = types.SimpleNamespace(session_id="session-1")
    runtime.bind_business_session(session.session_id)
    runtime.bind_ai_session(session.session_id, "ai-1")

    handled = handle_ai_stream_terminal_event(runtime, session, {"bad": True})

    assert handled is False
    assert runtime.get_ai_session_id(session.session_id) == "ai-1"


def test_handle_live_data_stream_terminal_event_ignores_non_string_message() -> None:
    runtime = WorkerRuntime()
    session = types.SimpleNamespace(session_id="session-1")
    runtime.set_live_data_active(session.session_id, True)

    handled = handle_live_data_stream_terminal_event(runtime, session, {"bad": True})

    assert handled is False
    assert runtime.is_live_data_active(session.session_id) is True


def test_navigation_status_payload_accepts_string_status() -> None:
    runtime = WorkerRuntime()
    session = types.SimpleNamespace(session_id="session-1")
    nav_session = NavSession(session_id="nav-1", goal="Navigate to Data Display")
    nav_session.status = "completed"
    runtime.set_navigation_session(nav_session.session_id, nav_session)
    runtime.bind_navigation_session(session.session_id, nav_session.session_id)

    payload = navigation_status_payload(runtime, session)

    assert payload["navigation_session_id"] == "nav-1"
    assert payload["status"] == "completed"


def test_start_navigation_prefers_bound_backend_navigation_handle() -> None:
    runtime = WorkerRuntime()
    session = types.SimpleNamespace(session_id="session-1")
    runtime.bind_business_session(session.session_id)

    nav_session = types.SimpleNamespace(
        session_id="nav-1",
        status=types.SimpleNamespace(value="running"),
    )
    navigation_handle = MagicMock()
    navigation_handle.start_navigation_session.return_value = nav_session
    runtime.active_backend_bundle = types.SimpleNamespace(navigation_handle=navigation_handle)

    messages: list[str] = []
    started = start_navigation(
        runtime,
        session,
        goal="Go to Data Display",
        emit_progress=messages.append,
    )

    assert started is nav_session
    assert runtime.get_navigation_session_id(session.session_id) == "nav-1"
    assert messages == ["Navigation started: Go to Data Display"]
    navigation_handle.start_navigation_session.assert_called_once_with(runtime, "Go to Data Display")


def test_start_navigation_rejects_backend_without_navigation_runtime() -> None:
    runtime = WorkerRuntime()
    session = types.SimpleNamespace(session_id="session-1")
    runtime.bind_business_session(session.session_id)

    with pytest.raises(RuntimeError, match="does not expose a navigation runtime"):
        start_navigation(
            runtime,
            session,
            goal="Go to Data Display",
            backend=types.SimpleNamespace(name="broken-backend"),
            emit_progress=lambda _message: None,
        )


def test_submit_business_navigation_decision_uses_bound_backend_navigation_handle() -> None:
    runtime = WorkerRuntime()
    session = types.SimpleNamespace(session_id="session-1")
    runtime.bind_business_session(session.session_id)
    runtime.bind_navigation_session(session.session_id, "nav-1")

    nav_session = NavSession(session_id="nav-1", goal="Navigate to Data Display")
    runtime.set_navigation_session(nav_session.session_id, nav_session)

    navigation_handle = MagicMock()
    navigation_handle.get_navigation_session.return_value = nav_session
    navigation_handle.submit_navigation_decision.return_value = {
        "success": True,
        "session_id": "nav-1",
        "selected_item": "ECM",
    }
    runtime.active_backend_bundle = types.SimpleNamespace(navigation_handle=navigation_handle)

    nav_sid, payload = submit_business_navigation_decision(
        runtime,
        session,
        decision_id="decision-1",
        selected_item="ECM",
    )

    assert nav_sid == "nav-1"
    assert payload["selected_item"] == "ECM"
    navigation_handle.get_navigation_session.assert_called_once_with(runtime, "nav-1")
    navigation_handle.submit_navigation_decision.assert_called_once_with(
        runtime,
        "nav-1",
        decision_id="decision-1",
        selected_item="ECM",
    )


def test_abort_navigation_uses_bound_backend_navigation_handle() -> None:
    runtime = WorkerRuntime()
    session = types.SimpleNamespace(session_id="session-1")
    runtime.bind_business_session(session.session_id)
    runtime.bind_navigation_session(session.session_id, "nav-1")

    nav_session = NavSession(session_id="nav-1", goal="Navigate to Data Display")
    runtime.set_navigation_session(nav_session.session_id, nav_session)

    navigation_handle = MagicMock()
    navigation_handle.get_navigation_session.return_value = nav_session
    navigation_handle.abort_navigation_session.return_value = {
        "success": True,
        "session_id": "nav-1",
        "status": "aborted",
    }
    runtime.active_backend_bundle = types.SimpleNamespace(navigation_handle=navigation_handle)

    nav_sid, payload = abort_navigation(runtime, session)

    assert nav_sid == "nav-1"
    assert payload["status"] == "aborted"
    assert runtime.get_navigation_session_id(session.session_id) is None
    navigation_handle.get_navigation_session.assert_called_once_with(runtime, "nav-1")
    navigation_handle.abort_navigation_session.assert_called_once_with(runtime, "nav-1")


def test_abort_business_session_clears_worker_bindings(monkeypatch):
    runtime, orchestrator, session, _ = _start_gds2_session(monkeypatch)

    nav_session = NavSession(session_id="nav-1", goal="Navigate to Data Display")
    runtime.set_navigation_session(nav_session.session_id, nav_session)
    runtime.bind_navigation_session(session.session_id, nav_session.session_id)
    runtime.bind_ai_session(session.session_id, "ai-1")
    runtime.set_live_data_active(session.session_id, True)

    class BackendWithLiveStop:
        name = "gds2"

        def __init__(self):
            self.stop_calls = 0
            self.go_back = MagicMock()

        def stop_live_data_session(self):
            self.stop_calls += 1
            self.go_back()
            return {"success": True, "message": "Live data stopped"}

    backend = BackendWithLiveStop()
    runtime.backend = backend

    ai_engine = MagicMock()
    payload = abort_business_session(
        runtime,
        orchestrator=orchestrator,
        session_id=session.session_id,
        reason="user_cancelled",
        get_ai_engine=lambda: ai_engine,
    )

    binding = runtime.get_business_session_binding(session.session_id)
    assert payload["success"] is True
    assert payload["status"] == "aborted"
    assert binding.session_id is None
    assert binding.navigation_session_id is None
    assert binding.ai_session_id is None
    assert binding.live_data_active is False
    assert nav_session.status == NavSessionStatus.ABORTED
    assert backend.stop_calls == 1
    backend.go_back.assert_called_once_with()
    ai_engine.abort_session.assert_called_once_with("ai-1")


def test_abort_business_session_cancels_active_worker_operation(monkeypatch):
    runtime, orchestrator, session, _ = _start_gds2_session(monkeypatch)
    assert hasattr(worker_runtime_module, "OperationCancelledError")
    operation = runtime.start_operation(session.session_id, "start_diagnostics")

    abort_business_session(
        runtime,
        orchestrator=orchestrator,
        session_id=session.session_id,
        reason="user_cancelled",
        get_ai_engine=lambda: MagicMock(),
    )

    with pytest.raises(worker_runtime_module.OperationCancelledError):
        operation.check_cancelled()


def test_build_session_status_payload_does_not_rebind_aborted_session(monkeypatch):
    runtime, orchestrator, session, _ = _start_gds2_session(monkeypatch)
    backend = types.SimpleNamespace(
        get_state=lambda: types.SimpleNamespace(
            current_page="module_list",
            is_connected=False,
            current_module=None,
            current_data_category=None,
            extra={},
        ),
        preflight=lambda: {
            "network_quality": {
                "grade": "good",
                "status": "healthy",
                "reason": "ok",
                "connection_epoch": "epoch-1",
                "sample_count": 5,
                "fresh": True,
                "connected": True,
            },
            "connection_epoch": "epoch-1",
        },
    )

    abort_business_session(
        runtime,
        orchestrator=orchestrator,
        session_id=session.session_id,
        reason="user_cancelled",
        get_ai_engine=lambda: MagicMock(),
    )

    assert runtime.get_business_session_binding(session.session_id).session_id is None

    status = build_session_status_payload(
        runtime,
        orchestrator=orchestrator,
        backend=backend,
        session_id=session.session_id,
    )

    assert status["status"] == "aborted"
    assert runtime.get_business_session_binding(session.session_id).session_id is None

    restarted = start_business_session(
        runtime,
        orchestrator=orchestrator,
        context=SessionContext(brand="Chevrolet", model="Malibu", vin="VIN456"),
    )

    assert restarted["success"] is True
    assert restarted["session_id"] != session.session_id


def test_start_business_session_serializes_concurrent_starts(monkeypatch):
    orchestrator = SessionOrchestrator()
    monkeypatch.setattr(platform_session_orchestrator_module, "route_backend", lambda brand: "gds2")

    real_uuid4 = platform_session_orchestrator_module.uuid.uuid4
    start_gate = threading.Event()
    created_sessions: list[str] = []
    failures: list[str] = []

    def slow_uuid4():
        start_gate.wait(timeout=1.0)
        time.sleep(0.05)
        return real_uuid4()

    monkeypatch.setattr(platform_session_orchestrator_module.uuid, "uuid4", slow_uuid4)

    def worker() -> None:
        try:
            session = orchestrator.start_session(
                SessionContext(brand="Chevrolet", model="Malibu", vin="VIN123"),
            )
            created_sessions.append(session.session_id)
        except RuntimeError as exc:
            failures.append(str(exc))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    start_gate.set()
    for thread in threads:
        thread.join(timeout=1.0)

    assert len(created_sessions) == 1
    assert len(failures) == 1
    assert "already active" in failures[0]


def test_complete_session_keeps_terminal_event_when_queue_is_full(monkeypatch):
    _, orchestrator, session, _ = _start_gds2_session(monkeypatch)
    event_queue = orchestrator.get_event_queue(session.session_id)
    assert event_queue is not None

    for index in range(499):
        orchestrator.emit_progress(session.session_id, f"progress-{index}")

    assert event_queue.qsize() == 500

    orchestrator.complete_session(session.session_id)

    drained = []
    while not event_queue.empty():
        drained.append(event_queue.get_nowait())

    assert any(message.startswith("event: done\n") for message in drained)


def test_emit_progress_stringifies_non_json_extra_values(monkeypatch):
    _, orchestrator, session, _ = _start_gds2_session(monkeypatch)
    event_queue = orchestrator.get_event_queue(session.session_id)
    assert event_queue is not None
    _ = event_queue.get_nowait()

    orchestrator.emit_progress(
        session.session_id,
        RuntimeError("progress update"),
        {
            "detail": RuntimeError("boom"),
            "value": _StableValue(),
            "nested": {"reason": RuntimeError("bad")},
        },
    )

    raw_message = event_queue.get_nowait()
    lines = raw_message.strip().splitlines()
    assert lines[0] == "event: progress"
    payload = json.loads(lines[1].removeprefix("data: "))
    assert payload == {
        "session_id": session.session_id,
        "message": "progress update",
        "detail": "boom",
        "value": "stable-value",
        "nested": {"reason": "bad"},
    }


def test_terminal_session_cleanup_evicts_session_state(monkeypatch):
    _, orchestrator, session, _ = _start_gds2_session(monkeypatch)

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

    monkeypatch.setattr(
        platform_session_orchestrator_module,
        "threading",
        types.SimpleNamespace(Timer=ImmediateTimer),
        raising=False,
    )

    orchestrator.complete_session(session.session_id)

    with pytest.raises(KeyError):
        orchestrator.get_session(session.session_id)
    assert orchestrator.get_event_queue(session.session_id) is None


def test_run_start_diagnostics_rejects_when_worker_is_busy(monkeypatch):
    runtime, orchestrator, session, _ = _start_gds2_session(monkeypatch)
    runtime.start_operation(session.session_id, "navigation")

    backend = MagicMock()
    backend.preflight.return_value = {
        "network_quality": {
            "grade": "good",
            "status": "healthy",
            "reason": "ok",
            "connection_epoch": "epoch-1",
            "sample_count": 5,
            "fresh": True,
            "connected": True,
        },
        "connection_epoch": "epoch-1",
    }

    with pytest.raises(RuntimeError, match="busy"):
        run_start_diagnostics(
            runtime,
            orchestrator=orchestrator,
            backend=backend,
            session_id=session.session_id,
        )

    backend.start.assert_not_called()


def test_run_start_diagnostics_cleans_backend_after_cancel(monkeypatch):
    runtime, orchestrator, session, _ = _start_gds2_session(monkeypatch)
    backend = MagicMock()
    backend.preflight.return_value = {
        "network_quality": {
            "grade": "good",
            "status": "healthy",
            "reason": "ok",
            "connection_epoch": "epoch-1",
            "sample_count": 5,
            "fresh": True,
            "connected": True,
        },
        "connection_epoch": "epoch-1",
    }
    backend.start.side_effect = worker_runtime_module.OperationCancelledError("cancelled")

    with pytest.raises(worker_runtime_module.OperationCancelledError):
        run_start_diagnostics(
            runtime,
            orchestrator=orchestrator,
            backend=backend,
            session_id=session.session_id,
        )

    backend.reset_startup_state.assert_called_once_with()


def test_run_start_diagnostics_cleans_backend_after_failure(monkeypatch):
    runtime, orchestrator, session, _ = _start_gds2_session(monkeypatch)
    backend = MagicMock()
    backend.preflight.return_value = {
        "network_quality": {
            "grade": "good",
            "status": "healthy",
            "reason": "ok",
            "connection_epoch": "epoch-1",
            "sample_count": 5,
            "fresh": True,
            "connected": True,
        },
        "connection_epoch": "epoch-1",
    }
    backend.start.side_effect = RuntimeError("startup failed")

    with pytest.raises(RuntimeError, match="startup failed"):
        run_start_diagnostics(
            runtime,
            orchestrator=orchestrator,
            backend=backend,
            session_id=session.session_id,
        )

    backend.reset_startup_state.assert_called_once_with()


def test_run_start_diagnostics_preserves_device_selection_result(monkeypatch):
    runtime, orchestrator, session, _ = _start_gds2_session(monkeypatch)
    backend = MagicMock()
    backend.preflight.return_value = {
        "network_quality": {
            "grade": "good",
            "status": "healthy",
            "reason": "ok",
            "connection_epoch": "epoch-1",
            "sample_count": 5,
            "fresh": True,
            "connected": True,
        },
        "connection_epoch": "epoch-1",
    }
    backend.start.return_value = {
        "devices": ["VCI Proxy (Remote)", "Bench VCI"],
        "at_device_explorer": True,
        "device_connected": False,
    }
    backend.get_state.return_value = types.SimpleNamespace(extra={})

    payload = run_start_diagnostics(
        runtime,
        orchestrator=orchestrator,
        backend=backend,
        session_id=session.session_id,
    )

    assert payload["success"] is True
    assert payload["result"] == {
        "devices": ["VCI Proxy (Remote)", "Bench VCI"],
        "at_device_explorer": True,
        "device_connected": False,
    }
    backend.get_modules.assert_not_called()


def test_run_start_diagnostics_ignores_non_mapping_state_extra(monkeypatch):
    runtime, orchestrator, session, _ = _start_gds2_session(monkeypatch)
    backend = MagicMock()
    backend.preflight.return_value = {
        "network_quality": {
            "grade": "good",
            "status": "healthy",
            "reason": "ok",
            "connection_epoch": "epoch-1",
            "sample_count": 5,
            "fresh": True,
            "connected": True,
        },
        "connection_epoch": "epoch-1",
    }
    backend.start.return_value = {}
    backend.get_state.return_value = types.SimpleNamespace(extra=["bad"])
    backend.get_modules.return_value = ["Engine", "ABS"]

    payload = run_start_diagnostics(
        runtime,
        orchestrator=orchestrator,
        backend=backend,
        session_id=session.session_id,
    )

    assert payload["success"] is True
    assert payload["result"] == {
        "modules": ["Engine", "ABS"],
        "vin": None,
        "device": None,
    }
    backend.get_modules.assert_called_once_with()
