# Architecture Overview

**Status note (2026-03-10)**: this document covers the full system, including the core cloud-to-local tunnel, diagnostics stack, and the LangGraph-based agentic navigation layer. The agentic layer lives in `src/agentic/` with these files: `graph.py`, `nodes.py`, `tools.py`, `knowledge_base.py`, `state.py`, `llm_factory.py`. The navigate API blueprint (`navigate_api.py`) exposes the LangGraph graph over HTTP/SSE at `/api/navigate/*`.

## High-Level Architecture

```
                        Internet
                           |
    +----------------------+-----------------------+
    |                 Cloud Server                  |
    |               (Alibaba Cloud)                 |
    |                                               |
    |  +-------------------+  +------------------+  |
    |  | OEM Diagnostic SW |  | RPA Automation   |  |
    |  | (GDS2 + Agent)    |  | (Python)         |  |
    |  +--------+----------+  +--------+---------+  |
    |           |  J2534 API           |             |
    |  +--------v----------+  +--------v---------+  |
    |  | virtual_j2534.dll |  | Debug Web UI     |  |
    |  | (Fake J2534 DLL)  |  | (Flask, temp)    |  |
    |  +--------+----------+  +------------------+  |
    |           |                                    |
    |  +--------v----------+                         |
    |  | reverse_server.py |                         |
    |  | :9001 (DLL conn)  |                         |
    |  | :9000 (VCI conn)  |                         |
    |  | + 3-layer cache   |                         |
    |  +--------+----------+                         |
    +-----------|-----------+-----------------------+
                | TCP tunnel (reverse connection)
    +-----------|-----------+-----------------------+
    |           |           User's Local Machine    |
    |  +--------v----------+                         |
    |  | reverse_client.py |                         |
    |  | + PSK auth        |                         |
    |  | + VBATT cache     |                         |
    |  +--------+----------+                         |
    |           | ctypes FFI                          |
    |  +--------v----------+                         |
    |  | Real J2534 DLL    |                         |
    |  | (Scanmatik SM2/3) |                         |
    |  +--------+----------+                         |
    |           | USB                                |
    |  +--------v----------+                         |
    |  | VCI Device        |                         |
    |  |      | OBD-II     |                         |
    |  |   Vehicle ECU     |                         |
    |  +-------------------+                         |
    +-----------------------------------------------+
```

## Three Layers

| Layer | Role | Lifespan |
|-------|------|----------|
| **VCI Proxy Tunnel** (`vci_proxy/`) | Core infrastructure: bridge cloud software to local hardware | Permanent, shared by all OEM tools |
| **RPA Automation** (`src/`) | Per-OEM-tool UI automation module (currently: GDS2) | Permanent, one per OEM tool |
| **Client UX + Service Layer** (`vci_proxy/client_gui.py`, `vci_proxy/diagnostics_window.py`, `app.py`) | Mechanic-facing local exe UX + cloud diagnostics/debug APIs | Current delivery path |

## Agentic Navigation Layer

The agentic layer (`src/agentic/`) replaces hardcoded step-by-step navigation with a hybrid approach: deterministic routes for known pages, human-in-the-loop (HITL) for user decisions, and an AI agent for everything else.

### LangGraph StateGraph

The navigation graph is a 3-node LangGraph `StateGraph` compiled with checkpointing and HITL support:

```
START --> deterministic --> [deterministic | agent | human | END]
                            agent --> [deterministic | agent | human | END]
                            human --> [deterministic | agent | END]
```

A central router function (`should_continue`) inspects the current page and `next_action` field after every node execution. It picks the next node based on these rules, evaluated in order:

1. Step limit (50) or wall-clock timeout (300s) exceeded: END
2. Current page is `data_display` or `next_action == "done"`: END
3. Too many retries (3+): END
4. Error detected: route to **agent** for recovery
5. `next_action == "ask_user"`: route to **human**
6. Current page is a user decision page: route to **human**
7. Deterministic route available for current page: route to **deterministic**
8. Fallback: route to **agent**

### Deterministic Node

Handles three known-safe pages where the correct action never changes:

| Current Page | Action | Target |
|---|---|---|
| `main_menu` | `click_button` | "Diagnostics" |
| `diagnostics_menu` | `select_list_item` | "Module Diagnostics" |
| `module_submenu` | `click_button` | "Data Display" |

These routes execute instantly with no LLM call. If the action fails, the router sends the error to the agent node for recovery.

### Human Node (HITL)

Three pages require user decisions because the correct choice depends on the vehicle and diagnostic intent:

| Page | What the user picks |
|---|---|
| `module_list` | Which ECU module to diagnose (e.g., "ECM", "TCM") |
| `data_list` | Which data category to view (e.g., "Engine Data 1") |
| `sub_data_list` | Which sub-category, if the module has nested data |

LangGraph pauses execution before the human node (`interrupt_before=["human"]`). The caller provides the user's selection via `graph.update_state()`, and the human node applies it by calling `select_list_item` with the chosen text.

