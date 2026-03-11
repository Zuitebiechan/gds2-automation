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

    # Pre-initialize knowledge base in background so agent node doesn't block
    try:
        from .knowledge_base import preload_knowledge_base
        preload_knowledge_base()
    except Exception:
        pass  # Non-fatal: agent will lazy-init if preload fails

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


def run_with_event_queue(
    goal: str,
    event_queue: "queue.Queue",
    decision_queue: "queue.Queue",
    thread_id: str | None = None,
) -> dict:
    """
    Run the navigation graph driven by queues instead of console input.

    This is the server-side entry point for Flask API integration.
    A background thread runs this function; HITL pauses put events
    into event_queue and block on decision_queue for the user's choice.

    Args:
        goal: Navigation goal (e.g. 'Navigate to Data Display').
        event_queue: Queue to push SSE-style events (dict) to the caller.
        decision_queue: Queue from which user decisions are received.
        thread_id: LangGraph thread/checkpoint id (auto-generated if None).

    Returns:
        Final graph state dict.

    Event types pushed to event_queue:
        {"type": "progress", "node": str, "page": str, "action": str}
        {"type": "decision_required", "decision_id": str, "page": str, "items": list}
        {"type": "done", "final_page": str, "steps": int, "selections": dict}
        {"type": "error", "error": str}
    """
    import queue as _queue_mod
    import uuid as _uuid

    if thread_id is None:
        thread_id = f"api_{_uuid.uuid4().hex[:8]}"

    graph = create_navigation_graph()
    config = {"configurable": {"thread_id": thread_id}}
    initial_state = make_initial_state(goal)

    current_input: dict | None = initial_state
    decision_counter = 0

    try:
        while True:
            # Stream graph events
            for event in graph.stream(current_input, config):
                if not isinstance(event, dict):
                    continue
                for node_name, node_output in event.items():
                    if node_name == "__end__" or not isinstance(node_output, dict):
                        continue
                    history = node_output.get("navigation_history", [])
                    last_step = history[-1] if history else {}
                    event_queue.put({
                        "type": "progress",
                        "node": node_name,
                        "page": node_output.get("current_page", ""),
                        "action": last_step.get("action", ""),
                        "error": node_output.get("error"),
                    })

            # Check graph state after streaming completes
            state = graph.get_state(config)

            if not state.next:
                # Graph finished — emit done event FIRST so the
                # client can proceed immediately, then record trace
                # in background (trace recording may download models).
                final = state.values
                event_queue.put({
                    "type": "done",
                    "final_page": final.get("current_page", "unknown"),
                    "steps": len(final.get("navigation_history", [])),
                    "selections": final.get("user_selections", {}),
                    "error": final.get("error"),
                })
                # Record trace in background (non-blocking)
                import threading as _threading
                _threading.Thread(
                    target=_record_trace,
                    args=(final,),
                    daemon=True,
                    name="nav-trace-record",
                ).start()
                return final

            # Graph paused for HITL
            if "human" in state.next:
                snapshot = state.values.get("page_snapshot", {})
                current_page = state.values.get("current_page", "unknown")
                items = snapshot.get("lists", [])

                decision_counter += 1
                decision_id = f"nav_decision_{decision_counter}"

                event_queue.put({
                    "type": "decision_required",
                    "decision_id": decision_id,
                    "page": current_page,
                    "items": items,
                    "prompt": state.values.get("agent_reasoning", "Please make a selection"),
                })

                # Block until user submits a decision
                try:
                    decision = decision_queue.get(timeout=300)  # 5 min timeout
                except _queue_mod.Empty:
                    event_queue.put({
                        "type": "error",
                        "error": "Decision timeout: no user response within 5 minutes",
                    })
                    return state.values

                selected_item = decision.get("selected_item", "")
                if not selected_item:
                    event_queue.put({
                        "type": "error",
                        "error": "Empty selection received",
                    })
                    return state.values

                # Resume graph with user's selection.
                # MERGE with existing selections so previous choices
                # (e.g. module) are preserved across HITL rounds.
                existing_selections = state.values.get("user_selections", {})
                merged = {**existing_selections, "selected_item": selected_item}
                graph.update_state(
                    config,
                    {"user_selections": merged},
                )

                # Continue streaming (None = resume from checkpoint)
                current_input = None
            else:
                # Unexpected pause
                event_queue.put({
                    "type": "error",
                    "error": f"Graph paused at unexpected node(s): {state.next}",
                })
                return state.values

    except Exception as exc:
        logger.exception(f"run_with_event_queue failed: {exc}")
        event_queue.put({"type": "error", "error": str(exc)})
        try:
            return graph.get_state(config).values
        except Exception:
            return {}
