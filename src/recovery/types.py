"""
Data types for AI exception recovery system.

This module defines all core data structures used across the recovery system:
- Anomaly detection results
- AI recovery decisions
- Execution results
- Enumerations for types and actions
"""

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Dict, Any, Optional
import time


class AnomalyType(Enum):
    """Types of anomalies that can be detected."""
    UNEXPECTED_DIALOG = auto()  # Modal dialog or popup appeared
    TIMEOUT = auto()  # Operation took longer than expected
    STATE_MISMATCH = auto()  # Page/UI state doesn't match expectation
    UNKNOWN = auto()  # Unclassified anomaly


class AnomalySeverity(Enum):
    """Severity levels for anomalies."""
    LOW = auto()  # Minor issue, can likely continue
    MEDIUM = auto()  # Significant issue, recovery recommended
    HIGH = auto()  # Critical issue, recovery required
    CRITICAL = auto()  # Fatal issue, may need to abort


class RecoveryAction(Enum):
    """Actions that can be taken to recover from anomalies."""
    CLICK_BUTTON = auto()  # Click a specific button (e.g., OK, Cancel)
    WAIT_LONGER = auto()  # Extend timeout and continue waiting
    GO_BACK = auto()  # Navigate back to previous page
    RETRY_FROM_START = auto()  # Reset to main menu and retry
    DISMISS_AND_NAVIGATE = auto()  # Dismiss dialog + navigate to recovery target page
    ABORT = auto()  # Give up, cannot recover


@dataclass
class Anomaly:
    """
    Represents a detected anomaly in the automation workflow.

    Attributes:
        type: Type of anomaly detected
        severity: Severity level of the anomaly
        context: Additional context about the anomaly (flexible dict)
        timestamp: When the anomaly was detected (Unix timestamp)
    """
    type: AnomalyType
    severity: AnomalySeverity = AnomalySeverity.MEDIUM
    context: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def __str__(self) -> str:
        return f"{self.type.name}({self.severity.name}) at {self.timestamp}"


@dataclass
class RecoveryDecision:
    """
    Represents an AI's decision on how to recover from an anomaly.

    Attributes:
        action: The recovery action to take
        confidence: AI's confidence in this decision (0.0-1.0)
        reasoning: Human-readable explanation of the decision
        parameters: Action-specific parameters (e.g., button_text, wait_seconds)
        estimated_time: Expected time for recovery in seconds
    """
    action: RecoveryAction
    confidence: float
    reasoning: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    estimated_time: float = 30.0

    def __post_init__(self):
        """Validate confidence is in valid range."""
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"Confidence must be between 0.0 and 1.0, got {self.confidence}")

    def __str__(self) -> str:
        return f"{self.action.name}(confidence={self.confidence:.2f}): {self.reasoning}"


@dataclass
class RecoveryResult:
    """
    Result of attempting to execute a recovery action.

    Attributes:
        success: Whether the recovery succeeded
        action: The action that was attempted
        attempts: Number of attempts made
        elapsed_time: Total time taken in seconds
        error: Error message if recovery failed
        new_state: Description of state after recovery
    """
    success: bool
    action: RecoveryAction
    attempts: int = 1
    elapsed_time: float = 0.0
    error: Optional[str] = None
    new_state: Optional[str] = None

    def __str__(self) -> str:
        status = "SUCCESS" if self.success else "FAILED"
        return f"{status}: {self.action.name} after {self.attempts} attempts ({self.elapsed_time:.1f}s)"


@dataclass
class OperationContext:
    """
    Context information for an operation that may fail.

    Used to provide the AI with sufficient context to make informed decisions.

    Attributes:
        operation_name: Name of the operation being performed
        current_page: Current GDS2 page (from detect_current_page())
        visible_buttons: List of visible button texts
        recent_actions: List of recent operations (last 5)
        elapsed_time: Time elapsed for current operation
        expected_time: Expected time for this operation
        additional_info: Any other relevant context
    """
    operation_name: str
    current_page: str = "UNKNOWN"
    visible_buttons: list[str] = field(default_factory=list)
    recent_actions: list[str] = field(default_factory=list)
    elapsed_time: float = 0.0
    expected_time: float = 30.0
    additional_info: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for prompt construction."""
        return {
            "operation": self.operation_name,
            "current_page": self.current_page,
            "visible_buttons": self.visible_buttons,
            "recent_actions": self.recent_actions[-5:],  # Last 5 only
            "elapsed_time": self.elapsed_time,
            "expected_time": self.expected_time,
            **self.additional_info
        }


class WorkflowRecoveryError(Exception):
    """
    Raised after successful AI recovery to signal workflow rerouting.

    Instead of blindly retrying the failed method with the same args,
    this error tells the caller that:
    1. The error dialog was dismissed
    2. GDS2 was navigated to a recovery target page
    3. The workflow should be restarted from that page

    Attributes:
        target_page: GDS2 page name where recovery navigated to
        reasoning: AI's explanation of the error and recovery path
        action: The recovery action that was taken
        original_error: The original exception that triggered recovery
    """

    def __init__(
        self,
        target_page: str,
        reasoning: str,
        action: RecoveryAction,
        original_error: Optional[Exception] = None,
    ):
        self.target_page = target_page
        self.reasoning = reasoning
        self.action = action
        self.original_error = original_error
        super().__init__(
            f"Recovery complete: navigated to {target_page}. {reasoning}"
        )
