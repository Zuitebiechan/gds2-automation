"""
Unit tests for AI Recovery Agent.

Tests LLM integration with mocked API calls.
"""

import json
import pytest
from unittest.mock import Mock, patch, MagicMock

from src.recovery import (
    AIRecoveryConfig,
    Anomaly,
    AnomalyType,
    AnomalySeverity,
    RecoveryAction,
    OperationContext,
)
from src.recovery.ai_recovery_agent import AIRecoveryAgent


class TestAIRecoveryAgent:
    """Test AIRecoveryAgent class."""

    @pytest.fixture
    def config(self):
        """Create test configuration."""
        return AIRecoveryConfig(
            enabled=True,
            provider="anthropic",
            model="claude-3-5-sonnet-20241022",
            api_key="sk-ant-test-key",
            confidence_threshold=0.7,
            max_llm_calls_per_session=3,
        )

    @pytest.fixture
    def mock_anthropic_client(self):
        """Mock Anthropic client for _get_client()."""
        mock_client = Mock()

        # Mock successful response
        mock_response = Mock()
        mock_response.content = [
            Mock(
                text=json.dumps({
                    "action": "CLICK_BUTTON",
                    "confidence": 0.95,
                    "reasoning": "Error dialog detected, click OK to dismiss",
                    "parameters": {"button_text": "OK"},
                    "estimated_time": 2.0,
                })
            )
        ]
        mock_client.messages.create.return_value = mock_response

        return mock_client

    @pytest.fixture
    def mock_openai_client(self):
        """Mock OpenAI client for _get_client()."""
        mock_client = Mock()

        # Mock successful response
        mock_response = Mock()
        mock_response.choices = [
            Mock(
                message=Mock(
                    content=json.dumps({
                        "action": "WAIT_LONGER",
                        "confidence": 0.85,
                        "reasoning": "VCI connection takes time",
                        "parameters": {"wait_seconds": 60},
                        "estimated_time": 60.0,
                    })
                )
            )
        ]
        mock_client.chat.completions.create.return_value = mock_response

        return mock_client

    def test_init_valid_config(self, config):
        """Test initialization with valid config."""
        agent = AIRecoveryAgent(config)

        assert agent.config == config
        assert agent.call_count == 0

    def test_init_invalid_config(self):
        """Test initialization fails with invalid config."""
        config = AIRecoveryConfig(enabled=True, api_key="")  # Missing API key

        with pytest.raises(ValueError, match="Invalid AIRecoveryConfig"):
            AIRecoveryAgent(config)

    def test_analyze_dialog_anomaly_anthropic(self, config, mock_anthropic_client):
        """Test analyzing dialog anomaly with Anthropic."""
        agent = AIRecoveryAgent(config)

        # Mock _get_client to return our mock client
        agent._get_client = Mock(return_value=mock_anthropic_client)

        anomaly = Anomaly(
            type=AnomalyType.UNEXPECTED_DIALOG,
            context={
                "modal_title": "Error 0x80004005",
                "modal_buttons": ["OK"],
                "isModalShowing": True,
            },
        )

        context = OperationContext(
            operation_name="connect_device",
            current_page="DEVICE_EXPLORER",
            visible_buttons=["Enter", "Disconnect"],
            recent_actions=["open_device_explorer", "select_device"],
        )

        decision = agent.analyze_anomaly(anomaly, context)

        # Verify decision
        assert decision.action == RecoveryAction.CLICK_BUTTON
        assert decision.confidence == 0.95
        assert "Error dialog" in decision.reasoning
        assert decision.parameters["button_text"] == "OK"
        assert decision.estimated_time == 2.0

        # Verify LLM was called
        assert agent.call_count == 1
        assert mock_anthropic_client.messages.create.called

        # Verify prompt structure
        call_args = mock_anthropic_client.messages.create.call_args
        assert call_args.kwargs["model"] == "claude-3-5-sonnet-20241022"
        assert call_args.kwargs["temperature"] == 0.0
        assert "system" in call_args.kwargs
        assert len(call_args.kwargs["messages"]) == 1

    def test_analyze_timeout_anomaly_openai(self, mock_openai_client):
        """Test analyzing timeout anomaly with OpenAI."""
        config = AIRecoveryConfig(
            enabled=True,
            provider="openai",
            model="gpt-4o-mini",
            api_key="sk-proj-test-key",
        )
        agent = AIRecoveryAgent(config)

        # Mock _get_client to return our mock client
        agent._get_client = Mock(return_value=mock_openai_client)

        anomaly = Anomaly(
            type=AnomalyType.TIMEOUT,
            context={"operation": "wait_for_enter", "elapsed": 45.0, "expected": 30.0},
        )

        context = OperationContext(
            operation_name="wait_for_enter",
            current_page="DEVICE_EXPLORER",
            elapsed_time=45.0,
            expected_time=30.0,
        )

        decision = agent.analyze_anomaly(anomaly, context)

        # Verify decision
        assert decision.action == RecoveryAction.WAIT_LONGER
        assert decision.confidence == 0.85
        assert decision.parameters["wait_seconds"] == 60

        # Verify OpenAI was called
        assert agent.call_count == 1
        assert mock_openai_client.chat.completions.create.called

    def test_low_confidence_uses_fallback(self, config, mock_anthropic_client):
        """Test that low confidence triggers fallback."""
        # Mock low confidence response
        mock_anthropic_client.messages.create.return_value.content[0].text = json.dumps({
            "action": "WAIT_LONGER",
            "confidence": 0.5,  # Below threshold (0.7)
            "reasoning": "Uncertain about this",
            "parameters": {},
            "estimated_time": 30.0,
        })

        agent = AIRecoveryAgent(config)
        agent._get_client = Mock(return_value=mock_anthropic_client)

        anomaly = Anomaly(type=AnomalyType.TIMEOUT)
        context = OperationContext(operation_name="test")

        decision = agent.analyze_anomaly(anomaly, context)

        # Should use fallback (GO_BACK)
        assert decision.action == RecoveryAction.GO_BACK
        assert decision.confidence == 0.6
        assert "fallback" in decision.reasoning.lower()

    def test_max_llm_calls_limit(self, config, mock_anthropic_client):
        """Test that max_llm_calls_per_session is enforced."""
        agent = AIRecoveryAgent(config)
        agent._get_client = Mock(return_value=mock_anthropic_client)

        anomaly = Anomaly(type=AnomalyType.TIMEOUT)
        context = OperationContext(operation_name="test")

        # Make 3 calls (max limit)
        for i in range(3):
            agent.analyze_anomaly(anomaly, context)

        assert agent.call_count == 3

        # 4th call should use fallback without calling LLM
        mock_anthropic_client.messages.create.reset_mock()
        decision = agent.analyze_anomaly(anomaly, context)

        assert agent.call_count == 3  # Should not increment
        assert not mock_anthropic_client.messages.create.called
        assert decision.action == RecoveryAction.GO_BACK  # Fallback

    def test_reset_call_count(self, config, mock_anthropic_client):
        """Test resetting call counter."""
        agent = AIRecoveryAgent(config)
        agent._get_client = Mock(return_value=mock_anthropic_client)

        anomaly = Anomaly(type=AnomalyType.TIMEOUT)
        context = OperationContext(operation_name="test")

        # Make 2 calls
        agent.analyze_anomaly(anomaly, context)
        agent.analyze_anomaly(anomaly, context)
        assert agent.call_count == 2

        # Reset
        agent.reset_call_count()
        assert agent.call_count == 0

    def test_parse_response_valid_json(self, config):
        """Test parsing valid LLM response."""
        agent = AIRecoveryAgent(config)

        llm_response = json.dumps({
            "action": "GO_BACK",
            "confidence": 0.8,
            "reasoning": "Stuck state detected",
            "parameters": {},
            "estimated_time": 5.0,
        })

        decision = agent._parse_response(llm_response)

        assert decision.action == RecoveryAction.GO_BACK
        assert decision.confidence == 0.8
        assert decision.reasoning == "Stuck state detected"
        assert decision.parameters == {}
        assert decision.estimated_time == 5.0

    def test_parse_response_with_markdown_blocks(self, config):
        """Test parsing response with markdown code blocks."""
        agent = AIRecoveryAgent(config)

        # Response wrapped in markdown
        llm_response = """```json
{
  "action": "CLICK_BUTTON",
  "confidence": 0.9,
  "reasoning": "Click OK",
  "parameters": {"button_text": "OK"}
}
```"""

        decision = agent._parse_response(llm_response)

        assert decision.action == RecoveryAction.CLICK_BUTTON
        assert decision.confidence == 0.9

    def test_parse_response_invalid_json(self, config):
        """Test parsing invalid JSON raises error."""
        agent = AIRecoveryAgent(config)

        with pytest.raises(ValueError, match="not valid JSON"):
            agent._parse_response("This is not JSON")

    def test_parse_response_missing_required_fields(self, config):
        """Test parsing response missing required fields."""
        agent = AIRecoveryAgent(config)

        # Missing 'reasoning' field
        llm_response = json.dumps({
            "action": "WAIT_LONGER",
            "confidence": 0.8,
        })

        with pytest.raises(ValueError, match="Missing 'reasoning'"):
            agent._parse_response(llm_response)

    def test_parse_response_invalid_action(self, config):
        """Test parsing response with invalid action."""
        agent = AIRecoveryAgent(config)

        llm_response = json.dumps({
            "action": "INVALID_ACTION",
            "confidence": 0.8,
            "reasoning": "Test",
        })

        with pytest.raises(ValueError, match="Invalid action"):
            agent._parse_response(llm_response)

    def test_parse_response_minimal_fields(self, config):
        """Test parsing response with only required fields."""
        agent = AIRecoveryAgent(config)

        llm_response = json.dumps({
            "action": "ABORT",
            "confidence": 0.9,
            "reasoning": "Fatal error",
        })

        decision = agent._parse_response(llm_response)

        assert decision.action == RecoveryAction.ABORT
        assert decision.parameters == {}  # Default
        assert decision.estimated_time == 30.0  # Default

    def test_fallback_for_critical_anomaly(self, config):
        """Test fallback uses ABORT for critical severity."""
        agent = AIRecoveryAgent(config)

        anomaly = Anomaly(
            type=AnomalyType.UNEXPECTED_DIALOG,
            severity=AnomalySeverity.CRITICAL,
        )

        fallback = agent._get_conservative_fallback(anomaly)

        assert fallback.action == RecoveryAction.ABORT
        assert "CRITICAL" in fallback.reasoning

    def test_fallback_for_medium_anomaly(self, config):
        """Test fallback uses GO_BACK for medium severity."""
        agent = AIRecoveryAgent(config)

        anomaly = Anomaly(
            type=AnomalyType.TIMEOUT,
            severity=AnomalySeverity.MEDIUM,
        )

        fallback = agent._get_conservative_fallback(anomaly)

        assert fallback.action == RecoveryAction.GO_BACK
        assert "fallback" in fallback.reasoning.lower()

    def test_llm_api_failure_uses_fallback(self, config):
        """Test that LLM API failure triggers fallback."""
        mock_client = Mock()
        # Simulate API error
        mock_client.messages.create.side_effect = Exception("API Error")

        agent = AIRecoveryAgent(config)
        agent._get_client = Mock(return_value=mock_client)

        anomaly = Anomaly(type=AnomalyType.TIMEOUT)
        context = OperationContext(operation_name="test")

        decision = agent.analyze_anomaly(anomaly, context)

        # Should use fallback
        assert decision.action == RecoveryAction.GO_BACK
        assert agent.call_count == 1  # Call was attempted

    def test_build_prompt_dialog(self, config):
        """Test building prompt for dialog anomaly."""
        agent = AIRecoveryAgent(config)

        anomaly = Anomaly(
            type=AnomalyType.UNEXPECTED_DIALOG,
            context={
                "modal_title": "Connection Failed",
                "modal_buttons": ["Retry", "Cancel"],
            },
        )

        context_dict = {
            "operation": "connect_device",
            "current_page": "DEVICE_EXPLORER",
            "visible_buttons": [],
            "recent_actions": ["open_explorer"],
            "elapsed_time": 10.0,
            "expected_time": 30.0,
        }

        prompt = agent._build_prompt(anomaly, context_dict)

        # Verify prompt contains key information
        assert "Connection Failed" in prompt
        assert "Retry" in prompt
        assert "Cancel" in prompt
        assert "DEVICE_EXPLORER" in prompt

    def test_build_prompt_timeout(self, config):
        """Test building prompt for timeout anomaly."""
        agent = AIRecoveryAgent(config)

        anomaly = Anomaly(type=AnomalyType.TIMEOUT)

        context_dict = {
            "operation": "wait_for_button",
            "current_page": "MODULE_LIST",
            "visible_buttons": ["Module A", "Module B"],
            "recent_actions": ["select_vehicle", "navigate_to_modules"],
            "elapsed_time": 60.0,
            "expected_time": 30.0,
        }

        prompt = agent._build_prompt(anomaly, context_dict)

        # Verify prompt contains timeout information
        assert "Timeout" in prompt
        assert "60.0" in prompt
        assert "30.0" in prompt
        assert "MODULE_LIST" in prompt

    def test_build_prompt_state_mismatch(self, config):
        """Test building prompt for state mismatch anomaly."""
        agent = AIRecoveryAgent(config)

        anomaly = Anomaly(
            type=AnomalyType.STATE_MISMATCH,
            context={
                "expected_page": "DIAGNOSTICS_MENU",
                "actual_page": "MAIN_MENU",
            },
        )

        context_dict = {
            "operation": "navigate_to_diagnostics",
            "current_page": "MAIN_MENU",
            "visible_buttons": ["Device", "Setup"],
            "recent_actions": ["click_back"],
            "elapsed_time": 5.0,
            "expected_time": 10.0,
        }

        prompt = agent._build_prompt(anomaly, context_dict)

        # Verify prompt contains state mismatch info
        assert "DIAGNOSTICS_MENU" in prompt
        assert "MAIN_MENU" in prompt
        assert "State Mismatch" in prompt

    def test_unsupported_provider_raises_error(self):
        """Test that unsupported provider raises error."""
        config = AIRecoveryConfig(
            enabled=True,
            provider="invalid_provider",
            api_key="test",
        )

        # Should fail validation
        with pytest.raises(ValueError):
            AIRecoveryAgent(config)
