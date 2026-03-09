# GDS2 Agentic Navigation Implementation Guide

**Last Updated**: 2026-03-09  
**Status**: Phase 1-3 local implementation complete; broader product integration still in progress

---

## Executive Summary

This document provides a complete implementation guide for replacing hardcoded GDS2 UI navigation with an AI-powered hybrid system using **LangGraph + ZhipuAI + LanceDB**.

## Current Implementation Snapshot (2026-03-09)

This document started as an implementation guide. On the current branch, a substantial part of that guide has already been built and verified locally:

- **Completed in code**: `src/agentic/state.py`, `llm_factory.py`, `tools.py`, `nodes.py`, `graph.py`, `knowledge_base.py`
- **Knowledge base bootstrap exists**: `scripts/init_knowledge_base.py` seeds LanceDB with page/error/icon records
- **Local interactive runner exists**: `scripts/test_local_navigation.py` + `run_local_interactive()` in `src/agentic/graph.py`
- **Locally verified**: graph compilation, imports, KB loading path, and a real local end-to-end navigation run from `main_menu` to `data_display`

What this guide still describes as future work should now be read in two buckets:

1. **Already delivered on this branch** — local hybrid navigation foundation, KB bootstrap, ZhipuAI wiring, HITL loop.
2. **Still remaining** — full screenshot-driven visual reasoning, production hardening, richer learning/replay, and complete cloud/client rollout of the newer session-oriented orchestration.

### Key Design Principles

1. **Hybrid Architecture**: Deterministic paths (hardcoded) + AI adaptation (dynamic) + Human-in-the-loop (critical decisions)
2. **Multimodal Knowledge Base**: Store text patterns AND screenshots for robust page recognition
3. **LLM Agnostic**: Start with ZhipuAI, easily switch to Gemini or others
4. **Production Ready**: Checkpointing, error recovery, debugging support

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    User (Mechanic)                          │
│  - Clicks "Start Diagnostics"                               │
│  - Receives decision prompts via GUI                        │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│              LangGraph State Machine (Cloud)                │
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐     │
│  │ Deterministic│  │  AI Agent    │  │  HITL Node   │     │
│  │   Router     │  │  (ZhipuAI)   │  │  (Pause)     │     │
│  └──────────────┘  └──────────────┘  └──────────────┘     │
│         │                  │                  │            │
│         └──────────────────┴──────────────────┘            │
│                         │                                  │
│                         ▼                                  │
│              ┌──────────────────────┐                      │
│              │  LanceDB Knowledge   │                      │
│              │  - Page screenshots  │                      │
│              │  - Recognition rules │                      │
│              │  - Error patterns    │                      │
│              └──────────────────────┘                      │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│           Existing GDS2 Automation Layer                    │
│  - NavigationController (Java Agent + Win32)                │
│  - VCI Proxy                                                │
└─────────────────────────────────────────────────────────────┘
```

---

## Technology Stack

| Component | Technology | Rationale |
|---|---|---|
| **State Machine** | LangGraph 0.2+ | Native HITL, checkpointing, tool calling |
| **LLM** | ZhipuAI glm-4 → Gemini 2.0 | Start with existing, easy to switch |
| **Vector DB** | LanceDB 0.5+ | Best multimodal support, embedded |
| **Embeddings** | OpenCLIP (images) + MiniLM (text) | Cross-modal retrieval |
| **Integration** | LangChain Community | Unified interface |

---

## Phase 1: Foundation Setup

### 1.1 Install Dependencies

```bash
# Core dependencies
pip install langgraph>=0.2.0
pip install langchain>=0.3.0
pip install langchain-community>=0.3.0
pip install langchain-openai>=0.2.0  # For OpenAI-compatible adapter

# Vector DB and embeddings
pip install lancedb>=0.5.0
pip install sentence-transformers>=2.2.0
pip install open-clip-torch>=2.20.0
pip install pillow>=10.0.0

