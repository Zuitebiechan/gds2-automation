from __future__ import annotations

import importlib
import importlib.util
import inspect
import sys
import time
import types
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_RETIRED_WORKFLOW_MARKERS = (
    "src.workflows",
    "interactive_workflow",
    "read_data_display_agent",
    "InteractiveWorkflow",
    "ReadDataDisplayAgentWorkflow",
)

import diagnostic_platform.contracts as contracts_module
import diagnostic_platform.runtime.worker_runtime as worker_runtime_module
from diagnostic_platform.runtime.session_lifecycle import (
    abort_business_session,
    build_session_status_payload,
    start_business_session,
)
from diagnostic_platform.runtime.session_actions import (
    start_live_data as start_session_live_data,
    stop_live_data as stop_session_live_data,
)
from diagnostic_platform.runtime.worker_runtime import WorkerRuntime
from src.gds2_orchestration.session_orchestrator import (
    Session,
    SessionContext,
    SessionOrchestrator,
    SessionStatus,
)


def _require_attr(obj: Any, name: str) -> Any:
    assert hasattr(obj, name), f"{obj!r} should expose {name}"
    return getattr(obj, name)


def _assert_no_retired_workflow_markers(source: str, context: str = "source") -> None:
    for marker in _RETIRED_WORKFLOW_MARKERS:
        assert marker not in source, f"{marker!r} should not appear in {context}"


