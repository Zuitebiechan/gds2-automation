"""
Tool definitions for GDS2 navigation.

These tools wrap NavigationController methods for use by the LangGraph agent.
Each tool returns a standardized dict for the state machine to consume.
"""

from langchain_core.tools import tool
from typing import Dict, Any, List
import logging
import threading

logger = logging.getLogger(__name__)

# Lazy-loaded controller (shared across tools)
_controller = None
_controller_lock = threading.Lock()


def get_controller():
    """Lazy-load NavigationController to avoid circular imports.

    Thread-safe: uses double-checked locking so concurrent callers
    never create duplicate instances.
    """
    global _controller
    if _controller is None:
        with _controller_lock:
            if _controller is None:
                from ..navigation.controller import NavigationController
                _controller = NavigationController()
    return _controller


def _snapshot_from_controller(controller) -> Dict[str, Any]:
    """
    Build a page snapshot dict from the current NavigationController state.

    Delegates to controller.get_snapshot() which uses cached IPC results
    from the most recent detect_current_page() call, avoiding redundant
    round-trips to the Java Agent.

    Returns:
        {"page": str, "buttons": [...], "lists": [...], "context": {...}}
    """
    return controller.get_snapshot()


@tool
def click_button(button_text: str) -> Dict[str, Any]:
    """
    Click a button in GDS2 and return the new page state.

    Args:
        button_text: Text of the button to click (e.g., "Diagnostics", "Data Display")

    Returns:
        Dictionary with success, new page snapshot, and error info.
    """
    controller = get_controller()

    try:
        logger.info(f"Tool: clicking button '{button_text}'")
        result = controller.click_button(button_text)

        if not result.success:
            return {
                "success": False,
                "snapshot": _snapshot_from_controller(controller),
                "error": result.error or f"Failed to click '{button_text}'",
            }

        snapshot = _snapshot_from_controller(controller)
        logger.info(f"Tool: click succeeded, now on page '{snapshot['page']}'")

        return {
            "success": True,
            "snapshot": snapshot,
            "error": None,
        }

    except Exception as e:
        logger.exception(f"Tool: click_button error: {e}")
        return {"success": False, "snapshot": None, "error": str(e)}


@tool
def select_list_item(item_text: str) -> Dict[str, Any]:
    """
    Select an item from the current list in GDS2 (partial match, double-click).

    Args:
        item_text: Text to match in the list (e.g., "ECM - Engine Control", "Module Diagnostics")

    Returns:
        Dictionary with success, new page snapshot, and error info.
    """
    controller = get_controller()

    try:
        logger.info(f"Tool: selecting list item '{item_text}'")
        result = controller.select_list_item(item_text)

        if not result.success:
            return {
                "success": False,
                "snapshot": _snapshot_from_controller(controller),
                "choices": result.choices,
                "error": result.error or f"Failed to select '{item_text}'",
            }

        snapshot = _snapshot_from_controller(controller)
        logger.info(f"Tool: selection succeeded, now on page '{snapshot['page']}'")

        return {
            "success": True,
            "snapshot": snapshot,
            "choices": None,
            "error": None,
        }

    except Exception as e:
        logger.exception(f"Tool: select_list_item error: {e}")
        return {"success": False, "snapshot": None, "choices": None, "error": str(e)}


@tool
def get_current_snapshot() -> Dict[str, Any]:
    """
    Get a snapshot of the current GDS2 page state.

    Returns:
        Page snapshot with page type, visible buttons, list items, and context.
    """
    controller = get_controller()

    try:
        return _snapshot_from_controller(controller)
    except Exception as e:
        logger.exception(f"Tool: get_current_snapshot error: {e}")
        return {"page": "UNKNOWN", "buttons": [], "lists": [], "context": {}, "error": str(e)}


@tool
def go_back() -> Dict[str, Any]:
    """
    Click the Back button in GDS2.

    Returns:
        Dictionary with success, new page snapshot, and error info.
    """
    controller = get_controller()

    try:
        logger.info("Tool: going back")
        result = controller.go_back()

        return {
            "success": result.success,
            "snapshot": _snapshot_from_controller(controller),
            "error": result.error,
        }

    except Exception as e:
        logger.exception(f"Tool: go_back error: {e}")
        return {"success": False, "snapshot": None, "error": str(e)}


@tool
def go_home() -> Dict[str, Any]:
    """
    Click the Home button in GDS2 to return to Main Menu.

    Returns:
        Dictionary with success, new page snapshot, and error info.
    """
    controller = get_controller()

    try:
        logger.info("Tool: going home")
        result = controller.go_home()

        return {
            "success": result.success,
            "snapshot": _snapshot_from_controller(controller),
            "error": result.error,
        }

    except Exception as e:
        logger.exception(f"Tool: go_home error: {e}")
        return {"success": False, "snapshot": None, "error": str(e)}


@tool
def get_list_items() -> Dict[str, Any]:
    """
    Get the current list items visible on the GDS2 page.

    Returns:
        Dictionary with list items.
    """
    controller = get_controller()

    try:
        items = controller.get_list_items(0)
        return {"items": items, "count": len(items)}
    except Exception as e:
        logger.exception(f"Tool: get_list_items error: {e}")
        return {"items": [], "count": 0, "error": str(e)}

# ---------------------------------------------------------------------------
# Signal tools (non-action decisions for the agent)
# ---------------------------------------------------------------------------

@tool
def ask_user(prompt: str, options: str = "") -> Dict[str, Any]:
    """
    Request user input. Call this when the page requires a human decision,
    such as selecting a module or data category from a list.

    Args:
        prompt: What to ask the user (e.g., "Please select a module")
        options: Comma-separated available options (e.g., "ECM,TCM,BCM")

    Returns:
        Signal dict indicating user input is needed.
    """
    logger.info(f"Tool: asking user - {prompt}")
    option_list = [o.strip() for o in options.split(",") if o.strip()] if options else []
    return {"signal": "ask_user", "prompt": prompt, "options": option_list}


@tool
def report_error(error_type: str, description: str, recommended_action: str = "go_back") -> Dict[str, Any]:
    """
    Report an error condition detected on the current page.
    Call this when you see an error dialog, unexpected popup, or anomalous state.

    Args:
        error_type: Type of error (e.g., "error_dialog", "timeout", "unexpected_page")
        description: What the error looks like
        recommended_action: Suggested recovery ("go_back", "dismiss", "retry", "go_home")

    Returns:
        Signal dict with error details.
    """
    logger.info(f"Tool: reporting error - {error_type}: {description}")
    return {"signal": "error", "error_type": error_type, "description": description, "recommended": recommended_action}


@tool
def mark_done(reasoning: str) -> Dict[str, Any]:
    """
    Signal that the navigation goal has been reached.
    Call this when the current page is the target page (e.g., Data Display).

    Args:
        reasoning: Why you believe the goal is reached

    Returns:
        Signal dict indicating completion.
    """
    logger.info(f"Tool: marking done - {reasoning}")
    return {"signal": "done", "reasoning": reasoning}


# Export all tools as a list for easy binding
# Action tools: interact with GDS2
ACTION_TOOLS = [
    click_button,
    select_list_item,
    go_back,
    go_home,
]

# Observation tools: read GDS2 state
OBSERVATION_TOOLS = [
    get_current_snapshot,
    get_list_items,
]

# Signal tools: non-action decisions
SIGNAL_TOOLS = [
    ask_user,
    report_error,
    mark_done,
]

# All tools combined (for bind_tools)
ALL_TOOLS = ACTION_TOOLS + OBSERVATION_TOOLS + SIGNAL_TOOLS
