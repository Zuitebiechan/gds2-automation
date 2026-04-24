# GDS2 A/B Log Comparison

This runbook describes how to collect and compare logs when testing direct local GDS2 against cloud GDS2 through the VCI proxy.

## Goal

Use the same vehicle and the same diagnostic workflow twice:

- baseline: direct local GDS2 with the real VCI
- candidate: cloud GDS2 using the virtual J2534 DLL and reverse VCI tunnel

The comparison should answer:

- whether both modes produced equivalent GDS2-native session records
- how much extra latency the cloud path introduced
- whether failures came from GDS2 itself, the tunnel, the local client, the worker, the J2534 driver, or the vehicle/VCI
- which cloud-only advantages were visible, such as structured failure domains and incident bundles

## Collection Flow

Start a run before touching GDS2:

```powershell
python scripts\collect_gds2_ab_run.py start --mode local --label local-engine-data --scenario "Engine Data Data Display"
```

The command prints a run directory such as:

```text
reports\gds2_ab_runs\20260423T101500Z-local-local-engine-data
```

Run the exact diagnostic workflow, then finish collection:

```powershell
python scripts\collect_gds2_ab_run.py finish --run-dir reports\gds2_ab_runs\20260423T101500Z-local-local-engine-data
```

For the cloud run, include session metadata when known:

```powershell
python scripts\collect_gds2_ab_run.py start --mode cloud --label cloud-engine-data --scenario "Engine Data Data Display" --session-id <session_id> --connection-epoch <epoch>
python scripts\collect_gds2_ab_run.py finish --run-dir <cloud-run-dir> --session-id <session_id> --connection-epoch <epoch>
```

If cloud and local logs live on different machines, run the collector on each machine and keep both run directories. The analyzer can still compare the directories later.

## Analysis Flow

Compare baseline and candidate:

```powershell
python scripts\compare_gds2_ab_runs.py --baseline <local-run-dir> --candidate <cloud-run-dir> --report reports\gds2_ab_runs\comparison.md --json reports\gds2_ab_runs\comparison.json
```

The Markdown report is the human-readable review surface. The JSON output keeps the full machine-readable summaries for deeper follow-up.

## What Gets Collected

The collector records file offsets at `start` and copies changed files or appended deltas at `finish`.

Important sources include:

- `%PROGRAMDATA%\RPA_Diagnostic\observability\cloud\raw\*.jsonl`
- `%APPDATA%\VCI_Proxy\observability\raw\*.jsonl`
- `%PROGRAMDATA%\VCI_Proxy\tunnel_quality.json`
- `%USERPROFILE%\gds2-data\latest.json`
- `%USERPROFILE%\gds2-data\vci_proxy_dll.log`
- `%USERPROFILE%\gds2-agent.log`
- `%PROGRAMDATA%\GDS 2\PersistentData\Debug\GDS2Errors_*.log`
- `%PROGRAMDATA%\GDS 2\PersistentData\SessionLogs\**\*.bin`
- `%PROGRAMDATA%\GDS 2\PersistentData\SessionLogs\**\*.zip`
- `%PROGRAMDATA%\GDS 2\PersistentData\SummaryFiles\*.vsf`
- `%PROGRAMDATA%\GDS 2\PersistentData\HardwareUsageLog.txt`
- local tray and worker compatibility logs under `%APPDATA%\VCI_Proxy\`

## Analysis Method

The analyzer uses five evidence lanes:

- Functional equivalence: GDS2 native `SessionLogs` and `SummaryFiles` are matched by VIN, module, application, and machine.
- Cloud overhead: `reverse_server` `proxy.request.response_received` events provide `duration_ms`, `hw_ms`, and `network_ms`.
- Stability: structured event errors and failure domains identify whether the issue belongs to the cloud DLL/proxy, tunnel, local reverse client, worker RPC, local J2534 driver, vehicle/VCI, GDS2 UI/agent, session runtime, or node routing.
- GDS2-native behavior: `GDS2Errors_*.log` keyword and level counts reveal extra native GDS2 state-machine or VCI symptoms.
- Operator impact: tunnel p95 thresholds classify cloud experience as good, warn, or high risk.

Do not treat native GDS2 logs alone as the full answer. They are useful for proving GDS2 behavior, but cloud-specific latency and failure ownership require the structured project observability logs.

## Interpretation Rules

- `network_ms <= 80ms` p95 is good.
- `80ms < network_ms <= 150ms` p95 is warn.
- `network_ms > 150ms` p95 is high risk for real diagnostic experience.
- `cache_hit=true` samples are useful for cache behavior but should not be used as direct tunnel-quality proof.
- Matching GDS2 sessions prove the same functional area was exercised; they do not prove equal responsiveness.
- Extra `cloud_proxy_tunnel`, `local_reverse_client`, `local_worker_rpc`, or `cloud_dll_local_proxy` failures are candidate-only risks.
