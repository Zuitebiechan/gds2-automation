# Testing And Quality

## Scope

This document describes the durable test layout, what kinds of changes should be validated, and how documentation quality is kept aligned with the codebase.

This document does not restate the full architecture. For design context, read `agent_docs/core/platform_architecture.md` and `agent_docs/core/backend_architecture.md`.

## Testing Principles

- Keep durable regression tests in `tests/`.
- Prefer backend-neutral tests first when changing platform behavior.
- Add backend-specific tests only where behavior is genuinely backend-specific.
- Do not leave one-off scripts or temporary validation files behind after the change is validated.
- Documentation changes should be checked for stale paths, outdated names, and removed technologies.

## Current Test Coverage Areas

| Test file | Main concern |
| --- | --- |
| `tests/test_backend_capability_architecture.py` | capability-first architecture and registry behavior |
| `tests/test_backend_capability_refactor.py` | backend-neutral session/runtime refactor behavior |
| `tests/test_runtime_session_layers.py` | business-session bindings, decisions, abort, and status logic |
| `tests/test_runtime_diagnostics_navigation.py` | diagnostics and navigation runtime behavior |
| `tests/test_gds2_controller_runtime.py` | GDS2 controller/runtime and alias-routing behavior |
| `tests/test_tunnel_quality.py` | tunnel-quality snapshot normalization and grading |
| `tests/test_proxy_benchmark.py` | proxy benchmark behavior |
| `tests/test_server_layout.py` | Flask/server layout and blueprint wiring |
| `tests/test_repo_layout_consistency.py` | repository/documentation consistency and stale-technology guards |
| `tests/test_data_viewer_workflow.py` | GDS2 workflow behavior |
| `tests/test_agent_data_collector.py` | Java agent data collection behavior |

## What To Validate By Change Type

### If you change `diagnostic_platform/`

Run focused architecture/runtime tests first:

```bash
python -m pytest tests\test_backend_capability_architecture.py tests\test_backend_capability_refactor.py tests\test_runtime_session_layers.py tests\test_runtime_diagnostics_navigation.py -q
```

### If you change `backends/gds2/` or `src/`

Run the focused GDS2/runtime regressions:

```bash
python -m pytest tests\test_gds2_controller_runtime.py tests\test_data_viewer_workflow.py tests\test_agent_data_collector.py -q
```

### If you change `server/`

Run at least:

```bash
python -m pytest tests\test_server_layout.py tests\test_runtime_session_layers.py tests\test_runtime_diagnostics_navigation.py -q
```

### If you change documentation structure or repository policy

Run:

```bash
python -m pytest tests\test_repo_layout_consistency.py -q
```

## Documentation Quality Rules

Documentation should satisfy these rules:

- one document should own one subject area
- avoid copying the same explanation across multiple files
- link to the owning file path when another document already owns the detail
- do not keep removed technologies in current docs
- do not keep obsolete path names after renames
- align docs with actual public routes and current field names

Examples of items that must stay current:

- `backend_name` vs `workflow`
- `src/gds2_orchestration/` vs removed `src/agentic`
- no LangGraph framing in current product docs
- current supported API surfaces

## Full Test Suite Command

When you need the broad tracked suite:

```bash
python -m pytest tests -q
```

## Read Next

- Directory-scoped quick guide: `tests/README.md`
- Repository layout: `agent_docs/core/project_structure.md`
- API and runtime behavior: `agent_docs/core/api_design.md`
