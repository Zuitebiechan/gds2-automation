# RPA Automation (GDS2)

This document describes the **GDS2-specific** automation/runtime layer. In the new architecture, GDS2 automation lives under `src/` and is wrapped by `backends/gds2/backend.py`, which exposes the shared `DiagnosticBackend` contract used by the API layer.

## GDS2 in the Multi-Backend Platform

| Layer | Location | Role |
|---|---|---|
| **Platform Core** | `diagnostic_platform/` | Shared contracts, schemas, backend registry, SSE helpers |
| **GDS2 Backend Facade** | `backends/gds2/backend.py` | Adapts GDS2 runtime to `DiagnosticBackend` |
| **GDS2 RPA Runtime** | `src/` | Actual GDS2 navigation, streaming, recovery, AI diagnosis |

## GDS2 Automation Stack

### Backend facade

| Component | File | Purpose |
|---|---|---|
| **GDS2DiagnosticBackend** | `backends/gds2/backend.py` | Standard backend facade over the existing GDS2 workflow/controller |

### Navigation layer (agentic)

| Component | File | Purpose |
|---|---|---|
| **NavigationGraph** | `src/agentic/graph.py` | LangGraph StateGraph: deterministic + agent + human nodes |
| **NavigationNodes** | `src/agentic/nodes.py` | Node implementations + routing logic |
| **NavigationTools** | `src/agentic/tools.py` | Native tool-calling functions for ZhipuAI |
| **NavigationState** | `src/agentic/state.py` | Typed state schema for LangGraph |
| **LLMFactory** | `src/agentic/llm_factory.py` | ZhipuAI/Gemini/OpenAI provider factory |

### Execution/runtime layer

| Component | File | Purpose |
|---|---|---|
| **DataViewerWorkflow** | `src/workflows/data_viewer.py` | Retained GDS2 workflow orchestration |
| **NavigationController** | `src/navigation/controller.py` | Page detection + state transitions (`GDS2Page`) |
| **AgentNavigator** | `src/streaming/agent_navigator.py` | Command/response IPC with Java Agent |
| **AgentDataCollector** | `src/streaming/agent_data_collector.py` | 100ms polling, parameter/DTC extraction, change detection |
| **DiagnosticBuffer** | `src/streaming/diagnostic_buffer.py` | 30s sliding window, dual-rate sampling, delta compression |
| **DeviceExplorerController** | `src/native/device_explorer.py` | Win32 Device Explorer automation |
| **VehicleMapping** | `src/discovery/vehicle_mapping.py` | Cache module/category indexes per vehicle |

## Agentic Navigation Flow

The LangGraph hybrid navigator uses three node types:

```text
START -> deterministic_node -> router -> deterministic | agent | human | END
                                   agent_node -> router -> ...
                                   human_node -> router -> ...
```

| Node | Pages Handled | LLM Call? |
|---|---|---|
| **deterministic** | known-safe pages | No |
| **human** (HITL) | module/data/sub-data selection pages | No |
| **agent** (AI) | unknown pages, recovery, unexpected states | Yes |

The agent node uses deterministic page hints before calling ZhipuAI with bound tools.

## GDS2 Page Flow

```text
MAIN_MENU -> DEVICE_EXPLORER -> VEHICLE_SELECTION -> DIAGNOSTICS_MENU
  -> MODULE_LIST -> MODULE_SUBMENU -> DATA_LIST -> [SUB_DATA_LIST] -> DATA_DISPLAY
```

## Java Agent Communication

| File | Writer | Purpose |
|---|---|---|
| `~/gds2-data/command.json` | Python | Commands to Agent |
| `~/gds2-data/result.json` | Java Agent | Command execution results |
| `~/gds2-data/latest.json` | Java Agent | Continuously updated snapshot |

Agent extraction is JVM-level, not screenshot-driven OCR.

## API Integration

`app.py` is now a **thin Flask entry point**. It registers only:

- `/api/diagnose/*`
- `/api/navigate/*`
- `/api/session/*`

These are the supported API surfaces for the current product path.

### Diagnostics API

`diagnostics_api.py`:

- uses `GDS2DiagnosticBackend`
- uses shared SSE callbacks/helpers from `diagnostic_platform/sse.py`
- keeps the mechanic flow centered on Data Display operations

Supported diagnostics endpoints:

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/diagnose/start` | POST | Start diagnostics, auto-connect `VCI Proxy (Remote)`, return modules + context |
| `/api/diagnose/select_module` | POST | Select module and return data categories |
| `/api/diagnose/dtcs` | GET | Read DTCs from current Data Display context |
| `/api/diagnose/live_data/start` | POST | Start live collection for selected data category |
| `/api/diagnose/live_data/events` | GET | SSE real-time stream |
| `/api/diagnose/live_data/stop` | POST | Stop stream and navigate back |
| `/api/diagnose/ai_diagnose` | POST | Start 30s collection + AI analysis |
| `/api/diagnose/ai_diagnose/events` | GET | SSE progress + streamed verdict |
| `/api/diagnose/ai_diagnose/retry` | POST | Retry LLM call with cached payload |

### Session API

`session_api.py` uses the backend abstraction for GDS2 session lifecycle/orchestration and keeps its own session event queues.

Supported session endpoints:

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/session/start` | POST | Start session and select workflow/backend context |
| `/api/session/start_diagnostics` | POST | Start GDS2 diagnostics for a running session |
| `/api/session/execute` | POST | Execute one guarded backend action |
| `/api/session/events` | GET | SSE events for session lifecycle and decisions |
| `/api/session/decision` | POST | Submit user decision option |
| `/api/session/select_module` | POST | Session-aware module selection |
| `/api/session/select_data_category` | POST | Session-aware data category selection |
| `/api/session/abort` | POST | Abort session |
| `/api/session/status` | GET | Query session state |

## Required Mechanic UX Flow

1. Start Diagnostics
2. Select Module and click **Select**
3. Select Data Category
4. Click **AI Diagnose** — collects 30s data and streams AI output
5. Optionally use **Read DTCs** or **Start Stream**

This ensures GDS2 stays on Data Display before DTC/live-data operations.

## Operational Gotchas

- Device Explorer is **Win32**, not JavaFX
- GDS2 frequently uses **GBK** encoding
- One GDS2 instance per machine
- DTC/live data are valid only on the correct page context
- AI diagnosis requires a full 30s collection window before the LLM call
