"""
Test script to verify all Phase 1 modules can be imported.

Run this after installing dependencies to ensure everything is set up correctly.
"""

import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def test_imports():
    """Test all module imports."""
    print("Testing Phase 1 module imports...\n")
    
    results = []
    
    # Test 1: State
    try:
        from src.agentic.state import NavigationState
        print("[OK] state.py imported successfully")
        results.append(("state.py", True, None))
    except Exception as e:
        print(f"[FAIL] state.py failed: {e}")
        results.append(("state.py", False, str(e)))
    
    # Test 2: LLM Factory
    try:
        from src.agentic.llm_factory import create_llm, LLMFactory
        print("[OK] llm_factory.py imported successfully")
        results.append(("llm_factory.py", True, None))
    except Exception as e:
        print(f"[FAIL] llm_factory.py failed: {e}")
        results.append(("llm_factory.py", False, str(e)))
    
    # Test 3: Tools
    try:
        from src.agentic.tools import ALL_TOOLS
        print("[OK] tools.py imported successfully")
        results.append(("tools.py", True, None))
    except Exception as e:
        print(f"[FAIL] tools.py failed: {e}")
        results.append(("tools.py", False, str(e)))
    
    # Test 4: Nodes
    try:
        from src.agentic.nodes import deterministic_node, agent_node, human_node
        print("[OK] nodes.py imported successfully")
        results.append(("nodes.py", True, None))
    except Exception as e:
        print(f"[FAIL] nodes.py failed: {e}")
        results.append(("nodes.py", False, str(e)))
    
    # Test 5: Graph
    try:
        from src.agentic.graph import create_navigation_graph
        print("[OK] graph.py imported successfully")
        results.append(("graph.py", True, None))
    except Exception as e:
        print(f"[FAIL] graph.py failed: {e}")
        results.append(("graph.py", False, str(e)))
    
    # Test 6: Knowledge Base
    try:
        from src.agentic.knowledge_base import get_knowledge_base
        print("[OK] knowledge_base.py imported successfully")
        results.append(("knowledge_base.py", True, None))
    except Exception as e:
        print(f"[FAIL] knowledge_base.py failed: {e}")
        results.append(("knowledge_base.py", False, str(e)))
    
    # Test 7: Main module
    try:
        from src.agentic import create_navigation_graph, NavigationState
        print("[OK] src.agentic package imported successfully")
        results.append(("src.agentic", True, None))
    except Exception as e:
        print(f"[FAIL] src.agentic package failed: {e}")
        results.append(("src.agentic", False, str(e)))
    
    # Summary
    print("\n" + "="*60)
    success_count = sum(1 for _, success, _ in results if success)
    total_count = len(results)
    
    print(f"Results: {success_count}/{total_count} modules imported successfully")
    
    if success_count == total_count:
        print("\n[SUCCESS] All imports successful! Phase 1 setup is complete.")
        return 0
    else:
        print("\n[ERROR] Some imports failed. Please check the errors above.")
        print("\nFailed modules:")
        for module, success, error in results:
            if not success:
                print(f"  - {module}: {error}")
        
        print("\nTroubleshooting:")
        print("1. Run: pip install -r requirements.txt")
        print("2. Check that you're in the correct virtual environment")
        print("3. Verify Python version >= 3.10")
        return 1

if __name__ == "__main__":
    sys.exit(test_imports())
