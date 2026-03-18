"""
Platform layer: Unified diagnostic backend contracts.

This module defines the abstract interface that all OEM diagnostic software backends
(GDS2, Honda, Toyota, etc.) must implement. It provides standardized data schemas
for vehicle context, diagnostics, and backend state management.
"""

from .contracts import (
    VehicleContext,
    DTC,
    LiveDataPoint,
    SamplingQuality,
    DiagnosticPayload,
    BackendState,
    ClearResult,
    ActionResult,
    LiveDataStream,
    DiagnosticBackend,
    BackendRegistry,
)
from .sse import (
    agent_clients,
    agent_lock,
    broadcast_to_agent_clients,
    on_agent_snapshot,
    on_agent_param_change,
    on_agent_dtc_change,
    on_agent_error,
)

__all__ = [
    "VehicleContext",
    "DTC",
    "LiveDataPoint",
    "SamplingQuality",
    "DiagnosticPayload",
    "BackendState",
    "ClearResult",
    "ActionResult",
    "LiveDataStream",
    "DiagnosticBackend",
    "BackendRegistry",
    "agent_clients",
    "agent_lock",
    "broadcast_to_agent_clients",
    "on_agent_snapshot",
    "on_agent_param_change",
    "on_agent_dtc_change",
    "on_agent_error",
]
