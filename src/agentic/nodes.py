"""
Node implementations for LangGraph state machine.

Each node represents a step in the navigation workflow:
- deterministic_node: Executes hardcoded, known-safe navigation steps
- agent_node: AI-driven page classification and decision for unknown pages
- human_node: HITL pause for user choices (module/data selection)
"""

import logging
import time
from typing import Any, Literal, Optional, cast

from langchain_core.messages import SystemMessage, HumanMessage

from .state import NavigationState
from .tools import (
    click_button, select_list_item, get_current_snapshot,
    ALL_TOOLS,
)
from .llm_factory import create_llm
logger = logging.getLogger(__name__)

MAX_AGENT_RETRIES = 2


def _invoke_tool(tool: Any, args: dict) -> dict:
    """Invoke a LangChain-style tool while keeping call sites concise."""
    return cast(dict, tool.invoke(args))


# ---------------------------------------------------------------------------
# Deterministic navigation rules
# ---------------------------------------------------------------------------
# Keys use GDS2Page.value (lowercase) for the page, and a step description
# for the intent. The deterministic node walks a fixed sequence toward
# "data_display" without needing AI.
#
# Format: (current_page_value, step_label) -> action dict
# step_label is matched from DETERMINISTIC_SEQUENCE order.
# ---------------------------------------------------------------------------

DETERMINISTIC_SEQUENCE = [
    # (from_page, action_type, target, expected_next_page)
    ("main_menu",        "click_button", "Diagnostics",        "diagnostics_menu"),
    # vehicle_selection: Enter clicks through to diagnostics_menu.
    # Device Explorer (Win32 dialog) is handled by start_diagnostics() before
    # the LangGraph flow begins, so by the time we're here the device is selected.
    ("vehicle_selection", "click_button", "Enter",              "diagnostics_menu"),
    ("diagnostics_menu", "select_list_item", "Module Diagnostics", "module_list"),
    # module_list -> HITL (user picks module)
    # module_submenu -> deterministic: select "Data Display" from list
    ("module_submenu",   "select_list_item", "Data Display",       "data_list"),
    # data_list -> HITL (user picks data category)
    # sub_data_list -> HITL (user picks sub-data)
    # After user picks data -> we land on data_display (goal)
]

# Build a lookup dict: page -> (action_type, target, expected_next)
DETERMINISTIC_ROUTES = {
    step[0]: {"action": step[1], "target": step[2], "expected_next": step[3]}
    for step in DETERMINISTIC_SEQUENCE
}

# Pages where execution must pause for user input.
# Uses GDS2Page.value (lowercase).
USER_DECISION_PAGES = [
    "module_list",     # User selects which module to diagnose
    "data_list",       # User selects which data category
    "sub_data_list",   # User selects sub-data (if present)
]


def _wait_for_enter_enabled(controller, timeout: float = 30.0, poll_interval: float = 1.0) -> bool:
    """Poll Java Agent until the Enter button is enabled or timeout.

    GDS2 disables the Enter button on vehicle_selection while it
    initialises the vehicle connection.  This mirrors the old
    DataViewerWorkflow._wait_for_button_enabled() behaviour.

    Returns:
        True if Enter became enabled within timeout, False otherwise.
    """
    import time as _time
    start = _time.time()
    while _time.time() - start < timeout:
        try:
            buttons = controller.nav.get_buttons()
            for btn in buttons:
                if btn.get('text') == 'Enter':
                    if btn.get('enabled', True):  # default True for older Agents
                        logger.info(
                            f"Enter button enabled after "
                            f"{_time.time() - start:.1f}s"
                        )
                        return True
                    else:
                        logger.debug("Enter button found but disabled, waiting...")
                        break  # found button, but disabled — keep polling
        except Exception as e:
            logger.debug(f"Button poll error (non-fatal): {e}")
        _time.sleep(poll_interval)
    logger.warning(f"Enter button did not become enabled within {timeout}s")
    return False


