"""
LangGraph state machine for GDS2 navigation.

Constructs the complete navigation workflow with deterministic,
agent, and human nodes.

Provides both the reusable graph factory and a local interactive
runner for testing with real GDS2 (no Flask needed).
"""

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
import logging
import time

from .state import NavigationState
from .nodes import deterministic_node, agent_node, human_node, should_continue

logger = logging.getLogger(__name__)


def _record_trace(state: dict):
    """Record the final navigation trace for future reference."""
    try:
        from .knowledge_base import record_navigation_trace

        navigation_history = state.get("navigation_history", [])
        goal = state.get("goal", "")
        current_page = state.get("current_page")
        user_selections = state.get("user_selections", {})
        step_count = state.get("step_count", 0)
        success = current_page == "data_display" or state.get("next_action") == "done"

        record_navigation_trace(
            steps=navigation_history,
            goal=goal,
            success=success,
            total_steps=step_count,
            user_selections=user_selections,
        )
    except Exception as e:
        logger.warning(f"Failed to record navigation trace: {e}")


def create_navigation_graph():
    """
    Create the GDS2 navigation state machine.

    Returns:
        Compiled LangGraph application with checkpointing and HITL support.

    Graph structure:
        START → deterministic → [deterministic | agent | human | END]
                agent → [deterministic | agent | human | END]
                human → [deterministic | agent | END]

    Examples:
        >>> graph = create_navigation_graph()
        >>> config = {"configurable": {"thread_id": "session_001"}}
        >>>
        >>> initial_state = {
        ...     "goal": "Navigate to Data Display",
        ...     "current_page": "main_menu",
        ...     "page_snapshot": {},
        ...     "navigation_history": [],
        ...     "user_selections": {},
        ...     "next_action": "continue",
        ...     "error": None,
        ...     "retry_count": 0,
        ...     "agent_confidence": 0.0,
        ...     "agent_reasoning": "",
        ... }
        >>>
        >>> for event in graph.stream(initial_state, config):
        ...     print(event)
    """
    logger.info("Creating navigation graph")

    # Initialize graph with state schema
    workflow = StateGraph(NavigationState)

    # Add nodes
    workflow.add_node("deterministic", deterministic_node)
    workflow.add_node("agent", agent_node)
    workflow.add_node("human", human_node)

    logger.info("Added nodes: deterministic, agent, human")

    # Set entry point
    workflow.add_edge(START, "deterministic")

    # Add conditional edges from deterministic node
    workflow.add_conditional_edges(
        "deterministic",
        should_continue,
        {
            "deterministic": "deterministic",
            "agent": "agent",
            "human": "human",
            "end": END,
        },
    )

    # Add conditional edges from agent node
    workflow.add_conditional_edges(
        "agent",
        should_continue,
        {
            "deterministic": "deterministic",
            "agent": "agent",
            "human": "human",
            "end": END,
        },
    )

    # Add conditional edges from human node
    workflow.add_conditional_edges(
        "human",
        should_continue,
        {
            "deterministic": "deterministic",
            "agent": "agent",
            "end": END,
        },
    )

    logger.info("Added conditional edges")

    # Compile with checkpointing and HITL support
    memory = MemorySaver()
    app = workflow.compile(
        checkpointer=memory,
        interrupt_before=["human"],  # Pause before human node
    )

    logger.info("Graph compiled successfully")

    return app


def make_initial_state(goal: str = "Navigate to Data Display") -> dict:
    """
    Create a valid initial state for the navigation graph.

    Args:
        goal: Navigation goal description.

    Returns:
        Dictionary matching NavigationState schema.
    """
    return {
        "goal": goal,
        "current_page": "main_menu",
        "page_snapshot": {},
        "screenshot": None,
        "navigation_history": [],
        "user_selections": {},
        "next_action": "continue",
        "error": None,
        "retry_count": 0,
        "max_steps": 50,
        "step_count": 0,
        "start_time": time.time(),
        "agent_confidence": 0.0,
        "agent_reasoning": "",
    }


