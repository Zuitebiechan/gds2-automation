"""Platform-owned deterministic action runtime primitives."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import time
from typing import Any, Callable

from diagnostic_platform.action_schema import ActionStep, GDS2Action


class ExecutionStatus(str, Enum):
    """Status for step/plan execution."""

    SUCCESS = "success"
    FAILED = "failed"


@dataclass
class ExecutionResult:
    """Result of deterministic step execution."""

    status: ExecutionStatus
    action: GDS2Action
    attempts: int = 1
    elapsed_time: float = 0.0
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.status == ExecutionStatus.SUCCESS


StepHandler = Callable[[ActionStep, Any], dict[str, Any] | None]


class DeterministicExecutor:
    """Executes validated ActionStep instances with bounded retry."""

    def __init__(self, policy_guard: Any | None = None) -> None:
        self.policy_guard = policy_guard
        self._handlers: dict[GDS2Action, StepHandler] = {}

    def register_handler(self, action: GDS2Action, handler: StepHandler) -> None:
        self._handlers[action] = handler

    def get_registered_actions(self) -> list[GDS2Action]:
        return sorted(self._handlers.keys(), key=lambda action: action.value)

    def execute_step(self, step: ActionStep, state: Any) -> ExecutionResult:
        start_time = time.time()

        if self.policy_guard is not None:
            allowed, reason = self.policy_guard.validate_action(step, state)
            if not allowed:
                return ExecutionResult(
                    status=ExecutionStatus.FAILED,
                    action=step.action,
                    attempts=0,
                    elapsed_time=time.time() - start_time,
                    error=reason,
                )

        handler = self._handlers.get(step.action)
        if handler is None:
            return ExecutionResult(
                status=ExecutionStatus.FAILED,
                action=step.action,
                attempts=0,
                elapsed_time=time.time() - start_time,
                error=f"No handler registered for action {step.action.value}",
            )

        attempts = step.retry_policy.max_attempts
        last_error: str | None = None

        for attempt in range(1, attempts + 1):
            try:
                result = handler(step, state)
                if isinstance(result, dict) and result.get("success") is False:
                    message = str(result.get("error", "handler returned success=False"))
                    raise RuntimeError(message)

                return ExecutionResult(
                    status=ExecutionStatus.SUCCESS,
                    action=step.action,
                    attempts=attempt,
                    elapsed_time=time.time() - start_time,
                    metadata=result or {},
                )
            except Exception as exc:
                last_error = str(exc)
                if attempt < attempts and step.retry_policy.backoff_sec > 0:
                    time.sleep(step.retry_policy.backoff_sec)

        return ExecutionResult(
            status=ExecutionStatus.FAILED,
            action=step.action,
            attempts=attempts,
            elapsed_time=time.time() - start_time,
            error=last_error or "execution failed",
        )

    def execute_plan(self, steps: list[ActionStep], state: Any) -> list[ExecutionResult]:
        results: list[ExecutionResult] = []
        for step in steps:
            step_result = self.execute_step(step, state)
            results.append(step_result)
            if not step_result.success:
                break
        return results


__all__ = [
    "DeterministicExecutor",
    "ExecutionResult",
    "ExecutionStatus",
    "StepHandler",
]
