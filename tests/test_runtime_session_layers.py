import types
from unittest.mock import MagicMock

import pytest

import src.gds2_orchestration.session_orchestrator as session_orchestrator_module
import diagnostic_platform.runtime.worker_runtime as worker_runtime_module
from diagnostic_platform.runtime.navigation_runtime import NavSession, NavSessionStatus
from diagnostic_platform.runtime.session_actions import clear_dtcs
from diagnostic_platform.runtime.session_decisions import build_branch_gate, submit_session_decision
from diagnostic_platform.runtime.session_lifecycle import (
    abort_business_session,
    build_session_status_payload,
    start_business_session,
)
from diagnostic_platform.runtime.session_preflight import run_start_diagnostics
from diagnostic_platform.runtime.session_preflight import build_network_quality_gate
from diagnostic_platform.runtime.worker_runtime import WorkerRuntime
from src.gds2_orchestration.session_orchestrator import SessionContext, SessionOrchestrator


def _start_gds2_session(monkeypatch):
    runtime = WorkerRuntime()
    orchestrator = SessionOrchestrator()
    monkeypatch.setattr(session_orchestrator_module, "route_backend", lambda brand: "gds2")
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
        get_data_viewer=lambda: MagicMock(),
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
    viewer = MagicMock()
    viewer.select_module.return_value = {"selected_module": "ECM-B"}
    runtime.set_data_viewer_getter(lambda: viewer)

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
        get_data_viewer=lambda: viewer,
        get_backend=lambda: MagicMock(),
    )

    assert result["success"] is True
    assert result["resumed"] is True
    assert result["resume_action"] == "select_module"
    assert result["selected_choice"] == "ECM-B"
    assert session.selected_module == "ECM-B"
    assert session.selected_data_category == ""
    viewer.select_module.assert_called_once_with("ECM-B")


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
