"""Tests for SessionOrchestrator — in-memory session lifecycle, routing, decisions."""

# pyright: reportOptionalMemberAccess=false

import json
import pytest

from src.agentic.session_orchestrator import (
    DecisionGate,
    DecisionOption,
    Session,
    SessionContext,
    SessionEventType,
    SessionOrchestrator,
    SessionStatus,
    route_workflow,
    sse_event,
)


# ---------------------------------------------------------------------------
# SSE helper
# ---------------------------------------------------------------------------


class TestSseEvent:
    def test_format(self):
        msg = sse_event("progress", {"key": "value"})
        assert msg.startswith("event: progress\n")
        assert msg.endswith("\n\n")
        lines = msg.strip().split("\n")
        assert lines[0] == "event: progress"
        payload = json.loads(lines[1].removeprefix("data: "))
        assert payload == {"key": "value"}

    def test_unicode(self):
        msg = sse_event("progress", {"text": "发动机"})
        assert "发动机" in msg


# ---------------------------------------------------------------------------
# Brand routing
# ---------------------------------------------------------------------------


class TestRouteWorkflow:
    @pytest.mark.parametrize("brand", [
        "Chevrolet", "chevrolet", "CHEVROLET",
        "buick", "GMC", "cadillac", "Holden", "Opel",
    ])
    def test_gm_brands_route_to_gds2(self, brand):
        assert route_workflow(brand) == "gds2"

    @pytest.mark.parametrize("brand", ["BMW", "Toyota", "Mercedes", "unknown"])
    def test_unknown_brands_return_none(self, brand):
        assert route_workflow(brand) is None

    def test_whitespace_stripped(self):
        assert route_workflow("  chevrolet  ") == "gds2"


# ---------------------------------------------------------------------------
# Dataclass validation
# ---------------------------------------------------------------------------


class TestDataclassValidation:
    def test_session_context_empty_brand_raises(self):
        with pytest.raises(ValueError, match="brand cannot be empty"):
            SessionContext(brand="")

    def test_decision_option_empty_id_raises(self):
        with pytest.raises(ValueError, match="option_id cannot be empty"):
            DecisionOption(option_id="", label="x")

    def test_decision_option_empty_label_raises(self):
        with pytest.raises(ValueError, match="label cannot be empty"):
            DecisionOption(option_id="a", label="")

    def test_decision_gate_no_options_raises(self):
        with pytest.raises(ValueError, match="options must have at least one"):
            DecisionGate(decision_id="d1", prompt="pick", options=[])

    def test_decision_gate_bad_timeout_raises(self):
        opt = DecisionOption(option_id="a", label="A")
        with pytest.raises(ValueError, match="timeout_sec must be > 0"):
            DecisionGate(decision_id="d1", prompt="pick", options=[opt], timeout_sec=0)

    def test_session_context_to_dict(self):
        ctx = SessionContext(brand="Chevrolet", model="Malibu", vin="123")
        d = ctx.to_dict()
        assert d["brand"] == "Chevrolet"
        assert d["model"] == "Malibu"

    def test_decision_gate_to_dict(self):
        opt = DecisionOption(option_id="gds2", label="GDS2", description="desc")
        gate = DecisionGate(decision_id="d1", prompt="pick one", options=[opt])
        d = gate.to_dict()
        assert d["decision_id"] == "d1"
        assert len(d["options"]) == 1
        assert d["options"][0]["option_id"] == "gds2"


# ---------------------------------------------------------------------------
# SessionOrchestrator — happy paths
# ---------------------------------------------------------------------------


class TestOrchestratorStartGM:
    """GM-like brands should route to gds2 automatically."""

    def setup_method(self):
        self.orch = SessionOrchestrator()

    def test_start_chevrolet_routes_gds2(self):
        ctx = SessionContext(brand="Chevrolet")
        session = self.orch.start_session(ctx)
        assert session.status == SessionStatus.RUNNING
        assert session.workflow == "gds2"
        assert session.pending_decision is None

    def test_start_emits_progress_event(self):
        ctx = SessionContext(brand="Buick")
        session = self.orch.start_session(ctx)
        q = self.orch.get_event_queue(session.session_id)
        assert q is not None
        msg = q.get_nowait()
        assert msg.startswith("event: progress\n")
        payload = json.loads(msg.split("\n")[1].removeprefix("data: "))
        assert payload["workflow"] == "gds2"

    def test_session_id_is_16_hex(self):
        ctx = SessionContext(brand="GMC")
        session = self.orch.start_session(ctx)
        assert len(session.session_id) == 16
        int(session.session_id, 16)  # should not raise


