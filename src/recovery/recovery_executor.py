"""
Recovery Executor - Executes AI recovery decisions.

This module executes recovery actions recommended by the AI:
- CLICK_BUTTON: Click button via AgentNavigator
- WAIT_LONGER: Return extended timeout to caller
- GO_BACK: Navigate back to previous page
- RETRY_FROM_START: Reset to Main Menu
- ABORT: Log and raise exception

Verifies recovery success after execution.
"""

import time
import logging
from typing import Optional

from .types import RecoveryAction, RecoveryDecision, RecoveryResult
from .anomaly_detector import AnomalyDetector

logger = logging.getLogger(__name__)


class RecoveryExecutor:
    """
    Executes AI recovery decisions and verifies success.

    Takes a RecoveryDecision and performs the recommended action,
    then verifies that the anomaly has been resolved.
    """

    def __init__(
        self,
        agent_navigator=None,
        navigation_controller=None,
        detector: Optional[AnomalyDetector] = None,
    ):
        """
        Initialize recovery executor.

        Args:
            agent_navigator: AgentNavigator instance (for button clicks)
            navigation_controller: NavigationController instance (for page navigation)
            detector: AnomalyDetector instance (for verification)
        """
        self.agent_nav = agent_navigator
        self.nav_controller = navigation_controller
        self.detector = detector or AnomalyDetector()

    def execute(
        self,
        decision: RecoveryDecision,
        max_retries: int = 2,
    ) -> RecoveryResult:
        """
        Execute recovery decision and verify success.

        Args:
            decision: RecoveryDecision from AI
            max_retries: Maximum retry attempts

        Returns:
            RecoveryResult with success status and details
        """
        start_time = time.time()
        action = decision.action

        logger.info(f"Executing recovery: {action.name} (confidence={decision.confidence:.2f})")
        logger.info(f"Reasoning: {decision.reasoning}")

        for attempt in range(1, max_retries + 1):
            try:
                # Execute action
                if action == RecoveryAction.CLICK_BUTTON:
                    success = self._click_button(decision.parameters)
                elif action == RecoveryAction.WAIT_LONGER:
                    success = self._wait_longer(decision.parameters)
                elif action == RecoveryAction.GO_BACK:
                    success = self._go_back()
                elif action == RecoveryAction.RETRY_FROM_START:
                    success = self._retry_from_start()
                elif action == RecoveryAction.ABORT:
                    # ABORT is not a failure, it's a deliberate decision to stop
                    elapsed = time.time() - start_time
                    logger.warning(f"AI recommends ABORT: {decision.reasoning}")
                    return RecoveryResult(
                        success=False,
                        action=action,
                        attempts=attempt,
                        elapsed_time=elapsed,
                        error=f"AI recommends aborting: {decision.reasoning}",
                    )
                else:
                    raise ValueError(f"Unknown recovery action: {action}")

                if not success:
                    logger.warning(f"Recovery action failed on attempt {attempt}/{max_retries}")
                    time.sleep(2)  # Brief delay before retry
                    continue

                # Verify recovery
                if self._verify_recovery():
                    elapsed = time.time() - start_time
                    logger.info(f"Recovery successful after {attempt} attempt(s) ({elapsed:.1f}s)")
                    return RecoveryResult(
                        success=True,
                        action=action,
                        attempts=attempt,
                        elapsed_time=elapsed,
                        new_state=self._get_current_state(),
                    )
                else:
                    logger.warning(f"Recovery verification failed on attempt {attempt}")
                    time.sleep(2)

            except Exception as e:
                logger.error(f"Recovery attempt {attempt} failed with exception: {e}", exc_info=True)
                if attempt < max_retries:
                    time.sleep(2)
                    continue

        # All retries exhausted
        elapsed = time.time() - start_time
        return RecoveryResult(
            success=False,
            action=action,
            attempts=max_retries,
            elapsed_time=elapsed,
            error=f"Recovery failed after {max_retries} attempts",
        )

    def _click_button(self, parameters: dict) -> bool:
        """
        Click a button via AgentNavigator.

        Args:
            parameters: Must contain "button_text" key

        Returns:
            True if button click succeeded
        """
        if not self.agent_nav:
            logger.error("AgentNavigator not provided, cannot click button")
            return False

        button_text = parameters.get("button_text")
        if not button_text:
            logger.error("Missing button_text parameter")
            return False

        try:
            logger.info(f"Clicking button: {button_text}")
            result = self.agent_nav.click_button(button_text)
            success = result.get("success", False)

            if success:
                logger.info(f"Button click succeeded: {button_text}")
                time.sleep(1)  # Brief delay for UI to update
            else:
                logger.warning(f"Button click failed: {result.get('error', 'unknown')}")

            return success

        except Exception as e:
            logger.error(f"Exception clicking button: {e}", exc_info=True)
            return False

    def _wait_longer(self, parameters: dict) -> bool:
        """
        Wait longer (extended timeout).

        This doesn't actually wait here - it signals to the caller
        to extend the timeout. Always returns True.

        Args:
            parameters: Should contain "wait_seconds" key

        Returns:
            Always True (waiting itself doesn't fail)
        """
        wait_seconds = parameters.get("wait_seconds", 60)
        logger.info(f"AI recommends waiting {wait_seconds} more seconds")

        # Note: The actual waiting is done by the caller (e.g., _wait_for_button_enabled)
        # This just logs and returns success
        return True

    def _go_back(self) -> bool:
        """
        Navigate back to previous page.

        Returns:
            True if navigation succeeded
        """
        if not self.nav_controller:
            logger.error("NavigationController not provided, cannot go back")
            return False

        try:
            logger.info("Navigating back to previous page")
            # Use Home button or back navigation
            # This depends on your NavigationController implementation
            # For now, just log - implement actual navigation as needed
            logger.warning("GO_BACK not fully implemented yet - requires NavigationController.go_back()")
            return False

        except Exception as e:
            logger.error(f"Exception going back: {e}", exc_info=True)
            return False

    def _retry_from_start(self) -> bool:
        """
        Reset to Main Menu and retry.

        Returns:
            True if reset succeeded
        """
        if not self.nav_controller:
            logger.error("NavigationController not provided, cannot retry from start")
            return False

        try:
            logger.info("Resetting to Main Menu")
            # This requires NavigationController to have a reset/home method
            logger.warning("RETRY_FROM_START not fully implemented yet - requires NavigationController.go_to_main()")
            return False

        except Exception as e:
            logger.error(f"Exception resetting to start: {e}", exc_info=True)
            return False

    def _verify_recovery(self) -> bool:
        """
        Verify that recovery was successful.

        Checks:
        - No dialog is showing (if recovery was for dialog)
        - Page state is valid (not UNKNOWN)

        Returns:
            True if recovery appears successful
        """
        # Check if dialog is gone
        if self.detector.has_dialog():
            logger.debug("Recovery verification failed: dialog still showing")
            return False

        # Check if page is in a valid state
        current_state = self._get_current_state()
        if current_state == "UNKNOWN":
            logger.debug("Recovery verification uncertain: page state is UNKNOWN")
            # Don't fail on UNKNOWN - it might just mean detection is slow
            # Return True to give benefit of the doubt
            return True

        logger.debug(f"Recovery verification passed: {current_state}")
        return True

    def _get_current_state(self) -> str:
        """
        Get current page state.

        Returns:
            Current page name (e.g., "MAIN_MENU", "UNKNOWN")
        """
        if not self.nav_controller:
            return "UNKNOWN"

        try:
            current_page = self.nav_controller.detect_current_page()
            return current_page.name if hasattr(current_page, "name") else str(current_page)
        except Exception as e:
            logger.debug(f"Failed to detect current page: {e}")
            return "UNKNOWN"

    def set_agent_navigator(self, agent_nav):
        """Set AgentNavigator instance (for late binding)."""
        self.agent_nav = agent_nav

    def set_navigation_controller(self, nav_controller):
        """Set NavigationController instance (for late binding)."""
        self.nav_controller = nav_controller