### Agent Node (AI Fallback)

When the current page doesn't match any deterministic route or user decision page, the agent node takes over. This covers unknown pages, error dialogs, unexpected popups, and any state the deterministic rules don't anticipate.

The agent node works in five steps:

1. **Snapshot**: get a fresh page snapshot (buttons, lists, context) from NavigationController
2. **RAG query**: search the LanceDB knowledge base for similar pages, error patterns, and tool suggestions
3. **LLM call**: send the snapshot + RAG context to ZhipuAI (glm-4.7) with 9 tools bound via native tool-calling
4. **Execute**: run the single tool call the LLM returns
5. **Learn**: if the action succeeded, record the example back to the knowledge base

The 9 bound tools fall into three categories:

| Category | Tools |
|---|---|
| **Action** | `click_button`, `select_list_item`, `go_back`, `go_home` |
| **Observation** | `get_current_snapshot`, `get_list_items` |
| **Signal** | `ask_user`, `report_error`, `mark_done` |

If a tool call fails, the agent retries up to 2 times. After that, it attempts structured recovery via the anomaly detection pipeline before escalating to the error handler.

### LanceDB Knowledge Base

The knowledge base stores GDS2 page patterns, error handling rules, navigation traces, and icon catalogs in a LanceDB vector database at `data/gds2_knowledge.lance`. Embeddings use the `all-MiniLM-L6-v2` sentence transformer model.

Four tables:

| Table | Contents | Used for |
|---|---|---|
| `gds2_pages` | Page type definitions with button/list signatures and deterministic actions | Classifying unknown pages via semantic similarity |
| `gds2_error_patterns` | Known error types with recommended recovery actions | Guiding the agent during error recovery |
| `gds2_navigation_traces` | Recorded navigation sessions (goal, pages visited, success/failure) | Finding proven paths for similar goals |
| `gds2_icons` | Icon catalog with categories and semantic names | Visual page identification |

Similarity search uses L2 distance with a configurable threshold (default 1.5). Results above the threshold are marked as low-confidence, so the agent knows when it's in unfamiliar territory.

The knowledge base also supports auto-learning: when the agent successfully navigates an unknown page, it records the page snapshot and action as a new example for future reference.

### LLM Factory

`llm_factory.py` provides a multi-provider factory for creating LangChain-compatible LLM instances. Currently supports ZhipuAI, Google Gemini, and OpenAI. The default is ZhipuAI glm-4.7 with thinking disabled (all tokens go to content output). API key resolution follows a three-step chain: explicit parameter, environment variable, then `%APPDATA%/VCI_Proxy/config.json`.

## Project Structure

```
RPA_demo/
├── vci_proxy/                      # LAYER 1: VCI Proxy Tunnel (core infra)
│   ├── protocol.py                 # Binary protocol: 14-byte header, J2534 msg types
│   ├── reverse_server.py           # Cloud-side server (port 9000 + 9001)
│   ├── reverse_client.py           # Local-side client (connects to cloud)
│   ├── j2534_driver.py             # ctypes wrapper for real J2534 DLL
│   ├── config.py                   # Frozen dataclass configuration
│   ├── auth.py                     # HMAC-SHA256 PSK authentication
│   ├── cache_read_msgs.py          # ReadMsgs BUFFER_EMPTY cache
│   ├── cache_filter_dedup.py       # StartFilter deduplication
│   ├── cache_vbatt.py              # READ_VBATT response cache
│   └── virtual_dll/                # C DLL that GDS2 loads instead of real HW
│       ├── virtual_j2534.c         # Virtual J2534 DLL source
│       └── register_vci_proxy.reg  # Windows registry entries
│
├── src/                            # LAYER 2: RPA Automation (GDS2-specific)
│   ├── core/                       # Reserved core package (currently minimal)
│   ├── navigation/
│   │   └── controller.py           # NavigationController + GDS2Page enum
│   ├── agentic/                    # Agentic navigation layer (LangGraph)
│   │   ├── graph.py                # StateGraph construction, HITL loop, local runner
│   │   ├── nodes.py                # deterministic_node, agent_node, human_node, router
│   │   ├── tools.py                # 9 LangChain tools (action + observation + signal)
│   │   ├── knowledge_base.py       # LanceDB interface: pages, errors, traces, icons
│   │   ├── state.py                # NavigationState TypedDict (graph state schema)
│   │   └── llm_factory.py          # Multi-provider LLM factory (ZhipuAI/Gemini/OpenAI)
│   ├── streaming/
│   │   ├── agent_navigator.py      # AgentNavigator: JSON command/response
│   │   ├── agent_data_collector.py # AgentDataCollector: 100ms poll + SSE
│   │   └── diagnostic_buffer.py   # DiagnosticBuffer: 30s window + delta compression
│   ├── diagnosis/                  # Phase 3.5: AI-powered diagnosis
│   │   ├── llm_client.py          # ZhipuAI streaming wrapper + prompt assembly
│   │   └── ai_engine.py           # Orchestration: collector -> buffer -> LLM -> SSE
│   ├── native/
│   │   └── device_explorer.py      # DeviceExplorerController (Win32)
│   ├── discovery/
│   │   └── vehicle_mapping.py      # VehicleMapping + VehicleDiscovery
│   ├── recovery/                   # AI-powered exception recovery
│   └── workflows/
│       ├── data_viewer.py          # PRIMARY: DataViewerWorkflow
│       ├── interactive_workflow.py # Step-by-step navigation
│       └── read_data_display_agent.py  # CLI workflow wrapper
│
├── data/                           # Knowledge base and reference data
│   ├── gds2_knowledge.lance/       # LanceDB vector database (4 tables)
│   └── screenshots/                # GDS2 page screenshots for visual reference
│
├── app.py                          # Flask service/debug backend (includes /api/diagnose, /api/navigate)
├── navigate_api.py                 # Navigate API blueprint (/api/navigate/*) — LangGraph SSE + HITL
├── diagnostics_api.py              # Phase 3 diagnostics API blueprint
├── diagnostics_api.py              # Phase 3 diagnostics API blueprint
├── templates/index.html            # Debug Web UI frontend
├── vci_proxy/client_gui.py         # Local tray client (mechanic-facing)
├── vci_proxy/diagnostics_window.py # Local diagnostics UX (module/category/DTC/live/navigate)
└── tests/                          # pytest test suite
```