def _handle_vehicle_selection(state: NavigationState) -> dict:
    """Handle vehicle_selection deterministically via controller.click_enter().

    Waits for the Enter button to become enabled (up to 30s) before
    clicking, since GDS2 disables it during vehicle connection init.

    Uses the full click_enter() method which includes:
    - Retry if still at vehicle_selection after first Enter click
    - Warning dialog dismissal
    - Handling GDS2 auto-skip to module_list (clicks Back to diagnostics_menu)
    """
    from .tools import get_controller, _snapshot_from_controller

    controller = get_controller()
    try:
        # Wait for Enter button to become enabled before clicking.
        # GDS2 disables it while initialising the vehicle connection.
        if not _wait_for_enter_enabled(controller, timeout=30.0):
            logger.warning("Vehicle selection: Enter button never became enabled")
            snapshot = _snapshot_from_controller(controller)
            return {
                "error": "Enter button is disabled (vehicle connection not ready)",
                "next_action": "handle_error",
                "page_snapshot": snapshot,
                "navigation_history": [{
                    "action": "waited for Enter button to become enabled",
                    "from_page": "vehicle_selection",
                    "deterministic": True,
                    "success": False,
                    "error": "Enter button remained disabled after 30s",
                }],
                "step_count": state.get("step_count", 0) + 1,
            }

        result = controller.click_enter()
        snapshot = _snapshot_from_controller(controller)
        new_page = result.page.value

        if result.success:
            logger.info(f"Vehicle selection: Enter succeeded, now on '{new_page}'")
            return {
                "current_page": new_page,
                "page_snapshot": snapshot,
                "navigation_history": [{
                    "action": "clicked 'Enter' (vehicle selection)",
                    "from_page": "vehicle_selection",
                    "to_page": new_page,
                    "deterministic": True,
                    "success": True,
                }],
                "next_action": "continue",
                "error": None,
                "step_count": state.get("step_count", 0) + 1,
            }
        else:
            error_msg = result.error or "Failed to click Enter at vehicle selection"
            logger.warning(f"Vehicle selection: Enter failed: {error_msg}")
            return {
                "error": error_msg,
                "next_action": "handle_error",
                "navigation_history": [{
                    "action": "tried clicking 'Enter' (vehicle selection)",
                    "from_page": "vehicle_selection",
                    "deterministic": True,
                    "success": False,
                    "error": error_msg,
                }],
                "step_count": state.get("step_count", 0) + 1,
            }
    except Exception as e:
        logger.exception(f"Vehicle selection handler error: {e}")
        return {
            "error": str(e),
            "next_action": "handle_error",
            "step_count": state.get("step_count", 0) + 1,
        }
# ---------------------------------------------------------------------------
# Node: Deterministic
# ---------------------------------------------------------------------------

def deterministic_node(state: NavigationState) -> dict:
    """
    Execute hardcoded deterministic navigation.

    Handles known, predictable paths (Main Menu → Diagnostics → Module Diagnostics, etc.)
    If no deterministic route exists for the current page, hands off to agent.
    """
    current_page = state["current_page"]

    logger.info(f"Deterministic node: current_page={current_page}")

    route = DETERMINISTIC_ROUTES.get(current_page)

    if route is None:
        # No deterministic route → hand off to agent
        logger.info("No deterministic route found, handing off to agent")
        return {
            "next_action": "agent",
            "step_count": state.get("step_count", 0) + 1,
        }

    logger.info(f"Found deterministic route: {route}")

    # Special case: vehicle_selection needs controller.click_enter() which has
    # retry logic, warning dialog dismissal, and auto-skip handling.
    if current_page == "vehicle_selection":
        return _handle_vehicle_selection(state)

    # Execute the deterministic action
    if route["action"] == "click_button":
        result = _invoke_tool(click_button, {"button_text": route["target"]})

        if result["success"]:
            snapshot = result["snapshot"]
            new_page = snapshot["page"] if snapshot else "unknown"
            logger.info(f"Deterministic click succeeded, now on '{new_page}'")

            return {
                "current_page": new_page,
                "page_snapshot": snapshot,
                "navigation_history": [{
                    "action": f"clicked '{route['target']}'",
                    "from_page": current_page,
                    "to_page": new_page,
                    "deterministic": True,
                    "success": True,
                }],
                "next_action": "continue",
                "error": None,
                "step_count": state.get("step_count", 0) + 1,
            }
        else:
            error_msg = result.get("error", f"Failed to click '{route['target']}'")
            logger.warning(f"Deterministic click failed: {error_msg}")
            return {
                "error": error_msg,
                "next_action": "handle_error",
                "navigation_history": [{
                    "action": f"tried clicking '{route['target']}'",
                    "from_page": current_page,
                    "deterministic": True,
                    "success": False,
                    "error": error_msg,
                }],
                "step_count": state.get("step_count", 0) + 1,
            }

    elif route["action"] == "select_list_item":
        result = _invoke_tool(select_list_item, {"item_text": route["target"]})

        if result["success"]:
            snapshot = result["snapshot"]
            new_page = snapshot["page"] if snapshot else "unknown"
            logger.info(f"Deterministic select succeeded, now on '{new_page}'")

            return {
                "current_page": new_page,
                "page_snapshot": snapshot,
                "navigation_history": [{
                    "action": f"selected '{route['target']}'",
                    "from_page": current_page,
                    "to_page": new_page,
                    "deterministic": True,
                    "success": True,
                }],
                "next_action": "continue",
                "error": None,
                "step_count": state.get("step_count", 0) + 1,
            }
        else:
            error_msg = result.get("error", f"Failed to select '{route['target']}'")
            logger.warning(f"Deterministic select failed: {error_msg}")
            return {
                "error": error_msg,
                "next_action": "handle_error",
                "navigation_history": [{
                    "action": f"tried selecting '{route['target']}'",
                    "from_page": current_page,
                    "deterministic": True,
                    "success": False,
                    "error": error_msg,
                }],
                "step_count": state.get("step_count", 0) + 1,
            }

    logger.warning(f"Unknown deterministic action type: {route['action']}")
    return {
        "next_action": "agent",
        "step_count": state.get("step_count", 0) + 1,
    }