class TestOrchestratorStartUnknown:
    """Unknown brands should trigger decision_required."""

    def setup_method(self):
        self.orch = SessionOrchestrator()

    def test_start_unknown_brand_awaits_decision(self):
        ctx = SessionContext(brand="BMW")
        session = self.orch.start_session(ctx)
        assert session.status == SessionStatus.AWAITING_DECISION
        assert session.workflow is None
        assert session.pending_decision is not None
        assert len(session.pending_decision.options) >= 1

    def test_start_unknown_emits_decision_required(self):
        ctx = SessionContext(brand="Toyota")
        session = self.orch.start_session(ctx)
        q = self.orch.get_event_queue(session.session_id)
        msg = q.get_nowait()
        assert msg.startswith("event: decision_required\n")


# ---------------------------------------------------------------------------
# Decision flow
# ---------------------------------------------------------------------------


class TestDecisionFlow:
    def setup_method(self):
        self.orch = SessionOrchestrator()
        ctx = SessionContext(brand="BMW")
        self.session = self.orch.start_session(ctx)
        self.sid = self.session.session_id
        # drain the decision_required event
        self.orch.get_event_queue(self.sid).get_nowait()

    def test_submit_valid_decision(self):
        gate = self.session.pending_decision
        session = self.orch.submit_decision(
            self.sid, gate.decision_id, "gds2"
        )
        assert session.status == SessionStatus.RUNNING
        assert session.workflow == "gds2"
        assert session.pending_decision is None
        assert len(session.resolved_decisions) == 1

    def test_submit_emits_resolved_and_progress(self):
        gate = self.session.pending_decision
        self.orch.submit_decision(self.sid, gate.decision_id, "gds2")
        q = self.orch.get_event_queue(self.sid)
        msg1 = q.get_nowait()
        msg2 = q.get_nowait()
        assert msg1.startswith("event: decision_resolved\n")
        assert msg2.startswith("event: progress\n")

    def test_wrong_decision_id_raises(self):
        with pytest.raises(ValueError, match="Decision ID mismatch"):
            self.orch.submit_decision(self.sid, "wrong_id", "gds2")

    def test_invalid_option_id_raises(self):
        gate = self.session.pending_decision
        with pytest.raises(ValueError, match="Invalid option_id"):
            self.orch.submit_decision(self.sid, gate.decision_id, "nonexistent")

    def test_submit_when_not_awaiting_raises(self):
        gate = self.session.pending_decision
        self.orch.submit_decision(self.sid, gate.decision_id, "gds2")
        with pytest.raises(ValueError, match="not awaiting a decision"):
            self.orch.submit_decision(self.sid, gate.decision_id, "gds2")

    def test_branch_decision_does_not_override_workflow(self):
        # Move session to running workflow first
        gate = self.session.pending_decision
        self.orch.submit_decision(self.sid, gate.decision_id, "gds2")
        # drain events
        q = self.orch.get_event_queue(self.sid)
        q.get_nowait()
        q.get_nowait()

        branch_gate = DecisionGate(
            decision_id="branch-1",
            prompt="Pick module",
            options=[
                DecisionOption(option_id="branch_0", label="[K20] Engine"),
                DecisionOption(option_id="branch_1", label="[K21] Engine"),
            ],
            kind="branch",
        )
        self.orch.raise_decision(self.sid, branch_gate)
        self.orch.submit_decision(self.sid, "branch-1", "branch_0")

        session = self.orch.get_session(self.sid)
        assert session.workflow == "gds2"


# ---------------------------------------------------------------------------
# Abort
# ---------------------------------------------------------------------------


class TestAbort:
    def setup_method(self):
        self.orch = SessionOrchestrator()
        ctx = SessionContext(brand="Chevrolet")
        self.session = self.orch.start_session(ctx)
        self.sid = self.session.session_id
        # drain progress event
        self.orch.get_event_queue(self.sid).get_nowait()

    def test_abort_sets_status(self):
        session = self.orch.abort_session(self.sid, "user cancel")
        assert session.status == SessionStatus.ABORTED
        assert session.error == "user cancel"

    def test_abort_emits_done_event(self):
        self.orch.abort_session(self.sid)
        q = self.orch.get_event_queue(self.sid)
        msg = q.get_nowait()
        assert msg.startswith("event: done\n")
        payload = json.loads(msg.split("\n")[1].removeprefix("data: "))
        assert payload["aborted"] is True

    def test_abort_default_reason(self):
        session = self.orch.abort_session(self.sid)
        assert session.error == "Aborted by user"

    def test_abort_already_aborted_raises(self):
        self.orch.abort_session(self.sid)
        with pytest.raises(ValueError, match="terminal state"):
            self.orch.abort_session(self.sid)

    def test_abort_awaiting_decision(self):
        orch = SessionOrchestrator()
        ctx = SessionContext(brand="BMW")
        session = orch.start_session(ctx)
        assert session.status == SessionStatus.AWAITING_DECISION
        result = orch.abort_session(session.session_id)
        assert result.status == SessionStatus.ABORTED
        assert result.pending_decision is None


