"""Tests for session_api.py Flask blueprint endpoints.

Uses Flask test_client to verify HTTP contract without network I/O.
Each test resets the module-level orchestrator to guarantee isolation.
"""

import json
import queue
from types import SimpleNamespace
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


def _make_network_quality(
    grade: str,
    *,
    epoch: str = "epoch-1",
    p95: float | None = None,
    reason: str | None = None,
) -> dict[str, object]:
    metrics = {
        "good": {"last": 54.0, "p50": 56.0, "p95": 60.0},
        "warn": {"last": 102.0, "p50": 110.0, "p95": 120.0},
        "block": {"last": 182.0, "p50": 176.0, "p95": 190.0},
    }[grade]
    if p95 is not None:
        metrics["p95"] = p95
    statuses = {
        "good": "healthy",
        "warn": "degraded",
        "block": "blocked",
    }
    reasons = {
        "good": "p95 within good threshold",
        "warn": "p95 above good threshold",
        "block": "p95 above warn threshold",
    }
    return {
        "connection_epoch": epoch,
        "connected": True,
        "fresh": True,
        "updated_at": "2026-03-27T00:00:00Z",
        "source": "probe",
        "sample_count": 5,
        "network_ms": metrics,
        "grade": grade,
        "status": statuses[grade],
        "reason": reason or reasons[grade],
        "probe_failures": 0,
    }


class _FakeBackendWithQuality:
    def __init__(self, quality: dict[str, object]):
        self.quality = quality
        self.start_calls = 0
        self.modules_calls = 0

    def preflight(self) -> dict[str, object]:
        return {
            "network_quality": self.quality,
            "connection_epoch": self.quality.get("connection_epoch"),
        }

    def start(self) -> None:
        self.start_calls += 1

    def get_state(self):
        return SimpleNamespace(extra={"vin": "VIN123", "device": "VCI Proxy (Remote)"})

    def get_modules(self) -> list[str]:
        self.modules_calls += 1
        return ["ECM", "TCM"]


class _FakeBackendWithStartResult(_FakeBackendWithQuality):
    def __init__(self, quality: dict[str, object], start_result: dict[str, object]):
        super().__init__(quality)
        self.start_result = start_result

    def start(self) -> dict[str, object]:
        self.start_calls += 1
        return dict(self.start_result)

    def get_modules(self) -> list[str]:
        self.modules_calls += 1
        raise AssertionError("session_start_diagnostics should reuse modules returned by start()")


def _patch_backend(monkeypatch: pytest.MonkeyPatch, backend: object) -> None:
    monkeypatch.setattr(session_api, "_backend", backend)
    monkeypatch.setattr(session_api, "_get_backend", lambda: backend)


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


