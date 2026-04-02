from typing import Dict, List, Set
from .contracts.action_schema import GDS2Action


class CapabilityRegistry:
    def __init__(self):
        self._page_capabilities: Dict[str, Set[GDS2Action]] = {
            "main_menu": {
                GDS2Action.START_DIAGNOSTICS,
                GDS2Action.SELECT_DEVICE,
                GDS2Action.ABORT_SESSION,
            },
            "device_explorer": {
                GDS2Action.SELECT_DEVICE,
                GDS2Action.CONNECT_DEVICE,
                GDS2Action.ABORT_SESSION,
            },
            "vehicle_selection": {
                GDS2Action.CONNECT_DEVICE,
                GDS2Action.GO_BACK,
                GDS2Action.ABORT_SESSION,
            },
            "module_list": {
                GDS2Action.SELECT_MODULE,
                GDS2Action.GO_HOME,
                GDS2Action.GO_BACK,
                GDS2Action.ABORT_SESSION,
            },
            "module_submenu": {
                GDS2Action.SELECT_MODULE,
                GDS2Action.GO_BACK,
                GDS2Action.ABORT_SESSION,
            },
            "data_list": {
                GDS2Action.SELECT_DATA_CATEGORY,
                GDS2Action.GO_HOME,
                GDS2Action.GO_BACK,
                GDS2Action.ABORT_SESSION,
            },
            "sub_data_list": {
                GDS2Action.SELECT_SUB_CATEGORY,
                GDS2Action.GO_BACK,
                GDS2Action.ABORT_SESSION,
            },
            "data_display": {
                GDS2Action.READ_DTCS,
                GDS2Action.START_LIVE_STREAM,
                GDS2Action.STOP_LIVE_STREAM,
                GDS2Action.GO_HOME,
                GDS2Action.GO_BACK,
                GDS2Action.ABORT_SESSION,
            },
        }

    def all_actions(self) -> List[GDS2Action]:
        return list(GDS2Action)

    def is_action_allowed(self, page: str, action: GDS2Action) -> bool:
        allowed = self._page_capabilities.get(page, set())
        return action in allowed

    def get_allowed_actions(self, page: str) -> List[str]:
        allowed = self._page_capabilities.get(page, set())
        return [a.value for a in allowed]

    def register_page_capability(self, page: str, action: GDS2Action):
        if page not in self._page_capabilities:
            self._page_capabilities[page] = set()
        self._page_capabilities[page].add(action)