# ---------------------------------------------------------------------------
# Complete / Fail / Progress
# ---------------------------------------------------------------------------


class TestCompleteAndFail:
    def setup_method(self):
        self.orch = SessionOrchestrator()
        ctx = SessionContext(brand="Chevrolet")
        self.session = self.orch.start_session(ctx)
        self.sid = self.session.session_id
        self.orch.get_event_queue(self.sid).get_nowait()

    def test_complete(self):
        session = self.orch.complete_session(self.sid, {"dtcs": 0})
        assert session.status == SessionStatus.COMPLETED
        q = self.orch.get_event_queue(self.sid)
        msg = q.get_nowait()
        assert msg.startswith("event: done\n")

    def test_fail_emits_error_and_done(self):
        session = self.orch.fail_session(self.sid, "timeout")
        assert session.status == SessionStatus.FAILED
        assert session.error == "timeout"
        q = self.orch.get_event_queue(self.sid)
        msg1 = q.get_nowait()
        msg2 = q.get_nowait()
        assert msg1.startswith("event: error\n")
        assert msg2.startswith("event: done\n")

    def test_emit_progress(self):
        self.orch.emit_progress(self.sid, "Step 1 done", {"step": 1})
        q = self.orch.get_event_queue(self.sid)
        msg = q.get_nowait()
        assert msg.startswith("event: progress\n")
        payload = json.loads(msg.split("\n")[1].removeprefix("data: "))
        assert payload["step"] == 1


# ---------------------------------------------------------------------------
# Raise decision mid-workflow
# ---------------------------------------------------------------------------


class TestRaiseDecision:
    def setup_method(self):
        self.orch = SessionOrchestrator()
        ctx = SessionContext(brand="Chevrolet")
        self.session = self.orch.start_session(ctx)
        self.sid = self.session.session_id
        self.orch.get_event_queue(self.sid).get_nowait()

    def test_raise_decision_changes_status(self):
        gate = DecisionGate(
            decision_id="mid1",
            prompt="Which module?",
            options=[
                DecisionOption(option_id="eng", label="Engine"),
                DecisionOption(option_id="abs", label="ABS"),
            ],
        )
        session = self.orch.raise_decision(self.sid, gate)
        assert session.status == SessionStatus.AWAITING_DECISION
        assert session.pending_decision is gate

    def test_raise_decision_not_running_raises(self):
        self.orch.abort_session(self.sid)
        gate = DecisionGate(
            decision_id="mid2",
            prompt="Which module?",
            options=[DecisionOption(option_id="x", label="X")],
        )
        with pytest.raises(ValueError, match="Cannot raise decision"):
            self.orch.raise_decision(self.sid, gate)


# ---------------------------------------------------------------------------
# get_session / get_event_queue edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_get_nonexistent_session_raises(self):
        orch = SessionOrchestrator()
        with pytest.raises(KeyError, match="not found"):
            orch.get_session("nonexistent")

    def test_get_event_queue_nonexistent_returns_none(self):
        orch = SessionOrchestrator()
        assert orch.get_event_queue("nonexistent") is None

    def test_session_to_dict(self):
        orch = SessionOrchestrator()
        ctx = SessionContext(brand="Chevrolet")
        session = orch.start_session(ctx)
        d = session.to_dict()
        assert d["session_id"] == session.session_id
        assert d["status"] == "running"
        assert d["workflow"] == "gds2"
        assert d["context"]["brand"] == "Chevrolet"


class TestDecisionTimeout:
    def setup_method(self):
        self.orch = SessionOrchestrator()
        ctx = SessionContext(brand="BMW")
        self.session = self.orch.start_session(ctx)
        self.sid = self.session.session_id
        # drain initial decision_required event
        self.orch.get_event_queue(self.sid).get_nowait()

    def test_timeout_applies_fallback_and_resolves(self):
        gate = self.session.pending_decision
        assert gate is not None
        gate.created_at -= (gate.timeout_sec + 1)

        handled = self.orch.check_decision_timeout(self.sid)
        assert handled is True

        session = self.orch.get_session(self.sid)
        assert session.status == SessionStatus.RUNNING
        assert session.workflow == "manual"
        assert session.pending_decision is None
        assert len(session.resolved_decisions) == 1
        assert session.resolved_decisions[0]["source"] == "timeout"

        q = self.orch.get_event_queue(self.sid)
        m1 = q.get_nowait()
        m2 = q.get_nowait()
        m3 = q.get_nowait()
        assert m1.startswith("event: decision_timeout")
        assert m2.startswith("event: decision_resolved")
        assert m3.startswith("event: progress")

    def test_timeout_noop_when_not_awaiting_decision(self):
        gm = self.orch.start_session(SessionContext(brand="Chevrolet"))
        # drain progress
        self.orch.get_event_queue(gm.session_id).get_nowait()
        assert self.orch.check_decision_timeout(gm.session_id) is False

