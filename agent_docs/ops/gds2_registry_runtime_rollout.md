# GDS2 Registry Runtime Rollout

## Scope

This document is the operational runbook for final rollout and troubleshooting of the GDS2 registry/path runtime.

It complements:

- `agent_docs/core/backend_architecture.md`
- `agent_docs/core/runtime_flows.md`
- `agent_docs/core/testing_and_quality.md`

## Runtime Source

The production GDS2 runtime source is:

- `registry_runtime`

## Runtime Telemetry

Current backend state now exposes:

- `navigation_runtime_source`
- `clear_dtcs_navigation_runtime_source`
- `recovery_navigation_runtime_source`
- `navigation_runtime_status`

`navigation_runtime_status` is the main structured telemetry object for the active GDS2 runtime.

Key fields include:

- `runtime_source`
- `status`
- `last_operation`
- `last_result`
- `last_route`
- `default_device_name`
- `loading_timeout_sec`
- `max_loading_restarts`

`last_route` is the most useful troubleshooting payload.

Key fields inside `last_route`:

- `route_target_page_key`
- `route_target_category`
- `canonical_path`
- `matched_start_node_id`
- `final_page_id`
- `planned_path`
- `executed_actions`
- `recovery_actions`
- `recovery_action_counts`
- `matched_state_trace`
- `match_diagnostics`
- `terminal_reason`

`matched_state_trace` records the runtime's observed page-state and recovery-policy choices at major recovery points such as:

- `loading`
- `device_explorer`
- `j2534_disconnect`
- `vehicle_selection`

`match_diagnostics` records non-exact or ambiguous action matching decisions. Each item includes:

- `target_label`
- `action_kind`
- `owner`
- `selected_policy`
- `candidate_labels`
- `resolution`

Expected `resolution` values are:

- `normalized_exact`: casing or spacing differed, but one exact normalized label matched
- `unique_contains`: the route used the bounded contains fallback and only one candidate matched
- `ambiguous`: multiple candidates matched; the runtime treats the action as unsafe to click

## Release Checklist

All items below should be green for the current registry-runtime production architecture.

1. The migration regression suite is green:
   `python -m pytest tests\test_backend_capability_architecture.py tests\test_backend_capability_refactor.py tests\test_runtime_diagnostics_navigation.py tests\test_runtime_session_layers.py tests\test_session_api_handlers.py tests\test_gds2_controller_runtime.py tests\test_navigation_registry_probe.py tests\test_simulated_session_api_flow.py tests\test_simulated_system_flow.py -q`
2. A compile sanity pass is green for touched Python files:
   `python -m py_compile backends\gds2\backend.py backends\gds2\controller_runtime.py backends\gds2\registry_navigation_runtime.py`
3. `diagnostic_platform/runtime/*` contains no production imports of `src.navigation` or removed workflow-brain modules.
4. `backends/gds2/*` contains no removed workflow-brain compatibility paths.
5. `backend.get_state().extra["navigation_runtime_status"]` contains the latest route target, recovery actions, and terminal reason after:
   - startup
   - module/category navigation
   - clear DTCs
   - Data Display recovery
6. Probe evidence is green for:
   - diagnostics startup
   - Engine Data navigation
   - DTC Display navigation
   - Clear DTCs execution
   - loading restart
   - J2534 disconnect recovery
   - breadcrumb/path divergence recovery
7. Cloud or staging runs confirm the runtime status payload is sufficient to explain failures without attaching an interactive debugger.

## Recommended Validation Order

1. Run the focused pytest suite.
2. Run `py_compile`.
3. Validate one local or staging GDS2 startup.
4. Validate `Engine Data`.
5. Validate `DTC Display`.
6. Validate `Clear DTCs`.
7. Trigger or simulate recovery-heavy paths:
   - loading
   - disconnect
   - wrong branch / breadcrumb correction
8. Inspect `navigation_runtime_status` after each run.

## Failure Triage

### Route does not converge

Inspect:

- `last_route.route_target_page_key`
- `last_route.matched_start_node_id`
- `last_route.planned_path`
- `last_route.executed_actions`
- `last_route.final_page_id`
- `last_route.match_diagnostics`
- `last_route.terminal_reason`

Typical causes:

- stale graph node match
- wrong entry alias
- unexpected modal or warning dialog
- controller snapshot drift
- ambiguous or overly broad action label match

### Loading page loops or restarts

Inspect:

- `loading_timeout_sec`
- `max_loading_restarts`
- `last_route.recovery_actions`
- `last_route.matched_state_trace`

Typical causes:

- true GDS2 hang
- transition page misclassified as steady state
- restart threshold too low for current infrastructure latency

### Device Explorer or Vehicle Selection stalls

Inspect:

- `default_device_name`
- `matched_state_trace`
- `selected_policy_key`
- `recovery_actions`

Typical causes:

- wrong VCI name
- transient connecting banner lasting longer than expected
- native dialog timing drift

### J2534 disconnect recovery fails

Inspect:

- `matched_state_trace`
- `recovery_actions`
- `recovery_action_counts`
- `terminal_reason`

Typical causes:

- disconnect page not recoverable in-place
- backtrack lands on an unexpected branch
- target data category is no longer restorable

## Rollback Guidance

If rollout confidence drops or field failures spike:

1. keep the current registry-runtime telemetry enabled
2. preserve the failing `navigation_runtime_status` payloads and logs
3. fix the blocking issue on the registry path before the next deployment

Rollback should be treated as a deployment decision, not an in-process fallback to the removed legacy runtime.

## Release Stability Criteria

The current registry-runtime production path should continue to satisfy all of the following:

1. `registry_runtime` is the production source across startup, navigation, clear DTCs, and recovery-heavy paths.
2. No blocking field issues remain on the registry-runtime production path.
3. The rollout checklist above stays green for an agreed burn-in period.
4. Failure triage in real environments can be completed from `navigation_runtime_status` plus logs.
5. Architect review confirms no removed production compatibility path has been reintroduced.
