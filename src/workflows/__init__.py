"""
GDS2 Workflows

Workflows contain business logic and orchestrate page operations.
"""

from .base_workflow import BaseWorkflow
from .read_vehicle_dtc import ReadVehicleDTCWorkflow

__all__ = [
    "BaseWorkflow",
    "ReadVehicleDTCWorkflow",
]


# Workflow registry for easy access
WORKFLOW_REGISTRY = {
    "read_vehicle_dtc": ReadVehicleDTCWorkflow,
}


def get_workflow(name: str, driver):
    """
    Get workflow by name.

    Args:
        name: Workflow name
        driver: GDS2Driver instance

    Returns:
        Workflow instance
    """
    if name not in WORKFLOW_REGISTRY:
        raise ValueError(f"Unknown workflow: {name}. Available: {list(WORKFLOW_REGISTRY.keys())}")

    return WORKFLOW_REGISTRY[name](driver)
