"""
Demo script: AI Recovery Agent usage example.

This script demonstrates how to use the AI Recovery Agent without
requiring a real API key (uses mock client for demo purposes).
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from src.recovery import (
    AIRecoveryConfig,
    AIRecoveryAgent,
    Anomaly,
    AnomalyType,
    AnomalySeverity,
    OperationContext,
)
from unittest.mock import Mock
import json


def demo_dialog_recovery():
    """Demo: AI recovers from unexpected dialog."""
    print("\n" + "="*60)
    print("DEMO 1: Unexpected Dialog Recovery")
    print("="*60)

    # Create config (demo mode, no real API key needed)
    config = AIRecoveryConfig(
        enabled=True,
        provider="anthropic",
        model="claude-3-5-sonnet-20241022",
        api_key="demo-key",
    )

    # Create agent
    agent = AIRecoveryAgent(config)

    # Mock the LLM client for demo
    mock_client = Mock()
    mock_client.messages.create.return_value = Mock(
        content=[Mock(text=json.dumps({
            "action": "CLICK_BUTTON",
            "confidence": 0.95,
            "reasoning": "Error dialog detected. Clicking OK will dismiss the dialog and allow the workflow to continue.",
            "parameters": {"button_text": "OK"},
            "estimated_time": 2.0,
        }))]
    )
    agent._get_client = Mock(return_value=mock_client)

    # Create anomaly: unexpected error dialog
    anomaly = Anomaly(
        type=AnomalyType.UNEXPECTED_DIALOG,
        severity=AnomalySeverity.MEDIUM,
        context={
            "modal_title": "Connection Error",
            "modal_buttons": ["OK", "Cancel"],
            "isModalShowing": True,
        },
    )

    # Create operation context
    context = OperationContext(
        operation_name="connect_to_vehicle",
        current_page="DEVICE_EXPLORER",
        visible_buttons=["Enter", "Disconnect"],
        recent_actions=[
            "open_device_explorer",
            "select_device:SM2_USB",
            "click_connect",
        ],
        elapsed_time=10.0,
        expected_time=30.0,
    )

    # Analyze and get decision
    print("\n📋 Anomaly Details:")
    print(f"  Type: {anomaly.type.name}")
    print(f"  Severity: {anomaly.severity.name}")
    print(f"  Dialog: {anomaly.context['modal_title']}")
    print(f"  Buttons: {anomaly.context['modal_buttons']}")

    print("\n🤖 AI is analyzing...")
    decision = agent.analyze_anomaly(anomaly, context)

    print("\n✅ AI Decision:")
    print(f"  Action: {decision.action.name}")
    print(f"  Confidence: {decision.confidence:.0%}")
    print(f"  Reasoning: {decision.reasoning}")
    print(f"  Parameters: {decision.parameters}")
    print(f"  Estimated Time: {decision.estimated_time}s")


def demo_timeout_recovery():
    """Demo: AI handles operation timeout."""
    print("\n" + "="*60)
    print("DEMO 2: Timeout Recovery")
    print("="*60)

    config = AIRecoveryConfig(
        enabled=True,
        provider="anthropic",
        model="claude-3-5-sonnet-20241022",
        api_key="demo-key",
    )

    agent = AIRecoveryAgent(config)

    # Mock LLM response for timeout
    mock_client = Mock()
    mock_client.messages.create.return_value = Mock(
        content=[Mock(text=json.dumps({
            "action": "WAIT_LONGER",
            "confidence": 0.85,
            "reasoning": "VCI connection to vehicle ECU typically takes 30-90 seconds on cold start. No error indicators visible. Recommend extending timeout.",
            "parameters": {"wait_seconds": 60},
            "estimated_time": 60.0,
        }))]
    )
    agent._get_client = Mock(return_value=mock_client)

    # Create timeout anomaly
    anomaly = Anomaly(
        type=AnomalyType.TIMEOUT,
        severity=AnomalySeverity.MEDIUM,
        context={
            "operation": "wait_for_enter_button",
            "elapsed": 45.0,
            "expected": 30.0,
        },
    )

    context = OperationContext(
        operation_name="wait_for_enter_button",
        current_page="DEVICE_EXPLORER",
        visible_buttons=["Enter (disabled)", "Disconnect"],
        recent_actions=[
            "select_device",
            "click_connect",
        ],
        elapsed_time=45.0,
        expected_time=30.0,
    )

    print("\n📋 Anomaly Details:")
    print(f"  Type: {anomaly.type.name}")
    print(f"  Operation: {anomaly.context['operation']}")
    print(f"  Elapsed: {anomaly.context['elapsed']}s")
    print(f"  Expected: {anomaly.context['expected']}s")
    print(f"  Timeout Ratio: {anomaly.context['elapsed']/anomaly.context['expected']:.1f}x")

    print("\n🤖 AI is analyzing...")
    decision = agent.analyze_anomaly(anomaly, context)

    print("\n✅ AI Decision:")
    print(f"  Action: {decision.action.name}")
    print(f"  Confidence: {decision.confidence:.0%}")
    print(f"  Reasoning: {decision.reasoning}")
    print(f"  Wait Additional: {decision.parameters['wait_seconds']}s")


def demo_low_confidence_fallback():
    """Demo: AI falls back to conservative action when uncertain."""
    print("\n" + "="*60)
    print("DEMO 3: Low Confidence Fallback")
    print("="*60)

    config = AIRecoveryConfig(
        enabled=True,
        provider="anthropic",
        api_key="demo-key",
        confidence_threshold=0.7,
    )

    agent = AIRecoveryAgent(config)

    # Mock LOW confidence response
    mock_client = Mock()
    mock_client.messages.create.return_value = Mock(
        content=[Mock(text=json.dumps({
            "action": "RETRY_FROM_START",
            "confidence": 0.5,  # Below threshold!
            "reasoning": "Uncertain about the best recovery action",
            "parameters": {},
            "estimated_time": 10.0,
        }))]
    )
    agent._get_client = Mock(return_value=mock_client)

    anomaly = Anomaly(type=AnomalyType.STATE_MISMATCH)
    context = OperationContext(operation_name="navigate_to_diagnostics")

    print("\n📋 Scenario: Unknown state mismatch")
    print(f"  Confidence Threshold: {config.confidence_threshold:.0%}")

    print("\n🤖 AI is analyzing...")
    decision = agent.analyze_anomaly(anomaly, context)

    print("\n✅ AI Decision:")
    print(f"  Action: {decision.action.name}")
    print(f"  Confidence: {decision.confidence:.0%}")
    print(f"  Reasoning: {decision.reasoning}")
    print("\n⚠️  Note: AI used conservative fallback (GO_BACK) due to low confidence.")


def demo_max_calls_limit():
    """Demo: Cost control via max LLM calls limit."""
    print("\n" + "="*60)
    print("DEMO 4: Max LLM Calls Limit (Cost Control)")
    print("="*60)

    config = AIRecoveryConfig(
        enabled=True,
        provider="anthropic",
        api_key="demo-key",
        max_llm_calls_per_session=3,  # Limit to 3 calls
    )

    agent = AIRecoveryAgent(config)

    # Mock client
    mock_client = Mock()
    mock_client.messages.create.return_value = Mock(
        content=[Mock(text=json.dumps({
            "action": "WAIT_LONGER",
            "confidence": 0.8,
            "reasoning": "Normal delay",
            "parameters": {},
        }))]
    )
    agent._get_client = Mock(return_value=mock_client)

    anomaly = Anomaly(type=AnomalyType.TIMEOUT)
    context = OperationContext(operation_name="test")

    print(f"\n📊 Max LLM Calls: {config.max_llm_calls_per_session}")
    print(f"  Current Count: {agent.call_count}")

    # Make 3 calls
    for i in range(4):
        decision = agent.analyze_anomaly(anomaly, context)
        used_llm = "✓ Used LLM" if agent.call_count > i else "✗ Used fallback (limit reached)"
        print(f"\n  Call {i+1}: {used_llm}")
        print(f"    Action: {decision.action.name}")
        print(f"    Total LLM Calls: {agent.call_count}")

    print(f"\n💡 Cost Control: After reaching limit, agent uses fallback without calling LLM")


if __name__ == "__main__":
    print("\n🎯 AI Exception Recovery System - Demo")
    print("This demo shows how AI analyzes and recovers from automation failures")
    print("(Using mocked LLM responses, no API key required)")

    demo_dialog_recovery()
    demo_timeout_recovery()
    demo_low_confidence_fallback()
    demo_max_calls_limit()

    print("\n" + "="*60)
    print("✅ Demo Complete!")
    print("="*60)
    print("\nNext steps:")
    print("  1. Set ENABLE_AI_RECOVERY=true in .env")
    print("  2. Add your ANTHROPIC_API_KEY")
    print("  3. Run real tests with GDS2 (Day 7)")
    print()