# ---------------------------------------------------------------------------
# Node: Agent (AI-driven, native tool-calling)
# ---------------------------------------------------------------------------

# System prompt for the agent — concise, no JSON format needed
AGENT_SYSTEM_PROMPT = """You are a GDS2 (General Motors Diagnostic System) Navigation Agent.
Your job is to navigate GDS2's UI to reach a target page.

You have tools to interact with GDS2:
- click_button: Click a button on the current page
- select_list_item: Select an item from a list (partial match, double-click)
- go_back: Go back to the previous page
- go_home: Return to Main Menu
- get_current_snapshot: Read the current page state (buttons, lists, context)
- get_list_items: Get the list items on the current page

You also have signal tools for non-action decisions:
- ask_user: Request human input (for module/data selection pages)
- report_error: Report an error dialog or unexpected state
- mark_done: Signal that the navigation goal has been reached

RULES:
- Call exactly ONE tool per turn.
- If the page has a "Data Display" button or similar, click it.
- If the page looks like a module/data selection list, call ask_user.
- If there is an error dialog or unexpected popup, call report_error.
- If you see buttons like "DTC", "Module Information", etc., this is a module submenu - click "Data Display".
- If the current page IS the target (Data Display), call mark_done.
- If unsure, call ask_user."""


def _build_user_message(
    snapshot: dict,
    state: NavigationState,
    similar_pages: list,
    error_patterns: Optional[list] = None,
    tool_suggestion: Optional[dict] = None,
) -> str:
    """
    Build a concise user message with current page state, RAG context,
    error recovery hints, and tool-call suggestions.
    """
    buttons = snapshot.get("buttons", [])
    lists = snapshot.get("lists", [])
    page_ctx = snapshot.get("context", {})

    parts = [
        f"Goal: {state.get('goal', 'Navigate to Data Display')}",
        f"Current page: {snapshot.get('page', 'unknown')}",
        f"Visible buttons: {buttons}",
        f"List items (first 10): {lists[:10]}",
    ]

    if page_ctx:
        parts.append(f"Page context: {page_ctx}")

    last_action = state["navigation_history"][-1] if state.get("navigation_history") else None
    if last_action:
        parts.append(f"Last action: {last_action}")

    user_selections = state.get("user_selections", {})
    if user_selections:
        parts.append(f"User selections: {user_selections}")

    # RAG: Similar pages with confidence
    if similar_pages:
        parts.append("")
        parts.append("Known similar pages from knowledge base:")
        for i, page in enumerate(similar_pages[:3], 1):
            page_type = page.get("page_type", "unknown")
            desc = page.get("description", "No description")
            det_action = page.get("deterministic_action", "")
            confident = page.get("is_confident", False)
            distance = page.get("_distance", None)
            conf_tag = "HIGH confidence" if confident else "low confidence"
            line = f"  {i}. [{conf_tag}] {page_type}: {desc}"
            if det_action:
                line += f" -> Recommended action: {det_action}"
            if distance is not None:
                line += f" (distance: {distance:.3f})"
            parts.append(line)

    # RAG: Tool-call suggestion (direct recommendation)
    if tool_suggestion:
        tool_name = tool_suggestion.get("tool_name", "")
        tool_args = tool_suggestion.get("args", {})
        source = tool_suggestion.get("source", "unknown")
        parts.append("")
        parts.append(
            f"STRONG RECOMMENDATION from knowledge base ({source}): "
            f"Call {tool_name} with {tool_args}"
        )

    # RAG: Error patterns (when in error recovery)
    if error_patterns:
        parts.append("")
        parts.append("Known error patterns from knowledge base:")
        for i, ep in enumerate(error_patterns[:2], 1):
            error_type = ep.get("error_type", "unknown")
            action = ep.get("recommended_action", "")
            detail = ep.get("action_detail", "")
            parts.append(f"  {i}. {error_type}: {detail} (action: {action})")

    # Error context
    error = state.get("error")
    if error:
        parts.append("")
        parts.append(f"CURRENT ERROR: {error}")
        parts.append("You are in error recovery mode. Use the error patterns above to decide your action.")

    return "\n".join(parts)


