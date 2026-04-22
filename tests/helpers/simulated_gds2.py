from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from types import MethodType
from typing import Any

from backends.gds2.backend import GDS2DiagnosticBackend
from backends.gds2.controller_runtime import GDS2ControllerRuntime
from diagnostic_platform.runtime.navigation_runtime import NavSession, NavSessionStatus
from src.navigation import GDS2Page, NavigationResult
from src.streaming.agent_data_collector import AgentSnapshot, DTCInfo


@dataclass
class SimulatedGDS2Model:
    page: GDS2Page = GDS2Page.MAIN_MENU
    vin: str = "SIMVIN1234567890"
    device: str = "VCI Proxy (Simulated)"
    modules: list[str] = field(default_factory=lambda: ["ECM", "TCM"])
    module_submenu: list[str] = field(
        default_factory=lambda: ["Data Display", "Diagnostic Trouble Codes"]
    )
    data_categories: list[str] = field(
        default_factory=lambda: ["Engine Data", "Transmission Data"]
    )
    context: dict[str, str | None] = field(
        default_factory=lambda: {
            "device": "VCI Proxy (Simulated)",
            "module": None,
            "data_category": None,
            "sub_category": None,
        }
    )
    dtcs: list[dict[str, str]] = field(
        default_factory=lambda: [
            {
                "code": "P0001",
                "control_module": "Engine",
                "status": "Active",
                "description": "Fuel Volume Regulator",
            },
            {
                "code": "P0002",
                "control_module": "Transmission",
                "status": "Stored",
                "description": "Fuel Volume Regulator Range",
            },
        ]
    )

    @property
    def dtc_count(self) -> int:
        return len(self.dtcs)


class SimulatedNavigationController:
    _MODULE_SUBMENU_DISPLAY_MARKERS = ("data display",)

    def __init__(self, model: SimulatedGDS2Model) -> None:
        self._model = model
        self._cancel_checker = None

    @property
    def current_module(self) -> str | None:
        return self._model.context.get("module")

    @property
    def current_data_category(self) -> str | None:
        return self._model.context.get("data_category")

    def set_cancel_checker(self, cancel_checker) -> None:
        self._cancel_checker = cancel_checker

    def _check_cancel(self) -> None:
        if self._cancel_checker is not None:
            self._cancel_checker()

    def detect_current_page(self, retries: int = 0) -> GDS2Page:
        del retries
        self._check_cancel()
        return self._model.page

    def get_context(self) -> dict[str, str | None]:
        return dict(self._model.context)

    def wait_for_list(self):
        return self.get_list_items(0)

    def get_list_items(self, list_index: int = 0):
        del list_index
        page = self._model.page
        if page == GDS2Page.DIAGNOSTICS_MENU:
            return ["Module Diagnostics"]
        if page == GDS2Page.MODULE_LIST:
            return list(self._model.modules)
        if page == GDS2Page.MODULE_SUBMENU:
            return list(self._model.module_submenu)
        if page == GDS2Page.DATA_LIST:
            return list(self._model.data_categories)
        return []

    def click_button(self, button_text: str) -> NavigationResult:
        self._check_cancel()
        page = self._model.page
        if page == GDS2Page.MAIN_MENU and button_text == "Diagnostics":
            self._model.page = GDS2Page.VEHICLE_SELECTION
        elif page == GDS2Page.VEHICLE_SELECTION and button_text == "Enter":
            self._model.page = GDS2Page.DIAGNOSTICS_MENU
        elif page == GDS2Page.DATA_DISPLAY and button_text == "Back":
            self._model.page = GDS2Page.DATA_LIST
        else:
            return NavigationResult(
                success=False,
                page=page,
                error=f"Unexpected click {button_text!r} on {page.value}",
                context=self.get_context(),
            )
        return NavigationResult(success=True, page=self._model.page, context=self.get_context())

    def click_enter(self) -> NavigationResult:
        return self.click_button("Enter")

    def go_back(self) -> NavigationResult:
        return self.click_button("Back")

    def select_list_item(self, item_text: str) -> NavigationResult:
        self._check_cancel()
        page = self._model.page
        if page == GDS2Page.DIAGNOSTICS_MENU and item_text == "Module Diagnostics":
            self._model.page = GDS2Page.MODULE_LIST
        elif page == GDS2Page.MODULE_LIST and item_text in self._model.modules:
            self._model.context["module"] = item_text
            self._model.page = GDS2Page.MODULE_SUBMENU
        elif page == GDS2Page.MODULE_SUBMENU and "data display" in item_text.lower():
            self._model.page = GDS2Page.DATA_LIST
        elif page == GDS2Page.DATA_LIST and item_text in self._model.data_categories:
            self._model.context["data_category"] = item_text
            self._model.page = GDS2Page.DATA_DISPLAY
        else:
            return NavigationResult(
                success=False,
                page=page,
                error=f"Unexpected selection {item_text!r} on {page.value}",
                context=self.get_context(),
            )
        return NavigationResult(success=True, page=self._model.page, context=self.get_context())

    @staticmethod
    def _normalize_text(text: str) -> str:
        return str(text).lower().strip()