class TestSessionNetworkGate:
    def test_blocked_quality_requires_explicit_decision(self, client, monkeypatch):
        start = _start_gm(client)
        backend = _FakeBackendWithQuality(_make_network_quality("block", epoch="epoch-block"))
        _patch_backend(monkeypatch, backend)

        resp = client.post("/api/session/start_diagnostics", json={"session_id": start["session_id"]})

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["decision_required"] is True
        assert data["status"] == "awaiting_decision"
        assert data["network_quality"]["grade"] == "block"
        assert data["decision"]["kind"] == "network_quality"
        option_ids = [opt["option_id"] for opt in data["decision"]["options"]]
        assert option_ids == ["continue_anyway", "cancel"]
        assert backend.start_calls == 0

    def test_continue_anyway_resumes_start_and_stores_override(self, client, monkeypatch):
        start = _start_gm(client)
        backend = _FakeBackendWithQuality(_make_network_quality("block", epoch="epoch-1"))
        _patch_backend(monkeypatch, backend)

        gate_resp = client.post("/api/session/start_diagnostics", json={"session_id": start["session_id"]})
        gate_payload = gate_resp.get_json()
        decision_id = gate_payload["decision"]["decision_id"]

        resp = client.post("/api/session/decision", json={
            "session_id": start["session_id"],
            "decision_id": decision_id,
            "option_id": "continue_anyway",
        })

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["resumed"] is True
        assert data["result"]["modules"] == ["ECM", "TCM"]
        assert data["network_quality"]["grade"] == "block"
        assert data["network_override"]["allowed"] is True
        assert data["network_override"]["connection_epoch"] == "epoch-1"
        assert backend.start_calls == 1

    def test_cancel_leaves_session_running_without_starting(self, client, monkeypatch):
        start = _start_gm(client)
        backend = _FakeBackendWithQuality(_make_network_quality("block", epoch="epoch-1"))
        _patch_backend(monkeypatch, backend)

        gate_resp = client.post("/api/session/start_diagnostics", json={"session_id": start["session_id"]})
        gate_payload = gate_resp.get_json()

        resp = client.post("/api/session/decision", json={
            "session_id": start["session_id"],
            "decision_id": gate_payload["decision"]["decision_id"],
            "option_id": "cancel",
        })

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["cancelled"] is True
        assert data["status"] == "running"
        assert backend.start_calls == 0

    def test_warn_quality_starts_and_returns_quality(self, client, monkeypatch):
        start = _start_gm(client)
        backend = _FakeBackendWithQuality(_make_network_quality("warn", epoch="epoch-warn"))
        _patch_backend(monkeypatch, backend)

        resp = client.post("/api/session/start_diagnostics", json={"session_id": start["session_id"]})

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["result"]["modules"] == ["ECM", "TCM"]
        assert data["network_quality"]["grade"] == "warn"
        assert data["connection_epoch"] == "epoch-warn"
        assert backend.start_calls == 1

    def test_warn_quality_reuses_start_result_without_second_module_read(self, client, monkeypatch):
        start = _start_gm(client)
        backend = _FakeBackendWithStartResult(
            _make_network_quality("warn", epoch="epoch-warn"),
            {
                "modules": ["ECM", "TCM"],
                "vin": "VIN123",
                "device": "VCI Proxy (Remote)",
            },
        )
        _patch_backend(monkeypatch, backend)

        resp = client.post("/api/session/start_diagnostics", json={"session_id": start["session_id"]})

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["result"]["modules"] == ["ECM", "TCM"]
        assert data["result"]["vin"] == "VIN123"
        assert data["result"]["device"] == "VCI Proxy (Remote)"
        assert data["network_quality"]["grade"] == "warn"
        assert backend.start_calls == 1
        assert backend.modules_calls == 0

    def test_status_reports_network_fields_and_invalidates_override_on_epoch_change(
        self,
        client,
        monkeypatch,
    ):
        start = _start_gm(client)
        backend = _FakeBackendWithQuality(_make_network_quality("block", epoch="epoch-1"))
        _patch_backend(monkeypatch, backend)

        gate_resp = client.post("/api/session/start_diagnostics", json={"session_id": start["session_id"]})
        decision_id = gate_resp.get_json()["decision"]["decision_id"]
        client.post("/api/session/decision", json={
            "session_id": start["session_id"],
            "decision_id": decision_id,
            "option_id": "continue_anyway",
        })

        backend.quality = _make_network_quality("block", epoch="epoch-2", reason="new tunnel epoch")

        resp = client.get(f"/api/session/status?session_id={start['session_id']}")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["network_quality"]["grade"] == "block"
        assert data["connection_epoch"] == "epoch-2"
        assert data["network_override"] is None


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


class _FakeAIEngine:
    def __init__(self):
        self.is_active = False
        self.start_calls: list[tuple[dict[str, object], object]] = []
        self.retry_calls: list[tuple[str, dict[str, object]]] = []
        self.event_queues: dict[str, queue.Queue[str]] = {}

    def start_session(self, vehicle_context: dict[str, object], collection_guard=None) -> str:
        self.start_calls.append((vehicle_context, collection_guard))
        session_id = "ai-session-1"
        self.event_queues[session_id] = queue.Queue()
        return session_id

    def retry_with_cached(self, payload_id: str, vehicle_context: dict[str, object]) -> str:
        self.retry_calls.append((payload_id, vehicle_context))
        session_id = "ai-session-2"
        self.event_queues[session_id] = queue.Queue()
        return session_id

    def get_event_queue(self, session_id: str):
        return self.event_queues.get(session_id)