def run_local_interactive(goal: str = "Navigate to Data Display"):
    """
    Run the navigation graph locally in interactive mode.

    This is the local testing entry point — no Flask, no cloud.
    GDS2 must be running on the same machine.

    The loop:
    1. Stream graph events until it pauses (HITL) or ends.
    2. When paused at a human node, display choices and ask for input.
    3. Resume with the user's selection.
    4. Repeat until done or error.

    Args:
        goal: Navigation goal (default: get to Data Display page).
    """
    import uuid

    graph = create_navigation_graph()
    thread_id = f"local_{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": thread_id}}
    initial_state = make_initial_state(goal)

    print(f"\n{'='*60}")
    print(f"GDS2 Agentic Navigation — Local Interactive Mode")
    print(f"Goal: {goal}")
    print(f"Thread: {thread_id}")
    print(f"{'='*60}\n")

    # First run
    _stream_and_handle(graph, initial_state, config)


def _stream_and_handle(graph, input_state, config):
    """
    Internal: stream graph events, handle HITL pauses, loop until done.
    """
    current_input = input_state

    while True:
        print("[GRAPH] Streaming...")
        events = []
        for event in graph.stream(current_input, config):
            events.append(event)
            _print_event(event)

        # Check graph state
        state = graph.get_state(config)

        if not state.next:
            # Graph finished
            print(f"\n{'='*60}")
            print("[DONE] Navigation complete.")
            final = state.values
            print(f"  Final page: {final.get('current_page', '?')}")
            print(f"  Error: {final.get('error', 'None')}")
            print(f"  Steps: {len(final.get('navigation_history', []))}")
            if final.get("user_selections"):
                print(f"  Selections: {final['user_selections']}")
            print(f"{'='*60}\n")
            _record_trace(final)
            return

        # Graph paused — HITL
        if "human" in state.next:
            snapshot = state.values.get("page_snapshot", {})
            current_page = state.values.get("current_page", "unknown")
            lists = snapshot.get("lists", [])

            print(f"\n{'─'*40}")
            print(f"[HITL] Paused on page: {current_page}")

            if lists:
                print(f"Available items ({len(lists)}):")
                for i, item in enumerate(lists, 1):
                    print(f"  {i}. {item}")
            else:
                print("  (no list items detected)")

            print()
            user_input = input("Enter selection (text or number, 'q' to quit): ").strip()

            if user_input.lower() in ("q", "quit", "exit"):
                print("[ABORT] User quit.")
                return

            # Resolve number to item text
            selected = user_input
            if user_input.isdigit():
                idx = int(user_input) - 1
                if 0 <= idx < len(lists):
                    selected = lists[idx]
                else:
                    print(f"[WARN] Invalid number {user_input}, using as text")

            print(f"[HITL] Selected: {selected}")

            # Resume graph with user's selection
            graph.update_state(
                config,
                {"user_selections": {"selected_item": selected}},
            )

            # Continue streaming (input=None continues from checkpoint)
            current_input = None
        else:
            # Unexpected pause
            print(f"[WARN] Graph paused at unexpected node(s): {state.next}")
            return


def _print_event(event):
    """Pretty-print a single graph event."""
    if not isinstance(event, dict):
        return
    for node_name, node_output in event.items():
        if node_name == "__end__":
            continue
        if not isinstance(node_output, dict):
            continue
        page = node_output.get("current_page", "")
        action = node_output.get("next_action", "")
        error = node_output.get("error", "")
        history = node_output.get("navigation_history", [])

        last_step = history[-1] if history else {}
        step_desc = last_step.get("action", "")

        parts = [f"[{node_name}]"]
        if step_desc:
            parts.append(step_desc)
        if page:
            parts.append(f"→ page={page}")
        if action:
            parts.append(f"(next={action})")
        if error:
            parts.append(f"ERROR: {error}")

        print("  ".join(parts))


def visualize_graph(graph=None):
    """
    Generate a visual representation of the graph.

    Args:
        graph: Compiled graph (if None, creates a new one)

    Returns:
        Mermaid diagram as string
    """
    if graph is None:
        graph = create_navigation_graph()

    try:
        return graph.get_graph().draw_mermaid()
    except Exception as e:
        logger.warning(f"Failed to generate graph visualization: {e}")
        return None