class SimulatedWorkflow:
    def __init__(
        self,
        model: SimulatedGDS2Model,
        *,
        controller: SimulatedNavigationController | None = None,
    ) -> None:
        self._model = model
        self.controller = controller or SimulatedNavigationController(model)
        self._cancel_checker = None

    def set_cancel_checker(self, cancel_checker) -> None:
        self._cancel_checker = cancel_checker
        self.controller.set_cancel_checker(cancel_checker)

    def auto_start(self) -> dict[str, object]:
        if self._cancel_checker is not None:
            self._cancel_checker()
        self._model.page = GDS2Page.MODULE_LIST
        self._model.context["device"] = self._model.device
        return {
            "modules": list(self._model.modules),
            "vin": self._model.vin,
            "device": self._model.device,
        }

    def get_state(self) -> dict[str, str | None]:
        return {
            "vin": self._model.vin,
            "device": self._model.context.get("device"),
            "module": self._model.context.get("module"),
            "data_category": self._model.context.get("data_category"),
        }

    def stop_monitoring(self) -> None:
        return None

    def reset_startup_state(self) -> None:
        self._model.page = GDS2Page.MAIN_MENU
        self._model.context["module"] = None
        self._model.context["data_category"] = None

    def select_module(self, module_name: str) -> dict[str, object]:
        if module_name not in self._model.modules:
            raise RuntimeError(f"Unknown module: {module_name}")
        self._model.context["module"] = module_name
        self._model.page = GDS2Page.DATA_LIST
        return {"data_categories": list(self._model.data_categories)}

    def select_data_category(self, category: str) -> dict[str, object]:
        if category not in self._model.data_categories:
            raise RuntimeError(f"Unknown data category: {category}")
        self._model.context["data_category"] = category
        self._model.page = GDS2Page.DATA_DISPLAY
        return {"monitoring": True, "sub_categories": None}

    def read_all_dtcs(self) -> dict[str, object]:
        return {"dtcs": list(self._model.dtcs)}

    def clear_dtcs(self) -> dict[str, object]:
        if self._model.page != GDS2Page.DATA_DISPLAY:
            raise RuntimeError("Not at Data Display page")
        cleared = len(self._model.dtcs)
        self._model.dtcs = []
        return {
            "success": True,
            "cleared_count": cleared,
            "message": "Clear DTCs completed",
            "page_context": self._model.page.value,
        }