# Existing dependencies (already in requirements.txt)
# zhipuai, flask, pyautogui, etc.
```

### 1.2 Project Structure

```
RPA_demo_clean_merge_main/
├── data/
│   ├── gds2_knowledge.lance/     # LanceDB database
│   └── screenshots/              # GDS2 page screenshots
│       ├── main_menu.png
│       ├── module_function_menu_ecm.png
│       ├── module_function_menu_tcm.png
│       ├── sub_module_selection.png
│       └── error_connection_timeout.png
│
├── src/
│   ├── agentic/
│   │   ├── __init__.py
│   │   ├── graph.py              # LangGraph state machine
│   │   ├── nodes.py              # Node implementations
│   │   ├── tools.py              # Tool definitions
│   │   ├── state.py              # State schema
│   │   ├── llm_factory.py        # LLM provider abstraction
│   │   └── knowledge_base.py     # LanceDB operations
│   │
│   ├── navigation/
│   │   └── controller.py         # Existing (wrapped as tools)
│   │
│   └── diagnosis/
│       └── llm_client.py         # Existing (reuse ZhipuAI client)
│
├── scripts/
│   ├── init_knowledge_base.py    # Initialize LanceDB
│   └── capture_screenshots.py    # Capture GDS2 screenshots
│
└── agent_docs/
    └── gds2_agentic_navigation_implementation.md  # This file
```

---

## Phase 2: Knowledge Base Setup

### 2.1 LanceDB Schema

```python
# src/agentic/knowledge_base.py

from lancedb.pydantic import LanceModel, Vector
from typing import List, Optional
import lancedb

class GDS2PageKnowledge(LanceModel):
    """GDS2 page knowledge entry"""
    
    # Identity
    id: str  # "page_module_function_menu_001"
    page_type: str  # "MODULE_FUNCTION_MENU"
    description: str
    
    # Text features (for semantic search)
    text_features: str  # Will be embedded
    text_embedding: Vector(384)  # MiniLM embedding dimension
    
    # Visual features (screenshots)
    screenshot_paths: List[str]  # Paths to PNG files
    screenshot_embeddings: List[Vector(512)]  # CLIP embeddings
    
    # Recognition rules
    must_have_buttons: List[str]
    usually_has_buttons: List[str]
    list_count_range: str  # "0-2"
    
    # Metadata
    usage_count: int = 0
    accuracy_rate: float = 1.0
    created_at: str
    updated_at: str


class GDS2ErrorKnowledge(LanceModel):
    """GDS2 error handling knowledge"""
    
    id: str
    error_type: str  # "connection_timeout"
    error_message_pattern: str
    
    # Visual
    error_screenshot_path: str
    screenshot_embedding: Vector(512)
    
    # Handling
    recommended_action: str  # "click_retry"
    action_params: dict
    success_rate: float
    
    # Text embedding
    text_embedding: Vector(384)
```

### 2.2 Initialize Knowledge Base

```python
# scripts/init_knowledge_base.py

import lancedb
from sentence_transformers import SentenceTransformer
from PIL import Image
import open_clip

def initialize_knowledge_base():
    """Initialize GDS2 knowledge base with initial data"""
    
    # Connect to LanceDB
    db = lancedb.connect("data/gds2_knowledge.lance")
    
    # Initialize embedding models
    text_model = SentenceTransformer('all-MiniLM-L6-v2')
    clip_model, _, preprocess = open_clip.create_model_and_transforms('ViT-B-32')
    
    # Define initial page knowledge
    pages = [
        {
            "id": "page_main_menu",
            "page_type": "MAIN_MENU",
            "description": "GDS2 main menu with Diagnostics button",
            "text_features": "Buttons: Diagnostics, Update, Settings. No lists.",
            "screenshot_paths": ["data/screenshots/main_menu.png"],
            "must_have_buttons": ["Diagnostics", "Update"],
            "usually_has_buttons": ["Settings", "Help"],
            "list_count_range": "0",
            "created_at": "2026-03-05",
            "updated_at": "2026-03-05"
        },
        {
            "id": "page_module_function_menu",
            "page_type": "MODULE_FUNCTION_MENU",
            "description": "Module function menu with Data Display button",
            "text_features": "Buttons: Data Display, DTCs, Special Functions. No lists or very short lists.",
            "screenshot_paths": [
                "data/screenshots/module_function_menu_ecm.png",
                "data/screenshots/module_function_menu_tcm.png"
            ],
            "must_have_buttons": ["Data Display"],
            "usually_has_buttons": ["DTCs", "Special Functions"],
            "list_count_range": "0-2",
            "created_at": "2026-03-05",
            "updated_at": "2026-03-05"
        },
        # Add more pages...
    ]
    
    # Embed and store
    for page in pages:
        # Text embedding
        page["text_embedding"] = text_model.encode(page["text_features"]).tolist()
        
        # Image embeddings
        embeddings = []
        for path in page["screenshot_paths"]:
            img = Image.open(path)
            img_tensor = preprocess(img).unsqueeze(0)
            with torch.no_grad():
                embedding = clip_model.encode_image(img_tensor)
            embeddings.append(embedding.squeeze().tolist())
        page["screenshot_embeddings"] = embeddings
    
    # Create table
    db.create_table("gds2_pages", data=pages, mode="overwrite")
    
    print("✅ Knowledge base initialized with", len(pages), "pages")

