import importlib
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import src.gds2_orchestration.session_orchestrator as session_orchestrator_module
from src.gds2_orchestration.planner import (
    BranchDecision,
    BranchDecisionRequiredError,
    DecisionDomain,
)
from diagnostic_platform.contracts import (
    BackendCapability,
    BackendRegistry,
    BackendState,
    ClearResult,
    DTC,
    DiagnosticBackend,
)
from diagnostic_platform.runtime.session_actions import (
    clear_dtcs as clear_session_dtcs,
    read_dtcs,
    select_data_category_action,
    select_module_action,
)
from diagnostic_platform.runtime.session_lifecycle import (
    build_session_status_payload,
)
from diagnostic_platform.runtime.worker_runtime import WorkerRuntime
from server.api import session_dependencies
from server.api import session_navigation_handlers
from src.gds2_orchestration.session_orchestrator import (
    Session,
    SessionContext,
    SessionOrchestrator,
    SessionStatus,
)


class FakeCoreBackend(DiagnosticBackend):
    def __init__(self, name: str, brands: list[str], capabilities: list[BackendCapability]):
        self._name = name
        self._brands = brands
        self._capabilities = capabilities
        self._selected_module = ""
        self._selected_category = ""

    @property
    def name(self) -> str:
        return self._name

    @property
    def supported_brands(self) -> list[str]:
        return list(self._brands)

    @property
    def capabilities(self) -> list[BackendCapability]:
        return list(self._capabilities)

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def connect_vci(self, device: str) -> None:
        return None

    def get_modules(self) -> list[str]:
        return ["ECM", "BCM"]

    def select_module(self, module: str) -> None:
        self._selected_module = module

    def get_data_categories(self) -> list[str]:
        return ["DTC", "Live Data"]

    def go_back(self) -> None:
        raise NotImplementedError("not part of the fake core backend")

    def detect_current_page(self) -> str:
        raise NotImplementedError("not part of the fake core backend")

    def execute_action(
        self,
        action: str,
        args: dict | None = None,
        timeout_sec: float = 30.0,
    ) -> dict:
        raise NotImplementedError("not part of the fake core backend")

    def select_data_category(self, category: str) -> list[str]:
        self._selected_category = category
        return []

    def read_dtcs(self) -> list[DTC]:
        return [
            DTC(
                code="P0001",
                module=self._selected_module or "ECM",
                status="active",
                description="fake fault",
                source_backend=self._name,
            )
        ]

    def start_live_data(self):
        raise NotImplementedError

    def stop_live_data(self) -> None:
        raise NotImplementedError

    def clear_dtcs(self):
        return ClearResult(
            success=True,
            cleared_count=1,
            message="Cleared by fake core backend",
        )

    def get_state(self) -> BackendState:
        return BackendState(
            current_page="module_list",
            is_connected=True,
            current_module=self._selected_module or None,
            current_data_category=self._selected_category or None,
            extra={},
        )


def _backend(
    name: str,
    *,
    brands: list[str],
    capabilities: list[BackendCapability] | None = None,
) -> FakeCoreBackend:
    if capabilities is None:
        capabilities = [
            BackendCapability.CORE_SESSION,
            BackendCapability.READ_DTCS,
        ]
    return FakeCoreBackend(name=name, brands=brands, capabilities=capabilities)


def _install_fake_flask_stack(monkeypatch):
    class FakeBlueprint:
        def __init__(self, name, import_name, url_prefix=""):
            self.name = name
            self.import_name = import_name
            self.url_prefix = url_prefix
            self._registered_routes = []

        def route(self, path, methods=None):
            def decorator(fn):
                self._registered_routes.append(path)
                return fn

            return decorator

    fake_flask = types.ModuleType("flask")
    fake_flask.Blueprint = FakeBlueprint
    fake_flask.Response = object
    fake_flask.jsonify = lambda payload=None, **kwargs: payload if payload is not None else kwargs
    fake_flask.request = types.SimpleNamespace(args={}, json=None)
    monkeypatch.setitem(sys.modules, "flask", fake_flask)


def test_start_session_sets_backend_metadata_for_unique_brand(monkeypatch):
    registry = BackendRegistry()
    registry.register(_backend("fake-alpha", brands=["alpha"]))
    orchestrator = SessionOrchestrator(registry_provider=lambda: registry)
    session = orchestrator.start_session(SessionContext(brand="alpha"))

    assert session.status == SessionStatus.RUNNING
    assert session.backend_name == "fake-alpha"
    assert not hasattr(session, "workflow")
    assert session.to_dict()["workflow"] == "fake-alpha"
    assert set(session.capabilities) == {
        BackendCapability.CORE_SESSION.value,
        BackendCapability.READ_DTCS.value,
    }