class SimulatedRegistryNavigationRuntime:
    def __init__(
        self,
        *,
        model: SimulatedGDS2Model,
        controller: SimulatedNavigationController,
    ) -> None:
        self._model = model
        self._controller = controller

    def ensure_started(self, *, cancel_checker=None):
        if cancel_checker is not None:
            cancel_checker()
        self._model.page = GDS2Page.MODULE_LIST
        self._model.context["device"] = self._model.device
        return {
            "modules": list(self._model.modules),
            "vin": self._model.vin,
            "device": self._model.device,
        }

    def connect_vci(self, device: str) -> None:
        self._model.device = device
        self._model.context["device"] = device
        self.ensure_started()

    def select_module(self, module: str) -> Any:
        if module not in self._model.modules:
            raise RuntimeError(f"Unknown module: {module}")
        self._model.context["module"] = module
        self._model.page = GDS2Page.DATA_LIST
        return {
            "selected_module": module,
            "data_categories": list(self._model.data_categories),
        }

    def select_data_category(self, category: str) -> Any:
        if category not in self._model.data_categories:
            raise RuntimeError(f"Unknown data category: {category}")
        self._model.context["data_category"] = category
        self._model.page = GDS2Page.DATA_DISPLAY
        return {
            "selected_data_category": category,
            "sub_categories": [],
        }

    def clear_dtcs(self) -> dict[str, object]:
        if self._model.page != GDS2Page.DATA_DISPLAY:
            self._model.page = GDS2Page.DATA_DISPLAY
        self._model.context["data_category"] = "DTC Display"
        cleared = len(self._model.dtcs)
        self._model.dtcs = []
        return {
            "success": True,
            "cleared_count": cleared,
            "message": "Clear DTCs completed",
            "page_context": self._model.page.value,
        }

    def detect_current_page(self) -> str:
        return self._model.page.value

    def go_back(self) -> Any:
        return self._controller.go_back()

    def start_navigation_session(self, runtime: Any, goal: str) -> Any:
        session_id = uuid.uuid4().hex[:16]
        session = NavSession(session_id=session_id, goal=str(goal or "Navigate to Data Display"))
        session.cleanup_callback = runtime.schedule_navigation_session_cleanup

        def _runner():
            try:
                self.ensure_started(cancel_checker=session.check_cancelled)
                session.event_queue.put(
                    {
                        "type": "decision_required",
                        "decision_id": "module-choice",
                        "page": "module_list",
                        "items": list(self._model.modules),
                    }
                )
                session.status = NavSessionStatus.AWAITING_DECISION
                session.pending_decision_id = "module-choice"
                session.pending_items = list(self._model.modules)
                module_payload = session.decision_queue.get(timeout=2.0)
                selected_module = str((module_payload or {}).get("selected_item") or "")
                self.select_module(selected_module)
                session.event_queue.put(
                    {
                        "type": "decision_required",
                        "decision_id": "data-choice",
                        "page": "data_list",
                        "items": list(self._model.data_categories),
                    }
                )
                session.status = NavSessionStatus.AWAITING_DECISION
                session.pending_decision_id = "data-choice"
                session.pending_items = list(self._model.data_categories)
                data_payload = session.decision_queue.get(timeout=2.0)
                selected_category = str((data_payload or {}).get("selected_item") or "")
                self.select_data_category(selected_category)
                session.status = NavSessionStatus.COMPLETED
                session.event_queue.put(
                    {
                        "type": "done",
                        "final_page": "data_display",
                        "steps": 2,
                        "selections": {
                            "module": selected_module,
                            "data_category": selected_category,
                        },
                        "error": None,
                    }
                )
            except Exception as exc:
                session.status = NavSessionStatus.FAILED
                session.error = str(exc)
                session.event_queue.put({"type": "error", "error": str(exc)})
            finally:
                if callable(session.cleanup_callback):
                    session.cleanup_callback(session.session_id)

        thread = threading.Thread(target=_runner, daemon=True, name=f"sim-nav-{session_id}")
        session.thread = thread
        runtime.set_navigation_session(session_id, session)
        thread.start()
        return session

    def submit_navigation_decision(
        self,
        runtime: Any,
        session_id: str,
        *,
        decision_id: str,
        selected_item: str,
    ) -> dict[str, Any]:
        session = runtime.get_navigation_session(session_id)
        session.status = NavSessionStatus.RUNNING
        session.pending_decision_id = None
        session.pending_items = []
        session.decision_queue.put({"selected_item": selected_item})
        return {
            "success": True,
            "session_id": session_id,
            "selected_item": selected_item,
        }

    def abort_navigation_session(self, runtime: Any, session_id: str) -> dict[str, Any]:
        session = runtime.get_navigation_session(session_id)
        session.status = NavSessionStatus.ABORTED
        session.error = "Aborted by user"
        session.cancel()
        session.event_queue.put({"type": "error", "error": "Aborted by user"})
        return {
            "success": True,
            "session_id": session_id,
            "status": "aborted",
        }

    def get_navigation_session(self, runtime: Any, session_id: str) -> Any:
        return runtime.get_navigation_session(session_id)

    def recover_data_display(
        self,
        *,
        data_category: str,
        mode: str,
        loading_watchdog=None,
    ) -> dict[str, Any] | None:
        if self._model.page == GDS2Page.DATA_DISPLAY:
            return None
        if self._model.page == GDS2Page.LOADING:
            return {
                "ok": True,
                "mode": mode,
                "message": "Waiting for GDS2 loading page to finish...",
            }
        self._model.page = GDS2Page.DATA_DISPLAY
        self._model.context["data_category"] = data_category
        return {
            "ok": True,
            "mode": mode,
            "recovered": True,
            "recovery_method": "route_reentry",
            "restart_collection": False,
            "message": "Recovered Data Display after page drift.",
            "recovery_actions": [],
        }


