"""Tests for session_api.py Flask blueprint endpoints.

Uses Flask test_client to verify HTTP contract without network I/O.
Each test resets the module-level orchestrator to guarantee isolation.
"""

import json
import pytest

flask = pytest.importorskip("flask")
Flask = flask.Flask

import session_api
from session_api import (
    get_orchestrator,
    session_bp,
    set_data_viewer_getter,
    set_orchestrator,
)
from src.agentic.planner import (
    BranchDecision,
    BranchDecisionRequiredError,
    DecisionDomain,
    RankedOption,
)
from src.agentic.session_orchestrator import SessionOrchestrator


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def app():
    """Create a minimal Flask app with only the session blueprint."""
    _app = Flask(__name__)
    _app.register_blueprint(session_bp)
    _app.config["TESTING"] = True
    return _app


@pytest.fixture()
def client(app):
    """Fresh test client with a reset orchestrator."""
    set_orchestrator(SessionOrchestrator())
    set_data_viewer_getter(None)
    with app.test_client() as c:
        yield c
    # Restore a fresh orchestrator after each test
    set_orchestrator(SessionOrchestrator())
    set_data_viewer_getter(None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _start_gm(client):
    """Start a GM session, return response JSON."""
    resp = client.post("/api/session/start", json={"brand": "Chevrolet"})
    return resp.get_json()


def _start_unknown(client):
    """Start an unknown-brand session, return response JSON."""
    resp = client.post("/api/session/start", json={"brand": "BMW"})
    return resp.get_json()


# ---------------------------------------------------------------------------
# POST /api/session/start
# ---------------------------------------------------------------------------


class TestSessionStart:
    def test_gm_brand_returns_running(self, client):
        resp = client.post("/api/session/start", json={"brand": "Chevrolet"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["status"] == "running"
        assert data["workflow"] == "gds2"
        assert "session_id" in data

    def test_unknown_brand_returns_awaiting_decision(self, client):
        resp = client.post("/api/session/start", json={"brand": "BMW"})
        data = resp.get_json()
        assert data["success"] is True
        assert data["status"] == "awaiting_decision"
        assert data["workflow"] is None
        assert "decision" in data
        assert len(data["decision"]["options"]) >= 1

    def test_missing_brand_returns_400(self, client):
        resp = client.post("/api/session/start", json={})
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["success"] is False
        assert "brand" in data["error"].lower()

    def test_empty_brand_returns_400(self, client):
        resp = client.post("/api/session/start", json={"brand": "   "})
        assert resp.status_code == 400

    def test_with_model_and_vin(self, client):
        resp = client.post("/api/session/start", json={
            "brand": "GMC",
            "model": "Sierra",
            "vin": "1GT0",
        })
        data = resp.get_json()
        assert data["success"] is True
        assert data["workflow"] == "gds2"

    def test_case_insensitive_brand(self, client):
        resp = client.post("/api/session/start", json={"brand": "cadillac"})
        data = resp.get_json()
        assert data["workflow"] == "gds2"


# ---------------------------------------------------------------------------
# POST /api/session/decision
# ---------------------------------------------------------------------------


class TestSessionDecision:
    def test_submit_valid_decision(self, client):
        start = _start_unknown(client)
        sid = start["session_id"]
        did = start["decision"]["decision_id"]

        resp = client.post("/api/session/decision", json={
            "session_id": sid,
            "decision_id": did,
            "option_id": "gds2",
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["status"] == "running"
        assert data["workflow"] == "gds2"

    def test_submit_manual_option(self, client):
        start = _start_unknown(client)
        resp = client.post("/api/session/decision", json={
            "session_id": start["session_id"],
            "decision_id": start["decision"]["decision_id"],
            "option_id": "manual",
        })
        data = resp.get_json()
        assert data["success"] is True
        assert data["workflow"] == "manual"

    def test_wrong_decision_id_returns_400(self, client):
        start = _start_unknown(client)
        resp = client.post("/api/session/decision", json={
            "session_id": start["session_id"],
            "decision_id": "wrong",
            "option_id": "gds2",
        })
        assert resp.status_code == 400

    def test_invalid_option_returns_400(self, client):
        start = _start_unknown(client)
        resp = client.post("/api/session/decision", json={
            "session_id": start["session_id"],
            "decision_id": start["decision"]["decision_id"],
            "option_id": "nonexistent",
        })
        assert resp.status_code == 400

    def test_missing_session_id_returns_400(self, client):
        resp = client.post("/api/session/decision", json={
            "decision_id": "x",
            "option_id": "y",
        })
        assert resp.status_code == 400

    def test_missing_decision_id_returns_400(self, client):
        resp = client.post("/api/session/decision", json={
            "session_id": "x",
            "option_id": "y",
        })
        assert resp.status_code == 400

    def test_missing_option_id_returns_400(self, client):
        resp = client.post("/api/session/decision", json={
            "session_id": "x",
            "decision_id": "y",
        })
        assert resp.status_code == 400

    def test_nonexistent_session_returns_404(self, client):
        resp = client.post("/api/session/decision", json={
            "session_id": "nonexistent",
            "decision_id": "x",
            "option_id": "y",
        })
        assert resp.status_code == 404

    def test_decision_on_running_session_returns_400(self, client):
        """Submitting a decision when session is already running should fail."""
        start = _start_gm(client)
        resp = client.post("/api/session/decision", json={
            "session_id": start["session_id"],
            "decision_id": "any",
            "option_id": "gds2",
        })
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /api/session/abort
# ---------------------------------------------------------------------------


class TestSessionAbort:
    def test_abort_running(self, client):
        start = _start_gm(client)
        resp = client.post("/api/session/abort", json={
            "session_id": start["session_id"],
            "reason": "User cancelled",
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["status"] == "aborted"

    def test_abort_awaiting_decision(self, client):
        start = _start_unknown(client)
        resp = client.post("/api/session/abort", json={
            "session_id": start["session_id"],
        })
        data = resp.get_json()
        assert data["success"] is True
        assert data["status"] == "aborted"

    def test_double_abort_returns_400(self, client):
        start = _start_gm(client)
        sid = start["session_id"]
        client.post("/api/session/abort", json={"session_id": sid})
        resp = client.post("/api/session/abort", json={"session_id": sid})
        assert resp.status_code == 400

    def test_missing_session_id_returns_400(self, client):
        resp = client.post("/api/session/abort", json={})
        assert resp.status_code == 400

    def test_nonexistent_session_returns_404(self, client):
        resp = client.post("/api/session/abort", json={"session_id": "nope"})
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/session/status
# ---------------------------------------------------------------------------


class TestSessionStatus:
    def test_status_running(self, client):
        start = _start_gm(client)
        resp = client.get(
            f"/api/session/status?session_id={start['session_id']}"
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["status"] == "running"
        assert data["workflow"] == "gds2"
        assert data["context"]["brand"] == "Chevrolet"

    def test_status_awaiting_decision(self, client):
        start = _start_unknown(client)
        resp = client.get(
            f"/api/session/status?session_id={start['session_id']}"
        )
        data = resp.get_json()
        assert data["status"] == "awaiting_decision"
        assert data["pending_decision"] is not None

    def test_missing_session_id_returns_400(self, client):
        resp = client.get("/api/session/status")
        assert resp.status_code == 400

    def test_nonexistent_returns_404(self, client):
        resp = client.get("/api/session/status?session_id=nope")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/session/events — basic checks (SSE generator tested at unit level)
# ---------------------------------------------------------------------------


class TestSessionEvents:
    def test_missing_session_id_returns_400(self, client):
        resp = client.get("/api/session/events")
        assert resp.status_code == 400

    def test_nonexistent_session_returns_404(self, client):
        resp = client.get("/api/session/events?session_id=nope")
        assert resp.status_code == 404

    def test_returns_event_stream_content_type(self, client):
        start = _start_gm(client)
        resp = client.get(
            f"/api/session/events?session_id={start['session_id']}"
        )
        assert resp.content_type.startswith("text/event-stream")


# ---------------------------------------------------------------------------
# End-to-end flows
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_unknown_brand_decide_then_abort(self, client):
        """Start unknown brand -> decide gds2 -> abort."""
        # Start
        start = _start_unknown(client)
        sid = start["session_id"]
        did = start["decision"]["decision_id"]

        # Decide
        resp = client.post("/api/session/decision", json={
            "session_id": sid,
            "decision_id": did,
            "option_id": "gds2",
        })
        data = resp.get_json()
        assert data["status"] == "running"
        assert data["workflow"] == "gds2"

        # Abort
        resp = client.post("/api/session/abort", json={
            "session_id": sid,
            "reason": "done testing",
        })
        data = resp.get_json()
        assert data["status"] == "aborted"

        # Verify via status
        resp = client.get(f"/api/session/status?session_id={sid}")
        data = resp.get_json()
        assert data["status"] == "aborted"
        assert data["error"] == "done testing"

    def test_gm_start_then_status(self, client):
        """GM brand auto-routes; status reflects running."""
        start = _start_gm(client)
        resp = client.get(
            f"/api/session/status?session_id={start['session_id']}"
        )
        data = resp.get_json()
        assert data["status"] == "running"
        assert data["workflow"] == "gds2"
        assert data["pending_decision"] is None


def test_events_emit_decision_timeout_when_expired(client):
    start = _start_unknown(client)
    sid = start["session_id"]

    orch = get_orchestrator()
    session = orch.get_session(sid)
    gate = session.pending_decision
    assert gate is not None
    gate.created_at -= (gate.timeout_sec + 1)

    stream_resp = client.get(f"/api/session/events?session_id={sid}", buffered=False)
    iterator = stream_resp.response

    connected = next(iterator).decode("utf-8")
    timeout_evt = next(iterator).decode("utf-8")

    assert connected.startswith("event: connected")
    assert timeout_evt.startswith("event: decision_timeout")

    stream_resp.close()


class _FakeViewer:
    def __init__(self):
        self._module_ambiguous_once = True
        self.last_module = None

    def select_module(self, module_name: str):
        if module_name == "Engine" and self._module_ambiguous_once:
            self._module_ambiguous_once = False
            decision = BranchDecision(
                domain=DecisionDomain.MODULE,
                target="Engine",
                selected_option=None,
                requires_human=True,
                confidence=0.54,
                ranked_options=[
                    RankedOption("[K20] Engine Control Module", 0.54, ["substring_match"]),
                    RankedOption("[K21] Engine Control Module", 0.51, ["substring_match"]),
                ],
                reason="Ambiguous top candidates",
            )
            raise BranchDecisionRequiredError(
                decision=decision,
                choices=[
                    "[K20] Engine Control Module",
                    "[K21] Engine Control Module",
                ],
            )

        self.last_module = module_name
        return {
            "data_categories": ["Fuel Pressure", "Engine Speed"],
        }

    def select_data_category(self, data_category: str):
        return {
            "monitoring": True,
            "sub_categories": None,
            "selected": data_category,
        }

    def select_sub_module(self, sub_module_name: str):
        return {
            "data_categories": ["Fuel Pressure", "Engine Speed"],
            "sub_module": sub_module_name,
        }

    def select_sub_category(self, sub_category: str):
        return {
            "monitoring": True,
            "sub_category": sub_category,
        }


class TestSessionBranchDecisionLoop:
    def test_module_ambiguity_triggers_decision_and_resumes(self, client):
        fake_viewer = _FakeViewer()
        set_data_viewer_getter(lambda: fake_viewer)

        start = _start_gm(client)
        sid = start["session_id"]

        sel_resp = client.post("/api/session/select_module", json={
            "session_id": sid,
            "module": "Engine",
        })
        assert sel_resp.status_code == 200
        sel_data = sel_resp.get_json()
        assert sel_data["success"] is True
        assert sel_data["decision_required"] is True
        assert sel_data["status"] == "awaiting_decision"
        decision_id = sel_data["decision"]["decision_id"]
        option_id = sel_data["decision"]["options"][0]["option_id"]

        decide_resp = client.post("/api/session/decision", json={
            "session_id": sid,
            "decision_id": decision_id,
            "option_id": option_id,
        })
        assert decide_resp.status_code == 200
        decide_data = decide_resp.get_json()
        assert decide_data["success"] is True
        assert decide_data["status"] == "running"
        assert decide_data["resumed"] is True
        assert decide_data["resume_action"] == "select_module"
        assert decide_data["result"]["data_categories"] == ["Fuel Pressure", "Engine Speed"]
        assert fake_viewer.last_module in (
            "[K20] Engine Control Module",
            "[K21] Engine Control Module",
        )

    def test_select_module_requires_running_session(self, client):
        start = _start_unknown(client)
        sid = start["session_id"]

        resp = client.post("/api/session/select_module", json={
            "session_id": sid,
            "module": "Engine",
        })
        assert resp.status_code == 409


class _FakeViewerSubModuleDecision:
    def __init__(self):
        self._sub_module_ambiguous_once = True
        self.last_sub_module = None

    def select_module(self, module_name: str):
        if module_name == "Engine" and self._sub_module_ambiguous_once:
            self._sub_module_ambiguous_once = False
            decision = BranchDecision(
                domain=DecisionDomain.SUB_MODULE,
                target="Data Display",
                selected_option=None,
                requires_human=True,
                confidence=0.49,
                ranked_options=[
                    RankedOption("Data Display", 0.49, ["label_match"]),
                    RankedOption("Snapshot", 0.47, ["token_overlap"]),
                ],
                reason="Ambiguous sub-module entries",
            )
            raise BranchDecisionRequiredError(
                decision=decision,
                choices=["Data Display", "Snapshot"],
            )

        return {"data_categories": ["Fuel Pressure", "Engine Speed"]}

    def select_sub_module(self, sub_module_name: str):
        self.last_sub_module = sub_module_name
        return {
            "data_categories": ["Fuel Pressure", "Engine Speed"],
            "sub_module": sub_module_name,
        }


class _FakeViewerSubCategoryDecision:
    def __init__(self):
        self._sub_category_ambiguous_once = True
        self.last_sub_category = None

    def select_data_category(self, data_category: str):
        if data_category == "Engine Data" and self._sub_category_ambiguous_once:
            self._sub_category_ambiguous_once = False
            decision = BranchDecision(
                domain=DecisionDomain.SUB_CATEGORY,
                target="Engine Data",
                selected_option=None,
                requires_human=True,
                confidence=0.50,
                ranked_options=[
                    RankedOption("PID Group A", 0.50, ["token_overlap"]),
                    RankedOption("PID Group B", 0.48, ["token_overlap"]),
                ],
                reason="Ambiguous sub-data entries",
            )
            raise BranchDecisionRequiredError(
                decision=decision,
                choices=["PID Group A", "PID Group B"],
            )

        return {
            "monitoring": True,
            "sub_categories": None,
            "selected": data_category,
        }

    def select_sub_category(self, sub_category: str):
        self.last_sub_category = sub_category
        return {
            "monitoring": True,
            "sub_category": sub_category,
        }


class TestSessionNestedBranchResumeActions:
    def test_sub_module_branch_maps_to_select_sub_module(self, client):
        fake_viewer = _FakeViewerSubModuleDecision()
        set_data_viewer_getter(lambda: fake_viewer)

        start = _start_gm(client)
        sid = start["session_id"]

        sel_resp = client.post("/api/session/select_module", json={
            "session_id": sid,
            "module": "Engine",
        })
        assert sel_resp.status_code == 200
        sel_data = sel_resp.get_json()
        assert sel_data["success"] is True
        assert sel_data["decision_required"] is True
        assert sel_data["decision"]["context"]["resume_action"] == "select_sub_module"

        decision_id = sel_data["decision"]["decision_id"]
        option_id = sel_data["decision"]["options"][0]["option_id"]

        decide_resp = client.post("/api/session/decision", json={
            "session_id": sid,
            "decision_id": decision_id,
            "option_id": option_id,
        })
        assert decide_resp.status_code == 200
        decide_data = decide_resp.get_json()
        assert decide_data["success"] is True
        assert decide_data["resumed"] is True
        assert decide_data["resume_action"] == "select_sub_module"
        assert decide_data["result"]["sub_module"] in ("Data Display", "Snapshot")
        assert fake_viewer.last_sub_module in ("Data Display", "Snapshot")

    def test_sub_category_branch_maps_to_select_sub_category(self, client):
        fake_viewer = _FakeViewerSubCategoryDecision()
        set_data_viewer_getter(lambda: fake_viewer)

        start = _start_gm(client)
        sid = start["session_id"]

        sel_resp = client.post("/api/session/select_data_category", json={
            "session_id": sid,
            "data_category": "Engine Data",
        })
        assert sel_resp.status_code == 200
        sel_data = sel_resp.get_json()
        assert sel_data["success"] is True
        assert sel_data["decision_required"] is True
        assert sel_data["decision"]["context"]["resume_action"] == "select_sub_category"

        decision_id = sel_data["decision"]["decision_id"]
        option_id = sel_data["decision"]["options"][0]["option_id"]

        decide_resp = client.post("/api/session/decision", json={
            "session_id": sid,
            "decision_id": decision_id,
            "option_id": option_id,
        })
        assert decide_resp.status_code == 200
        decide_data = decide_resp.get_json()
        assert decide_data["success"] is True
        assert decide_data["resumed"] is True
        assert decide_data["resume_action"] == "select_sub_category"
        assert decide_data["result"]["sub_category"] in ("PID Group A", "PID Group B")
        assert fake_viewer.last_sub_category in ("PID Group A", "PID Group B")
