"""GDS2 Action Adapter — bridges deterministic executor to real GDS2 automation.

Registers StepHandler callables for each GDS2Action that delegate to the
existing DataViewerWorkflow and NavigationController methods.

Usage::

    from src.gds2_orchestration.adapters import GDS2ActionAdapter
    from src.gds2_orchestration.executor import DeterministicExecutor

    workflow = DataViewerWorkflow()
    executor = DeterministicExecutor()
    adapter = GDS2ActionAdapter(workflow)
    adapter.register_all(executor)

    # Now executor.execute_step(step, state) dispatches to real GDS2 ops.
"""

import logging
from typing import Any, Callable, Dict, Optional

from ..contracts.action_schema import ActionStep, GDS2Action
from ..contracts.state_schema import UIState
from ..executor import DeterministicExecutor, StepHandler

logger = logging.getLogger(__name__)


class GDS2ActionAdapter:
    """Adapts DataViewerWorkflow / NavigationController into StepHandler callables.

    Each handler receives ``(ActionStep, UIState)`` and returns a dict with
    at minimum ``{"success": True/False, ...}``.  On failure the handler
    either raises or returns ``{"success": False, "error": "..."}``.
    """

    def __init__(self, workflow: Any, *, collector_factory: Optional[Callable] = None) -> None:
        """
        Args:
            workflow: A ``DataViewerWorkflow`` instance (or duck-typed equivalent)
                      that exposes ``.controller``, ``.start()``, ``.connect_device()``,
                      ``.select_module()``, ``.select_data_category()``,
                      ``.read_all_dtcs()``, and ``.stop_monitoring()``.
            collector_factory: Optional callable returning an ``AgentDataCollector``
                               for live-stream start/stop.  When ``None``, live-stream
                               handlers return an error.
        """
        self._workflow = workflow
        self._controller = workflow.controller
        self._collector_factory = collector_factory
        self._active_collector: Any = None

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_all(self, executor: DeterministicExecutor) -> None:
        """Register handlers for every supported GDS2Action."""
        mapping: Dict[GDS2Action, StepHandler] = {
            GDS2Action.START_DIAGNOSTICS: self._handle_start_diagnostics,
            GDS2Action.SELECT_DEVICE: self._handle_select_device,
            GDS2Action.CONNECT_DEVICE: self._handle_connect_device,
            GDS2Action.SELECT_MODULE: self._handle_select_module,
            GDS2Action.SELECT_DATA_CATEGORY: self._handle_select_data_category,
            GDS2Action.SELECT_SUB_CATEGORY: self._handle_select_sub_category,
            GDS2Action.READ_DTCS: self._handle_read_dtcs,
            GDS2Action.START_LIVE_STREAM: self._handle_start_live_stream,
            GDS2Action.STOP_LIVE_STREAM: self._handle_stop_live_stream,
            GDS2Action.GO_HOME: self._handle_go_home,
            GDS2Action.GO_BACK: self._handle_go_back,
            GDS2Action.ABORT_SESSION: self._handle_abort_session,
        }
        for action, handler in mapping.items():
            executor.register_handler(action, handler)
        logger.info(
            "GDS2ActionAdapter: registered %d handlers for executor", len(mapping),
        )

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _handle_start_diagnostics(
        self, step: ActionStep, state: UIState,
    ) -> Dict[str, Any]:
        """Start diagnostics flow via DataViewerWorkflow.start().

        Returns device list or module list depending on current GDS2 state.
        """
        logger.info("GDS2Adapter: START_DIAGNOSTICS")
        result = self._workflow.start()
        return {"success": True, **result}

    def _handle_select_device(
        self, step: ActionStep, state: UIState,
    ) -> Dict[str, Any]:
        """Select device in Device Explorer (Win32 dialog).

        Delegates to NavigationController.select_device() which handles
        the Win32 DeviceExplorerController internally.
        """
        device_name = step.args["device_name"]
        logger.info("GDS2Adapter: SELECT_DEVICE -> %s", device_name)
        nav_result = self._controller.select_device(device_name)
        return {
            "success": nav_result.success,
            "page": nav_result.page.value,
            "selected": nav_result.selected,
            "error": nav_result.error,
        }

    def _handle_connect_device(
        self, step: ActionStep, state: UIState,
    ) -> Dict[str, Any]:
        """Connect device end-to-end via DataViewerWorkflow.connect_device().

        This selects the device, clicks Enter on Vehicle Selection, and
        navigates all the way to MODULE_LIST.
        """
        device_name = step.args["device_name"]
        logger.info("GDS2Adapter: CONNECT_DEVICE -> %s", device_name)
        result = self._workflow.connect_device(device_name)
        return {"success": True, **result}

    def _handle_select_module(
        self, step: ActionStep, state: UIState,
    ) -> Dict[str, Any]:
        """Select module via DataViewerWorkflow.select_module().

        Navigates from MODULE_LIST through MODULE_SUBMENU to DATA_LIST.
        May raise BranchDecisionRequiredError if ambiguous.
        """
        module_name = step.args["module_name"]
        logger.info("GDS2Adapter: SELECT_MODULE -> %s", module_name)
        result = self._workflow.select_module(module_name)
        return {"success": True, **(result if isinstance(result, dict) else {})}

    def _handle_select_data_category(
        self, step: ActionStep, state: UIState,
    ) -> Dict[str, Any]:
        """Select data category via DataViewerWorkflow.select_data_category().

        Navigates from DATA_LIST to DATA_DISPLAY, handling SUB_DATA_LIST
        intermediate pages.  May raise BranchDecisionRequiredError.
        """
        category_name = step.args["category_name"]
        logger.info("GDS2Adapter: SELECT_DATA_CATEGORY -> %s", category_name)
        result = self._workflow.select_data_category(category_name)
        return {"success": True, **(result if isinstance(result, dict) else {})}

    def _handle_select_sub_category(
        self, step: ActionStep, state: UIState,
    ) -> Dict[str, Any]:
        """Select sub-category via NavigationController.select_sub_category()."""
        sub_category_name = step.args["sub_category_name"]
        logger.info("GDS2Adapter: SELECT_SUB_CATEGORY -> %s", sub_category_name)
        nav_result = self._controller.select_sub_category(sub_category_name)
        return {
            "success": nav_result.success,
            "page": nav_result.page.value,
            "selected": nav_result.selected,
            "error": nav_result.error,
        }

    def _handle_read_dtcs(
        self, step: ActionStep, state: UIState,
    ) -> Dict[str, Any]:
        """Read DTCs from current DATA_DISPLAY via DataViewerWorkflow.read_all_dtcs()."""
        logger.info("GDS2Adapter: READ_DTCS")
        result = self._workflow.read_all_dtcs()
        return {"success": True, **(result if isinstance(result, dict) else {})}

    def _handle_start_live_stream(
        self, step: ActionStep, state: UIState,
    ) -> Dict[str, Any]:
        """Start live data streaming via AgentDataCollector.

        Requires ``collector_factory`` to have been provided at init time.
        """
        if self._collector_factory is None:
            return {"success": False, "error": "No collector_factory configured for live stream"}

        interval_ms = step.args.get("interval_ms", 1000)
        logger.info("GDS2Adapter: START_LIVE_STREAM (interval=%dms)", interval_ms)

        collector = self._collector_factory()
        collector.start()
        self._active_collector = collector
        return {"success": True, "streaming": True, "interval_ms": interval_ms}

    def _handle_stop_live_stream(
        self, step: ActionStep, state: UIState,
    ) -> Dict[str, Any]:
        """Stop active live data streaming."""
        logger.info("GDS2Adapter: STOP_LIVE_STREAM")
        if self._active_collector is not None:
            self._active_collector.stop()
            self._active_collector = None
        self._workflow.stop_monitoring()
        return {"success": True, "streaming": False}

    def _handle_go_home(
        self, step: ActionStep, state: UIState,
    ) -> Dict[str, Any]:
        """Navigate to main menu via NavigationController.go_home()."""
        logger.info("GDS2Adapter: GO_HOME")
        nav_result = self._controller.go_home()
        return {
            "success": nav_result.success,
            "page": nav_result.page.value,
            "error": nav_result.error,
        }

    def _handle_go_back(
        self, step: ActionStep, state: UIState,
    ) -> Dict[str, Any]:
        """Navigate back one page via NavigationController.go_back()."""
        logger.info("GDS2Adapter: GO_BACK")
        nav_result = self._controller.go_back()
        return {
            "success": nav_result.success,
            "page": nav_result.page.value,
            "error": nav_result.error,
        }

    def _handle_abort_session(
        self, step: ActionStep, state: UIState,
    ) -> Dict[str, Any]:
        """Abort/cleanup: stop monitoring and navigate home."""
        logger.info("GDS2Adapter: ABORT_SESSION")
        try:
            if self._active_collector is not None:
                self._active_collector.stop()
                self._active_collector = None
            self._workflow.stop_monitoring()
        except Exception as exc:
            logger.warning("Cleanup error during abort: %s", exc)

        try:
            nav_result = self._controller.go_home()
            return {
                "success": True,
                "page": nav_result.page.value,
                "aborted": True,
            }
        except Exception as exc:
            logger.warning("go_home failed during abort: %s", exc)
            return {"success": True, "aborted": True, "cleanup_error": str(exc)}

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def get_current_ui_state(self) -> UIState:
        """Build a UIState snapshot from the real NavigationController."""
        page = self._controller.detect_current_page()
        buttons = []
        list_items = []
        try:
            buttons = [b.get("text", "") for b in (self._controller.get_visible_buttons() or [])]
        except Exception:
            pass
        try:
            list_items = self._controller.get_list_items() or []
        except Exception:
            pass
        context = self._controller.get_context()
        return UIState(
            current_page=page.value,
            visible_buttons=buttons,
            list_items=list_items,
            context=context,
        )
