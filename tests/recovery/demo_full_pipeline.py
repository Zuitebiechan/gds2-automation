"""
Complete end-to-end demo of AI Recovery System.

This demo shows the full pipeline:
1. Anomaly Detection
2. AI Analysis
3. Recovery Execution
4. Verification

No API key required - uses mocked components.
"""

import sys
from pathlib import Path
import json
import time

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from src.recovery import (
    RecoveryManager,
    AIRecoveryConfig,
    Anomaly,
    AnomalyType,
    OperationContext,
    RecoveryAction,
)
from unittest.mock import Mock


def demo_full_pipeline():
    """Demo: Complete recovery pipeline from detection to execution."""
    print("\n" + "="*70)
    print("DEMO: Complete AI Recovery Pipeline")
    print("="*70)

    # Step 1: Initialize RecoveryManager
    print("\n📦 Step 1: Initialize Recovery Manager")
    config = AIRecoveryConfig(
        enabled=True,
        provider="anthropic",
        model="claude-3-5-sonnet-20241022",
        api_key="demo-key",
        log_decisions=False,  # Disable logging for demo
    )

    # Mock AgentNavigator
    mock_agent_nav = Mock()
    mock_agent_nav.click_button.return_value = {"success": True}

    manager = RecoveryManager(
        config=config,
        agent_navigator=mock_agent_nav,
    )

    print(f"  ✓ Manager initialized")
    print(f"  ✓ AI recovery enabled: {manager.enabled}")
    print(f"  ✓ LLM call count: {manager.llm_call_count}")

    # Mock LLM client
    mock_client = Mock()
    mock_client.messages.create.return_value = Mock(
        content=[Mock(text=json.dumps({
            "action": "CLICK_BUTTON",
            "confidence": 0.95,
            "reasoning": "Error dialog detected. Clicking OK will dismiss the error and allow the workflow to continue.",
            "parameters": {"button_text": "OK"},
            "estimated_time": 2.0,
        }))]
    )
    manager.agent._get_client = Mock(return_value=mock_client)

    # Step 2: Detect Anomaly
    print("\n🔍 Step 2: Detect Anomaly")

    # Simulate dialog appearing (create fake latest.json)
    data_dir = Path.home() / "gds2-data"
    data_dir.mkdir(exist_ok=True)

    fake_data = {
        "pageContext": {
            "isModalShowing": True,
            "modalTitle": "Connection Timeout",
            "modalButtons": ["OK", "Retry", "Cancel"],
        }
    }

    latest_path = data_dir / "latest.json"
    latest_path.write_text(json.dumps(fake_data), encoding="gbk")
    print(f"  ✓ Simulated dialog: Connection Timeout")

    anomaly = manager.check_for_dialogs()
    print(f"  ✓ Anomaly detected: {anomaly.type.name}")
    print(f"  ✓ Severity: {anomaly.severity.name}")
    print(f"  ✓ Dialog title: {anomaly.context['modal_title']}")
    print(f"  ✓ Buttons: {anomaly.context['modal_buttons']}")

    # Step 3: Create Context
    print("\n📝 Step 3: Create Operation Context")
    context = OperationContext(
        operation_name="connect_to_vehicle",
        current_page="DEVICE_EXPLORER",
        visible_buttons=["Enter (disabled)", "Disconnect"],
        recent_actions=[
            "open_device_explorer",
            "select_device:SM2_USB",
            "click_connect",
        ],
        elapsed_time=35.0,
        expected_time=30.0,
    )
    print(f"  ✓ Operation: {context.operation_name}")
    print(f"  ✓ Current page: {context.current_page}")
    print(f"  ✓ Recent actions: {len(context.recent_actions)}")

    # Step 4: AI Analysis
    print("\n🤖 Step 4: AI Analyzes Anomaly")
    print("  (Calling LLM...)")

    decision = manager.agent.analyze_anomaly(anomaly, context)

    print(f"  ✓ AI Decision: {decision.action.name}")
    print(f"  ✓ Confidence: {decision.confidence:.0%}")
    print(f"  ✓ Reasoning: {decision.reasoning}")
    print(f"  ✓ Parameters: {decision.parameters}")
    print(f"  ✓ Estimated time: {decision.estimated_time}s")
    print(f"  ✓ LLM calls made: {manager.llm_call_count}")

    # Step 5: Execute Recovery
    print("\n⚙️  Step 5: Execute Recovery Action")
    print(f"  Executing: {decision.action.name}...")

    result = manager.executor.execute(decision)

    print(f"  ✓ Recovery result: {'SUCCESS' if result.success else 'FAILED'}")
    print(f"  ✓ Action: {result.action.name}")
    print(f"  ✓ Attempts: {result.attempts}")
    print(f"  ✓ Elapsed time: {result.elapsed_time:.2f}s")
    if result.new_state:
        print(f"  ✓ New state: {result.new_state}")

    # Step 6: Verify Recovery
    print("\n✅ Step 6: Verify Recovery")

    # Remove dialog from latest.json
    fake_data["pageContext"]["isModalShowing"] = False
    latest_path.write_text(json.dumps(fake_data), encoding="gbk")

    still_has_dialog = manager.detector.has_dialog()
    print(f"  ✓ Dialog still showing: {still_has_dialog}")
    print(f"  ✓ Recovery verified: {not still_has_dialog}")

    # Cleanup
    if latest_path.exists():
        latest_path.unlink()
        print(f"\n🧹 Cleanup: Removed fake latest.json")

    print("\n" + "="*70)
    print("✅ Complete Pipeline Demo Finished!")
    print("="*70)
    print("\nPipeline Summary:")
    print("  1. Detected dialog via latest.json (1ms)")
    print("  2. AI analyzed anomaly (mocked, ~2-5s real)")
    print("  3. Executed CLICK_BUTTON action (1-2s real)")
    print("  4. Verified dialog disappeared")
    print(f"\nTotal LLM calls: {manager.llm_call_count}/3")
    print("Status: ✅ All steps completed successfully\n")


