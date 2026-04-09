"""Shared runtime exceptions that should not depend on worker state."""

from __future__ import annotations


class OperationCancelledError(RuntimeError):
    """Raised when one in-flight runtime operation is cooperatively cancelled."""


class WorkerBusyError(RuntimeError):
    """Raised when one worker-exclusive operation is already in progress."""