def test_start_session_raises_dynamic_backend_decision_for_brand_conflict(monkeypatch):
    registry = BackendRegistry()
    registry.register(_backend("fake-alpha", brands=["shared"]))
    registry.register(_backend("fake-beta", brands=["shared"]))
    orchestrator = SessionOrchestrator(registry_provider=lambda: registry)
    session = orchestrator.start_session(SessionContext(brand="shared"))

    assert session.status == SessionStatus.AWAITING_DECISION
    assert session.pending_decision is not None
    assert session.pending_decision.kind == "backend"
    assert {option.option_id for option in session.pending_decision.options} == {
        "backend:fake-alpha",
        "backend:fake-beta",
        "manual",
    }


def test_start_session_unknown_brand_lists_registered_backends(monkeypatch):
    registry = BackendRegistry()
    registry.register(_backend("fake-alpha", brands=["alpha"]))
    registry.register(_backend("fake-beta", brands=["beta"]))
    orchestrator = SessionOrchestrator(registry_provider=lambda: registry)
    session = orchestrator.start_session(SessionContext(brand="unknown-brand"))

    assert session.status == SessionStatus.AWAITING_DECISION
    assert session.pending_decision is not None
    assert session.pending_decision.kind == "backend"
    assert [option.option_id for option in session.pending_decision.options] == [
        "backend:fake-alpha",
        "backend:fake-beta",
        "manual",
    ]
    assert session.pending_decision.fallback_option_id == "manual"


def test_build_session_status_payload_exposes_backend_metadata():
    runtime = WorkerRuntime()
    orchestrator = SessionOrchestrator()
    session = Session(
        session_id="session-1",
        context=SessionContext(brand="alpha"),
        status=SessionStatus.RUNNING,
    )
    session.backend_name = "fake-alpha"
    session.capabilities = [
        BackendCapability.CORE_SESSION.value,
        BackendCapability.READ_DTCS.value,
    ]
    orchestrator._sessions[session.session_id] = session

    payload = build_session_status_payload(
        runtime,
        orchestrator=orchestrator,
        backend=types.SimpleNamespace(preflight=lambda: {}),
        session_id=session.session_id,
    )

    assert payload["backend_name"] == "fake-alpha"
    assert payload["workflow"] == "fake-alpha"
    assert payload["capabilities"] == [
        BackendCapability.CORE_SESSION.value,
        BackendCapability.READ_DTCS.value,
    ]


def test_core_backend_flow_runs_without_gds2_specific_runtime():
    runtime = WorkerRuntime()
    session = Session(
        session_id="session-1",
        context=SessionContext(brand="alpha"),
        status=SessionStatus.RUNNING,
    )
    session.backend_name = "fake-alpha"
    session.capabilities = [
        BackendCapability.CORE_SESSION.value,
        BackendCapability.READ_DTCS.value,
    ]
    backend = _backend("fake-alpha", brands=["alpha"])
    progress_messages: list[str] = []

    module_payload = select_module_action(
        runtime,
        session,
        module="ECM",
        backend=backend,
        emit_progress=progress_messages.append,
    )
    category_payload = select_data_category_action(
        runtime,
        session,
        data_category="DTC",
        backend=backend,
        emit_progress=progress_messages.append,
    )
    dtc_payload = read_dtcs(
        session,
        {"session_id": session.session_id},
        backend=backend,
        emit_progress=progress_messages.append,
    )

    assert module_payload["success"] is True
    assert category_payload["success"] is True
    assert dtc_payload["dtc_count"] == 1
    assert progress_messages[-1].startswith("Read DTCs completed")


