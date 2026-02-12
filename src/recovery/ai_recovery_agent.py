"""
AI Recovery Agent - Core LLM integration.

This module handles:
- Calling Anthropic/OpenAI APIs
- Constructing prompts from anomalies and context
- Parsing LLM responses into RecoveryDecision objects
- Handling errors and fallbacks
"""

import json
import logging
from typing import Dict, Any, Optional

from .config import AIRecoveryConfig
from .types import (
    Anomaly,
    AnomalyType,
    RecoveryAction,
    RecoveryDecision,
    OperationContext,
)
from .prompts import (
    SYSTEM_PROMPT,
    build_dialog_prompt,
    build_timeout_prompt,
    build_state_mismatch_prompt,
)

logger = logging.getLogger(__name__)


class AIRecoveryAgent:
    """
    AI-powered recovery decision maker.

    Uses LLM (Claude/GPT) to analyze anomalies and recommend recovery actions.
    """

    def __init__(self, config: AIRecoveryConfig):
        """
        Initialize AI recovery agent.

        Args:
            config: AIRecoveryConfig with API credentials and settings
        """
        self.config = config
        self._llm_call_count = 0  # Track LLM calls for cost control

        # Validate configuration
        errors = config.validate()
        if errors:
            raise ValueError(f"Invalid AIRecoveryConfig: {', '.join(errors)}")

        # Initialize LLM client (lazy loaded)
        self._client = None

    def _get_client(self):
        """
        Lazy load LLM client.

        Returns:
            Anthropic, OpenAI, or ZhipuAI client based on config.provider
        """
        if self._client is None:
            if self.config.provider == "anthropic":
                import anthropic
                self._client = anthropic.Anthropic(api_key=self.config.api_key)
            elif self.config.provider == "openai":
                import openai
                self._client = openai.OpenAI(api_key=self.config.api_key)
            elif self.config.provider == "zhipuai":
                from zhipuai import ZhipuAI
                self._client = ZhipuAI(api_key=self.config.api_key)
            else:
                raise ValueError(f"Unsupported provider: {self.config.provider}")

        return self._client

    def analyze_anomaly(
        self,
        anomaly: Anomaly,
        context: OperationContext,
    ) -> RecoveryDecision:
        """
        Analyze an anomaly and decide recovery action.

        Args:
            anomaly: Detected anomaly
            context: Operation context with current state

        Returns:
            RecoveryDecision with recommended action

        Raises:
            RuntimeError: If LLM call fails after retries
        """
        # Check if we've exceeded max LLM calls
        if self._llm_call_count >= self.config.max_llm_calls_per_session:
            logger.warning(
                f"Max LLM calls ({self.config.max_llm_calls_per_session}) exceeded, using fallback"
            )
            return self._get_conservative_fallback(anomaly)

        # Build prompt based on anomaly type
        prompt = self._build_prompt(anomaly, context.to_dict())

        # Call LLM
        try:
            self._llm_call_count += 1
            llm_response = self._call_llm(prompt)
            decision = self._parse_response(llm_response)

            # Check confidence threshold
            if decision.confidence < self.config.confidence_threshold:
                logger.warning(
                    f"Low confidence ({decision.confidence:.2f} < {self.config.confidence_threshold}), using fallback"
                )
                return self._get_conservative_fallback(anomaly)

            logger.info(f"AI decision: {decision}")
            return decision

        except Exception as e:
            logger.error(f"LLM call failed: {e}", exc_info=True)
            return self._get_conservative_fallback(anomaly)

    def _build_prompt(self, anomaly: Anomaly, context: Dict[str, Any]) -> str:
        """
        Build prompt based on anomaly type.

        Args:
            anomaly: Anomaly object
            context: Context dictionary from OperationContext.to_dict()

        Returns:
            Formatted prompt string
        """
        if anomaly.type == AnomalyType.UNEXPECTED_DIALOG:
            return build_dialog_prompt(anomaly, context)
        elif anomaly.type == AnomalyType.TIMEOUT:
            return build_timeout_prompt(anomaly, context)
        elif anomaly.type == AnomalyType.STATE_MISMATCH:
            return build_state_mismatch_prompt(anomaly, context)
        else:
            # Unknown anomaly type, include all info
            return f"""## Anomaly: {anomaly.type.name}

**Anomaly Context:**
{json.dumps(anomaly.context, indent=2)}

**Operation Context:**
{json.dumps(context, indent=2)}

**Your Task:**
Analyze this unexpected situation and recommend the best recovery action.

Respond with JSON only (no markdown).
"""

    def _call_llm(self, prompt: str) -> str:
        """
        Call LLM API.

        Args:
            prompt: User prompt to send

        Returns:
            Raw LLM response text

        Raises:
            Exception: If API call fails
        """
        client = self._get_client()

        if self.config.provider == "anthropic":
            response = client.messages.create(
                model=self.config.model,
                max_tokens=self.config.max_tokens,
                temperature=self.config.temperature,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text

        elif self.config.provider == "openai":
            response = client.chat.completions.create(
                model=self.config.model,
                max_tokens=self.config.max_tokens,
                temperature=self.config.temperature,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            )
            return response.choices[0].message.content

        elif self.config.provider == "zhipuai":
            response = client.chat.completions.create(
                model=self.config.model,
                max_tokens=self.config.max_tokens,
                temperature=self.config.temperature,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            )
            return response.choices[0].message.content

        else:
            raise ValueError(f"Unsupported provider: {self.config.provider}")

    def _parse_response(self, llm_response: str) -> RecoveryDecision:
        """
        Parse LLM JSON response into RecoveryDecision.

        Args:
            llm_response: Raw LLM response text (should be JSON)

        Returns:
            RecoveryDecision object

        Raises:
            ValueError: If response is not valid JSON or missing required fields
        """
        # Clean response (remove markdown code blocks if present)
        llm_response = llm_response.strip()
        if llm_response.startswith("```json"):
            llm_response = llm_response[7:]  # Remove ```json
        if llm_response.startswith("```"):
            llm_response = llm_response[3:]  # Remove ```
        if llm_response.endswith("```"):
            llm_response = llm_response[:-3]  # Remove trailing ```
        llm_response = llm_response.strip()

        try:
            data = json.loads(llm_response)

            # Validate required fields
            if "action" not in data:
                raise ValueError("Missing 'action' field in LLM response")
            if "confidence" not in data:
                raise ValueError("Missing 'confidence' field in LLM response")
            if "reasoning" not in data:
                raise ValueError("Missing 'reasoning' field in LLM response")

            # Parse action enum
            try:
                action = RecoveryAction[data["action"]]
            except KeyError:
                raise ValueError(f"Invalid action: {data['action']}")

            # Build RecoveryDecision
            return RecoveryDecision(
                action=action,
                confidence=float(data["confidence"]),
                reasoning=data["reasoning"],
                parameters=data.get("parameters", {}),
                estimated_time=float(data.get("estimated_time", 30.0)),
            )

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse LLM response as JSON: {e}")
            logger.debug(f"LLM response: {llm_response}")
            raise ValueError(f"LLM response is not valid JSON: {e}")

        except (KeyError, ValueError, TypeError) as e:
            logger.error(f"Failed to parse LLM response: {e}")
            logger.debug(f"LLM response: {llm_response}")
            raise ValueError(f"Invalid LLM response structure: {e}")

    def _get_conservative_fallback(self, anomaly: Anomaly) -> RecoveryDecision:
        """
        Get conservative fallback decision when LLM fails or low confidence.

        Args:
            anomaly: Anomaly object

        Returns:
            RecoveryDecision with safe fallback action
        """
        # Conservative strategy: GO_BACK for most anomalies, ABORT for critical
        if anomaly.severity.name in ("CRITICAL", "HIGH"):
            action = RecoveryAction.ABORT
            reasoning = f"AI uncertainty with {anomaly.severity.name} severity, aborting for safety"
        else:
            action = RecoveryAction.GO_BACK
            reasoning = "AI uncertainty, using conservative fallback: navigate back"

        return RecoveryDecision(
            action=action,
            confidence=0.6,  # Medium confidence in fallback
            reasoning=reasoning,
            parameters={},
            estimated_time=5.0,
        )

    def reset_call_count(self):
        """Reset LLM call counter (call at start of new workflow session)."""
        self._llm_call_count = 0

    @property
    def call_count(self) -> int:
        """Get current LLM call count."""
        return self._llm_call_count
