from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

from diagnostic_platform.contracts import BackendNavigationRuntime, BackendState
from backends.gds2.navigation_registry import (
    DEFAULT_REGISTRY_PATH,
    connect_registry,
    list_entries,
    list_page_states,
    list_recovery_policies,
    lookup_recovery_policy,
    rebuild_registry_database,
)
from backends.gds2.registry_navigation_runtime import (
    DEFAULT_GRAPH_PATH,
    RegistryNavigationRuntime,
    load_or_rebuild_graph,
    policy_by_key,
    restart_gds2_runtime,
)
from backends.gds2.route_navigator import GDS2RouteNavigator
from src.navigation import GDS2Page, NavigationController
from src.streaming import AgentDataCollector
from src.streaming.agent_data_collector import _parse_agent_json
from vci_proxy.tunnel_quality import read_tunnel_quality_snapshot


logger = logging.getLogger(__name__)
_AGENT_JSON_ENCODINGS = ("gbk", "utf-8", "utf-8-sig", "latin-1", "cp1252")
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
        *,
        controller: NavigationController | None = None,
        state_reader: Callable[[], dict[str, Any]] | None = None,
        snapshot_reader: Callable[[], dict[str, Any]] = read_tunnel_quality_snapshot,
    ) -> None:
        self._controller: NavigationController | None = controller
        self._state_reader = state_reader
        self._snapshot_reader = snapshot_reader
        self._runtime_state: dict[str, Any] = {
            "vin": None,
            "device": None,
            "module": None,
            "data_category": None,
            "sub_category": None,
        }
        self._registry_navigation_runtime: BackendNavigationRuntime | None = None
        self._ensure_controller_helpers()

    def ensure_ready(
        self,
        *,
        cancel_checker: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        return self.build_navigation_runtime(source="registry_runtime").ensure_started(
            cancel_checker=cancel_checker
        )

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

        if self._controller is None:
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
        state_snapshot = self._read_state_snapshot()
        device = context.get("device") or state_snapshot.get("device")

        return BackendState(
            current_page=page_value,
            is_connected=bool(device) or page_value in self._CONNECTED_PAGE_VALUES,
            current_module=context.get("module") or state_snapshot.get("module"),
            current_data_category=(
                context.get("data_category") or state_snapshot.get("data_category")
            ),
            extra={
                "device": device,
                "vin": state_snapshot.get("vin"),
                "sub_category": context.get("sub_category"),
                "network_quality": network_quality,
                "connection_epoch": connection_epoch,
            },
        )

    def get_controller(self) -> NavigationController:
        if self._controller is None:
            self._controller = NavigationController()
            self._ensure_controller_helpers()
        return self._controller

    def get_network_quality(self) -> dict[str, Any]:
        return dict(self._snapshot_reader())

    def read_all_dtcs(self) -> dict[str, Any]:
        collector = AgentDataCollector()
        availability = collector.check_agent_available()
        if not availability.get("available"):
            raise RuntimeError("Java Agent not available. Start GDS2 with agent.")

        current = self.get_controller().detect_current_page()
        if current != GDS2Page.DATA_DISPLAY:
            raise RuntimeError(
                "Not at Data Display page. Select module and data category first."
            )

        snapshot = _parse_agent_json(self._load_agent_json_payload(collector.json_path))
        self._ensure_agent_snapshot_matches_page(snapshot.page_context)
        self._runtime_state.update(self.get_controller().get_context())
        return {
            "dtcs": [dtc.to_dict() for dtc in snapshot.dtcs],
            "dtc_count": len(snapshot.dtcs),
            "page_context": snapshot.page_context,
        }

    def build_navigation_runtime(
        self,
        *,
        source: str = "registry_runtime",
    ) -> BackendNavigationRuntime:
        if source != "registry_runtime":
            raise ValueError(f"Unsupported GDS2 navigation runtime source: {source}")
        if self._registry_navigation_runtime is None:
            self._registry_navigation_runtime = self._build_registry_navigation_runtime()
        return self._registry_navigation_runtime

    def _build_registry_navigation_runtime(self) -> BackendNavigationRuntime:
        registry_path = DEFAULT_REGISTRY_PATH
        if not registry_path.exists():
            rebuild_registry_database(output_path=registry_path)

        with connect_registry(registry_path) as connection:
            entries = list_entries(connection)
            page_states = list_page_states(connection)
            recovery_policies = list_recovery_policies(connection)
            loading_policy = lookup_recovery_policy(
                connection,
                "loading.restart_after_timeout",
            )

        if loading_policy is None:
            loading_policy = policy_by_key(
                recovery_policies,
                "loading.restart_after_timeout",
            )
        loading_params = dict((loading_policy or {}).get("params") or {})
        graph = load_or_rebuild_graph(DEFAULT_GRAPH_PATH)
        route_navigator = GDS2RouteNavigator(
            controller=self.get_controller(),
            graph=graph,
        )
        return RegistryNavigationRuntime(
            controller=self.get_controller(),
            route_navigator=route_navigator,
            entries=entries,
            page_states=page_states,
            recovery_policies=recovery_policies,
            loading_timeout_sec=float(
                loading_params.get("timeout_sec")
                or (loading_policy or {}).get("timeout_sec")
                or 20.0
            ),
            max_loading_restarts=int(
                loading_params.get("max_restarts")
                or (loading_policy or {}).get("max_attempts")
                or 1
            ),
            restart_runtime=self._restart_registry_runtime,
            read_dtcs_snapshot=self.read_all_dtcs,
            state_reader=self._read_state_snapshot,
            default_device_name="VCI Proxy (Remote)",
        )

    def _restart_registry_runtime(
        self,
        *,
        graph: dict[str, Any],
        recovery_actions: list[dict[str, Any]],
        wait_sec: float = 8.0,
    ) -> tuple[NavigationController, GDS2RouteNavigator]:
        controller, route_navigator = restart_gds2_runtime(
            graph=graph,
            recovery_actions=recovery_actions,
            wait_sec=wait_sec,
        )
        self._controller = controller
        self._ensure_controller_helpers()
        return controller, route_navigator

    def _read_state_snapshot(self) -> dict[str, Any]:
        state = dict(self._runtime_state)
        if self._state_reader is not None:
            try:
                state.update(self._state_reader() or {})
            except Exception:
                pass
        if self._controller is not None:
            try:
                state.update(self._controller.get_context() or {})
            except Exception:
                pass
        return state

    @staticmethod
    def _load_agent_json_payload(json_path_str: str) -> Any:
        raw = None
        json_path = Path(json_path_str)
        if not json_path.exists():
            raise RuntimeError(f"Agent JSON file not found: {json_path}")

        for encoding in _AGENT_JSON_ENCODINGS:
            try:
                with json_path.open("r", encoding=encoding) as handle:
                    raw = json.load(handle)
                break
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            except OSError as exc:
                raise RuntimeError(f"Failed to read Agent JSON file: {exc}") from exc

        if raw is None:
            raise RuntimeError("Failed to decode Agent JSON file")
        return raw

    @staticmethod
    def _ensure_agent_snapshot_matches_page(page_context: dict[str, Any] | None) -> None:
        if not isinstance(page_context, dict):
            return
        reported_page = (
            page_context.get("page")
            or page_context.get("currentPage")
            or page_context.get("current_page")
        )
        if reported_page is None:
            return
        normalized_page = str(reported_page).strip().lower()
        if normalized_page and normalized_page != GDS2Page.DATA_DISPLAY.value:
            raise RuntimeError(
                f"Detected stale Agent snapshot for page '{normalized_page}', expected data_display"
            )

    def _ensure_controller_helpers(self) -> None:
        if self._controller is not None and not hasattr(self._controller, "get_available_items"):
            setattr(self._controller, "get_available_items", self._controller.wait_for_list)

__all__ = ["GDS2ControllerRuntime"]
