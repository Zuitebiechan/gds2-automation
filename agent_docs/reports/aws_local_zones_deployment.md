# AWS Local Zones Deployment Plan for North America

**Last Updated**: 2026-03-27

## Purpose

This document describes the recommended AWS deployment pattern for this project when serving **North America mechanics** and trying to reduce network latency between:

- the **local mechanic PC** running `vci_proxy.client_gui`
- the **cloud diagnostic worker** running GDS2, the Java Agent, `reverse_server`, and the supported Flask APIs

The main goal is to improve the latency-sensitive parts of the product:

- reverse-tunnel J2534 traffic
- diagnostic workflow API calls
- SSE updates for session/navigation/diagnostics progress
- live-data and DTC interactions that depend on staying close to the cloud worker

## Current Project Reality

The current supported architecture is effectively a **single-worker real-time Windows deployment**:

- there is **no required external database** in the current supported runtime
- supported APIs are still the thin Flask surfaces:
  - `/api/diagnose/*`
  - `/api/navigate/*`
  - `/api/session/*`
- the cloud machine already hosts the latency-sensitive runtime:
  - GDS2
  - Java Agent
  - `vci_proxy.reverse_server`
  - Flask API + SSE endpoints

For that reason, the first AWS Local Zones recommendation is simple:

> For the current product stage, deploy the **entire Windows diagnostic worker** into a Local Zone rather than trying to split the runtime into many cloud services.

## Why Local Zones Fit This Project

AWS Local Zones are most useful when the application must keep a latency-sensitive compute node close to end users. That matches this project well because the tight loop is not a database query or a generic web request. The tight loop is:

```text
Mechanic PC <-> Cloud diagnostic worker <-> GDS2 / Java Agent / reverse tunnel
```

This means the parts that benefit most from Local Zones are the worker-side processes that directly participate in the diagnostic session:

- `reverse_server`
- the Flask API process that serves diagnostics/session/navigation requests
- SSE streams
- GDS2 itself
- the Java Agent feeding GDS2 state

By contrast, `AI Diagnose` includes a 30-second collection window, so total user-visible time there is much less dominated by raw network RTT than the live interactive operations.

## Recommended Deployment Pattern

### Option A: Current-stage single-worker pilot

This is the recommended starting point for the current codebase.

```text
North America mechanic PC
    |
    |  HTTPS / SSE / reverse tunnel
    v
AWS Local Zone Windows EC2
    - GDS2
    - Java Agent
    - reverse_server
    - Flask API
    - diagnostics/session/navigation SSE
```

In this phase, the Local Zone worker contains almost everything that exists in the current deployment model.

### What should run in the Local Zone

Put the full latency-sensitive worker in the Local Zone:

- Windows EC2 instance
- GDS2
- Java Agent writing `~/gds2-data/latest.json`
- `python -m vci_proxy.reverse_server`
- `python app.py --port 8080`
- any helper/runtime pieces directly needed by:
  - `/api/diagnose/*`
  - `/api/navigate/*`
  - `/api/session/*`

### What can stay in the parent Region

Even in the current project stage, some AWS resources should still stay in the parent Region:

- AMIs and EBS snapshots
- CloudWatch logs/metrics/alarms
- Systems Manager (SSM) automation
- build artifacts and optional S3 storage
- future control-plane services, if introduced later

This gives a clean rule:

> Put the **diagnostic session worker** in the Local Zone. Keep **durable infrastructure and management services** in the parent Region.

## Future Pattern: Region control plane + Local Zone workers

If the project later adds multi-user orchestration, worker scheduling, session metadata, or persistent state, the target pattern becomes:

```text
Client App
   |
   v
Global Accelerator / Route 53
   |
   +--> Parent Region control plane
   |      - session allocation
   |      - worker scheduler
   |      - auth / future persistence
   |      - metrics / automation
   |
   +--> Local Zone worker
          - one Windows worker per active session
          - GDS2
          - Java Agent
          - reverse_server
          - Flask API / SSE
```

This matches repository constraints already documented elsewhere:

- one GDS2 instance per machine
- GUI runtime needs an interactive Windows desktop context
- multi-user operation ultimately means one worker per session

## North America Region and Zone Selection

Do not assume one Local Zone will cover all of North America. A Local Zone is best treated as a **metro-area optimization**, not a continent-wide deployment answer.

Recommended selection logic:

### If first users are concentrated in the eastern US

Start with a parent Region in `us-east-1`, then evaluate nearby Local Zones for metros such as:

- New York
- Boston
- Chicago

### If first users are concentrated in the western US

Start with a parent Region in `us-west-2`, then evaluate nearby Local Zones for metros such as:

- Los Angeles
- Seattle

