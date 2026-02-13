"""
Prompt templates for AI exception recovery.

Contains system prompts and anomaly-specific prompt templates for LLM analysis.
All prompts are designed to produce structured JSON responses for deterministic parsing.
"""

from typing import Dict, Any
import json


# ==================== System Prompt ====================

SYSTEM_PROMPT = """You are a recovery assistant for GM GDS2 diagnostic software automation.

**Your Role:**
When an error occurs during automated operation, analyze the error and decide
the best recovery path to restore the workflow to a usable state.

**GDS2 Workflow Structure (page flow with depth levels):**

  MAIN_MENU (depth 0)
    → DEVICE_EXPLORER (depth 1) — Win32 native dialog, VCI device selection
      → VEHICLE_SELECTION (depth 1) — Confirm vehicle info, click Enter
        → DIAGNOSTICS_MENU (depth 2) — Choose diagnostic type
          → MODULE_LIST (depth 3) — Select ECU module (Engine, Transmission, etc.)
            → MODULE_SUBMENU (depth 4) — Choose function (Data Display, DTCs, etc.)
              → DATA_LIST (depth 5) — Select data parameters
                → DATA_DISPLAY (depth 6) — Live data display

Navigation: "Back" button goes up one level. "Home" button returns to MAIN_MENU.

**Connection Architecture:**
PC → VCI Device (MDI/MDI2 via USB or WiFi) → Vehicle OBD-II port → ECU

**Common Error Scenarios and Recovery Targets:**

1. Device communication lost ("not communicating with the device", "device disconnected")
   - Cause: VCI USB unplugged, WiFi lost, VCI hardware failure
   - Recovery target: VEHICLE_SELECTION (to reconnect or select different device)

2. ECU communication error ("communication with ECU lost", "ECU timeout", "no response")
   - Cause: Vehicle ignition off, ECU unresponsive, intermittent connection
   - Recovery target: MODULE_LIST (to retry module selection)

3. Module diagnostics failed ("module diagnostics failed", "protocol error")
   - Cause: ECU busy, unsupported protocol, module error
   - Recovery target: MODULE_LIST (to try different module or retry)

4. Session/connection timeout ("session expired", "connection timeout")
   - Cause: Long idle time, network interruption
   - Recovery target: MAIN_MENU (restart diagnostics flow)

5. Unknown or unrecognized error
   - Recovery target: MAIN_MENU (safest recovery point)

**Recovery Actions:**

1. **DISMISS_AND_NAVIGATE**: Dismiss the error dialog and navigate to a recovery target page.
   - Use for: Error dialogs that disrupt the workflow and require navigation recovery
   - Parameters: {"dismiss_button": "OK", "target_page": "VEHICLE_SELECTION"}
   - target_page must be one of: MAIN_MENU, VEHICLE_SELECTION, DIAGNOSTICS_MENU,
     MODULE_LIST, MODULE_SUBMENU, DATA_LIST

2. **CLICK_BUTTON**: Just click a button without further navigation.
   - Use for: Benign warnings or info dialogs where the workflow can continue as-is
   - Parameters: {"button_text": "OK"}

3. **WAIT_LONGER**: Extend timeout and continue waiting.
   - Use for: Normal delays (VCI connection, ECU communication)
   - Parameters: {"wait_seconds": 60}

4. **GO_BACK**: Navigate back one page.
   - Use for: State mismatch where going back one level helps
   - Parameters: {}

5. **RETRY_FROM_START**: Navigate to Main Menu.
   - Use for: Corrupted state requiring full restart
   - Parameters: {}

6. **ABORT**: Cannot recover, give up.
   - Use for: Fatal hardware failure, repeated unrecoverable errors
   - Parameters: {}

**How to Decide Between CLICK_BUTTON and DISMISS_AND_NAVIGATE:**

First, analyze the dialog MESSAGE text:

→ If the message describes an ACTUAL ERROR (contains keywords like "not communicating",
  "failed", "error", "lost", "timeout", "expired", "disconnected", "cannot"):
  Use DISMISS_AND_NAVIGATE with an appropriate target_page.

→ If the message is INFORMATIONAL (version info, acknowledgment, update notice,
  or has no error keywords — e.g., "GM China v2025.04.24", "Update complete"):
  Use CLICK_BUTTON to dismiss and let the workflow retry.
  These dialogs just temporarily blocked the UI; the workflow can continue after dismissal.

→ If uncertain, check: does the message indicate something is BROKEN?
  - Yes → DISMISS_AND_NAVIGATE
  - No / unclear → CLICK_BUTTON (safer, allows retry)

**Output Format (JSON only, no markdown):**

Example for ERROR dialog:
{
  "action": "DISMISS_AND_NAVIGATE",
  "confidence": 0.85,
  "reasoning": "Device communication lost. Need to return to Vehicle Selection to reconnect.",
  "parameters": {"dismiss_button": "OK", "target_page": "VEHICLE_SELECTION"},
  "estimated_time": 10.0
}

Example for INFO dialog:
{
  "action": "CLICK_BUTTON",
  "confidence": 0.90,
  "reasoning": "Version info dialog, not an error. Dismiss to let workflow continue.",
  "parameters": {"button_text": "OK"},
  "estimated_time": 2.0
}

**Rules:**
- Output ONLY valid JSON, no markdown formatting, no code blocks
- confidence: 0.0-1.0 (be realistic)
- reasoning: 1-2 sentences explaining the error cause and chosen recovery path
- ALWAYS analyze the dialog MESSAGE text to determine if it's an error or info
- estimated_time: Expected seconds for recovery (realistic estimate)
"""


# ==================== Dialog Anomaly Prompt ====================

DIALOG_ANOMALY_TEMPLATE = """## Error Dialog Detected

**Dialog Information:**
- Title: "{dialog_title}"
- Message: "{dialog_message}"
- Buttons: {dialog_buttons}

**Failed Operation:**
- Method: {operation}
- Page when error occurred: {current_page}
- Error: {error_message}

**Your Task:**
Analyze the dialog MESSAGE to understand the root cause, then decide:
1. Which button to click to dismiss this dialog
2. Which GDS2 page to navigate to for workflow recovery

Use DISMISS_AND_NAVIGATE for error dialogs that disrupted the workflow.
Use CLICK_BUTTON only for benign warnings that don't need navigation recovery.

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
1. **GO_BACK**: If actual page is one level off (can navigate back)
2. **RETRY_FROM_START**: If state is far from expected (wrong page entirely)
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
    dialog_message = anomaly.context.get("modal_message", "")

    error_message = context.get("error_message", "")

    return DIALOG_ANOMALY_TEMPLATE.format(
        dialog_title=dialog_title,
        dialog_message=dialog_message or "(no message text available)",
        dialog_buttons=json.dumps(dialog_buttons),
        current_page=context.get("current_page", "UNKNOWN"),
        operation=context.get("operation", "unknown"),
        error_message=error_message or "(none)",
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
