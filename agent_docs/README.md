# Documentation Index

This directory is the authoritative documentation set for this repository.

Use it as the primary source of truth after the code itself. Each document has a single scope to reduce overlap.

## How To Read This Set

- If you need product scope, terminology, or current platform boundaries, read `agent_docs/core/project_overview.md`.
- If you need repository layout or root-directory ownership, read `agent_docs/core/project_structure.md`.
- If you need package ownership or import boundaries, read `agent_docs/core/code_structure.md`.
- If you need end-to-end system architecture, read `agent_docs/core/platform_architecture.md`.
- If you need backend contracts, capabilities, registry behavior, or new OEM onboarding, read `agent_docs/core/backend_architecture.md`.
- If you need supported HTTP routes, payload conventions, SSE semantics, or error behavior, read `agent_docs/core/api_design.md`.
- If you need step-by-step runtime behavior for session, navigation, live data, DTC, AI, and abort flows, read `agent_docs/core/runtime_flows.md`.
- If you need tests, validation policy, or documentation-quality guards, read `agent_docs/core/testing_and_quality.md`.
- If you need environment setup, runtime commands, ports, secrets, or deployment notes, read `agent_docs/ops/deployment_and_operations.md`.
- If you need reverse tunnel, local tray client, virtual DLL, auth, cache, or tunnel-quality details, read `agent_docs/ops/vci_proxy_and_tunnel.md`.
- If you need real-vehicle local-vs-cloud GDS2 log collection and comparison workflow, read `agent_docs/ops/gds2_ab_log_comparison.md`.
- If you need the product-level observability schema, artifact layout, redaction policy, or incident artifact contract, read `agent_docs/ops/product_observability.md`.
- If you need AWS Local Zone node allocation, pool sizing, routing, or cost guidance, read `agent_docs/ops/aws_local_zone_node_allocation.md`.
- If you need GDS2 registry-runtime rollout, rollback, telemetry, or troubleshooting guidance, read `agent_docs/ops/gds2_registry_runtime_rollout.md`.

## Core Docs

| File | Scope | Out of scope |
| --- | --- | --- |
| `agent_docs/core/project_overview.md` | Product definition, supported scope, terminology, current platform model | Repo tree, per-package ownership, route tables |
| `agent_docs/core/project_structure.md` | Repository directories, root files, authoritative vs generated content | Runtime flow, API semantics |
| `agent_docs/core/code_structure.md` | Python package ownership, module boundaries, current dependency direction | Full API reference, deployment steps |
| `agent_docs/core/platform_architecture.md` | End-to-end cloud/local/runtime architecture and worker/session model | Per-endpoint request details |
| `agent_docs/core/backend_architecture.md` | Capability-first backend model, registry, resolution, new backend integration | Full repository map, ops commands |
| `agent_docs/core/api_design.md` | Supported API surfaces, payloads, status/error conventions, SSE | Detailed internals of backend implementation |
| `agent_docs/core/runtime_flows.md` | Business-session lifecycle and execution sequences | Repo ownership or deployment setup |
| `agent_docs/core/testing_and_quality.md` | Test layout, validation commands, quality gates | Full architecture restatement |

## Operations Docs

| File | Scope |
| --- | --- |
| `agent_docs/ops/deployment_and_operations.md` | Environment setup, ports, startup, secrets, logs, build/run conventions |
| `agent_docs/ops/vci_proxy_and_tunnel.md` | Tunnel protocol path, reverse server/client, GUI/tray client, auth, cache, quality tracking |
| `agent_docs/ops/gds2_ab_log_comparison.md` | Real-vehicle A/B log capture workflow and local-vs-cloud comparison method |
| `agent_docs/ops/product_observability.md` | Structured logging schema, artifact layout, redaction rules, and incident artifact contract |
| `agent_docs/ops/aws_local_zone_customer_node.md` | Plain-language deployment guide for one-customer-per-node AWS Local Zone Windows workers |
| `agent_docs/ops/aws_local_zone_node_allocation.md` | Automated Local Zone node allocation, pool strategy, routing, cost model, and rollout difficulty |
| `agent_docs/ops/gds2_registry_runtime_rollout.md` | GDS2 registry-runtime rollout switch, telemetry fields, cutover checklist, rollback, and troubleshooting |

## Supporting Material

- `agent_docs/reports/README.md` indexes curated reports and measurements that support design or deployment decisions.
- `agent_docs/archive/` contains historical material that is kept for context, not for current implementation guidance.
- `reports/network_benchmarks/` stores raw or ad-hoc benchmark artifacts. That directory is not part of the authoritative design documentation set.

## Maintenance Rules

- Put durable design and behavior documentation under `agent_docs/`.
- Keep `README.md` as a project entrypoint, not a full design specification.
- Keep `AGENTS.md` as a lightweight working guide that points here.
- Keep documents concise; prefer one owning document plus links over repeated explanations.
- When updating behavior, update the single owning document instead of copying the same explanation into multiple files.
- When a document needs detail owned elsewhere, reference the target file path directly, for example `agent_docs/core/api_design.md`.
