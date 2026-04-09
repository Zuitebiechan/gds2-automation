"""GDS2 action adapter bridging deterministic actions to the existing workflow."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from backends.gds2.action_runtime import UIState
from diagnostic_platform.action_runtime import DeterministicExecutor, StepHandler
from diagnostic_platform.action_schema import ActionStep, GDS2Action

logger = logging.getLogger(__name__)


class GDS2ActionAdapter:
    """Adapt the GDS2 workflow/controller pair into deterministic step handlers."""

    def __init__(
        self,
        workflow: Any,
        *,
        collector_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._workflow = workflow
        self._controller = workflow.controller
        self._collector_factory = collector_factory
        self._active_collector: Any = None

    def register_all(self, executor: DeterministicExecutor) -> None:
        """Register handlers for all supported GDS2 actions."""
        mapping: dict[GDS2Action, StepHandler] = {
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
        logger.info("GDS2ActionAdapter registered %d handlers for executor", len(mapping))

    def _handle_start_diagnostics(self, step: ActionStep, state: UIState) -> dict[str, Any]:
        logger.info("GDS2Adapter: START_DIAGNOSTICS")
        result = self._workflow.start()
        return {"success": True, **result}

    def _handle_select_device(self, step: ActionStep, state: UIState) -> dict[str, Any]:
        device_name = step.args["device_name"]
        logger.info("GDS2Adapter: SELECT_DEVICE -> %s", device_name)
        nav_result = self._controller.select_device(device_name)
        return {
            "success": nav_result.success,
            "page": nav_result.page.value,
            "selected": nav_result.selected,
            "error": nav_result.error,
        }

    def _handle_connect_device(self, step: ActionStep, state: UIState) -> dict[str, Any]:
        device_name = step.args["device_name"]
        logger.info("GDS2Adapter: CONNECT_DEVICE -> %s", device_name)
        result = self._workflow.connect_device(device_name)
        return {"success": True, **result}

    def _handle_select_module(self, step: ActionStep, state: UIState) -> dict[str, Any]:
        module_name = step.args["module_name"]
        logger.info("GDS2Adapter: SELECT_MODULE -> %s", module_name)
        return {"success": True, **self._coerce_mapping_result(self._workflow.select_module(module_name))}

    def _handle_select_data_category(
        self,
        step: ActionStep,
        state: UIState,
    ) -> dict[str, Any]:
        category_name = step.args["category_name"]
        logger.info("GDS2Adapter: SELECT_DATA_CATEGORY -> %s", category_name)
        return {
            "success": True,
            **self._coerce_mapping_result(self._workflow.select_data_category(category_name)),
        }

    def _handle_select_sub_category(
        self,
        step: ActionStep,
        state: UIState,
    ) -> dict[str, Any]:
        sub_category_name = step.args["sub_category_name"]
        logger.info("GDS2Adapter: SELECT_SUB_CATEGORY -> %s", sub_category_name)
        nav_result = self._controller.select_sub_category(sub_category_name)
        return {
            "success": nav_result.success,
            "page": nav_result.page.value,
            "selected": nav_result.selected,
            "error": nav_result.error,
        }

    def _handle_read_dtcs(self, step: ActionStep, state: UIState) -> dict[str, Any]:
        logger.info("GDS2Adapter: READ_DTCS")
        return {"success": True, **self._coerce_mapping_result(self._workflow.read_all_dtcs())}

    def _handle_start_live_stream(self, step: ActionStep, state: UIState) -> dict[str, Any]:
        if self._collector_factory is None:
            return {"success": False, "error": "No collector_factory configured for live stream"}

        interval_ms = step.args.get("interval_ms", 1000)
        logger.info("GDS2Adapter: START_LIVE_STREAM (interval=%dms)", interval_ms)

        collector = self._collector_factory()
        collector.start()
        self._active_collector = collector
        return {"success": True, "streaming": True, "interval_ms": interval_ms}

    def _handle_stop_live_stream(self, step: ActionStep, state: UIState) -> dict[str, Any]:
        logger.info("GDS2Adapter: STOP_LIVE_STREAM")
        if self._active_collector is not None:
            self._active_collector.stop()
            self._active_collector = None
        self._workflow.stop_monitoring()
        return {"success": True, "streaming": False}

    def _handle_go_home(self, step: ActionStep, state: UIState) -> dict[str, Any]:
        logger.info("GDS2Adapter: GO_HOME")
        nav_result = self._controller.go_home()
        return {
            "success": nav_result.success,
            "page": nav_result.page.value,
            "error": nav_result.error,
        }

    def _handle_go_back(self, step: ActionStep, state: UIState) -> dict[str, Any]:
        logger.info("GDS2Adapter: GO_BACK")
        nav_result = self._controller.go_back()
        return {
            "success": nav_result.success,
            "page": nav_result.page.value,
            "error": nav_result.error,
        }

    def _handle_abort_session(self, step: ActionStep, state: UIState) -> dict[str, Any]:
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

    def get_current_ui_state(self) -> UIState:
        """Build a UIState snapshot from the live navigation controller."""
        page = self._controller.detect_current_page()
        buttons: list[str] = []
        list_items: list[str] = []
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

    @staticmethod
    def _coerce_mapping_result(result: Any) -> dict[str, Any]:
        if isinstance(result, dict):
            return result
        return {}


__all__ = ["GDS2ActionAdapter"]