class TestSessionAIDiagnose:
    def test_ai_diagnose_starts_under_business_session(self, client, monkeypatch):
        start = _start_gm(client)
        sid = start["session_id"]
        engine = _FakeAIEngine()

        monkeypatch.setattr(session_api, "_get_ai_engine", lambda: engine)
        monkeypatch.setattr(
            session_api,
            "_make_ai_collection_guard",
            lambda data_category: {"guard_for": data_category},
        )

        resp = client.post("/api/session/ai_diagnose", json={
            "session_id": sid,
            "module": "ECM",
            "data_category": "Engine Data",
            "vin": "VIN123",
        })

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["session_id"] == sid
        assert data["ai_session_id"] == "ai-session-1"
        assert engine.start_calls == [
            (
                {
                    "vin": "VIN123",
                    "module": "ECM",
                    "data_category": "Engine Data",
                },
                {"guard_for": "Engine Data"},
            )
        ]
        assert get_orchestrator().get_session(sid).active_ai_session_id == "ai-session-1"

    def test_ai_diagnose_events_use_business_session_mapping(self, client, monkeypatch):
        start = _start_gm(client)
        sid = start["session_id"]
        engine = _FakeAIEngine()
        events = queue.Queue()
        events.put('event: progress\ndata: {"message":"collecting"}\n\n')
        events.put('event: done\ndata: {"ok":true}\n\n')
        engine.event_queues["ai-session-1"] = events

        monkeypatch.setattr(session_api, "_get_ai_engine", lambda: engine)
        get_orchestrator().get_session(sid).active_ai_session_id = "ai-session-1"

        resp = client.get(f"/api/session/ai_diagnose/events?session_id={sid}", buffered=False)
        iterator = resp.response

        connected = next(iterator).decode("utf-8")
        progress = next(iterator).decode("utf-8")
        done = next(iterator).decode("utf-8")

        assert connected.startswith("event: connected")
        assert progress.startswith("event: progress")
        assert done.startswith("event: done")

        resp.close()

    def test_ai_diagnose_retry_starts_under_business_session(self, client, monkeypatch):
        start = _start_gm(client)
        sid = start["session_id"]
        engine = _FakeAIEngine()

        monkeypatch.setattr(session_api, "_get_ai_engine", lambda: engine)

        resp = client.post("/api/session/ai_diagnose/retry", json={
            "session_id": sid,
            "cached_payload_id": "payload-1",
            "module": "ECM",
            "data_category": "Engine Data",
            "vin": "VIN123",
        })

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["session_id"] == sid
        assert data["ai_session_id"] == "ai-session-2"
        assert engine.retry_calls == [
            (
                "payload-1",
                {
                    "vin": "VIN123",
                    "module": "ECM",
                    "data_category": "Engine Data",
                },
            )
        ]
        assert get_orchestrator().get_session(sid).active_ai_session_id == "ai-session-2"


class _FakeNavStatus:
    def __init__(self, value: str):
        self.value = value


class _FakeNavSession:
    def __init__(self, session_id: str, goal: str):
        self.session_id = session_id
        self.goal = goal
        self.status = _FakeNavStatus("running")
        self.event_queue: queue.Queue = queue.Queue()
        self.decision_queue: queue.Queue = queue.Queue()
        self.thread = None
        self.final_state = None
        self.current_page = ""
        self.pending_decision_id = None
        self.pending_items = []
        self.error = None


