"""
Prompt templates for AI exception recovery.

Contains system prompts and anomaly-specific prompt templates for LLM analysis.
All prompts are designed to produce structured JSON responses for deterministic parsing.
"""

from typing import Dict, Any
import json


# ==================== System Prompt ====================

SYSTEM_PROMPT = """You are an automation recovery assistant for GM GDS2 diagnostic software.

**Your Role:**
Analyze automation failures and recommend ONE recovery action with high confidence.

**GDS2 Context:**
- Professional vehicle diagnostic software for GM vehicles
- Connection flow: PC → VCI device (USB) → Vehicle OBD-II port → ECU
- Common delays:
  - VCI connection: 10-30 seconds (normal), 30-90 seconds (cold start or slow ECU)
  - Module loading: 5-15 seconds per module
  - Data display: 2-5 seconds
- UI: JavaFX desktop application + Win32 native dialogs (Device Explorer)
- User flow: Main Menu → Device Explorer → Vehicle Selection → Diagnostics → Module → Data

**Recovery Actions:**
1. **CLICK_BUTTON**: Click a specific button (e.g., "OK", "Retry", "Cancel")
   - Use for: Error dialogs, warnings, confirmations
   - Parameters: {"button_text": "OK"}

2. **WAIT_LONGER**: Extend timeout and continue waiting
   - Use for: Normal delays (VCI connection, ECU communication)
   - Parameters: {"wait_seconds": 60}

3. **GO_BACK**: Navigate back to previous page
   - Use for: Stuck state, unexpected page, network error
   - Parameters: {}

4. **RETRY_FROM_START**: Reset to Main Menu and retry entire operation
   - Use for: Corrupted state, device disconnected
   - Parameters: {}

5. **ABORT**: Give up, cannot recover
   - Use for: Fatal errors, hardware failure, unrecoverable state
   - Parameters: {}

**Decision Criteria:**
- Prioritize WAIT_LONGER for first timeout (VCI/ECU delays are common)
- Use CLICK_BUTTON only if dialog buttons are visible
- Use GO_BACK if stuck but state is recoverable
- Use ABORT only for unrecoverable errors (hardware disconnected, fatal exceptions)

**Output Format (JSON only, no markdown):**
{
  "action": "WAIT_LONGER",
  "confidence": 0.85,
  "reasoning": "VCI connection typically takes 60s on cold start. No error indicators visible.",
  "parameters": {"wait_seconds": 60},
  "estimated_time": 60.0
}

**Rules:**
- Output ONLY valid JSON, no markdown formatting, no code blocks
- confidence: 0.0-1.0 (be realistic, not overconfident)
- reasoning: 1-2 sentences explaining why this action is best
- estimated_time: Expected seconds to complete recovery (realistic estimate)
"""


# ==================== Dialog Anomaly Prompt ====================

DIALOG_ANOMALY_TEMPLATE = """## Anomaly: Unexpected Dialog

**Dialog Information:**
- Title: "{dialog_title}"
- Buttons: {dialog_buttons}
- Is Modal: {is_modal}

**Current Context:**
- Page: {current_page}
- Operation: {operation}
- Elapsed Time: {elapsed_time:.1f}s

**Recent Actions:**
{recent_actions_formatted}

**Your Task:**
This dialog was NOT expected by the automation script. Determine:
1. Is this an ERROR dialog (requires CLICK_BUTTON or ABORT)?
2. Is this a WARNING dialog (click OK/Retry to continue)?
3. Is this an INFO dialog (safe to dismiss)?

Analyze the dialog title and buttons to make the best decision.

Respond with JSON only (no markdown).
"""


# ==================== Timeout Anomaly Prompt ====================

TIMEOUT_ANOMALY_TEMPLATE = """## Anomaly: Operation Timeout

**Operation Details:**
- Operation: {operation}
- Expected time: {expected_time:.1f}s
- Elapsed time: {elapsed_time:.1f}s
- Timeout ratio: {timeout_ratio:.1f}x

**Current State:**
- Page: {current_page}
- Visible buttons: {visible_buttons}
- Recent Actions: {recent_actions_formatted}

**Additional Context:**
{additional_context}

**Your Task:**
Determine if this timeout is:
1. **Normal delay** (VCI connection, ECU cold start, slow network)
   → Recommend WAIT_LONGER with appropriate extended timeout

2. **Stuck state** (UI frozen, network error, device disconnected)
   → Recommend GO_BACK or RETRY_FROM_START

3. **Fatal error** (hardware failure, unrecoverable state)
   → Recommend ABORT

**Key Indicators:**
- First timeout (ratio < 2.0) → likely normal delay, try WAIT_LONGER
- Multiple timeouts (ratio > 2.0) → likely stuck, try GO_BACK
- Device disconnected or fatal error → ABORT

Respond with JSON only (no markdown).
"""


