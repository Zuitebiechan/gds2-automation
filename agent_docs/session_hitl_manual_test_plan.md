# Session + HITL Manual Test Plan (GDS2)

**Goal**: Verify end-to-end session flow with branch ambiguity handling in client GUI.

## Preconditions

- Cloud services running:
  - `python -m vci_proxy.reverse_server`
  - `python app.py --port 8080`
  - GDS2 started with Java Agent
- Local client GUI can open Diagnostics window
- Session endpoints are available under `/api/session/*`

## Test Matrix

| Scenario | Expected Result |
|---|---|
| Start Session with GM brand | Session status becomes `running`, workflow routed to `gds2` |
| Start Session with unknown brand | Session status becomes `awaiting_decision`, decision modal appears |
| Module ambiguity | Decision modal appears with candidate modules |
| Category ambiguity | Decision modal appears with candidate categories |
| Submit decision | Session resumes automatically without restarting flow |
| Decision timeout | `decision_timeout` handled, fallback applied, flow continues |
| Abort session | Session ends cleanly, controls reset |

## Step-by-step Cases

## Case 1 — GM fast path

1. Open Diagnostics window
2. Enter brand: `Chevrolet`
3. Click **Start Session**
4. Select module in dropdown and click **Select**
5. Select data category and click right-side **Select**

Expected:
- Session hint guides each step
- After category confirm, **Read DTCs / Start Stream / AI Diagnose** become enabled

## Case 2 — Unknown brand decision at start

1. Enter brand: `BMW`
2. Click **Start Session**
3. Decision modal appears (workflow options)
4. Pick one option and submit

Expected:
- Status shows decision submitted and continuation
- Session remains active; UI continues without restart

## Case 3 — Module ambiguity loop

1. Start session
2. Choose a broad/ambiguous module label
3. Click module **Select**
4. Decision modal appears with module candidates
5. Submit one candidate

Expected:
- `/api/session/decision` returns `resumed=true`
- Category list refreshes based on selected module

## Case 4 — Category ambiguity loop

1. Select a broad category name likely to map to multiple options
2. Click category **Select**
3. Decision modal appears with category candidates
4. Submit one candidate

Expected:
- Session resumes to category-selected state
- DTC/Stream/AI buttons become enabled

## Case 5 — Timeout fallback

1. Trigger `decision_required`
2. Do not submit selection
3. Wait for timeout window

Expected:
- `decision_timeout` status displayed
- Fallback option applied automatically
- Session continues (or returns to running)

## Case 6 — Safety guard checks

1. In session mode, choose category but **do not click Select**
2. Try clicking **Read DTCs / Start Stream / AI Diagnose**

Expected:
- Warning shown: category not confirmed
- Session hint tells user to click category **Select** first

## Case 7 — Abort and cleanup

1. While session is running/awaiting decision, click **Abort**

Expected:
- Session status moves to aborted
- Decision modal closes if open
- Session controls reset to idle state

## Evidence to Capture

- Screenshots of each key status transition
- One full event trace from `/api/session/events`
- Any mismatch between hint text and enabled buttons

## Pass Criteria

- All 7 cases pass without app restart
- No dead-end state requiring manual backend reset
- No action enabled before required session confirmation