class TestSessionFacadeConvergence:
    def test_session_dtcs_reads_via_explicit_facade(self, client, monkeypatch):
        start = _start_gm(client)
        sid = start["session_id"]

        backend = SimpleNamespace(
            get_state=lambda: SimpleNamespace(current_module="", current_data_category="", extra={}),
            detect_current_page=lambda: "data_display",
            select_module=lambda module: None,
            select_data_category=lambda category: None,
            read_dtcs=lambda: [
                SimpleNamespace(
                    code="P0001",
                    module="ECM",
                    status="Current",
                    description="Fuel Volume Regulator",
                    source_backend="gds2",
                )
            ],
        )
        _patch_backend(monkeypatch, backend)

        resp = client.post("/api/session/dtcs", json={
            "session_id": sid,
            "module": "ECM",
            "data_category": "Engine Data",
        })

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["session_id"] == sid
        assert data["result"]["dtc_count"] == 1
        assert data["result"]["dtcs"][0]["code"] == "P0001"

    def test_session_live_data_start_uses_session_namespace(self, client, monkeypatch):
        start = _start_gm(client)
        sid = start["session_id"]
        calls: list[tuple[str, int]] = []

        import diagnostics_api

        monkeypatch.setattr(
            diagnostics_api,
            "start_live_data_stream",
            lambda category, interval_ms=100: calls.append((category, interval_ms)) or {
                "success": True,
                "message": "Live data streaming started",
                "interval_ms": interval_ms,
            },
        )

        resp = client.post("/api/session/live_data/start", json={
            "session_id": sid,
            "module": "ECM",
            "data_category": "Engine Data",
            "interval_ms": 150,
        })

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["session_id"] == sid
        assert calls == [("Engine Data", 150)]
        assert get_orchestrator().get_session(sid).live_data_active is True

    def test_session_live_data_stop_uses_session_namespace(self, client, monkeypatch):
        start = _start_gm(client)
        sid = start["session_id"]
        get_orchestrator().get_session(sid).live_data_active = True
        calls: list[str] = []

        import diagnostics_api

        monkeypatch.setattr(
            diagnostics_api,
            "stop_live_data_stream",
            lambda: calls.append("stop") or {"success": True, "message": "Live data stopped"},
        )

        resp = client.post("/api/session/live_data/stop", json={"session_id": sid})

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["session_id"] == sid
        assert calls == ["stop"]
        assert get_orchestrator().get_session(sid).live_data_active is False

    def test_session_navigate_start_binds_internal_navigation(self, client, monkeypatch):
        start = _start_gm(client)
        sid = start["session_id"]
        fake_nav = _FakeNavSession("nav-1", "Navigate to Data Display")

        import navigate_api

        monkeypatch.setattr(navigate_api, "start_navigation_session", lambda goal: fake_nav)

        resp = client.post("/api/session/navigate/start", json={
            "session_id": sid,
            "goal": "Navigate to Data Display",
        })

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["session_id"] == sid
        assert data["navigation_session_id"] == "nav-1"
        assert get_orchestrator().get_session(sid).active_navigation_session_id == "nav-1"

    def test_session_navigate_decision_maps_business_session_to_internal_navigation(
        self,
        client,
        monkeypatch,
    ):
        start = _start_gm(client)
        sid = start["session_id"]
        get_orchestrator().get_session(sid).active_navigation_session_id = "nav-1"
        calls: list[tuple[str, str, str]] = []

        import navigate_api

        monkeypatch.setattr(
            navigate_api,
            "submit_navigation_decision",
            lambda session_id, decision_id="", selected_item="": (
                calls.append((session_id, decision_id, selected_item)) or {
                    "success": True,
                    "session_id": session_id,
                    "selected_item": selected_item,
                }
            ),
        )

        resp = client.post("/api/session/navigate/decision", json={
            "session_id": sid,
            "decision_id": "d1",
            "selected_item": "Engine Data",
        })

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["session_id"] == sid
        assert calls == [("nav-1", "d1", "Engine Data")]

    def test_session_abort_stops_active_subflows(self, client, monkeypatch):
        start = _start_gm(client)
        sid = start["session_id"]
        session = get_orchestrator().get_session(sid)
        session.active_navigation_session_id = "nav-1"
        session.active_ai_session_id = "ai-1"
        session.live_data_active = True

        nav_calls: list[str] = []
        live_calls: list[str] = []

        import navigate_api
        import diagnostics_api

        monkeypatch.setattr(
            navigate_api,
            "abort_navigation_session",
            lambda nav_session_id: nav_calls.append(nav_session_id) or {
                "success": True,
                "session_id": nav_session_id,
                "status": "aborted",
            },
        )
        monkeypatch.setattr(
            diagnostics_api,
            "stop_live_data_stream",
            lambda: live_calls.append("stop") or {"success": True, "message": "Live data stopped"},
        )

        class _AbortableAIEngine(_FakeAIEngine):
            def abort_session(self, session_id: str) -> bool:
                self.retry_calls.append(("abort", {"session_id": session_id}))
                return True

        engine = _AbortableAIEngine()
        monkeypatch.setattr(session_api, "_get_ai_engine", lambda: engine)

        resp = client.post("/api/session/abort", json={"session_id": sid, "reason": "test"})

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["status"] == "aborted"
        assert nav_calls == ["nav-1"]
        assert live_calls == ["stop"]
        assert engine.retry_calls == [("abort", {"session_id": "ai-1"})]


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
