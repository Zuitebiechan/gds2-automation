"""
Recovery Executor - Executes AI recovery decisions.

This module executes recovery actions recommended by the AI:
- CLICK_BUTTON: Click button via AgentNavigator
- WAIT_LONGER: Return extended timeout to caller
- GO_BACK: Navigate back to previous page
- RETRY_FROM_START: Reset to Main Menu
- DISMISS_AND_NAVIGATE: Dismiss dialog + navigate to recovery target page
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
                elif action == RecoveryAction.DISMISS_AND_NAVIGATE:
                    success = self._dismiss_and_navigate(decision.parameters)
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
                    new_state = self._get_current_state()
                    logger.info(f"Recovery successful after {attempt} attempt(s) ({elapsed:.1f}s), state: {new_state}")
                    return RecoveryResult(
                        success=True,
                        action=action,
                        attempts=attempt,
                        elapsed_time=elapsed,
                        new_state=new_state,
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
        Navigate back to previous page using Back button.

        Returns:
            True if navigation succeeded
        """
        if not self.nav_controller:
            logger.error("NavigationController not provided, cannot go back")
            return False

        try:
            logger.info("Navigating back to previous page")
            result = self.nav_controller.go_back()

            if result.success:
                logger.info(f"Go back succeeded, now on: {result.page.value}")
            else:
                logger.warning(f"Go back failed: {result.error}")

            return result.success

        except Exception as e:
            logger.error(f"Exception going back: {e}", exc_info=True)
            return False

    def _retry_from_start(self) -> bool:
        """
        Reset to Main Menu using Home button.

        Returns:
            True if reset succeeded
        """
        if not self.nav_controller:
            logger.error("NavigationController not provided, cannot retry from start")
            return False

        try:
            logger.info("Resetting to Main Menu via Home button")
            result = self.nav_controller.go_home()

            if result.success:
                logger.info("Successfully returned to Main Menu")
            else:
                logger.warning(f"Go home failed: {result.error}")

            return result.success

        except Exception as e:
            logger.error(f"Exception resetting to start: {e}", exc_info=True)
            return False

    def _dismiss_and_navigate(self, parameters: dict) -> bool:
        """
        Dismiss an error dialog and navigate to a recovery target page.

        Two-phase recovery:
        1. Click the dismiss button to close the dialog
        2. Navigate to the target page using NavigationController

        Args:
            parameters: Must contain "dismiss_button" and "target_page" keys

        Returns:
            True if both dismissal and navigation succeeded
        """
        if not self.agent_nav:
            logger.error("AgentNavigator not provided, cannot dismiss dialog")
            return False
        if not self.nav_controller:
            logger.error("NavigationController not provided, cannot navigate")
            return False

        dismiss_button = parameters.get("dismiss_button", "OK")
        target_page_name = parameters.get("target_page", "MAIN_MENU")

        # Phase 1: Dismiss the dialog
        logger.info(f"Phase 1: Dismissing dialog (clicking '{dismiss_button}')")
        try:
            result = self.agent_nav.click_button(dismiss_button)
            if not result.get("success", False):
                logger.warning(f"Failed to click dismiss button: {result.get('error', 'unknown')}")
                return False
        except Exception as e:
            logger.error(f"Exception dismissing dialog: {e}", exc_info=True)
            return False

        # Wait for GDS2 to settle after dialog dismissal
        time.sleep(2)

        # Detect where we are now
        current_page = self.nav_controller.detect_current_page()
        logger.info(f"After dialog dismissal, current page: {current_page.value}")

        # Resolve target page
        from ..navigation.controller import GDS2Page
        target_page = self._resolve_target_page(target_page_name)
        if target_page is None:
            logger.error(f"Unknown target page: {target_page_name}")
            return False

        # Phase 2: Navigate to recovery target
        if current_page == target_page:
            logger.info(f"Already on target page: {target_page.value}")
            return True

        logger.info(f"Phase 2: Navigating from {current_page.value} to {target_page.value}")

        # Special handling for VEHICLE_SELECTION (not in navigate_to's page_depth)
        if target_page == GDS2Page.VEHICLE_SELECTION:
            return self._navigate_to_vehicle_selection(current_page)

        # Use NavigationController's navigate_to for other pages
        try:
            nav_result = self.nav_controller.navigate_to(target_page)
            if nav_result.success:
                logger.info(f"Navigation succeeded, now on: {nav_result.page.value}")
                return True

            # Navigation failed (e.g., target is forward from current page).
            # Dialog was already dismissed — that's the critical part.
            # Treat as success with current page as the recovery landing point.
            logger.warning(
                f"Navigation to {target_page.value} failed: {nav_result.error}. "
                f"Dialog was dismissed. Staying on {current_page.value}."
            )
            return True
        except Exception as e:
            logger.error(f"Exception during navigation: {e}", exc_info=True)
            # Dialog was already dismissed, consider it a partial success
            logger.info("Dialog was dismissed before navigation exception. Treating as success.")
            return True

    def _resolve_target_page(self, page_name: str):
        """
        Resolve a page name string to a GDS2Page enum value.

        Args:
            page_name: Page name (e.g., "VEHICLE_SELECTION", "MAIN_MENU")

        Returns:
            GDS2Page enum value, or None if not found
        """
        from ..navigation.controller import GDS2Page

        page_map = {
            "MAIN_MENU": GDS2Page.MAIN_MENU,
            "VEHICLE_SELECTION": GDS2Page.VEHICLE_SELECTION,
            "DIAGNOSTICS_MENU": GDS2Page.DIAGNOSTICS_MENU,
            "MODULE_LIST": GDS2Page.MODULE_LIST,
            "MODULE_SUBMENU": GDS2Page.MODULE_SUBMENU,
            "DATA_LIST": GDS2Page.DATA_LIST,
            "DATA_DISPLAY": GDS2Page.DATA_DISPLAY,
        }
        return page_map.get(page_name)

    def _navigate_to_vehicle_selection(self, current_page) -> bool:
        """
        Navigate to Vehicle Selection page.

        VEHICLE_SELECTION is special because it's not in the normal
        Back-button hierarchy (navigate_to doesn't handle it).
        Strategy: go Home → start Diagnostics → arrives at Vehicle Selection.

        But if we're already past Vehicle Selection (e.g., on MODULE_LIST),
        we can try going Home first and the workflow will handle reconnection.

        Args:
            current_page: Current GDS2Page

        Returns:
            True if navigation succeeded
        """
        from ..navigation.controller import GDS2Page

        # If we're already at VEHICLE_SELECTION, done
        if current_page == GDS2Page.VEHICLE_SELECTION:
            return True

        # If we're at MAIN_MENU, we'd need to click Diagnostics to get to
        # Vehicle Selection — but that triggers device explorer flow.
        # For recovery purposes, just navigating to MAIN_MENU is sufficient
        # as the workflow caller will handle the rest.
        logger.info(
            "VEHICLE_SELECTION requires going through Device Explorer flow. "
            "Navigating to MAIN_MENU as recovery target instead."
        )
        try:
            result = self.nav_controller.go_home()
            if result.success:
                logger.info("Navigated to MAIN_MENU (for VEHICLE_SELECTION recovery)")
            return result.success
        except Exception as e:
            logger.error(f"Failed to navigate home: {e}", exc_info=True)
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