def test_core_backend_clear_dtcs_flow_runs_without_gds2_specific_runtime():
    runtime = WorkerRuntime()
    session = Session(
        session_id="session-1",
        context=SessionContext(brand="alpha"),
        status=SessionStatus.RUNNING,
    )
    session.backend_name = "fake-alpha"
    session.capabilities = [
        BackendCapability.CORE_SESSION.value,
        BackendCapability.READ_DTCS.value,
        BackendCapability.CLEAR_DTCS.value,
    ]
    backend = _backend(
        "fake-alpha",
        brands=["alpha"],
        capabilities=[
            BackendCapability.CORE_SESSION,
            BackendCapability.READ_DTCS,
            BackendCapability.CLEAR_DTCS,
        ],
    )
    progress_messages: list[str] = []

    payload = clear_session_dtcs(
        runtime,
        session,
        {"session_id": session.session_id},
        backend=backend,
        emit_progress=progress_messages.append,
    )

    assert payload["success"] is True
    assert payload["cleared_count"] == 1
    assert payload["message"] == "Cleared by fake core backend"
    assert progress_messages[-1].startswith("Clear DTCs completed")


def test_navigation_handler_returns_501_when_backend_lacks_navigation(monkeypatch):
    orchestrator = SessionOrchestrator()
    session = Session(
        session_id="session-1",
        context=SessionContext(brand="alpha"),
        status=SessionStatus.RUNNING,
    )
    session.backend_name = "fake-alpha"
    session.capabilities = [
        BackendCapability.CORE_SESSION.value,
        BackendCapability.READ_DTCS.value,
    ]
    orchestrator._sessions[session.session_id] = session

    monkeypatch.setattr(session_navigation_handlers, "get_orchestrator", lambda: orchestrator)

    payload, status_code = session_navigation_handlers.start_navigation_session_for_business(
        {"session_id": session.session_id}
    )

    assert status_code == 501
    assert "Navigation" in payload["error"]


def test_session_execute_returns_501_when_backend_lacks_generic_actions(monkeypatch):
    _install_fake_flask_stack(monkeypatch)
    session_module = importlib.import_module("server.api.session")
    orchestrator = SessionOrchestrator()
    session = Session(
        session_id="session-1",
        context=SessionContext(brand="alpha"),
        status=SessionStatus.RUNNING,
    )
    session.backend_name = "fake-alpha"
    session.capabilities = [
        BackendCapability.CORE_SESSION.value,
        BackendCapability.READ_DTCS.value,
    ]
    orchestrator._sessions[session.session_id] = session

    monkeypatch.setattr(session_module, "jsonify", lambda payload: payload)
    monkeypatch.setattr(
        session_module,
        "request",
        types.SimpleNamespace(
            json={
                "session_id": session.session_id,
                "action": "select_module",
                "args": {"module_name": "ECM"},
            }
        ),
    )
    monkeypatch.setattr(session_module, "get_orchestrator", lambda: orchestrator)

    payload, status_code = session_module.session_execute()

    assert status_code == 501
    assert "generic actions" in payload["error"].lower()


def test_session_execute_returns_machine_readable_session_state_conflict(monkeypatch):
    _install_fake_flask_stack(monkeypatch)
    session_module = importlib.import_module("server.api.session")
    orchestrator = SessionOrchestrator()
    session = Session(
        session_id="session-1",
        context=SessionContext(brand="alpha"),
        status=SessionStatus.AWAITING_DECISION,
    )
    session.backend_name = "fake-alpha"
    session.capabilities = [
        BackendCapability.CORE_SESSION.value,
        BackendCapability.GENERIC_ACTIONS.value,
    ]
    orchestrator._sessions[session.session_id] = session

    monkeypatch.setattr(session_module, "jsonify", lambda payload: payload)
    monkeypatch.setattr(
        session_module,
        "request",
        types.SimpleNamespace(
            json={
                "session_id": session.session_id,
                "action": "select_module",
                "args": {"module_name": "ECM"},
            }
        ),
    )
    monkeypatch.setattr(session_module, "get_orchestrator", lambda: orchestrator)
    monkeypatch.setattr(
        session_module,
        "_get_backend",
        lambda session_id=None: (_ for _ in ()).throw(
            AssertionError("_get_backend should not be called")
        ),
    )

    payload, status_code = session_module.session_execute()

    assert status_code == 409
    assert payload == {
        "success": False,
        "error": "Session not running (status=awaiting_decision)",
        "error_code": "session_not_running",
        "session_status": "awaiting_decision",
    }


