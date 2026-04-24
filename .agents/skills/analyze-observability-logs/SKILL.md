---
name: analyze-observability-logs
description: Analyze and correlate this repository's cloud and local observability logs. Use when the user asks to inspect cloud logs, local logs, session traces, incident bundles, uploaded artifacts, or compare cloud and local evidence to identify the first failure and likely failure ordering.
---

# Analyze Observability Logs

Use this skill to reconstruct what failed first across the cloud-to-local diagnostics path. The goal is not to summarize one file in isolation. Correlate raw events, assembled traces, incidents, uploads, and outbox state so the result is evidence-first and ordered.

## Read First

Read these documents before interpreting artifacts:

- `agent_docs/ops/product_observability.md`
- `agent_docs/ops/gds2_ab_log_comparison.md`
- `agent_docs/ops/vci_proxy_and_tunnel.md`

They define the artifact layout, join keys, failure domains, and cloud/local tunnel boundaries.

## Default Targets

If the user does not provide paths, start here:

- Cloud mirror in repo: `vci_proxy/cloud_mirror/cloud`
- Local observability on the machine: `%APPDATA%\VCI_Proxy\observability`

Prioritize these artifact types:

- Cloud: `raw/`, `session_traces/`, `incidents/`, `uploads/<client_instance_id>/<connection_epoch>/`, `active_session_snapshot.json`
- Local: `raw/`, `outbox/pending/`, `outbox/uploaded/`, `outbox/artifacts/`, `cloud_mirror/`

If only the repo mirror exists, say so explicitly and do not imply that a live local directory was inspected.

## Workflow

1. Inventory the available artifacts.
   - Identify the newest raw files, session traces, incidents, and uploaded local bundles.
   - Note whether the cloud side contains cloud-first raw events, uploaded local artifacts, or both.

2. Build the timeline before explaining the issue.
   - Use timestamps to bound the relevant test window.
   - Separate the likely incident window from later reconnect noise.

3. Correlate with the observability join keys.
   - `session_id` for business flow
   - `connection_epoch` for tunnel lifecycle
   - `dll_seq` for DLL to server handoff
   - `proxy_seq` for server to reverse client or worker flow
   - `worker_request_id` for worker-side RPC activity

4. Read summaries, then verify them against raw evidence.
   - Review `session_traces/*.json` and `incidents/*.json`.
   - Treat them as derived hints, not ground truth.
   - Flag empty traces, partial traces, or mixed-session assembly as observability-quality issues.

5. Pull the highest-signal raw events.
   - Search for `status":"error"`, `failure_code`, `disconnect`, `connect_failed`, `503`, `403`, `timeout`, `retry`, `blocked`, `j2534_disconnect`, `page_guard`, `recovery_failed`, and `collector`.
   - Cloud API issues usually show up in `server.api` or `server.runtime`.
   - AI or provider issues usually show up in `ai_engine` or `session_runtime`.
   - UI or runtime flow issues usually show up in `gds2_ui_or_agent`, `agent_data_collector`, or `session_runtime`.
   - Tunnel or local hardware issues usually show up in `reverse_client`, `j2534_worker`, and uploaded local artifacts under cloud `uploads/`.

6. Distinguish first failure from downstream symptoms.
   - Identify the earliest abnormal event that changes system state.
   - Do not accept the incident failure domain without checking raw ordering.
   - If the UI reports `j2534_disconnect` before tunnel EOF or connection refusal, say so clearly.

7. Check observability health as part of the diagnosis.
   - Compare local `outbox/pending` versus `outbox/uploaded`.
   - Compare local uploaded manifests versus cloud `uploads/`.
   - Look for missing components, stale snapshots, empty traces, mixed sessions, or malformed client instance IDs.

## High-Signal Heuristics

- Repeated `/api/session/logs/sync` `503` with successful `/api/session/logs/upload` `201` usually means sync and upload fail in different code paths.
- AI `403` usually points to provider, auth, or config issues rather than tunnel instability.
- Clean `j2534_worker` activity with later `reverse_client` disconnect symptoms usually weakens the case for the J2534 driver or VCI being the first failure.
- A cloud UI transition into `j2534_disconnect` before socket-level EOF or refusal suggests the cloud/runtime layer detected breakage before the transport fully collapsed.
- Mixed-session `session_trace` or `incident` artifacts are usually assembly or filtering bugs, not user-flow truth.
- `active_session_snapshot.json` can still be useful when a trace is partial or missing.

## Output Contract

Default output shape:

- `Observations`: what the artifacts directly show
- `Ordering`: failure sequence with timestamps
- `Correlations`: how cloud and local artifacts line up
- `Mismatches`: where traces or incidents disagree with raw evidence
- `Likely explanation`: the strongest evidence-backed explanation
- `Next checks`: the next code path, component, or artifact to inspect

Lead with evidence. Key claims should include timestamps plus the relevant `session_id`, `connection_epoch`, component name, HTTP status, or `failure_code` whenever available.
