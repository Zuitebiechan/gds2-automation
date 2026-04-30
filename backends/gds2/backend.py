"""GDS2 diagnostic backend facade over the existing src/ implementation."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from backends.gds2.action_adapter import GDS2ActionAdapter
from backends.gds2.action_runtime import PolicyGuard
from diagnostic_platform.action_schema import ActionStep, GDS2Action
from diagnostic_platform.action_runtime import DeterministicExecutor
from diagnostic_platform.branch_planning import BranchDecisionRequiredError
from diagnostic_platform.contracts import (
    BackendActionRuntime,
    BackendCapability,
    BackendNavigationRuntime,
    BackendState,
    ClearResult,
    DTC,
    DiagnosticPayload,
    DiagnosticBackend,
    LiveDataPoint,
    LiveDataStream,
    SamplingQuality,
    VehicleContext,
)
from diagnostic_platform.runtime.errors import OperationCancelledError
from diagnostic_platform.session_observability import emit_gds2_ui_event
from diagnostic_platform.sse import (
    DEFAULT_AGENT_STREAM_SCOPE,
    broadcast_agent_event,
    make_scoped_agent_event_callbacks,
)
from backends.gds2.controller_runtime import GDS2ControllerRuntime
from backends.gds2.vehicle_dtc_status import vehicle_dtc_not_ready_message
from backends.gds2.vehicle_dtc_status import is_vehicle_dtc_information_label
from src.navigation import GDS2Page
from src.streaming import AgentDataCollector, DiagnosticBuffer
from src.streaming.agent_data_collector import AgentSnapshot

logger = logging.getLogger(__name__)

GM_BRANDS = [
    "chevrolet",
    "buick",
    "gmc",
    "cadillac",
    "holden",
    "opel",
    "vauxhall",
    "baojun",
    "wuling",
]

GDS2_ROUTING_ALIASES = [
    "gm china",
    "gmchina",
    "gds2",
]


class GDS2DiagnosticBackend(DiagnosticBackend):
    """Facade that adapts the existing GDS2 automation stack to DiagnosticBackend."""

    _CONNECTED_PAGES = {
        GDS2Page.VEHICLE_SELECTION,
        GDS2Page.DIAGNOSTICS_MENU,
        GDS2Page.MODULE_LIST,
        GDS2Page.MODULE_SUBMENU,
        GDS2Page.DATA_LIST,
        GDS2Page.SUB_DATA_LIST,
        GDS2Page.DATA_DISPLAY,
        GDS2Page.LOADING,
        GDS2Page.J2534_DISCONNECT,
    }

    def __init__(
        self,
        *,
        runtime: GDS2ControllerRuntime | None = None,
    ) -> None:
        """Initialize the backend and bind it to the existing GDS2 workflow."""
        self._runtime = runtime or GDS2ControllerRuntime()
        self._navigation_runtime_source = "registry_runtime"
        self._active_collector: AgentDataCollector | None = None
        self._active_stream: LiveDataStream | None = None
        self._active_stream_scope: str | None = None
        self._latest_live_data: list[LiveDataPoint] = []
        self._stream_error: str | None = None
        self._last_start_result: dict[str, Any] | None = None

    @property
    def name(self) -> str:
        """Return the backend identifier."""
        return "gds2"

    @property
    def display_name(self) -> str:
        return "GDS2"

    @property
    def supported_brands(self) -> list[str]:
        """Return GM brand inputs and compatibility aliases routed to GDS2."""
        return [*GM_BRANDS, *GDS2_ROUTING_ALIASES]

    @property
    def default_for_brands(self) -> list[str]:
        """Return routing aliases that should resolve directly to GDS2."""
        return GDS2_ROUTING_ALIASES.copy()

    @property
    def capabilities(self) -> list[BackendCapability]:
        return [
            BackendCapability.CORE_SESSION,
            BackendCapability.READ_DTCS,
            BackendCapability.LIVE_DATA,
            BackendCapability.AI_DATA_COLLECTION,
            BackendCapability.NAVIGATION,
            BackendCapability.GENERIC_ACTIONS,
            BackendCapability.CLEAR_DTCS,
        ]

    @property
    def latest_live_data(self) -> list[LiveDataPoint]:
        """Return the latest converted live data points from the active stream."""
        return list(self._latest_live_data)

    def start(self, *, cancel_checker: Callable[[], None] | None = None) -> dict[str, Any]:
        """Start GDS2 and auto-connect through the existing workflow."""
        try:
            navigation_runtime = self._get_navigation_runtime()
            if cancel_checker is None:
                result = navigation_runtime.ensure_started()
            else:
                result = navigation_runtime.ensure_started(cancel_checker=cancel_checker)
            self._last_start_result = result if isinstance(result, dict) else None
            return dict(self._last_start_result or {})
        except OperationCancelledError:
            self._last_start_result = None
            raise
        except Exception as exc:  # pragma: no cover - runtime integration wrapper
            self._last_start_result = None
            raise RuntimeError(f"Failed to start GDS2 backend: {exc}") from exc

    def preflight(self) -> dict[str, Any]:
        """Expose controller-runtime readiness and tunnel snapshot data."""
        try:
            return self._runtime.preflight()
        except Exception as exc:  # pragma: no cover - runtime integration wrapper
            raise RuntimeError(f"Failed to preflight GDS2 backend: {exc}") from exc

    def stop(self) -> None:
        """Stop active collection and clear monitoring state."""
        try:
            self._last_start_result = None
            if self._active_collector is not None and self._active_collector.is_running:
                self._active_collector.stop()

            self._active_collector = None
            self._active_stream = None
            self._active_stream_scope = None
            self._latest_live_data = []
            self._stream_error = None
        except Exception as exc:  # pragma: no cover - runtime integration wrapper
            raise RuntimeError(f"Failed to stop GDS2 backend: {exc}") from exc

    def reset_startup_state(self) -> None:
        """Best-effort cleanup for interrupted GDS2 startup flows."""
        try:
            self._last_start_result = None
        except Exception as exc:  # pragma: no cover - runtime integration wrapper
            raise RuntimeError(f"Failed to reset GDS2 startup state: {exc}") from exc

    def connect_vci(self, device: str) -> None:
        """Connect to a VCI device using the existing workflow."""
        try:
            self._last_start_result = None
            self._get_navigation_runtime().connect_vci(device)
        except Exception as exc:  # pragma: no cover - runtime integration wrapper
            raise RuntimeError(f"Failed to connect GDS2 VCI '{device}': {exc}") from exc

    def get_modules(self) -> list[str]:
        """Return available modules from the module list page."""
        try:
            self._require_page(GDS2Page.MODULE_LIST, "retrieve modules")
            modules = self._get_available_items()
            if not modules:
                raise RuntimeError("No modules available on the module list page")
            return modules
        except Exception as exc:  # pragma: no cover - runtime integration wrapper
            cached_modules = self._get_cached_start_modules()
            if cached_modules:
                logger.info(
                    "GDS2 module page check failed; using cached start result modules (%s)",
                    len(cached_modules),
                )
                return cached_modules
            raise RuntimeError(f"Failed to get GDS2 modules: {exc}") from exc

    def select_module(self, module: str) -> None:
        """Select a diagnostic module."""
        try:
            self._last_start_result = None
            self._get_navigation_runtime().select_module(module)
        except Exception as exc:  # pragma: no cover - runtime integration wrapper
            raise RuntimeError(f"Failed to select GDS2 module '{module}': {exc}") from exc

    def get_data_categories(self) -> list[str]:
        """Return available data categories from the data list page."""
        try:
            self._require_page(GDS2Page.DATA_LIST, "retrieve data categories")
            categories = self._get_available_items()
            if not categories:
                raise RuntimeError("No data categories available on the data list page")
            return categories
        except Exception as exc:  # pragma: no cover - runtime integration wrapper
            raise RuntimeError(f"Failed to get GDS2 data categories: {exc}") from exc

    def go_back(self) -> None:
        """Navigate back one step in GDS2."""
        self._last_start_result = None
        self._get_controller().go_back()

    def detect_current_page(self) -> str:
        """Return the current GDS2 page identifier."""
        return self._get_controller().detect_current_page().value

    def execute_action(
        self,
        action: str,
        args: dict[str, Any] | None = None,
        timeout_sec: float = 30.0,
    ) -> dict[str, Any]:
        """Execute a named backend action through the GDS2 action runtime."""
        try:
            step = ActionStep(
                action=GDS2Action[action.upper()],
                args=args or {},
                timeout_sec=timeout_sec,
            )
            action_runtime = self.build_action_runtime()
            executor = action_runtime.executor
            adapter = action_runtime.adapter
            result = executor.execute_step(step, adapter.get_current_ui_state())
            return {
                "success": result.success,
                "metadata": result.metadata,
                "error": result.error,
            }
        except BranchDecisionRequiredError:
            raise
        except Exception as exc:  # pragma: no cover - runtime integration wrapper
            return {
                "success": False,
                "metadata": {},
                "error": str(exc),
            }

    def select_data_category(self, category: str) -> list[str]:
        """Select a data category and return any available sub-items."""
        try:
            self._last_start_result = None
            result = self._get_navigation_runtime().select_data_category(category)
            if isinstance(result, dict):
                sub_categories = result.get("sub_categories")
                if isinstance(sub_categories, list):
                    return [str(item) for item in sub_categories]
            return []
        except Exception as exc:  # pragma: no cover - runtime integration wrapper
            raise RuntimeError(f"Failed to select GDS2 data category '{category}': {exc}") from exc

    def read_dtcs(self) -> list[DTC]:
        """Read DTCs through the controller runtime and map them to platform DTC objects."""
        try:
            result = self.read_dtcs_with_metadata()
            dtcs: list[DTC] = []
            current_module = self._get_controller().current_module or "Unknown Module"

            for raw_dtc in result.get("dtcs", []):
                dtcs.append(
                    DTC(
                        code=str(raw_dtc.get("code", "")).strip(),
                        module=str(raw_dtc.get("control_module") or current_module),
                        status=str(raw_dtc.get("status") or "unknown"),
                        description=str(raw_dtc.get("description") or ""),
                        source_backend=self.name,
                    )
                )

            return dtcs
        except Exception as exc:  # pragma: no cover - runtime integration wrapper
            raise RuntimeError(f"Failed to read GDS2 DTCs: {exc}") from exc

    def read_dtcs_with_metadata(self) -> dict[str, Any]:
        result = self._runtime.read_all_dtcs()
        current_module = self._get_controller().current_module or "Unknown Module"
        dtcs = [
            {
                "code": str(raw_dtc.get("code", "")).strip(),
                "control_module": str(raw_dtc.get("control_module") or current_module),
                "module": str(raw_dtc.get("control_module") or current_module),
                "status": str(raw_dtc.get("status") or "unknown"),
                "description": str(raw_dtc.get("description") or ""),
                "source_backend": self.name,
            }
            for raw_dtc in result.get("dtcs", [])
        ]
        return {
            "dtcs": dtcs,
            "dtc_count": int(result.get("dtc_count") or len(dtcs)),
            "page_context": result.get("page_context"),
            "vehicle_dtc_status": dict(result.get("vehicle_dtc_status") or {}),
            "dtc_display_mode": str(result.get("dtc_display_mode") or "dtc_detail"),
        }

    def start_live_data(self) -> LiveDataStream:
        """Start AgentDataCollector streaming from the current Data Display page."""
        try:
            self._require_page(GDS2Page.DATA_DISPLAY, "start live data streaming")

            if self._active_collector is not None and self._active_collector.is_running:
                return LiveDataStream(
                    session_id=self._active_stream.session_id if self._active_stream else uuid4().hex,
                    active=True,
                )

            self._stream_error = None
            self._latest_live_data = []
            session_id = uuid4().hex
            self._active_stream = LiveDataStream(session_id=session_id, active=True)
            self._active_collector = AgentDataCollector(
                on_snapshot=self._handle_snapshot,
                on_error=self._handle_stream_error,
            )
            self._active_collector.start()
            return self._active_stream
        except Exception as exc:  # pragma: no cover - runtime integration wrapper
            raise RuntimeError(f"Failed to start GDS2 live data: {exc}") from exc

    def stop_live_data(self) -> None:
        """Stop the active AgentDataCollector stream."""
        try:
            if self._active_collector is None or not self._active_collector.is_running:
                raise RuntimeError("No active GDS2 live data stream")

            self._active_collector.stop()
            self._active_collector = None

            if self._active_stream is not None:
                self._active_stream = LiveDataStream(
                    session_id=self._active_stream.session_id,
                    active=False,
                )
        except Exception as exc:  # pragma: no cover - runtime integration wrapper
            raise RuntimeError(f"Failed to stop GDS2 live data: {exc}") from exc

    def start_live_data_session(
        self,
        *,
        data_category: str,
        interval_ms: int,
        stream_scope: str,
    ) -> dict[str, Any]:
        """Own GDS2 live-data collection, SSE wiring, and Data Display guarding."""
        if not data_category:
            raise ValueError("Data category required")

        resolved_scope = stream_scope or DEFAULT_AGENT_STREAM_SCOPE
        if self._active_collector is not None and self._active_collector.is_running:
            if self._active_stream_scope == resolved_scope:
                return {
                    "success": True,
                    "message": "Streaming already running",
                }
            self.stop_live_data_session()

        self._ensure_data_display(data_category)
        callbacks = make_scoped_agent_event_callbacks(resolved_scope)
        page_guard = self.build_data_display_guard(
            data_category=data_category,
            mode="stream",
        )

        def on_snapshot(snapshot: AgentSnapshot, changes: list[dict[str, Any]]) -> None:
            self._handle_snapshot(snapshot, changes)
            callbacks["on_snapshot"](snapshot, changes)

        def on_error(message: str) -> None:
            self._handle_stream_error(message)
            callbacks["on_error"](message)

        def on_guard_event(event: dict[str, Any]) -> None:
            message = str(event.get("message") or "").strip()
            if message:
                broadcast_agent_event(resolved_scope, "guard", {"message": message})

        self._stream_error = None
        self._latest_live_data = []
        self._active_stream = LiveDataStream(session_id=uuid4().hex, active=True)
        self._active_stream_scope = resolved_scope
        self._active_collector = AgentDataCollector(
            on_snapshot=on_snapshot,
            on_param_change=callbacks["on_param_change"],
            on_dtc_change=callbacks["on_dtc_change"],
            on_error=on_error,
            page_guard=page_guard,
            on_guard_event=on_guard_event,
            interval_ms=interval_ms,
        )
        self._active_collector.start()
        return {
            "success": True,
            "message": "Live data streaming started",
            "interval_ms": interval_ms,
        }

    def stop_live_data_session(self) -> dict[str, Any]:
        """Stop backend-owned GDS2 live-data collection."""
        if self._active_collector is not None:
            self._active_collector.stop()
        self._active_collector = None
        self._active_stream_scope = None

        if self._active_stream is not None:
            self._active_stream = LiveDataStream(
                session_id=self._active_stream.session_id,
                active=False,
            )

        return {"success": True, "message": "Live data stopped"}

    def collect_ai_payload(
        self,
        *,
        vehicle_context: dict[str, Any],
        data_category: str,
        collection_seconds: int,
    ) -> DiagnosticPayload:
        """Collect a backend-owned AI payload from GDS2 Data Display."""
        if not data_category:
            raise ValueError("Data category required")

        self._ensure_data_display(data_category)
        buffer = DiagnosticBuffer(window_seconds=max(1, collection_seconds or 1))
        live_data: list[LiveDataPoint] = []
        latest_dtcs: list[DTC] = []
        collector_error: dict[str, str | None] = {"error": None}
        guard_event_state: dict[str, dict[str, Any] | None] = {"event": None}

        def on_snapshot(snapshot: AgentSnapshot, _changes: list[dict[str, Any]]) -> None:
            nonlocal latest_dtcs
            buffer.append_snapshot(snapshot)
            live_data.extend(self._snapshot_to_live_data_points(snapshot))
            latest_dtcs = self._snapshot_to_dtcs(snapshot)

        def on_error(message: str) -> None:
            collector_error["error"] = message

        def on_guard_event(event: dict[str, Any]) -> None:
            guard_event_state["event"] = event

        collector = AgentDataCollector(
            on_snapshot=on_snapshot,
            on_error=on_error,
            page_guard=self.build_data_display_guard(
                data_category=data_category,
                mode="ai_collect",
            ),
            on_guard_event=on_guard_event,
            interval_ms=100,
        )

        availability_checker = getattr(collector, "check_agent_available", None)
        if callable(availability_checker):
            availability = availability_checker()
            if isinstance(availability, dict) and not availability.get("available"):
                raise RuntimeError("Java Agent not available. Start GDS2 with the agent.")

        collector.start()
        try:
            deadline = time.time() + max(0, collection_seconds)
            while time.time() < deadline:
                if collector_error["error"]:
                    raise RuntimeError(str(collector_error["error"]))

                guard_event = guard_event_state.get("event")
                if guard_event:
                    guard_event_state["event"] = None
                    if not guard_event.get("ok", True):
                        raise RuntimeError(
                            str(guard_event.get("error") or "Data Display guard failed.")
                        )
                    if guard_event.get("restart_collection"):
                        buffer.clear()
                        live_data.clear()
                        latest_dtcs = []
                        deadline = time.time() + max(0, collection_seconds)

                time.sleep(1.0)
        finally:
            collector.stop()

        if collector_error["error"]:
            raise RuntimeError(str(collector_error["error"]))

        return DiagnosticPayload(
            vehicle_context=self._build_vehicle_context(vehicle_context),
            dtcs=latest_dtcs,
            live_data=live_data,
            sampling_quality=self._sampling_quality_from_buffer(buffer),
            source_backend=self.name,
        )

    def clear_dtcs(self) -> ClearResult:
        """Clear DTCs through the registry-driven runtime."""
        if self._active_collector is not None and self._active_collector.is_running:
            raise RuntimeError("Cannot clear GDS2 DTCs while live data streaming is active")

        try:
            state = self.get_state()
            vehicle_dtc_status = {}
            if isinstance(getattr(state, "extra", None), dict):
                vehicle_dtc_status = dict(state.extra.get("vehicle_dtc_status") or {})
            if (
                state.current_page == GDS2Page.DATA_DISPLAY.value
                and (
                    vehicle_dtc_status.get("applicable")
                    or is_vehicle_dtc_information_label(state.current_data_category)
                )
                and not vehicle_dtc_status.get("ready")
            ):
                raise RuntimeError(vehicle_dtc_not_ready_message(vehicle_dtc_status))

            outcome = self._get_clear_dtcs_navigation_runtime().clear_dtcs()
            if isinstance(outcome, ClearResult):
                if not outcome.success:
                    raise RuntimeError(outcome.message or "GDS2 clear DTCs failed")
                return outcome

            if isinstance(outcome, dict):
                success = bool(outcome.get("success", True))
                message = str(outcome.get("message") or "Clear DTCs completed")
                if not success:
                    raise RuntimeError(message)
                if outcome.get("recovery_actions"):
                    logger.info(
                        "[GDS2_CLEAR_DTCS] runtime=%s page_context=%s recovery_actions=%s",
                        "registry_runtime",
                        outcome.get("page_context"),
                        outcome.get("recovery_actions"),
                    )
                return ClearResult(
                    success=True,
                    cleared_count=int(outcome.get("cleared_count") or 0),
                    message=message,
                )

            raise RuntimeError("GDS2 clear DTCs returned an unsupported result")
        except Exception as exc:  # pragma: no cover - runtime integration wrapper
            raise RuntimeError(f"Failed to clear GDS2 DTCs: {exc}") from exc

    def get_state(self) -> BackendState:
        """Build platform BackendState from the workflow and navigation controller."""
        try:
            state = self._runtime.status()
            navigation_runtime_status = None
            runtime_status_reader = getattr(self._get_navigation_runtime(), "get_runtime_status", None)
            if callable(runtime_status_reader):
                navigation_runtime_status = runtime_status_reader()
            state.extra = {
                **state.extra,
                "navigation_runtime_source": self._navigation_runtime_source,
                "clear_dtcs_navigation_runtime_source": "registry_runtime",
                "recovery_navigation_runtime_source": "registry_runtime",
                "navigation_runtime_status": navigation_runtime_status,
                "stream_active": bool(
                    self._active_collector is not None and self._active_collector.is_running
                ),
                "stream_session_id": (
                    self._active_stream.session_id if self._active_stream is not None else None
                ),
                "latest_live_data_count": len(self._latest_live_data),
                "stream_error": self._stream_error,
            }
            return state
        except Exception as exc:  # pragma: no cover - runtime integration wrapper
            raise RuntimeError(f"Failed to get GDS2 backend state: {exc}") from exc

    def _handle_snapshot(self, snapshot: AgentSnapshot, _changes: list[dict[str, Any]]) -> None:
        """Convert the latest Agent snapshot into platform live data points."""
        timestamp = snapshot.collected_at_s or snapshot.agent_timestamp_s or time.time()
        converted_points: list[LiveDataPoint] = []

        for parameter in snapshot.parameters:
            numeric_value = self._coerce_float(parameter.get("value"))
            if numeric_value is None:
                continue

            name = str(parameter.get("name") or "").strip()
            if not name:
                continue

            converted_points.append(
                LiveDataPoint(
                    parameter=name,
                    value=numeric_value,
                    unit=str(parameter.get("unit") or "").strip(),
                    timestamp=timestamp,
                )
            )

        self._latest_live_data = converted_points

    def _handle_stream_error(self, message: str) -> None:
        """Record stream errors from AgentDataCollector callbacks."""
        self._stream_error = message
        logger.warning("GDS2 live data stream error: %s", message)

    def build_data_display_guard(
        self,
        *,
        data_category: str,
        mode: str,
        check_interval: float = 5.0,
    ):
        """Build the GDS2-specific Data Display guard used by live and AI collection."""
        if mode not in {"stream", "ai_collect"}:
            raise ValueError(f"Unsupported guard mode: {mode}")

        last_check_ts: list[float] = [0.0]
        last_result: list[dict[str, Any] | None] = [None]
        recovery_runtime = self._get_recovery_navigation_runtime()
        loading_watchdog = None
        if hasattr(recovery_runtime, "_loading_timeout_sec") and hasattr(
            recovery_runtime,
            "_max_loading_restarts",
        ):
            from backends.gds2.registry_navigation_runtime import LoadingWatchdog

            loading_watchdog = LoadingWatchdog(
                timeout_sec=float(getattr(recovery_runtime, "_loading_timeout_sec")),
                max_restarts=int(getattr(recovery_runtime, "_max_loading_restarts")),
            )

        def guard() -> dict[str, Any] | None:
            now = time.time()
            if now - last_check_ts[0] < check_interval:
                return last_result[0]

            last_check_ts[0] = now
            page = self.detect_current_page()
            if page == GDS2Page.DATA_DISPLAY.value:
                last_result[0] = None
                return None

            emit_gds2_ui_event(
                "page_guard_triggered",
                operation_kind=f"data_display_guard:{mode}",
                page=page,
                reason="page_drift",
                data_category=data_category,
                guard_mode=mode,
            )
            if page == GDS2Page.J2534_DISCONNECT.value:
                emit_gds2_ui_event(
                    "j2534_disconnect_page_seen",
                    operation_kind=f"data_display_guard:{mode}",
                    page=page,
                    reason="j2534_disconnect_seen",
                    data_category=data_category,
                    guard_mode=mode,
                )

            last_check_ts[0] = 0.0

            recover = getattr(recovery_runtime, "recover_data_display", None)
            if callable(recover):
                recovery_event = recover(
                    data_category=data_category,
                    mode=mode,
                    loading_watchdog=loading_watchdog,
                )
                if recovery_event is not None:
                    if recovery_event.get("recovery_actions"):
                        logger.info(
                            "[GDS2_GUARD] runtime=%s mode=%s page=%s recovery_actions=%s",
                            "registry_runtime",
                            mode,
                            page,
                            recovery_event.get("recovery_actions"),
                        )
                    last_result[0] = dict(recovery_event)
                    return last_result[0]

            last_result[0] = self._legacy_data_display_guard_error(page=page, mode=mode)
            return last_result[0]

        return guard

    @staticmethod
    def _legacy_data_display_guard_error(*, page: str, mode: str) -> dict[str, Any]:
        if page == GDS2Page.LOADING.value:
            return {
                "ok": True,
                "mode": mode,
                "message": "Waiting for GDS2 loading page to finish...",
            }
        if page == GDS2Page.J2534_DISCONNECT.value:
            if mode == "ai_collect":
                return {
                    "ok": False,
                    "mode": mode,
                    "error": (
                        "Lost communication with J2534 during AI collection and could not "
                        "restore Data Display in-place. Please reconnect and restart AI "
                        "Diagnostics."
                    ),
                }
            return {
                "ok": False,
                "mode": mode,
                "error": (
                    "Lost communication with J2534 and could not restore Data Display. "
                    "Please reconnect and restart live monitoring."
                ),
            }
        return {
            "ok": False,
            "mode": mode,
            "error": (
                f"Data Display guard detected page drift to {page}. "
                "Please return to Data Display and retry."
            ),
        }

    def _get_cached_start_modules(self) -> list[str]:
        if not isinstance(self._last_start_result, dict):
            return []
        modules = self._last_start_result.get("modules")
        if not isinstance(modules, list):
            return []
        return [str(module) for module in modules if str(module).strip()]

    def _get_available_items(self) -> list[str]:
        """Read current-page list items through the controller helper/fallback."""
        controller = self._get_controller()
        getter = getattr(controller, "get_available_items", None)
        if callable(getter):
            items = getter()
            if isinstance(items, list):
                return [str(item) for item in items]
            raise RuntimeError("Controller get_available_items() returned a non-list value")
        return list(controller.wait_for_list())

    def _get_controller(self) -> Any:
        """Return the bound navigation controller, creating the workflow if required."""
        return self._runtime.get_controller()

    def get_navigation_runtime(self) -> BackendNavigationRuntime:
        return self._get_navigation_runtime()

    def get_navigation_runtime_source(self) -> str:
        return self._navigation_runtime_source

    def _get_navigation_runtime(self) -> BackendNavigationRuntime:
        return self._runtime.build_navigation_runtime(source=self._navigation_runtime_source)

    def _get_clear_dtcs_navigation_runtime(self) -> BackendNavigationRuntime:
        return self._runtime.build_navigation_runtime(source="registry_runtime")

    def _get_recovery_navigation_runtime(self) -> BackendNavigationRuntime:
        return self._runtime.build_navigation_runtime(source="registry_runtime")

    def build_action_runtime(self) -> BackendActionRuntime:
        """Build a backend-owned executor/adapter bridge for generic actions."""
        executor = DeterministicExecutor(policy_guard=PolicyGuard())
        adapter = GDS2ActionAdapter(self, controller=self._get_controller())
        adapter.register_all(executor)
        return BackendActionRuntime(executor=executor, adapter=adapter)

    def _require_page(self, expected_page: GDS2Page, action: str) -> GDS2Page:
        """Ensure GDS2 is on the expected page before performing an action."""
        current_page = self._get_controller().detect_current_page()
        if current_page != expected_page:
            raise RuntimeError(
                f"Cannot {action} while on page '{current_page.value}'; expected '{expected_page.value}'"
            )
        return current_page

    def _ensure_data_display(self, data_category: str) -> None:
        current_page = self.detect_current_page()
        if current_page != GDS2Page.DATA_DISPLAY.value:
            self.select_data_category(data_category)
        emit_gds2_ui_event(
            "data_display_entered",
            operation_kind="ensure_data_display",
            page=GDS2Page.DATA_DISPLAY.value,
            reason="data_display_ready",
            data_category=data_category,
        )

    def _snapshot_to_live_data_points(self, snapshot: AgentSnapshot) -> list[LiveDataPoint]:
        timestamp = snapshot.collected_at_s or snapshot.agent_timestamp_s or time.time()
        converted_points: list[LiveDataPoint] = []

        for parameter in snapshot.parameters:
            numeric_value = self._coerce_float(parameter.get("value"))
            if numeric_value is None:
                continue

            name = str(parameter.get("name") or "").strip()
            if not name:
                continue

            converted_points.append(
                LiveDataPoint(
                    parameter=name,
                    value=numeric_value,
                    unit=str(parameter.get("unit") or "").strip(),
                    timestamp=timestamp,
                )
            )

        return converted_points

    def _snapshot_to_dtcs(self, snapshot: AgentSnapshot) -> list[DTC]:
        return [
            DTC(
                code=str(dtc.code).strip(),
                module=str(dtc.control_module or "Unknown Module"),
                status=str(dtc.status or "unknown"),
                description=str(dtc.description or ""),
                source_backend=self.name,
            )
            for dtc in snapshot.dtcs
            if str(dtc.code).strip()
        ]

    def _build_vehicle_context(self, vehicle_context: dict[str, Any]) -> VehicleContext:
        state = self.get_state()
        extra = state.extra if isinstance(state.extra, dict) else {}
        return VehicleContext(
            brand=str(vehicle_context.get("brand") or "GM"),
            model=str(vehicle_context.get("model") or ""),
            vin=(
                str(vehicle_context.get("vin") or "").strip()
                or str(extra.get("vin") or "").strip()
                or None
            ),
            problem_description=str(vehicle_context.get("problem_description") or "").strip()
            or None,
        )

    @staticmethod
    def _sampling_quality_from_buffer(buffer: DiagnosticBuffer) -> SamplingQuality:
        quality = buffer.get_delta_payload().get("sampling_quality", {})
        grade = str(quality.get("grade") or "").upper()
        if grade == "A":
            return SamplingQuality.GOOD
        if grade == "B":
            return SamplingQuality.FAIR
        return SamplingQuality.POOR

    @staticmethod
    def _coerce_float(value: Any) -> float | None:
        """Best-effort conversion of Agent parameter values to float."""
        if value is None:
            return None

        if isinstance(value, (int, float)):
            return float(value)

        text = str(value).strip()
        if not text:
            return None

        normalized = text.replace(",", "")
        try:
            return float(normalized)
        except ValueError:
            match = re.search(r"[-+]?\d+(?:\.\d+)?", normalized)
            if match is None:
                return None
            return float(match.group(0))