def _execute_tool_call(tool_call: dict, current_page: str) -> dict:
    """
    Execute a single tool call and return state updates.

    Maps tool name + args to the actual tool invocation, then converts
    the tool result into NavigationState updates.
    """
    tool_name = tool_call["name"]
    tool_args = tool_call.get("args", {})

    logger.info(f"Agent: executing tool '{tool_name}' with args {tool_args}")

    # --- Signal tools (non-action) ---
    if tool_name == "ask_user":
        return {
            "next_action": "ask_user",
            "agent_reasoning": tool_args.get("prompt", "Please make a selection"),
            "navigation_history": [{
                "action": "agent_requested_user_input",
                "prompt": tool_args.get("prompt", ""),
                "options": tool_args.get("options", ""),
                "success": True,
            }],
        }

    if tool_name == "report_error":
        error_type = tool_args.get("error_type", "unknown")
        return {
            "error": f"Agent detected error: {error_type} - {tool_args.get('description', '')}",
            "next_action": "handle_error",
            "agent_reasoning": tool_args.get("description", ""),
            "navigation_history": [{
                "action": "agent_detected_error",
                "error_type": error_type,
                "recommended": tool_args.get("recommended_action", "go_back"),
                "success": True,
            }],
        }

    if tool_name == "mark_done":
        return {
            "next_action": "done",
            "agent_reasoning": tool_args.get("reasoning", "Goal reached"),
            "navigation_history": [{
                "action": "agent_determined_goal_reached",
                "reasoning": tool_args.get("reasoning", ""),
                "success": True,
            }],
        }

    # --- Observation tools ---
    if tool_name == "get_current_snapshot":
        result = _invoke_tool(get_current_snapshot, {})
        new_page = result.get("page", "unknown")
        return {
            "current_page": new_page,
            "page_snapshot": result,
            "next_action": "agent",  # Agent should decide again with fresh data
            "navigation_history": [{
                "action": "agent refreshed page snapshot",
                "success": True,
            }],
        }

    if tool_name == "get_list_items":
        from .tools import get_list_items
        result = _invoke_tool(get_list_items, {})
        return {
            "next_action": "agent",  # Agent should decide again with list data
            "navigation_history": [{
                "action": "agent read list items",
                "items_count": result.get("count", 0),
                "success": True,
            }],
        }

    # --- Action tools ---
    if tool_name == "click_button":
        result = _invoke_tool(click_button, {"button_text": tool_args.get("button_text", "")})
        if result["success"]:
            new_snapshot = result["snapshot"]
            new_page = new_snapshot["page"] if new_snapshot else "unknown"
            return {
                "current_page": new_page,
                "page_snapshot": new_snapshot,
                "next_action": "continue",
                "error": None,
                "navigation_history": [{
                    "action": f"agent clicked '{tool_args.get('button_text', '')}'",
                    "from_page": current_page,
                    "to_page": new_page,
                    "deterministic": False,
                    "success": True,
                }],
            }
        else:
            error_msg = result.get("error", "Click failed")
            return {
                "error": error_msg,
                "next_action": "handle_error",
                "navigation_history": [{
                    "action": f"agent tried clicking '{tool_args.get('button_text', '')}'",
                    "from_page": current_page,
                    "deterministic": False,
                    "success": False,
                    "error": error_msg,
                }],
            }

    if tool_name == "select_list_item":
        result = _invoke_tool(select_list_item, {"item_text": tool_args.get("item_text", "")})
        if result["success"]:
            new_snapshot = result["snapshot"]
            new_page = new_snapshot["page"] if new_snapshot else "unknown"
            return {
                "current_page": new_page,
                "page_snapshot": new_snapshot,
                "next_action": "continue",
                "error": None,
                "navigation_history": [{
                    "action": f"agent selected '{tool_args.get('item_text', '')}'",
                    "from_page": current_page,
                    "to_page": new_page,
                    "deterministic": False,
                    "success": True,
                }],
            }
        else:
            error_msg = result.get("error", "Selection failed")
            return {
                "current_page": current_page,
                "error": error_msg,
                "next_action": "handle_error",
                "navigation_history": [{
                    "action": f"agent tried selecting '{tool_args.get('item_text', '')}'",
                    "from_page": current_page,
                    "deterministic": False,
                    "success": False,
                    "error": error_msg,
                }],
            }

    if tool_name == "go_back":
        from .tools import go_back
        result = _invoke_tool(go_back, {})
        new_snapshot = result.get("snapshot")
        new_page = new_snapshot["page"] if new_snapshot else "unknown"
        return {
            "current_page": new_page,
            "page_snapshot": new_snapshot,
            "next_action": "continue",
            "error": None,
            "navigation_history": [{
                "action": "agent went back",
                "from_page": current_page,
                "to_page": new_page,
                "success": result.get("success", False),
            }],
        }

    if tool_name == "go_home":
        from .tools import go_home
        result = _invoke_tool(go_home, {})
        new_snapshot = result.get("snapshot")
        new_page = new_snapshot["page"] if new_snapshot else "unknown"
        return {
            "current_page": new_page,
            "page_snapshot": new_snapshot,
            "next_action": "continue",
            "error": None,
            "navigation_history": [{
                "action": "agent went home",
                "from_page": current_page,
                "to_page": new_page,
                "success": result.get("success", False),
            }],
        }

    # Unknown tool (shouldn't happen with bind_tools)
    logger.warning(f"Agent called unknown tool: {tool_name}")
    return {
        "next_action": "ask_user",
        "navigation_history": [{
            "action": f"agent_unknown_tool_{tool_name}",
            "success": False,
        }],
    }


