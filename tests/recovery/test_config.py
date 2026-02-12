"""
Unit tests for AIRecoveryConfig.

Tests configuration loading from environment variables and validation.
"""

import os
import pytest
from src.recovery.config import AIRecoveryConfig


class TestAIRecoveryConfig:
    """Test AIRecoveryConfig class."""

    def test_default_config(self):
        """Test default configuration values."""
        config = AIRecoveryConfig()

        assert config.enabled is False
        assert config.provider == "anthropic"
        assert config.model == "claude-3-5-sonnet-20241022"
        assert config.api_key == ""
        assert config.max_tokens == 1024
        assert config.temperature == 0.0
        assert config.max_retries == 2
        assert config.confidence_threshold == 0.7
        assert config.max_llm_calls_per_session == 3
        assert config.default_timeout_multiplier == 2.0
        assert config.max_wait_time == 120.0
        assert config.log_dir == "logs/ai_recovery"
        assert config.log_decisions is True

    def test_from_env_default(self, monkeypatch):
        """Test loading from environment with defaults."""
        # Clear all relevant env vars
        for key in [
            "ENABLE_AI_RECOVERY",
            "AI_RECOVERY_PROVIDER",
            "AI_RECOVERY_MODEL",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
        ]:
            monkeypatch.delenv(key, raising=False)

        config = AIRecoveryConfig.from_env()

        assert config.enabled is False
        assert config.provider == "anthropic"
        assert config.api_key == ""

    def test_from_env_enabled(self, monkeypatch):
        """Test loading with AI recovery enabled."""
        monkeypatch.setenv("ENABLE_AI_RECOVERY", "true")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key-123")

        config = AIRecoveryConfig.from_env()

        assert config.enabled is True
        assert config.api_key == "sk-ant-test-key-123"

    def test_from_env_openai_provider(self, monkeypatch):
        """Test loading with OpenAI provider."""
        monkeypatch.setenv("AI_RECOVERY_PROVIDER", "openai")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-test-key-456")

        config = AIRecoveryConfig.from_env()

        assert config.provider == "openai"
        assert config.api_key == "sk-proj-test-key-456"

    def test_from_env_custom_values(self, monkeypatch):
        """Test loading custom configuration values."""
        monkeypatch.setenv("ENABLE_AI_RECOVERY", "true")
        monkeypatch.setenv("AI_RECOVERY_CONFIDENCE", "0.85")
        monkeypatch.setenv("AI_RECOVERY_MAX_RETRIES", "3")
        monkeypatch.setenv("AI_RECOVERY_MAX_CALLS", "5")
        monkeypatch.setenv("AI_RECOVERY_MAX_TOKENS", "2048")
        monkeypatch.setenv("AI_RECOVERY_TEMPERATURE", "0.2")
        monkeypatch.setenv("AI_RECOVERY_TIMEOUT_MULT", "3.0")
        monkeypatch.setenv("AI_RECOVERY_MAX_WAIT", "180.0")
        monkeypatch.setenv("AI_RECOVERY_LOG_DIR", "custom/log/dir")
        monkeypatch.setenv("AI_RECOVERY_LOG_DECISIONS", "false")

        config = AIRecoveryConfig.from_env()

        assert config.enabled is True
        assert config.confidence_threshold == 0.85
        assert config.max_retries == 3
        assert config.max_llm_calls_per_session == 5
        assert config.max_tokens == 2048
        assert config.temperature == 0.2
        assert config.default_timeout_multiplier == 3.0
        assert config.max_wait_time == 180.0
        assert config.log_dir == "custom/log/dir"
        assert config.log_decisions is False

    def test_validate_valid_config(self):
        """Test validation passes for valid config."""
        config = AIRecoveryConfig(
            enabled=True,
            api_key="sk-ant-test",
            provider="anthropic",
            confidence_threshold=0.7,
            max_retries=2,
            max_llm_calls_per_session=3,
        )

        errors = config.validate()
        assert len(errors) == 0

    def test_validate_missing_api_key(self):
        """Test validation fails when enabled without API key."""
        config = AIRecoveryConfig(enabled=True, api_key="")

        errors = config.validate()
        assert len(errors) == 1
        assert "no API key found" in errors[0]

    def test_validate_invalid_provider(self):
        """Test validation fails for invalid provider."""
        config = AIRecoveryConfig(provider="invalid_provider")

        errors = config.validate()
        assert len(errors) >= 1
        assert any("Invalid provider" in err for err in errors)

    def test_validate_invalid_confidence(self):
        """Test validation fails for out-of-range confidence."""
        config = AIRecoveryConfig(confidence_threshold=1.5)

        errors = config.validate()
        assert any("confidence_threshold" in err for err in errors)

    def test_validate_invalid_max_retries(self):
        """Test validation fails for negative max_retries."""
        config = AIRecoveryConfig(max_retries=-1)

        errors = config.validate()
        assert any("max_retries" in err for err in errors)

    def test_validate_invalid_max_calls(self):
        """Test validation fails for invalid max_llm_calls."""
        config = AIRecoveryConfig(max_llm_calls_per_session=0)

        errors = config.validate()
        assert any("max_llm_calls_per_session" in err for err in errors)

    def test_validate_invalid_timeout_multiplier(self):
        """Test validation fails for timeout multiplier < 1.0."""
        config = AIRecoveryConfig(default_timeout_multiplier=0.5)

        errors = config.validate()
        assert any("default_timeout_multiplier" in err for err in errors)

    def test_config_immutable(self):
        """Test that config is frozen (immutable)."""
        config = AIRecoveryConfig()

        with pytest.raises(Exception):  # FrozenInstanceError
            config.enabled = True

    def test_str_masks_api_key(self):
        """Test that __str__ masks API key."""
        config = AIRecoveryConfig(api_key="sk-ant-very-secret-key-12345678901234567890")

        str_repr = str(config)
        assert "sk-ant-v..." in str_repr  # First 8 chars + "..."
        assert "very-secret" not in str_repr

    def test_str_empty_api_key(self):
        """Test __str__ with empty API key."""
        config = AIRecoveryConfig(api_key="")

        str_repr = str(config)
        assert "<not set>" in str_repr

    def test_ensure_log_dir_creates_directory(self, tmp_path, monkeypatch):
        """Test that ensure_log_dir creates directory."""
        log_dir = tmp_path / "test_logs" / "ai_recovery"
        config = AIRecoveryConfig(log_dir=str(log_dir))

        assert not log_dir.exists()

        result_path = config.ensure_log_dir()

        assert log_dir.exists()
        assert result_path == log_dir
