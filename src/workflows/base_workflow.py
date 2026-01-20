"""
Base Workflow

Abstract base class for all workflows.
"""

import logging
from abc import ABC, abstractmethod
from typing import Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.driver import GDS2Driver

logger = logging.getLogger(__name__)


class BaseWorkflow(ABC):
    """
    Base class for all GDS2 workflows.

    A workflow orchestrates page operations to accomplish a business goal.
    Workflows contain business logic but delegate UI operations to pages.
    """

    def __init__(self, driver: 'GDS2Driver'):
        """
        Initialize workflow.

        Args:
            driver: GDS2Driver instance
        """
        self.driver = driver

    @property
    @abstractmethod
    def name(self) -> str:
        """Workflow name."""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Workflow description."""
        pass

    @abstractmethod
    def execute(self, **kwargs) -> Dict[str, Any]:
        """
        Execute the workflow.

        Returns:
            Dictionary with results
        """
        pass
