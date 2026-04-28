# RPA_demo AGENTS Guide

This file is the Codex-facing working guide for this repository.

`CLAUDE.md` may still exist as historical context, but prefer:

1. the current codebase
2. this file
3. the authoritative docs under `agent_docs/`

## Read First

When the task touches architecture, backend boundaries, runtime behavior, API design, or product flow, read:

- `README.md`
- `agent_docs/README.md`

Then choose the task-specific docs you need:

- project/repo layout: `agent_docs/core/project_structure.md`
- code package ownership: `agent_docs/core/code_structure.md`
- platform architecture: `agent_docs/core/platform_architecture.md`
- backend/capability model: `agent_docs/core/backend_architecture.md`
- API behavior: `agent_docs/core/api_design.md`
- session/navigation/AI/runtime flow: `agent_docs/core/runtime_flows.md`
- deployment/config/secrets/ports: `agent_docs/ops/deployment_and_operations.md`
- VCI proxy and tunnel internals: `agent_docs/ops/vci_proxy_and_tunnel.md`
- tests and validation: `agent_docs/core/testing_and_quality.md`

## Project Framework

This repository is a cloud remote vehicle diagnostics platform with a shared backend contract layer.

High-level layers:

- `diagnostic_platform/`: platform contracts, backend registry, worker runtime, session/runtime helpers, SSE helpers
- `backends/`: per-backend facades behind the shared contract
- `src/`: GDS2-specific automation, navigation, streaming, diagnosis, and legacy orchestration compatibility exports
- `server/`: supported HTTP surfaces and Flask app bootstrap
- `vci_proxy/`: reverse tunnel, local tray client, J2534 integration, tunnel-quality tracking
- `tests/`: durable regression tests

## Working Rules

- Keep `app.py` and `server/app.py` thin.
- Add new OEM/backend support through `diagnostic_platform/` plus `backends/`, not by hard-coding new behavior into `src/`.
- Treat `src/` as GDS2-specific unless a module is explicitly generalized.
- Preserve the backend contract boundary when changing diagnostics, session, live-data, navigation, or AI flows.
- Use `diagnostic_platform/` as the canonical platform-layer name in code and docs.
- Route supported behavior through:
  - `/api/session/*`
  - `/api/navigate/*`
- Keep durable regression coverage in `tests/`.
- Remove dead code and stale docs when replacing flows.

## Documentation Policy

- The authoritative documentation set lives under `agent_docs/`.
- `README.md` is the entrypoint, not the full design spec.
- `AGENTS.md` should stay lightweight and should link to detailed docs instead of duplicating them.
- Keep docs concise; prefer links to the owning document over duplicated detail.
- When changing routes, runtime behavior, config keys, package ownership, or deployment flow, update the single owning document in the same change.
- Local benchmark artifacts belong under `reports/network_benchmarks/` and are not authoritative design docs.

## Delegation Preference

- The user explicitly authorizes autonomous subagent/delegation use when it materially improves speed, focus, or code quality.
- Choose the most appropriate subagent without requiring a fresh permission request.
- Briefly tell the user which subagents were used and why when delegation is used.
- Do not delegate trivial tasks, and always follow direct user instructions when they prefer no delegation or request a specific agent.