def simulated_snapshot_reader() -> dict[str, object]:
    return {
        "connection_epoch": "sim-epoch-1",
        "connected": True,
        "fresh": True,
        "updated_at": "2026-04-08T00:00:00Z",
        "source": "simulated",
        "sample_count": 3,
        "network_ms": {"last": 8.0, "p50": 9.0, "p95": 12.0},
        "grade": "good",
        "status": "healthy",
        "reason": "simulated runtime",
        "probe_failures": 0,
    }


@dataclass
class SimulatedGDS2Harness:
    model: SimulatedGDS2Model
    controller: SimulatedNavigationController
    workflow: SimulatedWorkflow
    runtime: GDS2ControllerRuntime
    backend: GDS2DiagnosticBackend


def make_simulated_backend() -> SimulatedGDS2Harness:
    model = SimulatedGDS2Model()
    controller = SimulatedNavigationController(model)
    workflow = SimulatedWorkflow(model, controller=controller)
    runtime = GDS2ControllerRuntime(
        controller=controller,
        state_reader=workflow.get_state,
        snapshot_reader=simulated_snapshot_reader,
    )
    runtime.read_all_dtcs = MethodType(lambda self: workflow.read_all_dtcs(), runtime)
    original_build_navigation_runtime = runtime.build_navigation_runtime

    def build_navigation_runtime(self, *, source: str = "registry_runtime"):
        if source == "registry_runtime":
            return SimulatedRegistryNavigationRuntime(
                model=model,
                controller=controller,
            )
        return original_build_navigation_runtime(source=source)

    runtime.build_navigation_runtime = MethodType(build_navigation_runtime, runtime)
    backend = GDS2DiagnosticBackend(runtime=runtime)
    return SimulatedGDS2Harness(
        model=model,
        controller=controller,
        workflow=workflow,
        runtime=runtime,
        backend=backend,
    )


@dataclass
class FakeAgentCollectorRegistry:
    instances: list[Any] = field(default_factory=list)


