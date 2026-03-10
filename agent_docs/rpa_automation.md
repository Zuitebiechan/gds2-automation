# RPA Automation (GDS2)

Per-OEM-tool UI automation. Currently: **GDS2 (General Motors)**. Future OEM tools get dedicated adapters under `src/`.

## GDS2 Automation Stack

### Navigation Layer (Agentic)

| Component | File | Purpose |
|---|---|---|
| **NavigationGraph** | `src/agentic/graph.py` | LangGraph StateGraph: deterministic + agent + human nodes |
| **NavigationNodes** | `src/agentic/nodes.py` | 3 node implementations + routing logic (`should_continue`) |
| **NavigationTools** | `src/agentic/tools.py` | 9 native tools for ZhipuAI tool-calling (action + observation + signal) |
| **KnowledgeBase** | `src/agentic/knowledge_base.py` | LanceDB RAG: page matching, error patterns, tool suggestions, auto-learning |
| **NavigationState** | `src/agentic/state.py` | TypedDict state schema for LangGraph |
| **LLMFactory** | `src/agentic/llm_factory.py` | ZhipuAI/Gemini/OpenAI provider factory |

### Execution Layer (Deterministic Drivers)

| Component | File | Purpose |
|---|---|---|
| **DataViewerWorkflow** | `src/workflows/data_viewer.py` | Legacy orchestration (used by `/api/diagnose/*`) |
| **NavigationController** | `src/navigation/controller.py` | State detection + page transitions (`GDS2Page`) |
| **AgentNavigator** | `src/streaming/agent_navigator.py` | Command/response IPC with Java Agent |
| **AgentDataCollector** | `src/streaming/agent_data_collector.py` | 100ms polling, parameter/DTC extraction, change detection |
| **DiagnosticBuffer** | `src/streaming/diagnostic_buffer.py` | 30s sliding window buffer, dual-rate sampling, delta compression |
| **DeviceExplorerController** | `src/native/device_explorer.py` | Win32 Device Explorer automation |
| **VehicleMapping** | `src/discovery/vehicle_mapping.py` | Cache module/category indexes per vehicle |

## Agentic Navigation Flow

The LangGraph hybrid navigator uses three node types:

```
START --> deterministic_node --> should_continue() --> deterministic | agent | human | END
                                                         agent_node --> should_continue() --> ...
                                                         human_node --> should_continue() --> ...
```

| Node | Pages Handled | LLM Call? |
|---|---|---|
| **deterministic** | main_menu, diagnostics_menu, module_submenu | No |
| **human** (HITL) | module_list, data_list, sub_data_list | No |
| **agent** (AI) | Unknown pages, error recovery, unexpected states | Yes (ZhipuAI) |

The agent node queries LanceDB for similar pages and tool suggestions before calling ZhipuAI with bound tools.

## GDS2 Page Flow

```
MAIN_MENU → DEVICE_EXPLORER → VEHICLE_SELECTION → DIAGNOSTICS_MENU
  → MODULE_LIST → MODULE_SUBMENU → DATA_LIST → [SUB_DATA_LIST] → DATA_DISPLAY
```

## Java Agent Communication

| File | Writer | Purpose |
|---|---|---|
| `~/gds2-data/command.json` | Python | Commands to Agent |
| `~/gds2-data/result.json` | Java Agent | Command execution results |
| `~/gds2-data/latest.json` | Java Agent | Continuously updated snapshot |

Agent extraction is JVM-level (no screenshot OCR), enabling high-frequency monitoring.

## Phase 3 Diagnostics APIs

All endpoints are under `/api/diagnose`:

| Endpoint | Method | Purpose |
|---|---|---|
| `/start` | POST | Start diagnostics, auto-connect `VCI Proxy (Remote)`, return modules + VIN/device |
| `/select_module` | POST | Select module and return data categories |
| `/dtcs` | GET | Read DTCs from current Data Display context |
| `/live_data/start` | POST | Start live collection for selected data category |
| `/live_data/events` | GET | SSE real-time stream |
| `/live_data/stop` | POST | Stop stream and navigate back |
| `/ai_diagnose` | POST | Start 30s data collection + AI analysis, return session_id |
| `/ai_diagnose/events` | GET | SSE progress events + streamed LLM verdict |
| `/ai_diagnose/retry` | POST | Retry LLM call with cached payload |

### Required UX Flow (Mechanic)

1. Start Diagnostics
2. Select Module and click **Select**
3. Select Data Category
4. Click **AI Diagnose** (primary) — collects 30s data, sends to AI, streams result
5. Optionally: **Read DTCs** or **Start Stream** (advanced/fallback)

This ensures GDS2 is on Data Display page before DTC/live operations.

### AI Diagnosis Data Flow

```
User clicks "AI Diagnose"
  → POST /ai_diagnose → backend starts AgentDataCollector + DiagnosticBuffer
  → 30s collection (SSE progress: "Collecting... 15/30s")
  → Read DTCs from final snapshot
  → Assemble delta-compressed payload (initial state + timestamped changes)
  → Call ZhipuAI glm-4.7 (streaming, thinking disabled)
  → SSE-stream LLM tokens to client in real-time
  → Final structured verdict displayed
```
## Debug/Service APIs (Flask)

Current Flask layer in `app.py` exposes:

- Viewer APIs: `/api/viewer/*`
- Stream APIs: `/api/stream/*`
- Agent APIs: `/api/agent/*`
- Diagnostics APIs: `/api/diagnose/*`

The mechanic-facing product path is local exe UI + diagnostics APIs. Web UI remains debug/service support.

## Operational Gotchas

- Device Explorer is **Win32**, not JavaFX
- GDS2 frequently uses **GBK** encoding
- One GDS2 instance per machine (single-instance lock)
- DTC/Live data are valid only when page context is correct (Data Display)

## Session APIs (G3, Agentic Orchestration)

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/session/start` | POST | Start session and route workflow by brand/model/VIN |
| `/api/session/events` | GET | SSE events (progress, decision_required, decision_timeout, decision_resolved, error, done) |
| `/api/session/decision` | POST | Submit user decision option |
| `/api/session/abort` | POST | Abort session |
| `/api/session/status` | GET | Query session state snapshot |
| `/api/session/select_module` | POST | Execute module selection; if ambiguous emits `decision_required` |
| `/api/session/select_data_category` | POST | Execute data category selection; if ambiguous emits `decision_required` |

These APIs are additive and do not replace existing `/api/diagnose/*` endpoints.