if __name__ == "__main__":
    initialize_knowledge_base()
```

---

## Phase 3: LangGraph State Machine

### 3.1 State Definition

```python
# src/agentic/state.py

from typing import TypedDict, Annotated, Literal
import operator

class NavigationState(TypedDict):
    """State for GDS2 navigation agent"""
    
    # Goal
    goal: str  # "Navigate to Engine Data Display"
    
    # Current state
    current_page: str  # "MODULE_LIST"
    page_snapshot: dict  # Java Agent UI snapshot
    screenshot: bytes | None  # Optional screenshot
    
    # History
    navigation_history: Annotated[list[dict], operator.add]
    
    # User selections
    user_selections: dict  # {"module": "ECM", "data_category": "Engine Data"}
    
    # Control flow
    next_action: Literal["continue", "ask_user", "handle_error", "done"]
    
    # Error handling
    error: str | None
    retry_count: int
    
    # Agent reasoning
    agent_confidence: float
    agent_reasoning: str
```

### 3.2 Tool Definitions

```python
# src/agentic/tools.py

from langchain_core.tools import tool
from ..navigation.controller import NavigationController

controller = NavigationController()

@tool
def detect_page_type(snapshot: dict, context: dict) -> dict:
    """
    Detect current GDS2 page type using knowledge base.
    
    Args:
        snapshot: Page snapshot with buttons, lists, etc.
        context: Navigation context (last action, expected pages)
    
    Returns:
        {"page_type": str, "confidence": float, "reasoning": str}
    """
    from .knowledge_base import query_similar_pages
    
    # Query knowledge base
    similar_pages = query_similar_pages(snapshot, top_k=3)
    
    # Use LLM to classify based on similar pages
    # (Implementation in nodes.py)
    pass

@tool
def click_button(button_text: str) -> dict:
    """
    Click a button in GDS2.
    
    Args:
        button_text: Button text to click
    
    Returns:
        {"success": bool, "new_page": dict}
    """
    result = controller.click_button(button_text)
    return {
        "success": result.success,
        "new_page": controller.detect_current_page().to_dict()
    }

@tool
def select_from_list(item_text: str) -> dict:
    """
    Select an item from a list in GDS2.
    
    Args:
        item_text: List item text to select
    
    Returns:
        {"success": bool, "new_page": dict}
    """
    result = controller.select_from_list(item_text)
    return {
        "success": result.success,
        "new_page": controller.detect_current_page().to_dict()
    }
```


### 3.3 Complete Graph Implementation

```python
# src/agentic/graph.py - Complete implementation

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from .state import NavigationState
from .nodes import deterministic_node, agent_node, human_node, should_continue

def create_navigation_graph():
    """Create the GDS2 navigation state machine."""
    workflow = StateGraph(NavigationState)
    
    # Add nodes
    workflow.add_node("deterministic", deterministic_node)
    workflow.add_node("agent", agent_node)
    workflow.add_node("human", human_node)
    
    # Set entry point
    workflow.add_edge(START, "deterministic")
    
    # Add conditional edges
    workflow.add_conditional_edges(
        "deterministic",
        should_continue,
        {
            "deterministic": "deterministic",
            "agent": "agent",
            "human": "human",
            "end": END
        }
    )
    
    workflow.add_conditional_edges(
        "agent",
        should_continue,
        {
            "deterministic": "deterministic",
            "agent": "agent",
            "human": "human",
            "end": END
        }
    )
    
    workflow.add_conditional_edges(
        "human",
        should_continue,
        {
            "deterministic": "deterministic",
            "agent": "agent",
            "end": END
        }
    )
    
    # Compile with checkpointing
    memory = MemorySaver()
    app = workflow.compile(
        checkpointer=memory,
        interrupt_before=["human"]
    )
    
    return app
```

---

## Phase 4: Usage Example

### 4.1 Basic Usage

```python
# Example: Navigate to Data Display