## Data Flow

```
User action -> DataViewerWorkflow -> AgentNavigator -> Java Agent -> GDS2
  -> virtual_j2534.dll -> reverse_server (cache check) -> Internet
  -> reverse_client (VBATT cache) -> real J2534 DLL -> USB -> VCI -> Vehicle ECU
  -> response returns same path
  -> AgentDataCollector (100ms poll) -> SSE -> Local Diagnostics Window (primary)
                                                  and Debug Web UI (service/debug)

AI Diagnosis flow (Phase 3.5):
  User clicks "AI Diagnose"
  -> AgentDataCollector -> DiagnosticBuffer (30s accumulation)
  -> delta-compressed payload (initial state + timestamped changes)
  -> ZhipuAI glm-4.7 (streaming, thinking disabled) -> SSE -> Local Diagnostics Window

Agentic Navigation flow:
  Goal: reach Data Display page from any starting point
  Client clicks "Start Agent Diagnostics"
  -> POST /api/navigate/start (creates NavSession, launches graph thread)
  -> GET /api/navigate/events?session_id=... (SSE stream)
  -> LangGraph StateGraph starts at deterministic_node
  -> Router checks current page after each node:
       known page (main_menu, diagnostics_menu, module_submenu)
         -> deterministic_node executes fixed action, no LLM needed
       user decision page (module_list, data_list, sub_data_list)
         -> human_node pauses, SSE emits decision_required event
         -> Client shows dropdown, user picks, POST /api/navigate/decision
         -> Graph resumes with user's selection
       unknown/error page
         -> agent_node queries LanceDB RAG, calls ZhipuAI with bound tools
  -> Loop continues until data_display is reached or limits exceeded
  -> SSE emits done event with final_page, steps, and selections
  -> Navigation trace recorded to LanceDB for future reference
```

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Reverse connection (local -> cloud) | Avoids NAT/firewall at user location |
| Virtual DLL | GDS2 thinks hardware is local. Zero mods to GDS2 |
| Java Agent (not PyAutoGUI) | Direct JVM: 100ms collection, no screen dependency |
| Win32 API for Device Explorer | Not JavaFX. Agent can't control it |
| Server-side caching | ~70% round-trip reduction |
| Custom binary protocol | Minimal overhead for high-freq J2534 |
| Per-OEM RPA modules | Each tool has unique UI |
| Debug Web UI is temporary | Not the product interface |
| Deterministic-first navigation | Known pages skip LLM entirely, saving latency and cost |
| LanceDB for knowledge base | Embedded vector DB, no external service needed, auto-learning |
| Native tool-calling over ReAct | ZhipuAI supports `bind_tools` natively, cleaner than string parsing |
| HITL via LangGraph interrupt | Graph pauses cleanly at decision points, resumes with user input |

## Target Product Architecture

```
User-Facing Layer (future):
  Mobile App / Mini-Program / Simple Web Page
       |
Session Manager (future):
  - Creates/destroys cloud VMs per user
  - Generates connection codes
       |
Diagnostic API Layer (future):
  - "Read All DTCs" -> unified response format
  - "Start Live Data" -> SSE stream
       |
RPA Layer (per OEM tool):            VCI Proxy Tunnel:
  GDS2 adapter (working)              reverse_server (working)
  ISTA adapter (future)                reverse_client (working)
  ODIS adapter (future)                virtual_j2534.dll (working)
       |                                    |
  OEM Software in Cloud VM             User's Local VCI Hardware
```
