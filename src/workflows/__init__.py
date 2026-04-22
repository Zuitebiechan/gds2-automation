"""
GDS2 workflow-style utilities.

Available utilities:

- InteractiveWorkflow: step-by-step navigation helpers
- ReadDataDisplayAgentWorkflow: wrapper around the interactive workflow
"""

from .interactive_workflow import InteractiveWorkflow
from .read_data_display_agent import ReadDataDisplayAgentWorkflow

__all__ = [
    "InteractiveWorkflow",
    "ReadDataDisplayAgentWorkflow",
]


WORKFLOW_REGISTRY = {
    "interactive": InteractiveWorkflow,
    "read_data_display": ReadDataDisplayAgentWorkflow,
}


def get_workflow(name: str):
    if name not in WORKFLOW_REGISTRY:
        raise ValueError(f"Unknown workflow: {name}. Available: {list(WORKFLOW_REGISTRY.keys())}")

    return WORKFLOW_REGISTRY[name]()
