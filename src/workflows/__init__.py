"""
GDS2 Workflows

Agent-based navigation: Java Agent for GDS2 main window + Windows API for Device Explorer
- No PyAutoGUI, OpenCV, or screen dependency

Note: Legacy PyAutoGUI-based workflows have been archived to archive/src/workflows/
"""

from .read_data_display_agent import ReadDataDisplayAgentWorkflow

__all__ = [
    "ReadDataDisplayAgentWorkflow",
]


# Workflow registry for easy access
WORKFLOW_REGISTRY = {
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