### If users are distributed across the US

Use a staged rollout:

1. one parent Region + one Local Zone pilot
2. validate latency improvement and workflow stability
3. add a second Local Zone only after proving session quality gains

If traffic later spans both coasts, then consider:

- east Region + east Local Zone
- west Region + west Local Zone
- `AWS Global Accelerator` or Route 53 latency-based routing in front

## Deployment Guidance for This Repository

### EC2 worker profile

Use a **Windows EC2 instance in the target Local Zone** as the worker host.

The worker should contain:

- pre-installed GDS2
- Python environment and project checkout
- Java Agent
- registered `virtual_j2534.dll`
- startup scripts for `reverse_server` and Flask API

### Image strategy

Prefer a **pre-baked AMI** over fully bootstrapping each worker from scratch.

That AMI should already contain:

- GDS2
- Python dependencies
- Java runtime and agent setup
- project repo
- any registration steps needed for the virtual DLL

Then Local Zone workers can launch from that AMI with minimal boot-time work.

### Session model

For the current product stage:

- one Windows worker serves one active diagnostic session
- one Local Zone pilot worker is enough for validation

For later scale-out:

- one worker per session remains the safe baseline
- use warm instances if session startup time becomes important

## Networking and Security

Recommended externally reachable ports remain:

- `8080` for Flask API
- `9000` for reverse VCI listener
- `9001` for local proxy listener, if required by the chosen deployment path

Security guidance:

- restrict inbound access to known client IP ranges whenever possible
- prefer a fixed front door instead of exposing many ad-hoc worker IPs
- use PSK auth for the reverse tunnel
- use SSM for administration instead of opening broad RDP exposure

If the product later needs more than one worker, adding `AWS Global Accelerator` in front of worker allocation becomes more attractive than asking users to manage per-worker public endpoints.

## What Improvement To Expect

Do not treat Local Zones as a guarantee that all end-to-end requests will become single-digit milliseconds. The practical expectation for this project is narrower:

- if the mechanic is close to the selected Local Zone metro, the **PC <-> cloud worker** RTT can improve materially
- DTC reads, live-data interactions, and navigation actions should feel more responsive
- `AI Diagnose` improves less dramatically because the 30-second collection window dominates total wall-clock time

For this repository, success should be measured by workflow metrics, not by ping alone.

## Validation Checklist

Before committing to a Local Zone rollout, benchmark these items from real or representative North America client networks:

1. client-to-worker RTT
2. `POST /api/diagnose/start` total time
3. `GET /api/diagnose/dtcs` total time
4. live stream start to first SSE event
5. session start to first useful UI state
6. representative reverse-tunnel request/response timings
7. stability during a 10-20 minute diagnostic session

Suggested rollout sequence:

1. launch one Local Zone Windows worker
2. move the current single-machine deployment onto it unchanged
3. run the benchmark checklist from a North America client
4. compare results against a same-stack Region deployment
5. only then decide whether a second Local Zone is justified

## Important Caveats

- Local Zones are **extensions of a parent Region**, not full standalone Regions.
- Available services and EC2 instance types vary by Local Zone.
- Validate that the chosen Local Zone supports the required Windows EC2 profile before standardizing on it.
- Do not over-split the current architecture. At the current stage, simplicity matters more than theoretical cloud purity.

## Recommended Decision For The Current Codebase

For the current repository and deployment model, the recommended AWS pilot is:

1. choose one North America parent Region
2. choose one nearby Local Zone based on expected mechanic location
3. build or reuse a pre-baked Windows image
4. deploy the **full current worker stack** there:
   - GDS2
   - Java Agent
   - `reverse_server`
   - `app.py` / supported APIs / SSE
5. benchmark real session latency and responsiveness
6. expand only if the Local Zone materially improves the mechanic workflow

That is the simplest Local Zones adoption path that matches how this project actually runs today.

## Official References

- AWS Local Zones FAQ: <https://aws.amazon.com/about-aws/global-infrastructure/localzones/faqs/>
- AWS Local Zones locations: <https://aws.amazon.com/about-aws/global-infrastructure/localzones/locations/>
- AWS Local Zones User Guide: <https://docs.aws.amazon.com/local-zones/latest/ug/what-are-local-zones.html>
- Available Local Zones: <https://docs.aws.amazon.com/local-zones/latest/ug/available-local-zones.html>
- AWS Global Accelerator: <https://docs.aws.amazon.com/global-accelerator/latest/dg/what-is-global-accelerator.html>
- Route 53 latency-based routing: <https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/routing-policy-latency.html>
# Report Notice

This file is a supporting report, not the authoritative source of current architecture or API behavior. For current design docs, start with `agent_docs/README.md`.
