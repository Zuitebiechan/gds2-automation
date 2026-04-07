# Test Suite Guide

This README is intentionally narrow in scope.

It covers:

- what the `tests/` directory validates
- how to run the main regression suites
- how to add tests without breaking current project conventions

It does not duplicate architecture or API design. For those, read:

- `../agent_docs/core/platform_architecture.md`
- `../agent_docs/core/backend_architecture.md`
- `../agent_docs/core/api_design.md`
- `../agent_docs/core/testing_and_quality.md`

## Current Test Layout

The current top-level regression suites are:

- `test_backend_capability_architecture.py`: capability-first platform architecture, registry behavior, API integration boundaries
- `test_backend_capability_refactor.py`: backend-neutral session/runtime refactor behavior
- `test_runtime_session_layers.py`: worker/session bindings, status payloads, decisions, abort semantics
- `test_runtime_diagnostics_navigation.py`: diagnostics and navigation runtime behavior
- `test_gds2_controller_runtime.py`: GDS2 backend/controller runtime and alias routing behavior
- `test_tunnel_quality.py`: tunnel quality snapshot normalization and grading
- `test_proxy_benchmark.py`: proxy benchmark event/report behavior
- `test_server_layout.py`: Flask/server layout and blueprint wiring
- `test_repo_layout_consistency.py`: repository structure and documentation consistency guards
- `test_data_viewer_workflow.py`: GDS2 workflow behavior
- `test_read_data_display_agent.py`: read-data workflow wrapper success and error propagation
- `test_interactive_workflow.py`: step-by-step navigation workflow behavior and report parsing
- `test_agent_data_collector.py`: Java Agent data collection behavior
- `test_diagnostic_buffer.py`: sliding-window sampling, delta payload export, and raw-payload caching safety
- `test_agent_navigator.py`: agent command serialization and result handoff behavior
- `test_diagnostics_window.py`: diagnostics window button-state and session-status UI logic
- `test_client_gui.py`: tray-app config persistence, status updates, and diagnostics launch wiring
- `test_device_explorer.py`: Windows Device Explorer selection and convenience flow behavior
- `test_llm_client.py`: diagnostic prompt assembly, streaming fallback, and verdict parsing
- `test_ai_engine.py`: AI payload conversion, cache TTL, confidence capping, and SSE session flow
- `test_vehicle_mapping.py`: persisted vehicle/module/category mapping and discovery helpers
- `test_vci_proxy_auth_config.py`: PSK auth helpers and proxy config mapping
- `test_vci_proxy_caches.py`: `ReadMsgs`/filter/ioctl cache behavior and invalidation
- `test_vci_proxy_cache_vbatt.py`: legacy VBATT cache TTL and invalidation behavior
- `test_vci_proxy_protocol.py`: proxy protocol header and encode/decode regressions
- `test_session_streams.py`: business-session SSE iteration, keepalive behavior, and terminal cleanup
- `test_session_api_handlers.py`: session AI/live-data/navigation handler status mapping and SSE binding
- `test_gds2_orchestration_core.py`: constrained planner, policy guard, and deterministic executor logic
- `test_reverse_client.py`: reverse client registration, dispatch, and prewarm behavior
- `test_reverse_server.py`: reverse server authentication, cache helpers, and tunnel probe behavior
- `test_j2534_driver.py`: J2534 struct conversion, registry discovery, and DLL path selection

## Common Commands

Run the focused architecture/runtime regression set:

```bash
python -m pytest tests\test_backend_capability_architecture.py tests\test_backend_capability_refactor.py tests\test_runtime_session_layers.py tests\test_runtime_diagnostics_navigation.py tests\test_session_streams.py tests\test_session_api_handlers.py tests\test_gds2_orchestration_core.py tests\test_gds2_controller_runtime.py -q
```

Run the focused GDS2 workflow and agent-side regressions:

```bash
python -m pytest tests\test_gds2_controller_runtime.py tests\test_data_viewer_workflow.py tests\test_read_data_display_agent.py tests\test_interactive_workflow.py tests\test_agent_data_collector.py tests\test_diagnostic_buffer.py tests\test_agent_navigator.py tests\test_diagnostics_window.py tests\test_client_gui.py tests\test_device_explorer.py tests\test_llm_client.py tests\test_ai_engine.py tests\test_vehicle_mapping.py -q
```

Run repository/documentation consistency checks:

```bash
python -m pytest tests\test_server_layout.py tests\test_repo_layout_consistency.py -q
```

Run the proxy and driver boundary regressions:

```bash
python -m pytest tests\test_tunnel_quality.py tests\test_proxy_benchmark.py tests\test_vci_proxy_auth_config.py tests\test_vci_proxy_caches.py tests\test_vci_proxy_cache_vbatt.py tests\test_vci_proxy_protocol.py tests\test_reverse_client.py tests\test_reverse_server.py tests\test_j2534_driver.py tests\test_agent_navigator.py tests\test_diagnostics_window.py tests\test_client_gui.py -q
```

Run the full tracked test suite:

```bash
python -m pytest tests -q
```

## Test Conventions

- Prefer focused regression tests next to the area you change.
- Keep durable tests in `tests/`; do not leave one-off scripts or temporary validation files behind.
- When adding platform behavior, prefer backend-neutral tests first, then add GDS2 regression tests only where the behavior is backend-specific.
- When changing documentation structure, update `test_repo_layout_consistency.py` so the documentation policy remains enforced.

## Live vs Mocked Behavior

Most current tests are mocked or isolated runtime regressions.

If you add live or hardware-dependent tests:

- gate them clearly
- keep them opt-in
- do not make the default suite depend on a running GDS2 instance or attached hardware

## Related Docs

- Repository/test policy: `../agent_docs/core/testing_and_quality.md`
- Project structure: `../agent_docs/core/project_structure.md`
- Code ownership boundaries: `../agent_docs/core/code_structure.md`
