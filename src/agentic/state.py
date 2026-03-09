"""
State definition for GDS2 Agentic Navigation.

Defines the state schema used by LangGraph state machine.
"""

from typing import TypedDict, Annotated, Literal, Optional
import operator


class NavigationState(TypedDict):
    """
    State for GDS2 navigation agent.
    
    This state is passed between nodes in the LangGraph state machine.
    """
    
    # Goal
    goal: str  # "Navigate to Engine Data Display"
    
    # Current state
    current_page: str  # GDS2Page.value: "main_menu", "data_display", etc.
    page_snapshot: dict  # {"page": str, "buttons": [...], "lists": [...], "context": {...}}
    screenshot: Optional[bytes]  # Optional screenshot for visual analysis
    
    # History (accumulated across nodes)
    navigation_history: Annotated[list[dict], operator.add]
    """
    List of navigation actions taken.
    Example: [{"action": "clicked Diagnostics", "deterministic": True}]
    """
    
    # User selections
    user_selections: dict
    """
    User's selections at decision points.
    Example: {"module": "ECM - Engine Control", "data_category": "Engine Data"}
    """
    
    # Control flow
    next_action: Literal["continue", "ask_user", "handle_error", "agent", "done"]
    """
    Determines which node to execute next.
    - continue: proceed with automation (router picks deterministic or agent)
    - ask_user: pause for HITL
    - handle_error: error recovery needed (router sends to agent)
    - agent: hand off to AI agent node directly
    - done: goal reached
    """
    
    # Error handling
    error: Optional[str]  # Error message if any
    retry_count: int  # Number of retries attempted

    # Execution limits
    max_steps: int  # Maximum number of node executions
    step_count: int  # Current step count
    start_time: float  # Unix timestamp when graph execution started

    # Agent reasoning (for debugging)
    agent_confidence: float  # 0.0 - 1.0
    agent_reasoning: str  # LLM's reasoning for its decision
