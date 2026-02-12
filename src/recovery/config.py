"""
Configuration for AI exception recovery system.

Loads settings from environment variables with sensible defaults.
All settings are immutable (frozen dataclass) to prevent accidental modification.
"""

from dataclasses import dataclass
from typing import Optional
import os
from pathlib import Path


@dataclass(frozen=True)
class AIRecoveryConfig:
    """
    Configuration for AI-powered exception recovery.

    All settings can be overridden via environment variables (see from_env()).
    """

    # ==================== Feature Toggle ====================
    enabled: bool = False

    # ==================== LLM Configuration ====================
    provider: str = "anthropic"  # "anthropic" or "openai"
    model: str = "claude-3-5-sonnet-20241022"
    api_key: str = ""
    max_tokens: int = 1024
    temperature: float = 0.0  # Deterministic for consistent decisions

    # ==================== Recovery Strategy ====================
    max_retries: int = 2  # Max recovery attempts per anomaly
    confidence_threshold: float = 0.7  # Minimum confidence to proceed
    max_llm_calls_per_session: int = 3  # Cost control: max 3 AI calls per workflow

    # ==================== Timeout Configuration ====================
    default_timeout_multiplier: float = 2.0  # Extend timeout to 2x original
    max_wait_time: float = 120.0  # Max wait time in seconds

    # ==================== Logging ====================
    log_dir: str = "logs/ai_recovery"
    log_decisions: bool = True  # Log all AI decisions for analysis

    @classmethod
    def from_env(cls) -> "AIRecoveryConfig":
        """
        Load configuration from environment variables.

        Environment variables:
            ENABLE_AI_RECOVERY: "true" to enable (default: false)
            AI_RECOVERY_PROVIDER: "anthropic", "openai", or "zhipuai"
            AI_RECOVERY_MODEL: Model identifier
            ANTHROPIC_API_KEY, OPENAI_API_KEY, or ZHIPUAI_API_KEY: API key
            AI_RECOVERY_CONFIDENCE: Minimum confidence (0.0-1.0)
            AI_RECOVERY_MAX_RETRIES: Max retries per anomaly
            AI_RECOVERY_MAX_CALLS: Max LLM calls per session
            AI_RECOVERY_LOG_DIR: Log directory path

        Returns:
            AIRecoveryConfig with settings loaded from environment
        """
        enabled = os.getenv("ENABLE_AI_RECOVERY", "false").lower() == "true"
        provider = os.getenv("AI_RECOVERY_PROVIDER", "anthropic").lower()

        # Select API key based on provider
        if provider == "anthropic":
            api_key = os.getenv("ANTHROPIC_API_KEY", "")
        elif provider == "openai":
            api_key = os.getenv("OPENAI_API_KEY", "")
        elif provider == "zhipuai":
            api_key = os.getenv("ZHIPUAI_API_KEY", "")
        else:
            api_key = ""

        # Default model based on provider
        default_models = {
            "anthropic": "claude-3-5-sonnet-20241022",
            "openai": "gpt-4o-mini",
            "zhipuai": "glm-4-plus",
        }
        default_model = default_models.get(provider, "claude-3-5-sonnet-20241022")

        return cls(
            enabled=enabled,
            provider=provider,
            model=os.getenv("AI_RECOVERY_MODEL", default_model),
            api_key=api_key,
            max_tokens=int(os.getenv("AI_RECOVERY_MAX_TOKENS", "1024")),
            temperature=float(os.getenv("AI_RECOVERY_TEMPERATURE", "0.0")),
            max_retries=int(os.getenv("AI_RECOVERY_MAX_RETRIES", "2")),
            confidence_threshold=float(os.getenv("AI_RECOVERY_CONFIDENCE", "0.7")),
            max_llm_calls_per_session=int(os.getenv("AI_RECOVERY_MAX_CALLS", "3")),
            default_timeout_multiplier=float(os.getenv("AI_RECOVERY_TIMEOUT_MULT", "2.0")),
            max_wait_time=float(os.getenv("AI_RECOVERY_MAX_WAIT", "120.0")),
            log_dir=os.getenv("AI_RECOVERY_LOG_DIR", "logs/ai_recovery"),
            log_decisions=os.getenv("AI_RECOVERY_LOG_DECISIONS", "true").lower() == "true",
        )

    def validate(self) -> list[str]:
        """
        Validate configuration settings.

        Returns:
            List of validation error messages (empty if valid)
        """
        errors = []

        if self.enabled and not self.api_key:
            errors.append(f"AI recovery enabled but no API key found for provider '{self.provider}'")

        if self.provider not in ("anthropic", "openai", "zhipuai"):
            errors.append(f"Invalid provider '{self.provider}', must be 'anthropic', 'openai', or 'zhipuai'")

        if not 0.0 <= self.confidence_threshold <= 1.0:
            errors.append(f"confidence_threshold must be 0.0-1.0, got {self.confidence_threshold}")

        if self.max_retries < 0:
            errors.append(f"max_retries must be >= 0, got {self.max_retries}")

        if self.max_llm_calls_per_session < 1:
            errors.append(f"max_llm_calls_per_session must be >= 1, got {self.max_llm_calls_per_session}")

        if self.default_timeout_multiplier < 1.0:
            errors.append(f"default_timeout_multiplier must be >= 1.0, got {self.default_timeout_multiplier}")

        return errors

    def ensure_log_dir(self) -> Path:
        """
        Ensure log directory exists.

        Returns:
            Path object for log directory
        """
        log_path = Path(self.log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        return log_path

    def __str__(self) -> str:
        """String representation (masks API key)."""
        masked_key = f"{self.api_key[:8]}..." if self.api_key else "<not set>"
        return (
            f"AIRecoveryConfig(enabled={self.enabled}, "
            f"provider={self.provider}, model={self.model}, "
            f"api_key={masked_key}, confidence_threshold={self.confidence_threshold})"
        )