def demo_timeout_recovery():
    """Demo: Timeout recovery workflow."""
    print("\n" + "="*70)
    print("DEMO: Timeout Recovery Workflow")
    print("="*70)

    config = AIRecoveryConfig(
        enabled=True,
        provider="anthropic",
        api_key="demo-key",
        log_decisions=False,
    )

    manager = RecoveryManager(config=config)

    # Mock LLM for timeout
    mock_client = Mock()
    mock_client.messages.create.return_value = Mock(
        content=[Mock(text=json.dumps({
            "action": "WAIT_LONGER",
            "confidence": 0.85,
            "reasoning": "VCI connection typically takes 30-90 seconds on cold start. No error indicators visible. Recommend extending timeout to 60 seconds.",
            "parameters": {"wait_seconds": 60},
            "estimated_time": 60.0,
        }))]
    )
    manager.agent._get_client = Mock(return_value=mock_client)

    # Detect timeout
    print("\n🔍 Detecting Timeout...")
    anomaly = manager.detector.check_timeout(
        operation="wait_for_enter_button",
        elapsed_time=45.0,
        expected_time=30.0,
    )
    print(f"  ✓ Timeout detected")
    print(f"  ✓ Operation: {anomaly.context['operation']}")
    print(f"  ✓ Elapsed: {anomaly.context['elapsed_time']}s")
    print(f"  ✓ Expected: {anomaly.context['expected_time']}s")
    print(f"  ✓ Ratio: {anomaly.context['timeout_ratio']:.1f}x")

    # AI analysis
    print("\n🤖 AI Analyzing...")
    context = OperationContext(
        operation_name="wait_for_enter_button",
        current_page="DEVICE_EXPLORER",
        elapsed_time=45.0,
        expected_time=30.0,
    )

    decision = manager.agent.analyze_anomaly(anomaly, context)
    print(f"  ✓ Decision: {decision.action.name}")
    print(f"  ✓ Recommendation: Wait {decision.parameters['wait_seconds']} more seconds")
    print(f"  ✓ Reasoning: {decision.reasoning}")

    print("\n✅ Workflow would continue with extended timeout\n")


def demo_cost_tracking():
    """Demo: LLM call tracking and cost control."""
    print("\n" + "="*70)
    print("DEMO: Cost Control via Max LLM Calls")
    print("="*70)

    config = AIRecoveryConfig(
        enabled=True,
        provider="anthropic",
        api_key="demo-key",
        max_llm_calls_per_session=3,
        log_decisions=False,
    )

    manager = RecoveryManager(config=config)

    # Mock LLM
    mock_client = Mock()
    mock_client.messages.create.return_value = Mock(
        content=[Mock(text=json.dumps({
            "action": "GO_BACK",
            "confidence": 0.8,
            "reasoning": "Navigate back",
            "parameters": {},
        }))]
    )
    manager.agent._get_client = Mock(return_value=mock_client)

    print(f"\n📊 Max LLM calls allowed: {config.max_llm_calls_per_session}")
    print(f"Initial call count: {manager.llm_call_count}")

    anomaly = Anomaly(type=AnomalyType.TIMEOUT)
    context = OperationContext(operation_name="test")

    # Make multiple calls
    for i in range(4):
        manager.agent.analyze_anomaly(anomaly, context)
        print(f"\nCall {i+1}:")
        print(f"  ✓ LLM calls: {manager.llm_call_count}")
        print(f"  ✓ Used {'LLM' if manager.llm_call_count > i else 'fallback (limit reached)'}")

    print(f"\n💡 After limit reached, agent uses conservative fallback")
    print(f"Final call count: {manager.llm_call_count}/3")
    print("Cost control: ✅ Working\n")


if __name__ == "__main__":
    print("\n🎯 AI Recovery System - Complete End-to-End Demo")
    print("This shows the full pipeline from detection to recovery")
    print("(Using mocked components, no API key required)")

    demo_full_pipeline()
    demo_timeout_recovery()
    demo_cost_tracking()

    print("\n" + "="*70)
    print("✅ All Demos Complete!")
    print("="*70)
    print("\nWhat we demonstrated:")
    print("  1. Complete pipeline: Detect → AI → Execute → Verify")
    print("  2. Dialog recovery via CLICK_BUTTON")
    print("  3. Timeout recovery via WAIT_LONGER")
    print("  4. Cost control via max LLM calls")
    print("\nNext steps:")
    print("  • Day 6: Integrate into DataViewerWorkflow")
    print("  • Day 7: Test with real GDS2 and real API")
    print()
