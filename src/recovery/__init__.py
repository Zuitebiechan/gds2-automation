"""
AI Exception Recovery System for GDS2 RPA Automation.

This package provides AI-powered exception recovery for the RPA workflow:
- Detects anomalies (unexpected dialogs, timeouts, state mismatches)
- Uses LLM (Claude/GPT) to analyze and decide recovery actions
- Executes recovery and verifies success

Core components:
    - AIRecoveryConfig: Configuration loaded from environment variables
    - RecoveryManager: Main orchestrator (use this in workflows)
    - AIRecoveryAgent: LLM-powered decision maker
    - AnomalyDetector: Fast rule-based anomaly detection
    - RecoveryExecutor: Executes recovery actions
    - Anomaly: Detected exception with context
    - RecoveryDecision: AI's recommended action
    - RecoveryResult: Outcome of recovery attempt

Usage:
    from src.recovery import RecoveryManager

    # Initialize (auto-loads config from environment)
    manager = RecoveryManager()

    # Check for dialogs during workflow
    if anomaly := manager.check_for_dialogs():
        result = manager.handle_anomaly(anomaly, context)
        if not result.success:
            raise RuntimeError(f"Recovery failed: {result.error}")
"""

from .config import AIRecoveryConfig
from .types import (
    Anomaly,
    AnomalyType,
    AnomalySeverity,
    RecoveryAction,
    RecoveryDecision,
    RecoveryResult,
    OperationContext,
    WorkflowRecoveryError,
)
from .ai_recovery_agent import AIRecoveryAgent
from .anomaly_detector import AnomalyDetector
from .recovery_executor import RecoveryExecutor
from .recovery_manager import RecoveryManager
from .decorators import with_recovery

__all__ = [
    # Configuration
    "AIRecoveryConfig",
    # Main coordinator (use this!)
    "RecoveryManager",
    # Decorators
    "with_recovery",
    # Components
    "AIRecoveryAgent",
    "AnomalyDetector",
    "RecoveryExecutor",
    # Data types
    "Anomaly",
    "AnomalyType",
    "AnomalySeverity",
    "RecoveryAction",
    "RecoveryDecision",
    "RecoveryResult",
    "OperationContext",
    "WorkflowRecoveryError",
]

__version__ = "0.4.0"  # Day 8: Smart recovery with DISMISS_AND_NAVIGATE
