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

The suite is organized around these areas instead of a permanently complete file list:

- platform contracts, backend registry, session runtime, API handlers, SSE, and bootstrap/node allocation
- GDS2 backend behavior, registry navigation runtime, route graph/navigation proofing, controller state, reports, and simulated system flows
- AI diagnosis, OpenAI-compatible client configuration, prompt/streaming behavior, sampling quality, and provider error handling
- VCI proxy, reverse tunnel, auth/config, cache behavior, J2534 driver/worker, virtual DLL observability, tray GUI, and diagnostics window
- observability, artifact upload/delivery, local-vs-cloud comparison tooling, repository layout, stale-technology guards, and cloud startup scripts

## What To Validate By Change Type

### If you change `diagnostic_platform/`

Run focused architecture/runtime tests first:

```bash
python -m pytest tests\test_backend_capability_architecture.py tests\test_backend_capability_refactor.py tests\test_runtime_session_layers.py tests\test_runtime_diagnostics_navigation.py tests\test_session_streams.py tests\test_session_api_handlers.py tests\test_gds2_orchestration_core.py -q
```

### If you change `backends/gds2/` or `src/`

Run the focused GDS2/runtime regressions:

```bash
python -m pytest tests\test_gds2_controller_runtime.py tests\test_gds2_report_parser.py tests\test_agent_data_collector.py tests\test_diagnostic_buffer.py tests\test_agent_navigator.py tests\test_diagnostics_window.py tests\test_device_explorer.py tests\test_llm_client.py tests\test_ai_engine.py tests\test_vehicle_mapping.py -q
```

### If you change `vci_proxy/`

Run the client/proxy-facing regressions:

```bash
python -m pytest tests\test_tunnel_quality.py tests\test_proxy_benchmark.py tests\test_vci_proxy_auth_config.py tests\test_vci_proxy_caches.py tests\test_vci_proxy_cache_vbatt.py tests\test_vci_proxy_protocol.py tests\test_reverse_client.py tests\test_reverse_server.py tests\test_j2534_driver.py tests\test_agent_navigator.py tests\test_diagnostics_window.py tests\test_client_gui.py -q
```

### If you change `server/`

Run at least:

```bash
python -m pytest tests\test_server_layout.py tests\test_api_security_guards.py tests\test_runtime_session_layers.py tests\test_runtime_diagnostics_navigation.py -q
```

### If you change documentation structure or repository policy

Run:

```bash
python -m pytest tests\test_repo_layout_consistency.py -q
```

## Registry Runtime Release Gate

Before finalizing GDS2 registry-runtime rollout, run at minimum:

```bash
python -m pytest tests\test_backend_capability_architecture.py tests\test_backend_capability_refactor.py tests\test_runtime_session_layers.py tests\test_runtime_diagnostics_navigation.py tests\test_session_api_handlers.py tests\test_gds2_controller_runtime.py tests\test_navigation_registry_probe.py tests\test_simulated_session_api_flow.py tests\test_simulated_system_flow.py -q
python -m py_compile backends\gds2\backend.py backends\gds2\controller_runtime.py backends\gds2\registry_navigation_runtime.py
```

Operational rollout work should also confirm:

- `navigation_runtime_status` is present in backend state
- route target, recovery actions, recovery action counts, and terminal reason are populated after navigation-heavy operations
- the troubleshooting flow in `agent_docs/ops/gds2_registry_runtime_rollout.md` is sufficient to explain a failed route without interactive debugging

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
- OpenAI-compatible AI config vs legacy provider names
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
