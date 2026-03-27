# Architecture Overview

**Status note (2026-03-18)**: the project is no longer documented as a GDS2-only stack. The codebase is being reshaped into a **multi-software diagnostic platform** with a shared backend contract layer, per-software backend facades, and GDS2 as the first concrete backend.

## High-Level Architecture

```text
                        Internet
                           |
    +----------------------+-----------------------+
    |                 Cloud Server                  |
    |                                               |
    |  +-------------------+  +------------------+  |
    |  | OEM Diagnostic SW |  | RPA Automation   |  |
    |  | (GDS2 today)      |  | (src/, GDS2)     |  |
    |  +--------+----------+  +--------+---------+  |
    |           |                      |             |
    |  +--------v----------+  +--------v---------+  |
    |  | Backend Facade    |  | Platform Core    |  |
    |  | backends/gds2/    |  | diagnostic_      |  |
    |  | DiagnosticBackend |  | platform/        |  |
    |  +--------+----------+  +--------+---------+  |
    |           |                      |             |
    |  +--------v---------------------------------+ |
    |  | Flask API Entry Point                    | |
    |  | app.py -> /api/diagnose, /navigate,     | |
    |  |          /session blueprints            | |
    |  +--------+--------------------------------+ |
    |           |                                  |
    |  +--------v----------+                       |
    |  | reverse_server.py |                       |
    |  | :9001 DLL side    |                       |
    |  | :9000 VCI side    |                       |
    |  +--------+----------+                       |
    +-----------|----------------------------------+
                | TCP reverse tunnel
    +-----------|----------------------------------+
    |           |          Local Machine           |
    |  +--------v----------+                       |
    |  | reverse_client.py |                       |
    |  +--------+----------+                       |
    |           | ctypes FFI                       |
    |  +--------v----------+                       |
    |  | Real J2534 DLL    |                       |
    |  +--------+----------+                       |
    |           | USB                              |
    |  +--------v----------+                       |
    |  | VCI Device        |                       |
    |  +-------------------+                       |
    +----------------------------------------------+
```

## Layered Model

| Layer | Location | Responsibility |
|---|---|---|
| **VCI Proxy Tunnel** | `vci_proxy/` | OEM-agnostic bridge between cloud software and local VCI hardware |
| **Platform Core** | `diagnostic_platform/` | Shared contracts, dataclasses, backend discovery, SSE helpers |
| **Backend Facades** | `backends/`, `backends/gds2/` | Per-software implementations of the shared backend interface |
| **RPA Automation** | `src/` | GDS2-specific navigation, collection, recovery, AI diagnosis, agentic navigation |
| **API + Client UX** | `app.py`, `diagnostics_api.py`, `navigate_api.py`, `session_api.py`, `vci_proxy/client_gui.py`, `vci_proxy/diagnostics_window.py` | Thin HTTP entry point and mechanic-facing local UX |

## Platform Core

The new platform layer is the key architectural change.

### Shared contract

`diagnostic_platform/contracts.py` defines:

- `DiagnosticBackend` — abstract API for backend lifecycle, module/category selection, DTCs, live data, actions, and state
- `BackendRegistry` — backend lookup by backend name or supported brand
- Standard schemas:
  - `VehicleContext`
  - `DTC`
  - `LiveDataPoint`
  - `DiagnosticPayload`
  - `BackendState`
  - `ClearResult`
  - `ActionResult`
  - `LiveDataStream`

### Shared SSE helpers

`diagnostic_platform/sse.py` provides reusable diagnostics/live-data SSE helpers used by backend-facing API layers.

## Backend Facades

Backends now sit between the shared contract layer and the software-specific automation stack.

### GDS2 backend

`backends/gds2/backend.py` exposes `GDS2DiagnosticBackend`, which adapts the existing GDS2 runtime in `src/` to the shared `DiagnosticBackend` interface.

Responsibilities:

- start/connect GDS2 through the existing workflow
- expose module and data-category selection
- map GDS2 DTCs into standard platform dataclasses
- expose backend state in a standardized shape
- bridge shared actions to the retained executor/adapter path where needed

## API Layer

`app.py` is now a **thin Flask entry point** that only registers blueprints:

- `diagnostics_api.py` → `/api/diagnose/*`
- `navigate_api.py` → `/api/navigate/*`
- `session_api.py` → `/api/session/*`

This blueprint registration layer is the supported HTTP surface for the current product path.

### Diagnostics API

`diagnostics_api.py` is backend-oriented:

- uses `GDS2DiagnosticBackend`
- uses shared SSE helpers from `diagnostic_platform/sse.py`
- keeps the mechanic UX centered on Data Display operations

### Session API

`session_api.py` uses the backend abstraction for GDS2 session lifecycle/orchestration and keeps its own in-memory session event queues for `/api/session/*`.

It is the preferred public facade for product-facing flows. Session-scoped subroutes now wrap:

- diagnostics start and selection
- DTC reads
- live-data start/stop/events
- AI diagnosis start/retry/events
- agentic navigation start/decision/events/abort/status

`diagnostics_api.py` and `navigate_api.py` remain useful as lower-level capability/debug layers, but they are no longer the recommended external entrypoints for the mechanic/product flow.

### Navigate API

`navigate_api.py` remains the GDS2 LangGraph navigation surface with its own in-memory navigation session store.

## GDS2-Specific Automation Layer

The shared platform layer does **not** replace the existing GDS2 runtime. `src/` still contains the GDS2-specific implementation:

- `src/navigation/` — page detection and transitions
- `src/streaming/` — agent navigator, live collection, diagnostic buffering
- `src/diagnosis/` — AI diagnosis orchestration and LLM client
- `src/agentic/` — LangGraph navigator, tools, nodes
- `src/native/` — Win32 Device Explorer path
- `src/workflows/` — retained GDS2 workflow orchestration

## Supported API Surfaces

Only these API surfaces remain supported:

- `/api/diagnose/*`
- `/api/navigate/*`
- `/api/session/*`

## Project Structure

```text
RPA_demo/
├── diagnostic_platform/            # Shared backend contracts + SSE helpers
│   ├── contracts.py
│   └── sse.py
├── backends/
│   └── gds2/
│       └── backend.py             # GDS2DiagnosticBackend facade
├── src/                           # GDS2-specific automation/runtime implementation
├── vci_proxy/                     # Tunnel + local tray client
├── app.py                         # Thin Flask entry point
├── diagnostics_api.py             # /api/diagnose/*
├── navigate_api.py                # /api/navigate/*
├── session_api.py                 # /api/session/*
└── tests/                         # Test suite
```

## Target Direction

The architecture now targets this model:

1. Keep the VCI tunnel OEM-agnostic
2. Keep per-software automation isolated in software-specific backend/runtime layers
3. Standardize API behavior and payloads through `diagnostic_platform/`
4. Add more OEM backends by implementing `DiagnosticBackend`, not by rewriting the whole stack