def test_session_execute_delegates_to_backend_execute_action(monkeypatch):
    _install_fake_flask_stack(monkeypatch)
    session_module = importlib.import_module("server.api.session")
    orchestrator = SessionOrchestrator()
    session = Session(
        session_id="session-1",
        context=SessionContext(brand="alpha"),
        status=SessionStatus.RUNNING,
    )
    session.backend_name = "fake-alpha"
    session.capabilities = [
        BackendCapability.CORE_SESSION.value,
        BackendCapability.READ_DTCS.value,
        BackendCapability.GENERIC_ACTIONS.value,
    ]
    orchestrator._sessions[session.session_id] = session

    calls: list[dict[str, object]] = []

    class ActionBackend:
        def execute_action(self, action, args=None, timeout_sec=30.0):
            calls.append(
                {
                    "action": action,
                    "args": args,
                    "timeout_sec": timeout_sec,
                }
            )
            return {
                "success": True,
                "metadata": {"selected": "ECM"},
                "error": None,
            }

    monkeypatch.setattr(session_module, "jsonify", lambda payload: payload)
    monkeypatch.setattr(
        session_module,
        "request",
        types.SimpleNamespace(
            json={
                "session_id": session.session_id,
                "action": "select_module",
                "args": {"module_name": "ECM"},
                "timeout_sec": 12.5,
            }
        ),
    )
    monkeypatch.setattr(session_module, "get_orchestrator", lambda: orchestrator)
    monkeypatch.setattr(session_module, "_get_backend", lambda: ActionBackend())
    monkeypatch.setattr(
        session_module,
        "get_executor",
        lambda: (_ for _ in ()).throw(AssertionError("executor wiring should stay unused")),
    )
    monkeypatch.setattr(
        session_module,
        "get_adapter",
        lambda: (_ for _ in ()).throw(AssertionError("adapter wiring should stay unused")),
    )

    payload = session_module.session_execute()

    assert payload["success"] is True
    assert payload["result"] == {"selected": "ECM"}
    assert calls == [
        {
            "action": "select_module",
            "args": {"module_name": "ECM"},
            "timeout_sec": 12.5,
        }
    ]


def test_session_execute_surfaces_branch_decision_from_backend(monkeypatch):
    _install_fake_flask_stack(monkeypatch)
    session_module = importlib.import_module("server.api.session")
    orchestrator = SessionOrchestrator()
    session = Session(
        session_id="session-1",
        context=SessionContext(brand="alpha"),
        status=SessionStatus.RUNNING,
    )
    session.backend_name = "fake-alpha"
    session.capabilities = [
        BackendCapability.CORE_SESSION.value,
        BackendCapability.READ_DTCS.value,
        BackendCapability.GENERIC_ACTIONS.value,
    ]
    orchestrator._sessions[session.session_id] = session

    decision = BranchDecision(
        domain=DecisionDomain.MODULE,
        target="ECM",
        selected_option=None,
        requires_human=True,
        confidence=0.42,
        reason="ambiguous module",
    )

    class BranchingBackend:
        def execute_action(self, action, args=None, timeout_sec=30.0):
            raise BranchDecisionRequiredError(decision, ["ECM-A", "ECM-B"])

    monkeypatch.setattr(session_module, "jsonify", lambda payload: payload)
    monkeypatch.setattr(
        session_module,
        "request",
        types.SimpleNamespace(
            json={
                "session_id": session.session_id,
                "action": "select_module",
                "args": {"module_name": "ECM"},
            }
        ),
    )
    monkeypatch.setattr(session_module, "get_orchestrator", lambda: orchestrator)
    monkeypatch.setattr(session_module, "_get_backend", lambda: BranchingBackend())
    monkeypatch.setattr(
        session_module,
        "get_executor",
        lambda: (_ for _ in ()).throw(AssertionError("executor wiring should stay unused")),
    )
    monkeypatch.setattr(
        session_module,
        "get_adapter",
        lambda: (_ for _ in ()).throw(AssertionError("adapter wiring should stay unused")),
    )

    payload = session_module.session_execute()

    assert payload["decision_required"] is True
    assert payload["decision"]["context"]["resume_action"] == "select_module"


def test_session_dependencies_resolve_backend_from_active_session(monkeypatch):
    registry = BackendRegistry()
    fake_backend = _backend("fake-alpha", brands=["alpha"])
    registry.register(fake_backend)

    runtime = WorkerRuntime()
    orchestrator = SessionOrchestrator(registry_provider=lambda: registry)
    session = Session(
        session_id="session-1",
        context=SessionContext(brand="alpha"),
        status=SessionStatus.RUNNING,
    )
    session.backend_name = "fake-alpha"
    session.capabilities = [
        BackendCapability.CORE_SESSION.value,
        BackendCapability.READ_DTCS.value,
    ]
    orchestrator._sessions[session.session_id] = session
    runtime.set_orchestrator(orchestrator)
    runtime.bind_business_session(session.session_id)

    monkeypatch.setattr(session_dependencies, "get_worker_runtime", lambda: runtime)
    monkeypatch.setattr(session_dependencies, "get_backend_registry", lambda: registry)

    backend = session_dependencies.get_backend()

    assert backend is fake_backend
    bundle = runtime.get_active_backend_bundle(session.session_id)
    assert bundle is not None
    assert bundle.backend_name == "fake-alpha"