def _try_recovery(state: NavigationState, error: str) -> Optional[dict]:
    """Attempt structured recovery using the anomaly detection pipeline."""
    try:
        from src.recovery.anomaly_detector import AnomalyDetector
        from src.recovery.recovery_manager import RecoveryManager
        from src.recovery.types import OperationContext

        detector = AnomalyDetector()
        anomaly = detector.check_unexpected_dialog()

        if anomaly is None:
            anomaly = detector.check_state_mismatch(
                expected_page="data_display",
                actual_page=state.get("current_page", "unknown"),
            )

        if anomaly is None:
            return None

        context = OperationContext(
            operation_name="agent_navigation",
            current_page=state.get("current_page", "unknown"),
        )

        try:
            recovery_manager = RecoveryManager()
        except Exception as e:
            logger.warning(f"RecoveryManager unavailable, skipping structured recovery: {e}")
            return None

        result = recovery_manager.handle_anomaly(anomaly, context)
        if result.success:
            return {
                "error": None,
                "next_action": "continue",
                "navigation_history": [{
                    "action": f"recovery: {result.action.name}",
                    "success": True,
                    "new_state": result.new_state,
                }],
            }

        return None
    except Exception as e:
        logger.warning(f"Structured recovery attempt failed: {e}")
        return None


