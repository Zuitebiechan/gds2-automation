"""
Recovery decorators for workflow methods.

Provides @with_recovery to wrap any workflow method with automatic
dialog detection and AI-powered recovery on exception.
"""

import functools
import logging
import time

from .types import OperationContext, RecoveryAction, WorkflowRecoveryError

logger = logging.getLogger(__name__)


def with_recovery(method):
    """
    Decorator that adds AI recovery to a workflow method.

    On exception:
    1. Check if a modal dialog is showing (fast file read, no LLM cost)
    2. If dialog found, let AI analyze the error and decide recovery action
    3. Execute recovery (dismiss dialog, optionally navigate)
    4. Behavior depends on recovery action:
       - CLICK_BUTTON: dialog dismissed, retry method once (benign warning)
       - DISMISS_AND_NAVIGATE: dialog dismissed + navigated, raise WorkflowRecoveryError
       - Other: raise WorkflowRecoveryError with new state

    Requires the instance (self) to have:
    - self.recovery: RecoveryManager (or None if disabled)
    - self.controller: NavigationController (for page context)
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except WorkflowRecoveryError:
            # Don't intercept recovery errors from nested calls
            raise
        except Exception as original_error:
            # If recovery is not available, re-raise immediately
            if not self.recovery or not self.recovery.enabled:
                raise

            # Check if a modal dialog is causing the problem
            logger.info(
                f"[with_recovery] {method.__name__} failed: {original_error}. "
                f"Checking for recoverable dialog..."
            )

            anomaly = self.recovery.check_for_dialogs()
            if not anomaly:
                logger.info(
                    f"[with_recovery] No dialog detected, re-raising original error"
                )
                raise

            # Dialog found - attempt AI recovery
            modal_title = anomaly.context.get("modal_title", "Unknown")
            modal_message = anomaly.context.get("modal_message", "")
            logger.warning(
                f"[with_recovery] Dialog detected: '{modal_title}' "
                f"message='{modal_message}'. Attempting AI recovery..."
            )

            context = OperationContext(
                operation_name=method.__name__,
                current_page=self.controller.current_page.value,
                recent_actions=[method.__name__],
                additional_info={"error_message": str(original_error)},
            )

            result = self.recovery.handle_anomaly(anomaly, context)

            if not result.success:
                logger.error(
                    f"[with_recovery] AI recovery failed: {result.error}. "
                    f"Re-raising original error."
                )
                raise original_error

            # Recovery succeeded — decide what to do next based on action type
            new_state = result.new_state or "UNKNOWN"
            action = result.action

            if action == RecoveryAction.CLICK_BUTTON:
                # Simple dialog dismissal (benign warning/info dialog).
                # The dialog was just blocking the UI — retry the method once.
                logger.info(
                    f"[with_recovery] Benign dialog dismissed ({action.name}). "
                    f"Retrying {method.__name__}..."
                )
                time.sleep(1)
                return method(self, *args, **kwargs)

            # DISMISS_AND_NAVIGATE or other actions: workflow was rerouted.
            # Don't retry — inform the caller of the new state.
            logger.info(
                f"[with_recovery] Recovery succeeded ({action.name}). "
                f"GDS2 now on page: {new_state}. "
                f"Raising WorkflowRecoveryError to inform caller."
            )

            raise WorkflowRecoveryError(
                target_page=new_state,
                reasoning=f"Dialog '{modal_title}' dismissed. {modal_message}",
                action=action,
                original_error=original_error,
            )

    return wrapper
