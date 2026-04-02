"""Deterministic executor for Action DSL steps."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional
import time

from .contracts.action_schema import ActionStep, GDS2Action
from .contracts.state_schema import UIState
from .policy_guard import PolicyGuard


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
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return self.status == ExecutionStatus.SUCCESS


StepHandler = Callable[[ActionStep, UIState], Dict[str, Any] | None]


class DeterministicExecutor:
    """Executes validated ActionStep instances with bounded retry."""

    def __init__(self, policy_guard: Optional[PolicyGuard] = None) -> None:
        self.policy_guard = policy_guard or PolicyGuard()
        self._handlers: Dict[GDS2Action, StepHandler] = {}

    def register_handler(self, action: GDS2Action, handler: StepHandler) -> None:
        self._handlers[action] = handler

    def get_registered_actions(self) -> List[GDS2Action]:
        return sorted(self._handlers.keys(), key=lambda a: a.value)

    def execute_step(self, step: ActionStep, state: UIState) -> ExecutionResult:
        start_time = time.time()

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
        last_error: Optional[str] = None

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

    def execute_plan(self, steps: List[ActionStep], state: UIState) -> List[ExecutionResult]:
        results: List[ExecutionResult] = []
        for step in steps:
            step_result = self.execute_step(step, state)
            results.append(step_result)
            if not step_result.success:
                break
        return results