from src.agentic.graph import create_navigation_graph
from src.navigation.controller import NavigationController

# Initialize
graph = create_navigation_graph()
controller = NavigationController()

# Get current page
current_page = controller.detect_current_page()

# Create initial state
initial_state = {
    "goal": "Navigate to Engine Data Display",
    "current_page": current_page.page.value,
    "page_snapshot": current_page.to_dict(),
    "screenshot": None,
    "navigation_history": [],
    "user_selections": {},
    "next_action": "continue",
    "error": None,
    "retry_count": 0,
    "agent_confidence": 1.0,
    "agent_reasoning": ""
}

# Run
config = {"configurable": {"thread_id": "session_001"}}

for event in graph.stream(initial_state, config):
    print(f"Event: {event}")
    
    # Check if paused for user input
    state = graph.get_state(config)
    if state.next == ("human",):
        print("⏸️  Paused for user input")
        print(f"Current page: {state.values['current_page']}")
        print(f"Options: {state.values['page_snapshot'].get('lists', [])}")
        
        # Simulate user selection
        user_choice = "ECM - Engine Control"
        
        # Resume
        graph.update_state(config, {
            "user_selections": {
                **state.values["user_selections"],
                "module": user_choice
            },
            "next_action": "continue"
        })
        
        # Continue
        for event in graph.stream(None, config):
            print(f"Event: {event}")
```

---

## Phase 5: Deployment

### 5.1 Requirements Update

```txt
# Add to requirements.txt

# Agentic navigation
langgraph>=0.2.0
langchain>=0.3.0
langchain-community>=0.3.0
langchain-openai>=0.2.0
langchain-google-genai>=0.0.5

# Vector DB
lancedb>=0.5.0
sentence-transformers>=2.2.0
open-clip-torch>=2.20.0

# Existing dependencies remain
zhipuai>=2.0.0
flask>=2.0.0
# ... etc
```

### 5.2 Environment Variables

```bash
# .env
ZHIPUAI_API_KEY=your_key_here
GEMINI_API_KEY=your_key_here  # Optional
LLM_PROVIDER=zhipuai  # or gemini
LLM_MODEL=glm-4
KNOWLEDGE_BASE_PATH=data/gds2_knowledge.lance
```

---

## Phase 6: Remaining Work From This Guide

### Already completed
1. ✅ Dependencies added and importable in the current Python 3.11 environment
2. ✅ Project structure created under `src/agentic/`
3. ✅ LanceDB bootstrap script implemented
4. ✅ Deterministic + agent + HITL graph implemented
5. ✅ ZhipuAI agent node implemented through `langchain_openai.ChatOpenAI`
6. ✅ Knowledge base query path implemented
7. ✅ Local full-flow test path implemented and used for real GDS2 validation
8. ✅ Gemini/OpenAI provider switching support added in `llm_factory.py`

### Still remaining
9. ⏳ Replace text-first page reasoning with richer screenshot/icon-aware reasoning in the actual runtime loop
10. ⏳ Turn `knowledge_base.add_example()` into an automatic learning pipeline instead of a dormant helper
11. ⏳ Harden error-dialog / device-explorer / unknown-page handling for broader production coverage
12. ⏳ Integrate this local LangGraph flow cleanly with the newer session API / GUI workflow used by the product path
13. ⏳ Add more replay/observability and regression tests for edge cases

---

## Appendix A: Key Design Decisions

### Why LangGraph?
- Native HITL support (interrupt_before)
- Checkpointing for debugging
- Tool calling integration
- State machine visualization

### Why LanceDB?
- Best multimodal support (text + images)
- Embedded (no separate server)
- Fast for small-medium datasets
- LangChain integration

### Why OpenAI-Compatible Adapter for ZhipuAI?
- More stable than native ChatZhipuAI
- Better tool calling support
- Easier to switch to other providers

---

## Appendix B: Troubleshooting

### Issue: Graph doesn't pause at human node
**Solution**: Ensure `interrupt_before=["human"]` in compile()

### Issue: Knowledge base returns empty
**Solution**: Run `scripts/init_knowledge_base.py` first

### Issue: ZhipuAI authentication fails
**Solution**: Check API key format, ensure using OpenAI-compatible endpoint

---

**Document status**: still useful as the original implementation guide, but no longer the source of truth for current status by itself.  
For branch status, also read `gds2_agentic_refactor_execution_plan.md`, `roadmap.md`, and `phase1_completion_summary.md`.
