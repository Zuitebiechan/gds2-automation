# RPA_demo AGENTS Guide

This file is the Codex-facing project guide. Use it as the primary instruction file for this repository.

`CLAUDE.md` remains a reference document, but if it drifts from the current codebase or from this file, prefer the codebase, this file, and `agent_docs/`.

## Read First

When a task touches architecture, backend boundaries, or product flow, read these first:

- `README.md`
- `agent_docs/architecture.md`
- `agent_docs/roadmap.md`

## Project Shape

This repository is no longer a GDS2-only app. It is a cloud remote vehicle diagnostics platform with a shared backend contract layer.

Key layers:

- `diagnostic_platform/`: shared contracts, standard dataclasses, backend registry, SSE helpers
- `backends/gds2/`: GDS2 facade implementing the shared backend contract
- `src/`: GDS2-specific automation, navigation, recovery, diagnosis, and agentic flows
- `vci_proxy/`: reverse tunnel and local tray client
- `app.py`: thin Flask entry point that registers blueprints only
- `diagnostics_api.py`, `navigate_api.py`, `session_api.py`: supported HTTP surfaces

## Architectural Rules

- Keep `app.py` thin. Do not push business logic back into the Flask entry point.
- Add new OEM support through `diagnostic_platform/` plus `backends/`, not by hard-coding directly into the GDS2-specific `src/` runtime.
- Treat `src/` as GDS2-specific unless a module is explicitly generalized.
- Preserve the backend contract boundary when changing diagnostics or session flows.
- Use `diagnostic_platform/` as the canonical platform-layer name in code and docs.

## Product Flow

The primary mechanic-facing flow is:

1. Start Diagnostics
2. Select Module
3. Select Data Category
4. Run AI Diagnose
5. Optionally read DTCs or start live streaming

Keep GDS2 on the Data Display page for DTC and live-data operations.

## Runtime Facts

- GDS2 is JavaFX, but Device Explorer is Win32 and follows a separate automation path.
- Java Agent state is written to `~/gds2-data/latest.json`.
- GDS2 commonly uses GBK encoding.
- One GDS2 instance per machine.
- ZhipuAI API keys belong in `%APPDATA%/VCI_Proxy/config.json`, never in source.
- AI diagnosis requires a 30-second data collection window before the LLM call.
- Knowledge-base embedding flows have been removed. Do not reintroduce SentenceTransformer-based learning paths unless explicitly requested.

## Supported APIs

Only these API surfaces are considered supported:

- `/api/diagnose/*`
- `/api/navigate/*`
- `/api/session/*`

If you add behavior, route it through these supported surfaces unless the task explicitly expands the API.

## Common Commands

Environment setup:

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Cloud runtime:

```bash
python -m vci_proxy.reverse_server
python app.py --port 8080
```

Local client:

```bash
python -m vci_proxy.client_gui
```

Client build:

```bash
pyinstaller --clean --noconfirm pyinstaller_client.spec
```

## Code Hygiene

- Remove dead code when replacing flows or abstractions.
- Keep durable regression tests in `tests/`.
- Delete one-off validation scripts or throwaway tests after the targeted change is validated.
- Do not add broad documentation duplication when the source of truth already exists in `README.md` or `agent_docs/`.
