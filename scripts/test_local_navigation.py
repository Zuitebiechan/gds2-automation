#!/usr/bin/env python
"""
Local interactive test for GDS2 Agentic Navigation.

Runs the LangGraph navigation state machine against a real, locally-running
GDS2 instance. No Flask, no cloud — pure local testing.

Prerequisites:
    1. GDS2 must be running with the Java Agent loaded.
    2. Java Agent writes to ~/gds2-data/latest.json.
    3. ZhipuAI API key must be available:
       - Environment variable ZHIPUAI_API_KEY, OR
       - %APPDATA%/VCI_Proxy/config.json → {"zhipuai_api_key": "..."}
    4. For knowledge base queries, run `python scripts/init_knowledge_base.py` first.
       (Set HF_ENDPOINT=https://hf-mirror.com if huggingface.co is unreachable.)

Usage:
    python scripts/test_local_navigation.py
    python scripts/test_local_navigation.py --goal "Navigate to Data Display"
    python scripts/test_local_navigation.py --dry-run   # verify imports only
"""

import argparse
import logging
import os
import sys

# Ensure project root is on sys.path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def setup_logging(verbose: bool = False):
    """Configure logging for local test."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet down noisy loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("sentence_transformers").setLevel(logging.WARNING)


def verify_imports():
    """Verify all required imports work."""
    print("Verifying imports...")

    checks = [
        ("state", "from src.agentic.state import NavigationState"),
        ("tools", "from src.agentic.tools import ALL_TOOLS"),
        ("llm_factory", "from src.agentic.llm_factory import create_llm, LLMFactory"),
        ("nodes", "from src.agentic.nodes import deterministic_node, agent_node, human_node, should_continue"),
        ("graph", "from src.agentic.graph import create_navigation_graph, make_initial_state, run_local_interactive"),
        ("knowledge_base", "from src.agentic.knowledge_base import get_knowledge_base, query_similar_pages"),
    ]

    all_ok = True
    for name, import_stmt in checks:
        try:
            exec(import_stmt)
            print(f"  [OK] {name}")
        except Exception as e:
            print(f"  [FAIL] {name}: {e}")
            all_ok = False

    return all_ok


def verify_graph():
    """Verify graph compiles and has correct structure."""
    print("\nVerifying graph compilation...")

    from src.agentic.graph import create_navigation_graph, make_initial_state

    graph = create_navigation_graph()
    nodes = list(graph.get_graph().nodes)
    print(f"  [OK] Graph compiled with nodes: {nodes}")

    state = make_initial_state()
    print(f"  [OK] Initial state: page={state['current_page']}, goal={state['goal']}")

    expected_nodes = {"__start__", "deterministic", "agent", "human", "__end__"}
    if set(nodes) == expected_nodes:
        print(f"  [OK] Node set matches expected")
    else:
        print(f"  [FAIL] Node mismatch! Expected {expected_nodes}, got {set(nodes)}")
        return False

    return True


def verify_llm_key():
    """Check if ZhipuAI API key is available."""
    print("\nVerifying LLM API key...")

    # Check env var
    key = os.getenv("ZHIPUAI_API_KEY")
    if key:
        print(f"  [OK] ZHIPUAI_API_KEY from environment ({key[:8]}...)")
        return True

    # Check config.json
    appdata = os.getenv("APPDATA", "")
    if appdata:
        config_path = os.path.join(appdata, "VCI_Proxy", "config.json")
        if os.path.exists(config_path):
            import json
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                key = config.get("zhipu_api_key") or config.get("zhipuai_api_key") or config.get("ZHIPUAI_API_KEY")
                if key:
                    print(f"  [OK] ZhipuAI key from config.json ({key[:8]}...)")
                    return True
                else:
                    print(f"  [FAIL] config.json exists but no zhipuai_api_key field")
            except Exception as e:
                print(f"  [FAIL] Failed to read config.json: {e}")
        else:
            print(f"  [FAIL] Config file not found: {config_path}")

    print("  [FAIL] No ZhipuAI API key found!")
    print("    Set ZHIPUAI_API_KEY env var or add to %APPDATA%/VCI_Proxy/config.json")
    return False


def verify_knowledge_base():
    """Check if knowledge base is initialized."""
    print("\nVerifying knowledge base...")

    db_path = os.path.join(PROJECT_ROOT, "data", "gds2_knowledge.lance")
    if os.path.exists(db_path):
        print(f"  [OK] LanceDB found at {db_path}")
        try:
            # Need HF_ENDPOINT for sentence-transformers
            if not os.getenv("HF_ENDPOINT"):
                os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

            from src.agentic.knowledge_base import get_knowledge_base
            kb = get_knowledge_base()
            stats = kb.get_stats()
            print(f"  [OK] Pages: {stats.get('page_count', 0)}")
            print(f"  [OK] Error patterns: {stats.get('error_pattern_count', 0)}")
            print(f"  [OK] Icons: {stats.get('icon_count', 0)}")
            return True
        except Exception as e:
            print(f"  [WARN] KB exists but failed to load: {e}")
            print("    (Non-fatal — agent will work without KB)")
            return True  # Non-fatal
    else:
        print(f"  [WARN] LanceDB not found at {db_path}")
        print("    Run: python scripts/init_knowledge_base.py")
        print("    (Non-fatal — agent will work without KB)")
        return True  # Non-fatal


def check_gds2_running():
    """Check if GDS2 Java Agent is accessible."""
    print("\nChecking GDS2 Java Agent...")

    agent_json = os.path.expanduser("~/gds2-data/latest.json")
    if os.path.exists(agent_json):
        import json
        try:
            with open(agent_json, "r", encoding="utf-8") as f:
                data = json.load(f)
            buttons = data.get("buttons", [])
            print(f"  [OK] Java Agent data found ({len(buttons)} buttons)")
            return True
        except Exception as e:
            print(f"  [WARN] Java Agent file exists but failed to read: {e}")
    else:
        print(f"  [FAIL] Java Agent data not found: {agent_json}")
        print("    Make sure GDS2 is running with the Java Agent loaded.")

    return False


def main():
    parser = argparse.ArgumentParser(description="Test GDS2 Agentic Navigation locally")
    parser.add_argument("--goal", default="Navigate to Data Display",
                        help="Navigation goal (default: Navigate to Data Display)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Only verify imports and config, don't run navigation")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable debug logging")
    parser.add_argument("--skip-kb", action="store_true",
                        help="Skip knowledge base verification (faster startup)")

    args = parser.parse_args()
    setup_logging(args.verbose)

    print("=" * 60)
    print("GDS2 Agentic Navigation — Local Test")
    print("=" * 60)

    # 1. Verify imports
    if not verify_imports():
        print("\n[FAIL] Import verification failed. Fix errors above.")
        sys.exit(1)

    # 2. Verify graph
    if not verify_graph():
        print("\n[FAIL] Graph verification failed.")
        sys.exit(1)

    # 3. Verify LLM key
    has_key = verify_llm_key()

    # 4. Verify KB (optional)
    if not args.skip_kb:
        verify_knowledge_base()

    if args.dry_run:
        print("\n" + "=" * 60)
        print("Dry run complete. All verifications passed." if has_key
              else "Dry run complete. Note: LLM key missing (agent node will fail).")
        print("=" * 60)
        sys.exit(0)

    # 5. Check GDS2 is running
    if not check_gds2_running():
        print("\n[WARN] GDS2 not detected. Continue anyway? (y/n)")
        answer = input().strip().lower()
        if answer != "y":
            print("Aborted.")
            sys.exit(0)

    if not has_key:
        print("\n[FAIL] Cannot run navigation without LLM API key.")
        sys.exit(1)

    # 6. Run interactive navigation
    print("\n" + "=" * 60)
    print("Starting interactive navigation...")
    print("=" * 60 + "\n")

    from src.agentic.graph import run_local_interactive
    run_local_interactive(goal=args.goal)


if __name__ == "__main__":
    main()