def agent_node(state: NavigationState) -> dict:
    """
    AI agent analyzes the current page and decides what to do.

    Uses native tool-calling (bind_tools) instead of manual JSON prompting.
    The LLM sees all available tools and calls exactly one per turn.

    Flow:
    1. Get fresh snapshot of the current page
    2. Query knowledge base (similar pages, error patterns, tool suggestion)
    3. Call LLM with tools bound -- LLM returns a tool_call
    4. Execute the tool call and return state updates
    5. If successful, record example for auto-learning
    """
    logger.info("Agent node: analyzing page")

    # 1. Get fresh snapshot
    try:
        snapshot = _invoke_tool(get_current_snapshot, {})
    except Exception as e:
        logger.exception(f"Agent: failed to get snapshot: {e}")
        return {
            "error": f"Cannot read GDS2 state: {e}",
            "next_action": "handle_error",
            "step_count": state.get("step_count", 0) + 1,
        }

    current_page = snapshot.get("page", "unknown")
    logger.info(f"Agent: current page from snapshot = '{current_page}'")

    # Base state updates (always set fresh snapshot)
    state_updates: dict = {
        "current_page": current_page,
        "page_snapshot": snapshot,
        "step_count": state.get("step_count", 0) + 1,
    }

    # 2. Query knowledge base (RAG)
    similar_pages: list = []
    error_patterns: Optional[list] = None
    tool_suggestion: Optional[dict] = None

    try:
        from .knowledge_base import (
            query_similar_pages,
            query_error_patterns,
            get_tool_suggestion,
            get_knowledge_base,
        )

        # Always query similar pages
        similar_pages = query_similar_pages(snapshot, top_k=3)

        # Query error patterns if we're in error recovery
        if state.get("error") or state.get("next_action") == "handle_error":
            error_text = state.get("error") or "unknown error"
            error_patterns = query_error_patterns(error_text, top_k=2)
            logger.info(f"Agent: found {len(error_patterns)} error patterns for recovery")

        # Get tool suggestion from RAG
        tool_suggestion = get_tool_suggestion(snapshot)
        if tool_suggestion:
            logger.info(
                f"Agent: RAG suggests {tool_suggestion['tool_name']}"
                f"({tool_suggestion.get('args', {})}) from {tool_suggestion.get('source')}"
            )

    except Exception as e:
        logger.warning(f"Agent: KB query failed (non-fatal): {e}")

    # 3. Call LLM with bound tools (with retry for rate limits)
    llm_max_retries = 3
    llm_retry_delay = 5.0  # initial backoff in seconds
    response = None

    for llm_attempt in range(1, llm_max_retries + 1):
        try:
            llm = create_llm()
            llm_with_tools = llm.bind_tools(ALL_TOOLS)

            messages = [
                SystemMessage(content=AGENT_SYSTEM_PROMPT),
                HumanMessage(content=_build_user_message(
                    snapshot, state, similar_pages,
                    error_patterns=error_patterns,
                    tool_suggestion=tool_suggestion,
                )),
            ]

            response = llm_with_tools.invoke(messages)
            logger.info(
                f"Agent: LLM response received "
                f"(tool_calls={len(response.tool_calls) if response.tool_calls else 0})"
            )
            break  # success

        except Exception as e:
            error_str = str(e)
            is_rate_limit = (
                "429" in error_str
                or "rate" in error_str.lower()
                or "too many" in error_str.lower()
                or "quota" in error_str.lower()
            )

            if is_rate_limit and llm_attempt < llm_max_retries:
                wait = llm_retry_delay * (2 ** (llm_attempt - 1))
                logger.warning(
                    f"Agent: LLM rate limited (attempt {llm_attempt}/{llm_max_retries}), "
                    f"retrying in {wait:.0f}s..."
                )
                time.sleep(wait)
                continue

            # Non-retriable error or exhausted retries
            logger.exception(f"Agent: LLM call failed: {e}")
            return {
                **state_updates,
                "error": f"LLM error: {e}",
                "next_action": "ask_user",
                "agent_confidence": 0.0,
                "agent_reasoning": f"LLM call failed, falling back to user: {e}",
                "navigation_history": [{
                    "action": "agent_analysis_failed",
                    "error": error_str,
                    "success": False,
                }],
            }

    # 4. Process tool calls
    if response.tool_calls:
        # Take only the first tool call (one action per turn)
        tool_call = response.tool_calls[0]
        logger.info(f"Agent: tool_call = {tool_call['name']}({tool_call.get('args', {})})")

        try:
            tool_result = _execute_tool_call(tool_call, current_page)

            # #4: Auto-learning -- record successful actions
            if tool_result.get("next_action") == "continue":
                tool_result["retry_count"] = 0
                _record_learned_example(
                    snapshot, current_page,
                    tool_call["name"], tool_call.get("args", {}),
                    success=True,
                )

            return {
                **state_updates,
                "agent_confidence": 1.0,
                "agent_reasoning": f"Called {tool_call['name']}({tool_call.get('args', {})})",
                **tool_result,
            }
        except Exception as e:
            retry_count = state.get("retry_count", 0)
            if retry_count < MAX_AGENT_RETRIES:
                logger.warning(
                    f"Tool execution failed, retrying (attempt {retry_count + 1}/{MAX_AGENT_RETRIES})"
                )
                return {
                    **state_updates,
                    "error": f"Tool execution failed: {e}",
                    "next_action": "agent",
                    "retry_count": retry_count + 1,
                    "agent_confidence": 0.0,
                    "agent_reasoning": f"Tool {tool_call['name']} failed, retrying: {e}",
                    "navigation_history": [{
                        "action": f"agent_tool_failed_{tool_call['name']}",
                        "error": str(e),
                        "success": False,
                    }],
                }

            logger.error(
                f"Tool execution failed after {MAX_AGENT_RETRIES} retries, escalating to error handler"
            )

            recovery_state = {**state, **state_updates}
            recovery_result = _try_recovery(recovery_state, str(e))
            if recovery_result:
                logger.info("Structured recovery succeeded, continuing")
                return {**state_updates, **recovery_result, "retry_count": 0}

            return {
                **state_updates,
                "error": f"Tool execution failed: {e}",
                "next_action": "handle_error",
                "retry_count": retry_count,
                "agent_confidence": 0.0,
                "agent_reasoning": f"Tool {tool_call['name']} failed: {e}",
                "navigation_history": [{
                    "action": f"agent_tool_failed_{tool_call['name']}",
                    "error": str(e),
                    "success": False,
                }],
            }

    # No tool calls -- LLM responded with plain text
    content = response.content or ""
    logger.warning(f"Agent: LLM returned no tool calls. Content: {content[:200]}")

    return {
        **state_updates,
        "next_action": "ask_user",
        "agent_confidence": 0.0,
        "agent_reasoning": f"LLM returned text instead of tool call: {content[:100]}",
        "navigation_history": [{
            "action": "agent_no_tool_call",
            "llm_content": content[:200],
            "success": False,
        }],
    }


