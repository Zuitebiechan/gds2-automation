"""
GDS2 Workflows

Agent-based navigation: Java Agent for GDS2 main window + Windows API for Device Explorer
- No PyAutoGUI, OpenCV, or screen dependency

Available workflows:
- DataViewerWorkflow: Simplified 3-dropdown UI (Device → Module → Data) - PRIMARY
- InteractiveWorkflow: Step-by-step interactive navigation (internal)
- ReadDataDisplayAgentWorkflow: CLI workflow wrapper

Note: Legacy PyAutoGUI-based workflows were removed from the active codebase.
"""

from .interactive_workflow import InteractiveWorkflow
from .read_data_display_agent import ReadDataDisplayAgentWorkflow
from .data_viewer import DataViewerWorkflow

__all__ = [
    "DataViewerWorkflow",
    "InteractiveWorkflow",
    "ReadDataDisplayAgentWorkflow",
]


# Workflow registry for easy access
WORKFLOW_REGISTRY = {
    "data_viewer": DataViewerWorkflow,
    "interactive": InteractiveWorkflow,
    "read_data_display": ReadDataDisplayAgentWorkflow,
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