def test_session_dependencies_get_navigation_runtime_from_backend(monkeypatch):
    registry = BackendRegistry()
    fake_backend = _backend("fake-alpha", brands=["alpha"])
    fake_navigation_runtime = types.SimpleNamespace(name="navigation-runtime")
    fake_backend.get_navigation_runtime = lambda: fake_navigation_runtime
    registry.register(fake_backend)

    runtime = WorkerRuntime()
    orchestrator = SessionOrchestrator(registry_provider=lambda: registry)
    session = Session(
        session_id="session-1",
        context=SessionContext(brand="alpha"),
        status=SessionStatus.RUNNING,
    )
    session.backend_name = "fake-alpha"
    session.capabilities = [
        BackendCapability.CORE_SESSION.value,
        BackendCapability.READ_DTCS.value,
        BackendCapability.NAVIGATION.value,
    ]
    orchestrator._sessions[session.session_id] = session
    runtime.set_orchestrator(orchestrator)
    runtime.bind_business_session(session.session_id)

    monkeypatch.setattr(session_dependencies, "get_worker_runtime", lambda: runtime)
    monkeypatch.setattr(session_dependencies, "get_backend_registry", lambda: registry)

    navigation_runtime = session_dependencies.get_navigation_runtime()

    assert navigation_runtime is fake_navigation_runtime


def test_worker_backend_bundle_populates_navigation_handle_from_backend():
    registry = BackendRegistry()
    fake_backend = _backend("fake-alpha", brands=["alpha"])
    fake_navigation_runtime = object()
    fake_backend.get_navigation_runtime = lambda: fake_navigation_runtime
    registry.register(fake_backend)

    runtime = WorkerRuntime()
    orchestrator = SessionOrchestrator(registry_provider=lambda: registry)
    session = Session(
        session_id="session-1",
        context=SessionContext(brand="alpha"),
        status=SessionStatus.RUNNING,
    )
    session.backend_name = "fake-alpha"
    orchestrator._sessions[session.session_id] = session
    runtime.set_orchestrator(orchestrator)

    bundle = runtime.ensure_backend_bundle(
        session.session_id,
        descriptor=fake_backend.descriptor,
        backend_factory=lambda: fake_backend,
    )

    assert bundle.navigation_handle is fake_navigation_runtime


def test_session_dependencies_get_executor_from_backend_action_runtime(monkeypatch):
    registry = BackendRegistry()
    fake_backend = _backend(
        "fake-alpha",
        brands=["alpha"],
        capabilities=[
            BackendCapability.CORE_SESSION,
            BackendCapability.READ_DTCS,
            BackendCapability.GENERIC_ACTIONS,
        ],
    )
    fake_executor = object()
    fake_adapter = types.SimpleNamespace(name="fake-adapter")
    fake_backend.build_action_runtime = lambda: types.SimpleNamespace(
        executor=fake_executor,
        adapter=fake_adapter,
    )
    registry.register(fake_backend)

    runtime = WorkerRuntime()
    orchestrator = SessionOrchestrator(registry_provider=lambda: registry)
    session = Session(
        session_id="session-1",
        context=SessionContext(brand="alpha"),
        status=SessionStatus.RUNNING,
    )
    session.backend_name = "fake-alpha"
    session.capabilities = [
        BackendCapability.CORE_SESSION.value,
        BackendCapability.READ_DTCS.value,
        BackendCapability.GENERIC_ACTIONS.value,
    ]
    orchestrator._sessions[session.session_id] = session
    runtime.set_orchestrator(orchestrator)
    runtime.bind_business_session(session.session_id)

    monkeypatch.setattr(session_dependencies, "get_worker_runtime", lambda: runtime)
    monkeypatch.setattr(session_dependencies, "get_backend_registry", lambda: registry)

    executor = session_dependencies.get_executor()
    adapter = session_dependencies.get_adapter()

    assert executor is fake_executor
    assert adapter is fake_adapter