def _record_learned_example(
    snapshot: dict,
    page_type: str,
    tool_name: str,
    tool_args: dict,
    success: bool,
):
    """Record a successful agent action to the knowledge base for auto-learning."""
    try:
        from .knowledge_base import get_knowledge_base
        kb = get_knowledge_base()
        action_desc = f"{tool_name}({tool_args})"
        kb.add_example(
            snapshot=snapshot,
            classification=page_type,
            confidence=1.0 if success else 0.5,
            action_taken=action_desc,
            action_succeeded=success,
        )
    except Exception as e:
        # Auto-learning is best-effort, never block navigation
        logger.debug(f"Auto-learning failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Node: Human (HITL)
# ---------------------------------------------------------------------------

def human_node(state: NavigationState) -> dict:
    """
    HITL node -- pauses execution and waits for user input.

    LangGraph will interrupt before this node due to interrupt_before=["human"].
    The caller provides user input via graph.update_state() when resuming.

    After resumption, this node applies the user's selection:
    - If user_selections has a pending "selected_item", select it from the list.
    - Otherwise, just return state unchanged for the graph to re-evaluate.
    """
    logger.info("Human node: processing user input")

    user_selections = state.get("user_selections", {})
    selected_item = user_selections.get("selected_item")

    if selected_item:
        logger.info(f"Human node: user selected '{selected_item}'")

        # Execute the user's selection
        result = _invoke_tool(select_list_item, {"item_text": selected_item})

        if result["success"]:
            snapshot = result["snapshot"]
            new_page = snapshot["page"] if snapshot else "unknown"

            # Determine which field to store the selection
            current_page = state.get("current_page", "unknown")
            selection_key = _selection_key_for_page(current_page)

            # Update user_selections with the concrete selection
            updated_selections = {**user_selections}
            if selection_key:
                updated_selections[selection_key] = selected_item
            # Clear the pending selection
            updated_selections.pop("selected_item", None)

            return {
                "current_page": new_page,
                "page_snapshot": snapshot,
                "user_selections": updated_selections,
                "next_action": "continue",
                "error": None,
                "navigation_history": [{
                    "action": f"user selected '{selected_item}'",
                    "from_page": current_page,
                    "to_page": new_page,
                    "deterministic": False,
                    "user_driven": True,
                    "success": True,
                }],
                "step_count": state.get("step_count", 0) + 1,
            }
        else:
            error_msg = result.get("error", f"Failed to select '{selected_item}'")
            logger.warning(f"Human node: selection failed: {error_msg}")
            return {
                "error": error_msg,
                "next_action": "handle_error",
                "navigation_history": [{
                    "action": f"user selection '{selected_item}' failed",
                    "success": False,
                    "error": error_msg,
                }],
                "step_count": state.get("step_count", 0) + 1,
            }

    # No selection provided -- just return state unchanged
    # (graph will re-evaluate via should_continue)
    logger.info("Human node: no selection provided, returning state unchanged")
    return {"step_count": state.get("step_count", 0) + 1}


def _selection_key_for_page(page: str) -> str | None:
    """Map a page to the user_selections key for storing the choice."""
    mapping = {
        "module_list": "module",
        "data_list": "data_category",
        "sub_data_list": "sub_category",
    }
    return mapping.get(page)


# ---------------------------------------------------------------------------
# Router: should_continue
# ---------------------------------------------------------------------------

def should_continue(state: NavigationState) -> Literal["deterministic", "agent", "human", "end"]:
    """
    Routing function -- decides which node to execute next.

    Reads `next_action` and `current_page` from state to determine flow.
    """
    current_page = state.get("current_page", "unknown")
    next_action = state.get("next_action", "continue")
    error = state.get("error")
    retry_count = state.get("retry_count", 0)

    # 0a. Step limit exceeded
    step_count = state.get("step_count", 0)
    max_steps = state.get("max_steps", 50)
    if step_count >= max_steps:
        logger.warning(f"Routing: step limit exceeded ({step_count}/{max_steps})")
        return "end"

    # 0b. Wall-clock timeout (5 minutes default)
    start_time = state.get("start_time", 0)
    if start_time > 0:
        elapsed = time.time() - start_time
        MAX_WALL_CLOCK = 300  # 5 minutes
        if elapsed > MAX_WALL_CLOCK:
            logger.warning(f"Routing: wall-clock timeout ({elapsed:.0f}s > {MAX_WALL_CLOCK}s)")
            return "end"

    # 1. Goal reached
    if current_page == "data_display":
        logger.info("Routing: goal reached (data_display)")
        return "end"

    # 2. Explicit "done"
    if next_action == "done":
        logger.info("Routing: task complete (done)")
        return "end"

    # 3. Error with too many retries -> end
    if error and retry_count >= 3:
        logger.info(f"Routing: too many retries ({retry_count}), ending")
        return "end"

    # 4. Error -> agent (try to recover)
    if next_action == "handle_error":
        logger.info("Routing: error detected, sending to agent for recovery")
        return "agent"

    # 5. Explicit ask_user
    if next_action == "ask_user":
        logger.info("Routing: agent requested user input")
        return "human"

    # 6. Current page is a user decision point
    if current_page in USER_DECISION_PAGES:
        logger.info(f"Routing: user decision page ({current_page})")
        return "human"

    # 7. Deterministic route available
    if current_page in DETERMINISTIC_ROUTES:
        logger.info("Routing: deterministic route available")
        return "deterministic"

    # 8. Fallback -> agent
    logger.info(f"Routing: no deterministic route for '{current_page}', using agent")
    return "agent"
