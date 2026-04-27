"""GDS2 action adapter bridging deterministic actions to the backend facade."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from backends.gds2.action_runtime import UIState
from diagnostic_platform.action_runtime import DeterministicExecutor, StepHandler
from diagnostic_platform.action_schema import ActionStep, GDS2Action
from src.navigation import ControllerSnapshot

logger = logging.getLogger(__name__)


class GDS2ActionAdapter:
    """Adapt the GDS2 backend/controller pair into deterministic step handlers."""

    def __init__(
        self,
        backend: Any,
        *,
        controller: Any,
        collector_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._backend = backend
        self._controller = controller
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
        result = self._backend.start()
        return {"success": True, **result}

    def _handle_select_device(self, step: ActionStep, state: UIState) -> dict[str, Any]:
        device_name = step.args["device_name"]
        logger.info("GDS2Adapter: SELECT_DEVICE -> %s", device_name)
        return self._run_navigation_command(
            "select_device",
            device_name,
            include_selected=True,
        )

    def _handle_connect_device(self, step: ActionStep, state: UIState) -> dict[str, Any]:
        device_name = step.args["device_name"]
        logger.info("GDS2Adapter: CONNECT_DEVICE -> %s", device_name)
        self._backend.connect_vci(device_name)
        return {"success": True, "device": device_name}

    def _handle_select_module(self, step: ActionStep, state: UIState) -> dict[str, Any]:
        module_name = step.args["module_name"]
        logger.info("GDS2Adapter: SELECT_MODULE -> %s", module_name)
        self._backend.select_module(module_name)
        return {
            "success": True,
            "selected_module": module_name,
            "data_categories": self._backend.get_data_categories(),
        }

    def _handle_select_data_category(
        self,
        step: ActionStep,
        state: UIState,
    ) -> dict[str, Any]:
        category_name = step.args["category_name"]
        logger.info("GDS2Adapter: SELECT_DATA_CATEGORY -> %s", category_name)
        return {
            "success": True,
            "selected_data_category": category_name,
            "items": self._backend.select_data_category(category_name),
        }

    def _handle_select_sub_category(
        self,
        step: ActionStep,
        state: UIState,
    ) -> dict[str, Any]:
        sub_category_name = step.args["sub_category_name"]
        logger.info("GDS2Adapter: SELECT_SUB_CATEGORY -> %s", sub_category_name)
        return self._run_navigation_command(
            "select_sub_category",
            sub_category_name,
            include_selected=True,
        )

    def _handle_read_dtcs(self, step: ActionStep, state: UIState) -> dict[str, Any]:
        logger.info("GDS2Adapter: READ_DTCS")
        dtcs = self._backend.read_dtcs()
        return {
            "success": True,
            "dtcs": [
                {
                    "code": dtc.code,
                    "control_module": dtc.module,
                    "module": dtc.module,
                    "status": dtc.status,
                    "description": dtc.description,
                    "source_backend": dtc.source_backend,
                }
                for dtc in dtcs
            ],
            "dtc_count": len(dtcs),
        }

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
        stop_session = getattr(self._backend, "stop_live_data_session", None)
        if callable(stop_session):
            stop_session()
        return {"success": True, "streaming": False}

    def _handle_go_home(self, step: ActionStep, state: UIState) -> dict[str, Any]:
        logger.info("GDS2Adapter: GO_HOME")
        return self._run_navigation_command("go_home")

    def _handle_go_back(self, step: ActionStep, state: UIState) -> dict[str, Any]:
        logger.info("GDS2Adapter: GO_BACK")
        return self._run_navigation_command("go_back")

    def _handle_abort_session(self, step: ActionStep, state: UIState) -> dict[str, Any]:
        logger.info("GDS2Adapter: ABORT_SESSION")
        try:
            if self._active_collector is not None:
                self._active_collector.stop()
                self._active_collector = None
            stop_session = getattr(self._backend, "stop_live_data_session", None)
            if callable(stop_session):
                stop_session()
        except Exception as exc:
            logger.warning("Cleanup error during abort: %s", exc)

        try:
            nav_result = self._run_navigation_command("go_home")
            return {
                "success": True,
                "page": nav_result.get("page"),
                "aborted": True,
            }
        except Exception as exc:
            logger.warning("go_home failed during abort: %s", exc)
            return {"success": True, "aborted": True, "cleanup_error": str(exc)}

    def _run_navigation_command(
        self,
        command_name: str,
        *args: Any,
        include_selected: bool = False,
    ) -> dict[str, Any]:
        command = getattr(self._controller, command_name)
        nav_result = command(*args)
        page = getattr(nav_result, "page", None)
        payload = {
            "success": bool(getattr(nav_result, "success", False)),
            "page": getattr(page, "value", str(page) if page is not None else None),
            "error": getattr(nav_result, "error", None),
        }
        if include_selected:
            payload["selected"] = getattr(nav_result, "selected", None)
        return payload

    def get_current_ui_state(self) -> UIState:
        """Build a UIState snapshot from the live navigation controller."""
        snapshot = self._read_controller_snapshot()
        return UIState(
            current_page=snapshot.raw_page_id,
            visible_buttons=list(snapshot.buttons),
            list_items=list(snapshot.list_items),
            context=dict(snapshot.context),
        )

    def _read_controller_snapshot(self) -> ControllerSnapshot:
        """Read UI state through the raw snapshot contract with legacy fallback."""
        snapshot_reader = getattr(self._controller, "get_controller_snapshot", None)
        if callable(snapshot_reader):
            try:
                snapshot = snapshot_reader(capture_mode="action_adapter_ui_state")
                if isinstance(snapshot, ControllerSnapshot):
                    return snapshot
                if isinstance(snapshot, dict):
                    return ControllerSnapshot.from_legacy_dict(
                        snapshot,
                        capture_source="action_adapter",
                        capture_mode="controller_snapshot_dict",
                    )
            except Exception as exc:
                logger.debug("Raw controller snapshot unavailable for UIState: %s", exc)

        return self._read_legacy_controller_snapshot()

    def _read_legacy_controller_snapshot(self) -> ControllerSnapshot:
        """Bridge older controller implementations into ControllerSnapshot."""
        page = self._controller.detect_current_page()
        buttons: list[str] = []
        list_items: list[str] = []
        try:
            buttons = [
                str(button.get("text", "") if isinstance(button, dict) else button)
                for button in (self._controller.get_visible_buttons() or [])
            ]
        except Exception:
            pass
        try:
            list_items = self._controller.get_list_items() or []
        except Exception:
            pass

        context = self._controller.get_context()
        return ControllerSnapshot.from_legacy_dict(
            {
                "page": getattr(page, "value", str(page)),
                "buttons": buttons,
                "lists": list_items,
                "context": context,
            },
            capture_source="action_adapter",
            capture_mode="legacy_controller_methods",
        )

__all__ = ["GDS2ActionAdapter"]
