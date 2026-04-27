"""
Navigation module for GDS2 state-aware navigation.

Provides:
- GDS2Page: Enum of all known pages
- NavigationController: State machine for managing navigation
"""

from .action_matcher import ActionMatch, ActionMatchError, find_action_match, find_list_item_match
from .controller import GDS2Page, NavigationController, NavigationResult
from .snapshot import ControllerSnapshot

__all__ = [
    "ActionMatch",
    "ActionMatchError",
    "ControllerSnapshot",
    "GDS2Page",
    "NavigationController",
    "NavigationResult",
    "find_action_match",
    "find_list_item_match",
]