def install_fake_agent_collector(
    monkeypatch,
    *,
    registry: FakeAgentCollectorRegistry | None = None,
) -> FakeAgentCollectorRegistry | None:
    parameters = [
        {"name": "RPM", "value": "820", "unit": "rpm"},
        {"name": "Coolant Temp", "value": "91", "unit": "C"},
    ]
    dtcs = [
        DTCInfo(
            control_module="Engine",
            dtc_type="Current",
            code="P0001",
            symptom_byte="00",
            description="Fuel Volume Regulator",
            symptom_description="Open",
            status="Active",
        )
    ]

    class FakeAgentCollector:
        def __init__(self, **kwargs) -> None:
            self._on_snapshot = kwargs["on_snapshot"]
            self._on_param_change = kwargs.get("on_param_change")
            self._on_dtc_change = kwargs.get("on_dtc_change")
            self._on_error = kwargs["on_error"]
            self._page_guard = kwargs.get("page_guard")
            self._on_guard_event = kwargs.get("on_guard_event")
            self._is_running = False
            if registry is not None:
                registry.instances.append(self)

        @property
        def is_running(self) -> bool:
            return self._is_running

        def check_agent_available(self):
            return {"available": True}

        def emit_snapshot(self) -> None:
            self._on_snapshot(
                AgentSnapshot(
                    timestamp=1,
                    extraction_count=1,
                    extraction_duration_ms=5,
                    page_context={"page": "data_display"},
                    parameters=parameters,
                    dtcs=dtcs,
                    table_count=1,
                ),
                [],
            )

        def emit_param_change(self, changes: list[dict[str, Any]]) -> None:
            if callable(self._on_param_change):
                self._on_param_change(changes)

        def emit_dtc_change(self, added=None, removed=None) -> None:
            if callable(self._on_dtc_change):
                self._on_dtc_change(added or [], removed or [])

        def emit_guard_event(self, event: dict[str, Any]) -> None:
            if callable(self._on_guard_event):
                self._on_guard_event(event)

        def run_guard(self):
            if callable(self._page_guard):
                return self._page_guard()
            return None

        def emit_error(self, message: str) -> None:
            self._on_error(message)

        def start(self) -> None:
            self._is_running = True
            if registry is None:
                self.emit_snapshot()

        def stop(self) -> None:
            self._is_running = False

    monkeypatch.setattr("backends.gds2.backend.AgentDataCollector", FakeAgentCollector)
    return registry


class FakeAIEngine:
    def __init__(self, *, collection_seconds: int = 0) -> None:
        self.collection_seconds = collection_seconds
        self.started_sessions: list[dict[str, Any]] = []
        self._event_queues: dict[str, Any] = {}
        self._active_session_id: str | None = None

    @property
    def is_active(self) -> bool:
        return self._active_session_id is not None

    def start_session_from_payload(self, vehicle_context, diagnostic_payload) -> str:
        class _AutoCleanupEventQueue:
            def __init__(self, engine: "FakeAIEngine", session_id: str) -> None:
                self._engine = engine
                self._session_id = session_id
                self._queue: queue.Queue[str] = queue.Queue()

            def put_nowait(self, item: str) -> None:
                self._queue.put_nowait(item)

            def get(self, timeout=None):
                item = self._queue.get(timeout=timeout)
                if item.startswith("event: done\n") or item.startswith("event: error\n"):
                    if self._engine._active_session_id == self._session_id:
                        self._engine._active_session_id = None
                    self._engine._event_queues.pop(self._session_id, None)
                return item

        session_id = f"ai-sim-{len(self.started_sessions) + 1}"
        self._active_session_id = session_id
        self.started_sessions.append(
            {
                "session_id": session_id,
                "vehicle_context": dict(vehicle_context),
                "diagnostic_payload": diagnostic_payload,
            }
        )

        event_queue = _AutoCleanupEventQueue(self, session_id)
        event_queue.put_nowait(
            'event: progress\ndata: {"phase": "analyzing", "message": "Simulated AI analysis running"}\n\n'
        )
        event_queue.put_nowait(
            f"event: done\ndata: {json.dumps({'session_id': session_id})}\n\n"
        )
        self._event_queues[session_id] = event_queue
        return session_id

    def get_event_queue(self, session_id: str):
        return self._event_queues.get(session_id)
