# Reports Index

This directory indexes curated reports that support engineering decisions, deployment evaluation, or historical investigation.

These files are not the primary source of truth for current architecture or API behavior. Use `agent_docs/core/` and `agent_docs/ops/` for current design and runtime guidance.

## Current Reports

| File | Topic |
| --- | --- |
| `agent_docs/reports/aws_local_zones_deployment.md` | AWS Local Zones deployment notes |
| `agent_docs/reports/network_ms.md` | network measurement notes |
| `agent_docs/reports/texas_to_dallas_local_zone_network_test_report.md` | regional network test report |

## Historical Material

- Historical architecture or prototype notes that are no longer current belong in `agent_docs/archive/`.
- Raw or ad-hoc benchmark artifacts belong in `reports/network_benchmarks/`.

## Usage Rule

If a report conflicts with:

- the current code
- `README.md`
- `AGENTS.md`
- any file under `agent_docs/core/` or `agent_docs/ops/`

then treat the current code and the authoritative docs as correct.
