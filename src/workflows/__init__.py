"""
GDS2 Workflows

Architecture:
- PyAutoGUI+OpenCV: for buttons and fixed list items (template matching)
- pywinauto: for discovering list items and checking button state
- Keyboard navigation: for selecting items in lists (DOWN + ENTER)
"""

from .base_workflow import BaseWorkflow
from .read_data_display import ReadDataDisplayWorkflow

__all__ = [
    "BaseWorkflow",
    "ReadDataDisplayWorkflow",
]


# Workflow registry for easy access
WORKFLOW_REGISTRY = {
    "read_data_display": ReadDataDisplayWorkflow,
}


def get_workflow(name: str):
    """
    Get workflow by name.

    Args:
        name: Workflow name

    Returns:
        Workflow instance
    """
    if name not in WORKFLOW_REGISTRY:
        raise ValueError(f"Unknown workflow: {name}. Available: {list(WORKFLOW_REGISTRY.keys())}")

    return WORKFLOW_REGISTRY[name]()
