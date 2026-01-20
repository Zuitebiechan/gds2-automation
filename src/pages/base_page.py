"""
Base Page Object

Abstract base class for all page objects with fluent navigation support.
"""

import logging
import time
from abc import ABC, abstractmethod
from typing import TypeVar, Type, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.driver import GDS2Driver

logger = logging.getLogger(__name__)

# Type variable for fluent returns
T = TypeVar('T', bound='BasePage')


class BasePage(ABC):
    """
    Base class for all GDS2 page objects.

    Each page object represents a specific page/screen in GDS2.
    Pages should only contain UI operations, not business logic.

    Features:
    - Fluent navigation: methods return next page object
    - Centralized locators: use Loc.PageName.ELEMENT
    - Driver abstraction: no direct pywinauto access
    """

    def __init__(self, driver: 'GDS2Driver'):
        """
        Initialize page.

        Args:
            driver: GDS2Driver instance
        """
        self.driver = driver
        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    @property
    @abstractmethod
    def name(self) -> str:
        """Page name for logging and identification."""
        pass

    @abstractmethod
    def is_displayed(self) -> bool:
        """
        Check if this page is currently displayed.

        Returns:
            True if this page is currently visible
        """
        pass

    def wait_for_page(self, timeout: float = 30) -> 'BasePage':
        """
        Wait for this page to be displayed.

        Args:
            timeout: Maximum wait time in seconds

        Returns:
            Self for method chaining
        """
        start = time.time()
        while time.time() - start < timeout:
            if self.is_displayed():
                self._logger.debug(f"Page '{self.name}' is displayed")
                return self
            time.sleep(0.5)

        self._logger.warning(f"Page '{self.name}' did not appear after {timeout}s")
        return self

    def _navigate_to(self, page_class: Type[T], action: callable, timeout: float = 30) -> T:
        """
        Helper for navigation that returns next page.

        Args:
            page_class: The page class to navigate to
            action: The action to perform (callable)
            timeout: Timeout for next page to appear

        Returns:
            Instance of the next page
        """
        self._logger.info(f"Navigating from '{self.name}' to '{page_class.__name__}'...")
        action()
        next_page = page_class(self.driver)
        next_page.wait_for_page(timeout=timeout)
        return next_page

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}>"
