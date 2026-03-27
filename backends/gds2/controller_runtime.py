from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

from diagnostic_platform.contracts import BackendState
from src.navigation import GDS2Page, NavigationController
from vci_proxy.tunnel_quality import read_tunnel_quality_snapshot

if TYPE_CHECKING:
    from src.workflows.data_viewer import DataViewerWorkflow


logger = logging.getLogger(__name__)


class GDS2ControllerRuntime:
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
    _CONNECTED_PAGE_VALUES = {page.value for page in _CONNECTED_PAGES}

    def __init__(
        self,
        workflow: DataViewerWorkflow | None = None,
        *,
        snapshot_reader: Callable[[], dict[str, Any]] = read_tunnel_quality_snapshot,
    ) -> None:
        self._workflow: Any | None = workflow
        self._controller: NavigationController | None = (
            workflow.controller if workflow is not None else None
        )
        self._snapshot_reader = snapshot_reader
        self._ensure_controller_helpers()

    def ensure_ready(self) -> None:
        workflow = self.get_workflow()
        logger.info("[GDS2_RUNTIME] ensure_ready workflow=%s", type(workflow).__name__)
        workflow.auto_start()

    def preflight(self) -> dict[str, Any]:
        network_quality = self.get_network_quality()
        logger.info(
            "[GDS2_RUNTIME] preflight epoch=%s grade=%s status=%s reason=%s",
            network_quality.get("connection_epoch"),
            network_quality.get("grade"),
            network_quality.get("status"),
            network_quality.get("reason"),
        )
        return {
            "network_quality": network_quality,
            "connection_epoch": network_quality.get("connection_epoch"),
        }

    def status(self) -> BackendState:
        network_quality = self.get_network_quality()
        connection_epoch = network_quality.get("connection_epoch")

        if self._workflow is None or self._controller is None:
            return BackendState(
                current_page=GDS2Page.UNKNOWN.value,
                is_connected=False,
                current_module=None,
                current_data_category=None,
                extra={
                    "device": None,
                    "vin": None,
                    "sub_category": None,
                    "network_quality": network_quality,
                    "connection_epoch": connection_epoch,
                },
            )

        page = self._controller.detect_current_page()
        page_value = getattr(page, "value", str(page))
        context = self._controller.get_context()
        workflow_state = self._workflow.get_state()
        device = context.get("device") or workflow_state.get("device")

        return BackendState(
            current_page=page_value,
            is_connected=bool(device) or page_value in self._CONNECTED_PAGE_VALUES,
            current_module=context.get("module") or workflow_state.get("module"),
            current_data_category=(
                context.get("data_category") or workflow_state.get("data_category")
            ),
            extra={
                "device": device,
                "vin": workflow_state.get("vin"),
                "sub_category": context.get("sub_category"),
                "network_quality": network_quality,
                "connection_epoch": connection_epoch,
            },
        )

    def get_workflow(self) -> Any:
        if self._workflow is None:
            self._workflow = self._create_workflow()
            self._controller = self._workflow.controller
            self._ensure_controller_helpers()
        return self._workflow

    def get_controller(self) -> NavigationController:
        self.get_workflow()
        if self._controller is None:
            raise RuntimeError("Navigation controller is not initialized")
        return self._controller

    def get_network_quality(self) -> dict[str, Any]:
        return dict(self._snapshot_reader())

    def _ensure_controller_helpers(self) -> None:
        if self._controller is not None and not hasattr(self._controller, "get_available_items"):
            setattr(self._controller, "get_available_items", self._controller.wait_for_list)

    @staticmethod
    def _create_workflow() -> Any:
        try:
            from src.workflows.data_viewer import DataViewerWorkflow

            return DataViewerWorkflow()
        except Exception as exc:  # pragma: no cover - runtime integration wrapper
            raise RuntimeError(f"Failed to initialize DataViewerWorkflow: {exc}") from exc


__all__ = ["GDS2ControllerRuntime"]