# ==================== State Mismatch Anomaly Prompt ====================

STATE_MISMATCH_ANOMALY_TEMPLATE = """## Anomaly: State Mismatch

**State Information:**
- Expected page: {expected_page}
- Actual page: {actual_page}
- Operation: {operation}

**Current Context:**
- Visible buttons: {visible_buttons}
- Recent Actions: {recent_actions_formatted}

**Your Task:**
The automation expected to be on "{expected_page}" but is actually on "{actual_page}".

Determine the best recovery action:
1. **GO_BACK**: If actual page is recoverable (can navigate back)
2. **RETRY_FROM_START**: If state is corrupted (wrong page entirely)
3. **ABORT**: If in unknown/unrecoverable state

Respond with JSON only (no markdown).
"""


# ==================== Prompt Builder Functions ====================

def build_dialog_prompt(anomaly: "Anomaly", context: Dict[str, Any]) -> str:
    """
    Build prompt for unexpected dialog anomaly.

    Args:
        anomaly: Anomaly object with dialog context
        context: OperationContext.to_dict() output

    Returns:
        Formatted prompt string
    """
    dialog_title = anomaly.context.get("modal_title", "Unknown")
    dialog_buttons = anomaly.context.get("modal_buttons", [])
    is_modal = anomaly.context.get("isModalShowing", False)

    recent_actions = context.get("recent_actions", [])
    recent_actions_formatted = "\n".join(f"  {i+1}. {action}" for i, action in enumerate(recent_actions)) or "  (none)"

    return DIALOG_ANOMALY_TEMPLATE.format(
        dialog_title=dialog_title,
        dialog_buttons=json.dumps(dialog_buttons),
        is_modal=is_modal,
        current_page=context.get("current_page", "UNKNOWN"),
        operation=context.get("operation", "unknown"),
        elapsed_time=context.get("elapsed_time", 0.0),
        recent_actions_formatted=recent_actions_formatted,
    )


def build_timeout_prompt(anomaly: "Anomaly", context: Dict[str, Any]) -> str:
    """
    Build prompt for timeout anomaly.

    Args:
        anomaly: Anomaly object with timeout context
        context: OperationContext.to_dict() output

    Returns:
        Formatted prompt string
    """
    operation = context.get("operation", "unknown")
    expected_time = context.get("expected_time", 30.0)
    elapsed_time = context.get("elapsed_time", 0.0)
    timeout_ratio = elapsed_time / expected_time if expected_time > 0 else 1.0

    visible_buttons = context.get("visible_buttons", [])
    visible_buttons_str = json.dumps(visible_buttons) if visible_buttons else "[]"

    recent_actions = context.get("recent_actions", [])
    recent_actions_formatted = "\n".join(f"  {i+1}. {action}" for i, action in enumerate(recent_actions)) or "  (none)"

    # Extract additional context
    additional_info = []
    for key, value in context.items():
        if key not in ("operation", "current_page", "visible_buttons", "recent_actions", "elapsed_time", "expected_time"):
            additional_info.append(f"- {key}: {value}")
    additional_context = "\n".join(additional_info) if additional_info else "  (none)"

    return TIMEOUT_ANOMALY_TEMPLATE.format(
        operation=operation,
        expected_time=expected_time,
        elapsed_time=elapsed_time,
        timeout_ratio=timeout_ratio,
        current_page=context.get("current_page", "UNKNOWN"),
        visible_buttons=visible_buttons_str,
        recent_actions_formatted=recent_actions_formatted,
        additional_context=additional_context,
    )


def build_state_mismatch_prompt(anomaly: "Anomaly", context: Dict[str, Any]) -> str:
    """
    Build prompt for state mismatch anomaly.

    Args:
        anomaly: Anomaly object with state mismatch context
        context: OperationContext.to_dict() output

    Returns:
        Formatted prompt string
    """
    expected_page = anomaly.context.get("expected_page", "UNKNOWN")
    actual_page = anomaly.context.get("actual_page", "UNKNOWN")

    visible_buttons = context.get("visible_buttons", [])
    visible_buttons_str = json.dumps(visible_buttons) if visible_buttons else "[]"

    recent_actions = context.get("recent_actions", [])
    recent_actions_formatted = "\n".join(f"  {i+1}. {action}" for i, action in enumerate(recent_actions)) or "  (none)"

    return STATE_MISMATCH_ANOMALY_TEMPLATE.format(
        expected_page=expected_page,
        actual_page=actual_page,
        operation=context.get("operation", "unknown"),
        visible_buttons=visible_buttons_str,
        recent_actions_formatted=recent_actions_formatted,
    )
