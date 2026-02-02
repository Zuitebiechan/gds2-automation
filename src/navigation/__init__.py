"""
Navigation module for GDS2 state-aware navigation.

Provides:
- GDS2Page: Enum of all known pages
- NavigationController: State machine for managing navigation
"""

from .controller import GDS2Page, NavigationController, NavigationResult

__all__ = ["GDS2Page", "NavigationController", "NavigationResult"]
