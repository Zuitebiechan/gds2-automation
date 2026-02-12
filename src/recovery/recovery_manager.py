"""
Recovery Manager - Orchestrates the recovery pipeline.

This is the main entry point for the recovery system:
1. Detect anomalies (AnomalyDetector)
2. Analyze with AI (AIRecoveryAgent)
3. Execute recovery (RecoveryExecutor)
4. Verify success

Provides a simple interface for DataViewerWorkflow integration.
"""

import logging
from typing import Optional
from pathlib import Path

from .config import AIRecoveryConfig
from .anomaly_detector import AnomalyDetector
from .ai_recovery_agent import AIRecoveryAgent
from .recovery_executor import RecoveryExecutor
from .types import Anomaly, RecoveryResult, OperationContext

logger = logging.getLogger(__name__)


class RecoveryManager:
    """
    Orchestrates anomaly detection, AI analysis, and recovery execution.

    This is the main coordinator for the AI exception recovery system.
    """

    def __init__(
        self,
        config: Optional[AIRecoveryConfig] = None,
        agent_navigator=None,
        navigation_controller=None,
    ):
        """
        Initialize recovery manager.

        Args:
            config: AIRecoveryConfig (if None, loads from environment)
            agent_navigator: AgentNavigator instance (for button clicks)
            navigation_controller: NavigationController instance (for page navigation)
        """
        self.config = config or AIRecoveryConfig.from_env()

        # Validate config
        if self.config.enabled:
            errors = self.config.validate()
            if errors:
                logger.error(f"Invalid AI recovery config: {', '.join(errors)}")
                logger.warning("AI recovery is disabled due to config errors")
                self.config = AIRecoveryConfig(enabled=False)

        # Initialize components
        self.detector = AnomalyDetector()
        self.agent = AIRecoveryAgent(self.config) if self.config.enabled else None
        self.executor = RecoveryExecutor(
            agent_navigator=agent_navigator,
            navigation_controller=navigation_controller,
            detector=self.detector,
        )

        # Create log directory
        if self.config.enabled and self.config.log_decisions:
            self.config.ensure_log_dir()

        logger.info(f"Recovery manager initialized (enabled={self.config.enabled})")

    def handle_anomaly(
        self,
        anomaly: Anomaly,
        context: Optional[OperationContext] = None,
    ) -> RecoveryResult:
        """
        Handle an anomaly: analyze with AI and execute recovery.

        This is the main entry point for recovery.

        Args:
            anomaly: Detected anomaly
            context: Operation context (if None, creates minimal context)

        Returns:
            RecoveryResult with success status

        Raises:
            RuntimeError: If recovery fails and anomaly is critical
        """
        if not self.config.enabled:
            raise RuntimeError(f"AI recovery disabled, cannot handle {anomaly.type.name}")

        logger.info(f"Handling anomaly: {anomaly}")

        # Create default context if not provided
        if context is None:
            context = OperationContext(
                operation_name="unknown",
                current_page="UNKNOWN",
            )

        try:
            # Step 1: AI analyzes anomaly and decides recovery action
            logger.debug("Step 1: AI analyzing anomaly...")
            decision = self.agent.analyze_anomaly(anomaly, context)
            logger.info(f"AI decision: {decision}")

            # Log decision if enabled
            if self.config.log_decisions:
                self._log_decision(anomaly, context, decision)

            # Step 2: Execute recovery action
            logger.debug("Step 2: Executing recovery action...")
            result = self.executor.execute(
                decision,
                max_retries=self.config.max_retries,
            )

            # Log result
            if result.success:
                logger.info(f"Recovery succeeded: {result}")
            else:
                logger.error(f"Recovery failed: {result}")

            return result

        except Exception as e:
            logger.error(f"Exception during recovery: {e}", exc_info=True)
            return RecoveryResult(
                success=False,
                action=None,
                attempts=0,
                elapsed_time=0.0,
                error=f"Recovery exception: {str(e)}",
            )

    def check_for_dialogs(self) -> Optional[Anomaly]:
        """
        Quick check for unexpected dialogs.

        Returns:
            Anomaly if dialog detected, None otherwise
        """
        return self.detector.check_unexpected_dialog()

    def reset_session(self):
        """
        Reset for new workflow session.

        Resets LLM call counter to allow new session to use full quota.
        """
        if self.agent:
            self.agent.reset_call_count()
            logger.debug("AI agent call counter reset for new session")

    def _log_decision(self, anomaly: Anomaly, context: OperationContext, decision):
        """
        Log AI decision to file for analysis.

        Args:
            anomaly: The anomaly that was detected
            context: Operation context
            decision: AI's decision
        """
        try:
            import json
            from datetime import datetime

            log_dir = Path(self.config.log_dir)
            log_file = log_dir / "decisions.jsonl"

            log_entry = {
                "timestamp": datetime.now().isoformat(),
                "anomaly": {
                    "type": anomaly.type.name,
                    "severity": anomaly.severity.name,
                    "context": anomaly.context,
                },
                "operation_context": context.to_dict(),
                "decision": {
                    "action": decision.action.name,
                    "confidence": decision.confidence,
                    "reasoning": decision.reasoning,
                    "parameters": decision.parameters,
                    "estimated_time": decision.estimated_time,
                },
                "llm_call_count": self.agent.call_count,
            }

            # Append to JSONL file
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")

        except Exception as e:
            logger.warning(f"Failed to log decision: {e}")

    @property
    def enabled(self) -> bool:
        """Check if AI recovery is enabled."""
        return self.config.enabled

    @property
    def llm_call_count(self) -> int:
        """Get current LLM call count."""
        return self.agent.call_count if self.agent else 0
