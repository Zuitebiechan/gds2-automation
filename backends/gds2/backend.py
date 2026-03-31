"""GDS2 diagnostic backend facade over the existing src/ implementation."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from diagnostic_platform.contracts import (
    BackendState,
    ClearResult,
    DTC,
    DiagnosticBackend,
    LiveDataPoint,
    LiveDataStream,
)
from diagnostic_platform.runtime.worker_runtime import OperationCancelledError
from backends.gds2.controller_runtime import GDS2ControllerRuntime
from src.navigation import GDS2Page, NavigationController
from src.streaming import AgentDataCollector
from src.streaming.agent_data_collector import AgentSnapshot

if TYPE_CHECKING:
    from src.workflows.data_viewer import DataViewerWorkflow

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
        workflow: DataViewerWorkflow | None = None,
        *,
        runtime: GDS2ControllerRuntime | None = None,
    ) -> None:
        """Initialize the backend and bind it to the existing GDS2 workflow."""
        self._runtime = runtime or GDS2ControllerRuntime(workflow=workflow)
        self._active_collector: AgentDataCollector | None = None
        self._active_stream: LiveDataStream | None = None
        self._latest_live_data: list[LiveDataPoint] = []
        self._stream_error: str | None = None
        self._last_start_result: dict[str, Any] | None = None

    @property
    def name(self) -> str:
        """Return the backend identifier."""
        return "gds2"

    @property
    def supported_brands(self) -> list[str]:
        """Return the GM brands supported by GDS2."""
        return GM_BRANDS.copy()

    @property
    def latest_live_data(self) -> list[LiveDataPoint]:
        """Return the latest converted live data points from the active stream."""
        return list(self._latest_live_data)

    def start(self, *, cancel_checker: Callable[[], None] | None = None) -> dict[str, Any]:
        """Start GDS2 and auto-connect through the existing workflow."""
        try:
            if cancel_checker is None:
                result = self._runtime.ensure_ready()
            else:
                result = self._runtime.ensure_ready(cancel_checker=cancel_checker)
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
            self._latest_live_data = []
            self._stream_error = None

            workflow = getattr(self._runtime, "_workflow", None)
            if workflow is not None and workflow.get_state().get("data_category"):
                workflow.stop_monitoring()
        except Exception as exc:  # pragma: no cover - runtime integration wrapper
            raise RuntimeError(f"Failed to stop GDS2 backend: {exc}") from exc

    def reset_startup_state(self) -> None:
        """Best-effort cleanup for interrupted GDS2 startup flows."""
        try:
            self._last_start_result = None
            workflow = getattr(self._runtime, "_workflow", None)
            if workflow is not None:
                workflow.reset_startup_state()
        except Exception as exc:  # pragma: no cover - runtime integration wrapper
            raise RuntimeError(f"Failed to reset GDS2 startup state: {exc}") from exc

    def connect_vci(self, device: str) -> None:
        """Connect to a VCI device using the existing workflow."""
        try:
            self._last_start_result = None
            self._get_workflow().connect_device(device)
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
            self._get_workflow().select_module(module)
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
        """Execute a named GDS2 action through the shared agentic executor."""
        try:
            try:
                from src.agentic import DeterministicExecutor, GDS2ActionAdapter
                from src.agentic import ActionStep, GDS2Action
            except ImportError:
                from src.agentic import DeterministicExecutor, GDS2ActionAdapter
                from src.agentic.contracts import ActionStep, GDS2Action

            step = ActionStep(
                action=GDS2Action[action.upper()],
                args=args or {},
                timeout_sec=timeout_sec,
            )
            executor = DeterministicExecutor()
            adapter = GDS2ActionAdapter(self._get_workflow())
            adapter.register_all(executor)
            result = executor.execute_step(step, adapter.get_current_ui_state())
            return {
                "success": result.success,
                "metadata": result.metadata,
                "error": result.error,
            }
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
            result = self._get_workflow().select_data_category(category)
            if isinstance(result, dict):
                sub_categories = result.get("sub_categories")
                if isinstance(sub_categories, list):
                    return [str(item) for item in sub_categories]
            return []
        except Exception as exc:  # pragma: no cover - runtime integration wrapper
            raise RuntimeError(f"Failed to select GDS2 data category '{category}': {exc}") from exc

    def read_dtcs(self) -> list[DTC]:
        """Read DTCs through the existing workflow and map them to platform DTC objects."""
        try:
            result = self._get_workflow().read_all_dtcs()
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

    def clear_dtcs(self) -> ClearResult:
        """Clear DTCs is not implemented for GDS2 yet."""
        raise NotImplementedError("GDS2 clear DTC not yet implemented.")

    def get_state(self) -> BackendState:
        """Build platform BackendState from the workflow and navigation controller."""
        try:
            state = self._runtime.status()
            state.extra = {
                **state.extra,
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

    def _get_workflow(self) -> Any:
        """Return the bound workflow, creating it lazily when needed."""
        return self._runtime.get_workflow()

    def _get_controller(self) -> NavigationController:
        """Return the bound navigation controller, creating the workflow if required."""
        return self._runtime.get_controller()

    @staticmethod
    def _create_workflow() -> Any:
        """Lazily import and construct DataViewerWorkflow."""
        try:
            from src.workflows.data_viewer import DataViewerWorkflow

            return DataViewerWorkflow()
        except Exception as exc:  # pragma: no cover - runtime integration wrapper
            raise RuntimeError(f"Failed to initialize DataViewerWorkflow: {exc}") from exc

    def _require_page(self, expected_page: GDS2Page, action: str) -> GDS2Page:
        """Ensure GDS2 is on the expected page before performing an action."""
        current_page = self._get_controller().detect_current_page()
        if current_page != expected_page:
            raise RuntimeError(
                f"Cannot {action} while on page '{current_page.value}'; expected '{expected_page.value}'"
            )
        return current_page

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