def _wait_until(predicate, *, timeout: float = 2.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _require_module(module_name: str):
    spec = importlib.util.find_spec(module_name)
    assert spec is not None, f"{module_name} should exist"
    return importlib.import_module(module_name)


def _capability_values(capabilities: list[Any] | tuple[Any, ...]) -> list[str]:
    return sorted(
        capability.value if hasattr(capability, "value") else str(capability)
        for capability in capabilities
    )


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

    fake_request = types.SimpleNamespace(args={}, json=None)
    fake_flask = types.ModuleType("flask")
    fake_flask.Blueprint = FakeBlueprint
    fake_flask.Response = object
    fake_flask.jsonify = lambda payload=None, **kwargs: payload if payload is not None else kwargs
    fake_flask.request = fake_request
    monkeypatch.setitem(sys.modules, "flask", fake_flask)

    return fake_request


def _import_session_api(monkeypatch):
    fake_request = _install_fake_flask_stack(monkeypatch)
    for module_name in [
        "server.api.session",
        "server.api.session_ai_handlers",
        "server.api.session_live_data_handlers",
        "server.api.session_navigation_handlers",
        "server.api.session_dependencies",
    ]:
        sys.modules.pop(module_name, None)
    session_api = importlib.import_module("server.api.session")
    return session_api, fake_request


def _unwrap_response(result):
    if isinstance(result, tuple):
        payload, status = result
        return payload, status
    return result, 200


class FakeCoreBackend(contracts_module.DiagnosticBackend):
    def __init__(
        self,
        *,
        name: str = "fakecore",
        display_name: str = "Fake Core",
        brands: list[str] | None = None,
        default_brands: list[str] | None = None,
        capabilities: list[Any] | None = None,
    ) -> None:
        self._name = name
        self._display_name = display_name
        self._brands = [str(brand).strip().lower() for brand in (brands or ["fakebrand"])]
        self._default_brands = [
            str(brand).strip().lower()
            for brand in (default_brands if default_brands is not None else self._brands)
        ]
        self._capabilities = list(capabilities or [])
        self.started = False
        self.selected_module = ""
        self.selected_category = ""

    @property
    def name(self) -> str:
        return self._name

    @property
    def supported_brands(self) -> list[str]:
        return list(self._brands)

    @property
    def descriptor(self) -> Any:
        BackendDescriptor = _require_attr(contracts_module, "BackendDescriptor")
        return BackendDescriptor(
            backend_name=self._name,
            display_name=self._display_name,
            supported_brands=list(self._brands),
            capabilities=tuple(self._capabilities),
            default_for_brands=list(self._default_brands),
            ui_mode="guided",
        )

    def preflight(self) -> dict[str, Any]:
        return {
            "network_quality": None,
            "connection_epoch": None,
        }

    def start(self, *args, **kwargs) -> dict[str, Any]:
        self.started = True
        return {
            "modules": self.get_modules(),
            "vin": "VIN-FAKE-001",
            "device": "FAKE-VCI",
        }

    def stop(self) -> None:
        self.started = False

    def connect_vci(self, device: str) -> None:
        return None

    def get_modules(self) -> list[str]:
        return ["Engine", "ABS"]

    def select_module(self, module: str) -> None:
        self.selected_module = module

    def get_data_categories(self) -> list[str]:
        return ["DTCs", "Live Data"]

    def go_back(self) -> None:
        return None

    def detect_current_page(self) -> str:
        return "data_display" if self.selected_category else "module_list"

    def execute_action(
        self,
        action: str,
        args: dict[str, Any] | None = None,
        timeout_sec: float = 30.0,
    ) -> dict[str, Any]:
        return {
            "success": True,
            "action": action,
            "metadata": args or {},
        }

    def select_data_category(self, category: str) -> list[str]:
        self.selected_category = category
        return ["RPM", "Coolant Temp"]

    def read_dtcs(self) -> list[contracts_module.DTC]:
        return [
            contracts_module.DTC(
                code="P0001",
                module=self.selected_module or "Engine",
                status="active",
                description="Fake DTC for backend-neutral regression",
                source_backend=self._name,
            )
        ]

    def start_live_data(self) -> contracts_module.LiveDataStream:
        return contracts_module.LiveDataStream(session_id="fake-live", active=True)

    def stop_live_data(self) -> None:
        return None

    def clear_dtcs(self) -> contracts_module.ClearResult:
        return contracts_module.ClearResult(
            success=True,
            cleared_count=1,
            message="Cleared by fake backend",
        )

    def get_state(self) -> contracts_module.BackendState:
        return contracts_module.BackendState(
            current_page=self.detect_current_page(),
            is_connected=self.started,
            current_module=self.selected_module or None,
            current_data_category=self.selected_category or None,
            extra={"device": "FAKE-VCI", "vin": "VIN-FAKE-001"},
        )


class FakeTelemetryBackend(FakeCoreBackend):
    def __init__(self, *, capabilities: list[Any]) -> None:
        super().__init__(
            name="fake-telemetry",
            display_name="Fake Telemetry",
            brands=["telebrand"],
            capabilities=capabilities,
        )
        self.live_start_calls: list[dict[str, Any]] = []
        self.live_stop_calls = 0
        self.ai_payload_calls: list[dict[str, Any]] = []

    def start_live_data_session(
        self,
        *,
        data_category: str,
        interval_ms: int,
        stream_scope: str,
    ) -> dict[str, Any]:
        self.live_start_calls.append(
            {
                "data_category": data_category,
                "interval_ms": interval_ms,
                "stream_scope": stream_scope,
            }
        )
        self.selected_category = data_category
        return {
            "success": True,
            "message": "Backend-owned live stream started",
            "interval_ms": interval_ms,
        }

    def stop_live_data_session(self) -> dict[str, Any]:
        self.live_stop_calls += 1
        return {
            "success": True,
            "message": "Backend-owned live stream stopped",
        }

    def collect_ai_payload(
        self,
        *,
        vehicle_context: dict[str, Any],
        data_category: str,
        collection_seconds: int,
    ) -> contracts_module.DiagnosticPayload:
        self.ai_payload_calls.append(
            {
                "vehicle_context": dict(vehicle_context),
                "data_category": data_category,
                "collection_seconds": collection_seconds,
            }
        )
        return contracts_module.DiagnosticPayload(
            vehicle_context=contracts_module.VehicleContext(
                brand=vehicle_context.get("brand") or "telebrand",
                model=vehicle_context.get("model") or "Demo",
                vin=vehicle_context.get("vin") or "VIN-FAKE-001",
            ),
            dtcs=[
                contracts_module.DTC(
                    code="P0001",
                    module=vehicle_context.get("module") or "Engine",
                    status="active",
                    description="Fake telemetry DTC",
                    source_backend=self.name,
                )
            ],
            live_data=[
                contracts_module.LiveDataPoint(
                    parameter="RPM",
                    value=850.0,
                    unit="rpm",
                    timestamp=1.0,
                )
            ],
            sampling_quality=contracts_module.SamplingQuality.GOOD,
            source_backend=self.name,
        )


class FakeDeviceSelectionBackend(FakeCoreBackend):
    def __init__(self, *, capabilities: list[Any]) -> None:
        super().__init__(
            name="fake-device-selection",
            display_name="Fake Device Selection",
            brands=["devicebrand"],
            capabilities=capabilities,
        )

    def start(self, *args, **kwargs) -> dict[str, Any]:
        self.started = True
        return {
            "devices": ["VCI Proxy (Remote)", "Bench VCI"],
            "at_device_explorer": True,
            "device_connected": False,
        }


def test_backend_registry_resolves_unique_ambiguous_and_unmatched_brands():
    BackendCapability = _require_attr(contracts_module, "BackendCapability")
    BackendResolutionResult = _require_attr(contracts_module, "BackendResolutionResult")

    registry = contracts_module.BackendRegistry()
    registry.register(
        FakeCoreBackend(
            name="fakecore",
            brands=["fakebrand"],
            capabilities=[
                BackendCapability.CORE_SESSION,
                BackendCapability.READ_DTCS,
            ],
        )
    )
    registry.register(
        FakeCoreBackend(
            name="alt-a",
            brands=["sharedbrand"],
            default_brands=[],
            capabilities=[BackendCapability.CORE_SESSION],
        )
    )
    registry.register(
        FakeCoreBackend(
            name="alt-b",
            brands=["sharedbrand"],
            default_brands=[],
            capabilities=[BackendCapability.CORE_SESSION],
        )
    )

    assert hasattr(registry, "resolve_brand"), "BackendRegistry should support resolve_brand()"

    unique = registry.resolve_brand("fakebrand")
    assert isinstance(unique, BackendResolutionResult)
    assert unique.reason == "unique_match"
    assert unique.decision_required is False
    assert unique.selected_backend_name == "fakecore"

    ambiguous = registry.resolve_brand("sharedbrand")
    assert ambiguous.reason == "ambiguous_brand"
    assert ambiguous.decision_required is True
    assert ambiguous.selected_backend_name is None
    assert sorted(item.backend_name for item in ambiguous.candidates) == ["alt-a", "alt-b"]

    unmatched = registry.resolve_brand("missingbrand")
    assert unmatched.reason == "no_brand_match"
    assert unmatched.decision_required is True
    assert unmatched.selected_backend_name is None
    assert sorted(item.backend_name for item in unmatched.candidates) == ["alt-a", "alt-b", "fakecore"]


def test_default_backend_registry_routes_gds2_brand_aliases_without_decision():
    registry_module = _require_module("diagnostic_platform.backend_registry")
    create_backend_registry = _require_attr(registry_module, "create_backend_registry")

    registry = create_backend_registry()

    gds2_brand = registry.resolve_brand("GDS2")
    gm_china_brand = registry.resolve_brand("GM China")
    gm_china_hyphen = registry.resolve_brand("GM-China")

    assert gds2_brand.selected_backend_name == "gds2"
    assert gds2_brand.decision_required is False
    assert gm_china_brand.selected_backend_name == "gds2"
    assert gm_china_brand.decision_required is False
    assert gm_china_hyphen.selected_backend_name == "gds2"
    assert gm_china_hyphen.decision_required is False


def test_start_business_session_routes_gds2_alias_inputs_without_backend_decision():
    registry_module = _require_module("diagnostic_platform.backend_registry")
    create_backend_registry = _require_attr(registry_module, "create_backend_registry")

    for brand in ("GM China", "GDS2"):
        runtime = WorkerRuntime()
        orchestrator = SessionOrchestrator(registry_provider=create_backend_registry)

        payload = start_business_session(
            runtime,
            orchestrator=orchestrator,
            context=SessionContext(brand=brand, model="Demo"),
        )

        assert payload["status"] == "running"
        assert payload["backend_name"] == "gds2"
        assert payload.get("decision") is None


def test_start_business_session_and_status_include_backend_metadata():
    BackendCapability = _require_attr(contracts_module, "BackendCapability")

    assert "registry_provider" in inspect.signature(SessionOrchestrator).parameters

    registry = contracts_module.BackendRegistry()
    fake_backend = FakeCoreBackend(
        capabilities=[
            BackendCapability.CORE_SESSION,
            BackendCapability.READ_DTCS,
        ]
    )
    registry.register(fake_backend)

    runtime = WorkerRuntime()
    orchestrator = SessionOrchestrator(registry_provider=lambda: registry)

    payload = start_business_session(
        runtime,
        orchestrator=orchestrator,
        context=SessionContext(brand="fakebrand", model="Demo", vin="VIN-FAKE-001"),
    )

    expected_capabilities = _capability_values(
        [BackendCapability.CORE_SESSION, BackendCapability.READ_DTCS]
    )
    assert payload.get("backend_name") == "fakecore"
    assert sorted(payload.get("capabilities") or []) == expected_capabilities
    assert payload.get("workflow") == "fakecore"

    status = build_session_status_payload(
        runtime,
        orchestrator=orchestrator,
        backend=fake_backend,
        session_id=payload["session_id"],
    )

    assert status.get("backend_name") == "fakecore"
    assert sorted(status.get("capabilities") or []) == expected_capabilities
    summary = status.get("backend_state_summary") or {}
    assert summary.get("current_page") == "module_list"
    assert summary.get("is_connected") is False


def test_start_business_session_rolls_back_failed_worker_bind_without_retaining_session():
    BackendCapability = _require_attr(contracts_module, "BackendCapability")

    registry = contracts_module.BackendRegistry()
    registry.register(
        FakeCoreBackend(
            capabilities=[BackendCapability.CORE_SESSION]
        )
    )

    runtime = WorkerRuntime()
    orchestrator = SessionOrchestrator(registry_provider=lambda: registry)

    def _raise_bind_error(_session_id: str) -> None:
        raise RuntimeError("worker bind failed")

    runtime.bind_business_session = _raise_bind_error

    with pytest.raises(RuntimeError, match="worker bind failed"):
        start_business_session(
            runtime,
            orchestrator=orchestrator,
            context=SessionContext(brand="fakebrand", model="Demo", vin="VIN-FAKE-001"),
        )

    assert orchestrator.get_active_session() is None
    assert runtime.get_business_session_binding().session_id is None

    runtime.bind_business_session = WorkerRuntime.bind_business_session.__get__(runtime)
    restarted = start_business_session(
        runtime,
        orchestrator=orchestrator,
        context=SessionContext(brand="fakebrand", model="Demo", vin="VIN-FAKE-002"),
    )

    assert restarted["success"] is True


def test_session_api_runs_fake_backend_core_chain_and_gates_extensions(monkeypatch):
    BackendCapability = _require_attr(contracts_module, "BackendCapability")
    registry_module = _require_module("diagnostic_platform.backend_registry")
    get_backend_registry = _require_attr(registry_module, "get_backend_registry")
    set_backend_registry = _require_attr(registry_module, "set_backend_registry")

    original_registry = get_backend_registry()
    fake_registry = contracts_module.BackendRegistry()
    fake_registry.register(
        FakeCoreBackend(
            capabilities=[
                BackendCapability.CORE_SESSION,
                BackendCapability.READ_DTCS,
            ]
        )
    )

    try:
        set_backend_registry(fake_registry)
        monkeypatch.setattr(
            worker_runtime_module,
            "_WORKER_RUNTIME",
            worker_runtime_module.WorkerRuntime(),
        )

        session_api, fake_request = _import_session_api(monkeypatch)

        fake_request.json = {"brand": "fakebrand", "model": "Demo"}
        start_payload, start_status = _unwrap_response(session_api.session_start())
        assert start_status == 200
        session_id = start_payload["session_id"]
        assert start_payload["backend_name"] == "fakecore"

        fake_request.json = {"session_id": session_id}
        diagnostics_payload, diagnostics_status = _unwrap_response(
            session_api.session_start_diagnostics()
        )
        assert diagnostics_status == 200
        assert diagnostics_payload["result"]["modules"] == ["Engine", "ABS"]

        fake_request.json = {"session_id": session_id, "module": "Engine"}
        module_payload, module_status = _unwrap_response(session_api.session_select_module())
        assert module_status == 200
        assert module_payload["result"]["data_categories"] == ["DTCs", "Live Data"]

        fake_request.json = {"session_id": session_id, "data_category": "DTCs"}
        category_payload, category_status = _unwrap_response(
            session_api.session_select_data_category()
        )
        assert category_status == 200
        assert category_payload["result"]["selected_data_category"] == "DTCs"

        fake_request.json = {"session_id": session_id}
        dtc_payload, dtc_status = _unwrap_response(session_api.session_dtcs())
        assert dtc_status == 200
        assert dtc_payload["result"]["dtc_count"] == 1

        fake_request.json = {"session_id": session_id}
        clear_payload, clear_status = _unwrap_response(session_api.session_clear_dtcs())
        assert clear_status == 501
        assert "Clear DTC" in clear_payload["error"]

        fake_request.json = {"session_id": session_id, "goal": "Go to Data Display"}
        navigate_payload, navigate_status = _unwrap_response(session_api.session_navigate_start())
        assert navigate_status == 501

        fake_request.json = {"session_id": session_id, "action": "go_back"}
        execute_payload, execute_status = _unwrap_response(session_api.session_execute())
        assert execute_status == 501

        fake_request.args = {"session_id": session_id}
        status_payload, status_code = _unwrap_response(session_api.session_status())
        assert status_code == 200
        assert status_payload["backend_name"] == "fakecore"
        assert "core_session" in status_payload["capabilities"]
    finally:
        set_backend_registry(original_registry)


def test_session_start_diagnostics_accepts_device_selection_result(monkeypatch):
    BackendCapability = _require_attr(contracts_module, "BackendCapability")
    registry_module = _require_module("diagnostic_platform.backend_registry")
    get_backend_registry = _require_attr(registry_module, "get_backend_registry")
    set_backend_registry = _require_attr(registry_module, "set_backend_registry")

    original_registry = get_backend_registry()
    fake_registry = contracts_module.BackendRegistry()
    fake_registry.register(
        FakeDeviceSelectionBackend(
            capabilities=[BackendCapability.CORE_SESSION]
        )
    )

    try:
        set_backend_registry(fake_registry)
        monkeypatch.setattr(
            worker_runtime_module,
            "_WORKER_RUNTIME",
            worker_runtime_module.WorkerRuntime(),
        )

        session_api, fake_request = _import_session_api(monkeypatch)

        fake_request.json = {"brand": "devicebrand", "model": "Demo"}
        start_payload, start_status = _unwrap_response(session_api.session_start())
        assert start_status == 200

        fake_request.json = {"session_id": start_payload["session_id"]}
        diagnostics_payload, diagnostics_status = _unwrap_response(
            session_api.session_start_diagnostics()
        )

        assert diagnostics_status == 200
        assert diagnostics_payload["success"] is True
        assert diagnostics_payload["result"] == {
            "devices": ["VCI Proxy (Remote)", "Bench VCI"],
            "at_device_explorer": True,
            "device_connected": False,
        }
    finally:
        set_backend_registry(original_registry)

def test_session_api_allows_backend_decision_flow_before_backend_binding(monkeypatch):
    BackendCapability = _require_attr(contracts_module, "BackendCapability")
    registry_module = _require_module("diagnostic_platform.backend_registry")
    get_backend_registry = _require_attr(registry_module, "get_backend_registry")
    set_backend_registry = _require_attr(registry_module, "set_backend_registry")

    original_registry = get_backend_registry()
    fake_registry = contracts_module.BackendRegistry()
    fake_registry.register(
        FakeCoreBackend(
            name="fake-alpha",
            display_name="Fake Alpha",
            brands=["sharedbrand"],
            default_brands=[],
            capabilities=[BackendCapability.CORE_SESSION],
        )
    )
    fake_registry.register(
        FakeCoreBackend(
            name="fake-beta",
            display_name="Fake Beta",
            brands=["sharedbrand"],
            default_brands=[],
            capabilities=[
                BackendCapability.CORE_SESSION,
                BackendCapability.READ_DTCS,
            ],
        )
    )

    try:
        set_backend_registry(fake_registry)
        monkeypatch.setattr(
            worker_runtime_module,
            "_WORKER_RUNTIME",
            worker_runtime_module.WorkerRuntime(),
        )

        session_api, fake_request = _import_session_api(monkeypatch)
        monkeypatch.setattr(session_api, "_sse_response", lambda stream: stream)

        fake_request.json = {"brand": "sharedbrand", "model": "Demo"}
        start_payload, start_status = _unwrap_response(session_api.session_start())
        assert start_status == 200
        assert start_payload["status"] == "awaiting_decision"
        assert start_payload["backend_name"] is None

        session_id = start_payload["session_id"]
        decision = start_payload["decision"]

        fake_request.args = {"session_id": session_id}
        event_stream = session_api.session_events()
        assert next(event_stream).startswith("event: connected\n")
        assert "event: decision_required\n" in next(event_stream)
        event_stream.close()

        fake_request.args = {"session_id": session_id}
        status_payload, status_code = _unwrap_response(session_api.session_status())
        assert status_code == 200
        assert status_payload["status"] == "awaiting_decision"
        assert status_payload["backend_name"] is None
        assert status_payload["backend_state_summary"] is None

        fake_request.json = {
            "session_id": session_id,
            "decision_id": decision["decision_id"],
            "option_id": "backend:fake-beta",
        }
        decision_payload, decision_status = _unwrap_response(session_api.session_decision())
        assert decision_status == 200
        assert decision_payload["success"] is True
        assert decision_payload["status"] == "running"
        assert decision_payload["backend_name"] == "fake-beta"
        assert "read_dtcs" in (decision_payload.get("capabilities") or [])
    finally:
        set_backend_registry(original_registry)


def test_session_status_does_not_rebind_aborted_session(monkeypatch):
    BackendCapability = _require_attr(contracts_module, "BackendCapability")
    registry_module = _require_module("diagnostic_platform.backend_registry")
    get_backend_registry = _require_attr(registry_module, "get_backend_registry")
    set_backend_registry = _require_attr(registry_module, "set_backend_registry")

    original_registry = get_backend_registry()
    fake_registry = contracts_module.BackendRegistry()
    fake_registry.register(
        FakeCoreBackend(
            capabilities=[BackendCapability.CORE_SESSION]
        )
    )

    try:
        set_backend_registry(fake_registry)
        runtime = worker_runtime_module.WorkerRuntime()
        monkeypatch.setattr(worker_runtime_module, "_WORKER_RUNTIME", runtime)

        session_api, fake_request = _import_session_api(monkeypatch)

        started = start_business_session(
            runtime,
            orchestrator=runtime.orchestrator,
            context=SessionContext(brand="fakebrand", model="Demo", vin="VIN123"),
        )
        session_id = started["session_id"]

        abort_business_session(
            runtime,
            orchestrator=runtime.orchestrator,
            session_id=session_id,
            reason="user_cancelled",
            get_ai_engine=lambda: types.SimpleNamespace(abort_session=lambda _sid: None),
        )

        assert _wait_until(lambda: runtime.get_business_session_binding(session_id).session_id is None)
        assert runtime.get_active_backend_bundle(session_id) is None

        fake_request.args = {"session_id": session_id}
        status_payload, status_code = _unwrap_response(session_api.session_status())

        assert status_code == 200
        assert status_payload["status"] == "aborted"
        assert runtime.get_business_session_binding(session_id).session_id is None
        assert runtime.get_active_backend_bundle(session_id) is None

        restarted = start_business_session(
            runtime,
            orchestrator=runtime.orchestrator,
            context=SessionContext(brand="fakebrand", model="Demo", vin="VIN456"),
        )

        assert restarted["success"] is True
        assert restarted["session_id"] != session_id
    finally:
        set_backend_registry(original_registry)


def test_session_events_does_not_rebind_aborted_session(monkeypatch):
    BackendCapability = _require_attr(contracts_module, "BackendCapability")
    registry_module = _require_module("diagnostic_platform.backend_registry")
    get_backend_registry = _require_attr(registry_module, "get_backend_registry")
    set_backend_registry = _require_attr(registry_module, "set_backend_registry")

    original_registry = get_backend_registry()
    fake_registry = contracts_module.BackendRegistry()
    fake_registry.register(
        FakeCoreBackend(
            capabilities=[BackendCapability.CORE_SESSION]
        )
    )

    try:
        set_backend_registry(fake_registry)
        runtime = worker_runtime_module.WorkerRuntime()
        monkeypatch.setattr(worker_runtime_module, "_WORKER_RUNTIME", runtime)

        session_api, fake_request = _import_session_api(monkeypatch)
        monkeypatch.setattr(session_api, "_sse_response", lambda stream: stream)

        started = start_business_session(
            runtime,
            orchestrator=runtime.orchestrator,
            context=SessionContext(brand="fakebrand", model="Demo", vin="VIN123"),
        )
        session_id = started["session_id"]

        abort_business_session(
            runtime,
            orchestrator=runtime.orchestrator,
            session_id=session_id,
            reason="user_cancelled",
            get_ai_engine=lambda: types.SimpleNamespace(abort_session=lambda _sid: None),
        )

        assert _wait_until(lambda: runtime.get_business_session_binding(session_id).session_id is None)
        assert runtime.get_active_backend_bundle(session_id) is None

        fake_request.args = {"session_id": session_id}
        event_stream = session_api.session_events()

        assert runtime.get_business_session_binding(session_id).session_id is None
        assert runtime.get_active_backend_bundle(session_id) is None
        assert next(event_stream).startswith("event: connected\n")
        event_stream.close()
    finally:
        set_backend_registry(original_registry)


def test_session_live_data_uses_backend_owned_streaming_extensions(monkeypatch):
    BackendCapability = _require_attr(contracts_module, "BackendCapability")
    backend = FakeTelemetryBackend(
        capabilities=[
            BackendCapability.CORE_SESSION,
            BackendCapability.READ_DTCS,
            BackendCapability.LIVE_DATA,
        ]
    )
    runtime = WorkerRuntime()
    session = Session(
        session_id="session-live",
        context=SessionContext(brand="telebrand"),
        status=SessionStatus.RUNNING,
        backend_name=backend.name,
        capabilities=_capability_values(
            [
                BackendCapability.CORE_SESSION,
                BackendCapability.READ_DTCS,
                BackendCapability.LIVE_DATA,
            ]
        ),
    )
    diagnostics_calls: list[dict[str, Any]] = []

    import diagnostic_platform.runtime.session_actions as session_actions_module

    monkeypatch.setattr(
        session_actions_module,
        "start_diagnostics_live_data_stream",
        lambda *args, **kwargs: diagnostics_calls.append(kwargs) or {
            "success": True,
            "message": "legacy diagnostics runtime should stay unused here",
        },
    )
    monkeypatch.setattr(
        session_actions_module,
        "stop_diagnostics_live_data_stream",
        lambda *args, **kwargs: diagnostics_calls.append(kwargs) or {
            "success": True,
            "message": "legacy diagnostics runtime should stay unused here",
        },
    )

    data_category, payload = start_session_live_data(
        runtime,
        session,
        {"data_category": "Live Data"},
        interval_ms=250,
        backend=backend,
        stream_scope="session:session-live",
        emit_progress=lambda message: None,
    )

    assert data_category == "Live Data"
    assert payload["message"] == "Backend-owned live stream started"
    assert backend.live_start_calls == [
        {
            "data_category": "Live Data",
            "interval_ms": 250,
            "stream_scope": "session:session-live",
        }
    ]
    assert diagnostics_calls == []
    assert runtime.is_live_data_active(session.session_id) is True

    stop_payload = stop_session_live_data(
        runtime,
        session,
        backend=backend,
        emit_progress=lambda message: None,
    )

    assert stop_payload["message"] == "Backend-owned live stream stopped"
    assert backend.live_stop_calls == 1
    assert diagnostics_calls == []
    assert runtime.is_live_data_active(session.session_id) is False


def test_session_ai_handler_uses_backend_owned_payload_collection(monkeypatch):
    BackendCapability = _require_attr(contracts_module, "BackendCapability")
    session_ai_handlers = importlib.import_module("server.api.session_ai_handlers")

    backend = FakeTelemetryBackend(
        capabilities=[
            BackendCapability.CORE_SESSION,
            BackendCapability.AI_DATA_COLLECTION,
        ]
    )
    session = Session(
        session_id="session-ai",
        context=SessionContext(brand="telebrand", vin="VIN-FAKE-001"),
        status=SessionStatus.RUNNING,
        backend_name=backend.name,
        capabilities=_capability_values(
            [
                BackendCapability.CORE_SESSION,
                BackendCapability.AI_DATA_COLLECTION,
            ]
        ),
        selected_module="Engine",
        selected_data_category="Live Data",
    )
    observed: dict[str, Any] = {}

    class FakeOrchestrator:
        def get_session(self, session_id: str):
            assert session_id == "session-ai"
            return session

        def emit_progress(self, session_id: str, message: str):
            observed.setdefault("progress", []).append((session_id, message))

    class FakeEngine:
        is_active = False

        def start_session_from_payload(self, vehicle_context, diagnostic_payload):
            observed["vehicle_context"] = dict(vehicle_context)
            observed["diagnostic_payload"] = diagnostic_payload
            return "ai-session-1"

        def start_session(self, *args, **kwargs):
            raise AssertionError("Legacy AI collection path should not be used")

    monkeypatch.setattr(session_ai_handlers, "get_orchestrator", lambda: FakeOrchestrator())
    monkeypatch.setattr(session_ai_handlers, "get_backend", lambda session_id=None: backend)
    monkeypatch.setattr(session_ai_handlers, "get_ai_engine", lambda: FakeEngine())
    monkeypatch.setattr(session_ai_handlers, "_runtime", lambda: WorkerRuntime())

    payload, status = session_ai_handlers.start_ai_diagnose({"session_id": "session-ai"})

    assert status == 200
    assert payload["ai_session_id"] == "ai-session-1"
    assert backend.ai_payload_calls == [
        {
            "vehicle_context": {
                "vin": "VIN-FAKE-001",
                "module": "Engine",
                "data_category": "Live Data",
                "brand": "telebrand",
            },
            "data_category": "Live Data",
            "collection_seconds": 30,
        }
    ]
    diagnostic_payload = observed["diagnostic_payload"]
    assert diagnostic_payload.source_backend == "fake-telemetry"
    assert observed["vehicle_context"]["module"] == "Engine"


def test_session_ai_stack_drops_legacy_guard_shim():
    session_dependencies = importlib.import_module("server.api.session_dependencies")
    session_ai_handlers = importlib.import_module("server.api.session_ai_handlers")
    session_ai_source = inspect.getsource(session_ai_handlers)

    assert not hasattr(session_dependencies, "make_ai_collection_guard")
    assert "make_ai_collection_guard" not in session_ai_source
    assert "collection_guard" not in session_ai_source


def test_platform_diagnostics_runtime_stays_backend_neutral():
    diagnostics_runtime_module = importlib.import_module(
        "diagnostic_platform.runtime.diagnostics_runtime"
    )
    source = inspect.getsource(diagnostics_runtime_module)

    assert "AgentDataCollector" not in source
    assert "GDS2Page" not in source


def test_platform_runtime_modules_do_not_import_legacy_navigation_brains():
    runtime_module_names = [
        "diagnostic_platform.runtime.navigation_runtime",
        "diagnostic_platform.runtime.session_actions",
        "diagnostic_platform.runtime.session_decisions",
        "diagnostic_platform.runtime.worker_runtime",
    ]
    for module_name in runtime_module_names:
        module = importlib.import_module(module_name)
        source = inspect.getsource(module)
        assert "src.workflows.data_viewer" not in source
        assert "from src.navigation" not in source
        assert "import src.navigation" not in source
        _assert_no_retired_workflow_markers(source, module_name)


def test_gds2_backend_modules_do_not_reference_data_viewer_workflow():
    backend_module_names = [
        "backends.gds2.backend",
        "backends.gds2.action_adapter",
        "backends.gds2.controller_runtime",
        "backends.gds2.registry_navigation_runtime",
    ]
    for module_name in backend_module_names:
        module = importlib.import_module(module_name)
        source = inspect.getsource(module)
        assert "DataViewerWorkflow" not in source
        assert "src.workflows.data_viewer" not in source
        _assert_no_retired_workflow_markers(source, module_name)


def test_retired_workflow_source_modules_are_removed():
    workflow_dir = ROOT / "src" / "workflows"
    if not workflow_dir.exists():
        return

    remaining_python_sources = [
        path.relative_to(ROOT)
        for path in workflow_dir.rglob("*.py")
        if "__pycache__" not in path.parts
    ]

    assert remaining_python_sources == []


def test_production_code_does_not_reference_retired_workflow_layer():
    production_roots = [
        ROOT / "backends",
        ROOT / "diagnostic_platform",
        ROOT / "server",
        ROOT / "src",
    ]

    for root in production_roots:
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            source = path.read_text(encoding="utf-8", errors="ignore")
            _assert_no_retired_workflow_markers(source, str(path.relative_to(ROOT)))


def test_registry_navigation_runtime_no_longer_delegates_production_navigation_to_legacy():
    runtime_module = importlib.import_module("backends.gds2.registry_navigation_runtime")
    source = inspect.getsource(runtime_module)

    assert "_legacy_runtime" not in source
    assert "_require_legacy_runtime" not in source


def test_platform_ai_runtime_is_payload_only():
    ai_engine_module = importlib.import_module("src.diagnosis.ai_engine")
    source = inspect.getsource(ai_engine_module)

    assert "AgentDataCollector" not in source
    assert "DiagnosticBuffer" not in source
    assert "def start_session(" not in source
    assert "def _diagnosis_worker(" not in source


def test_gds2_backend_owns_live_and_ai_collection_extensions():
    gds2_backend_module = importlib.import_module("backends.gds2.backend")
    GDS2DiagnosticBackend = getattr(gds2_backend_module, "GDS2DiagnosticBackend")

    assert (
        GDS2DiagnosticBackend.start_live_data_session
        is not contracts_module.DiagnosticBackend.start_live_data_session
    )
    assert (
        GDS2DiagnosticBackend.collect_ai_payload
        is not contracts_module.DiagnosticBackend.collect_ai_payload
    )


def test_worker_runtime_drops_legacy_live_collector_fields():
    runtime = WorkerRuntime()

    assert not hasattr(runtime, "diag_collector")
    assert not hasattr(runtime, "active_live_data_scope")
