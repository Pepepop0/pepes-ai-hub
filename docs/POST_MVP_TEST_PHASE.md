# Post-MVP test phase contract

Status: frozen foundation for parallel implementation

Baseline: `checkpoint-mvp` at `5475810e08274726731bc48cd5bd0dfbdc2ad3f8`

This document defines the internal test architecture after the functional MVP. It
does not add product behavior, a public API, remote administration, VRAM
orchestration, image editing, or maintenance/update features.

The architecture is illustrated in
[post-mvp-test-architecture.html](post-mvp-test-architecture.html). The versioned
JSON beside it is the editable source of that diagram.

## Decisions

- `local_ai_stack.test_contracts` is the shared, transport-neutral contract.
  Neither implementation branch may edit it independently.
- The end-to-end (E2E) harness is the sole scenario executor. The Test Control API schedules
  the harness and exposes its reports; it does not reimplement test phases.
- Both layers call the existing application domains. They never reproduce
  provisioning, model, workflow, configuration, or runtime logic.
- Server-sent events (SSE) are the initial event transport. The required flow is
  server-to-client, so WebSocket command handling would add authority and
  complexity without a current need.
- The Test Control API is an internal, opt-in, loopback-only development tool. It
  is not re-exported from the product package and is not started by the TUI.
- Safe defaults are an ephemeral root, controlled providers, no network, and no
  GPU workload. Real local providers require explicit capabilities.

## Test philosophy

`checkpoint-mvp` is the known-good baseline. Post-MVP tests must prove:

- a fresh managed installation can be created from the repository sources of
  truth;
- a second plan/run is idempotent;
- external services coexist without being adopted or mutated;
- corrupted, incomplete, or unavailable inputs fail closed;
- service lifecycle and readiness use the existing runtime ownership rules;
- model bytes, managed configuration, and workflow graphs retain integrity;
- managed roots, processes, registries, logs, and test state remain isolated;
- cleanup leaves no managed process or unintended managed artifact behind;
- a run is reproducible from its structured report and declared inputs.

Every failure follows the same workflow:

1. Reproduce it with the smallest deterministic scenario.
2. Classify it as a product defect, test defect, environment limitation, or data
   defect.
3. Apply the smallest correct fix in the owning domain. Never weaken a contract
   merely to make a test pass.
4. Add a regression test at the lowest layer that proves the defect, plus an E2E
   scenario when the failure crosses domain boundaries.

## Shared types

`TestRunSpec` declares a known profile ID and an ordered, unique sequence of `TestPhase` values. The
initial phases are:

1. `preflight`
2. `plan`
3. `provision`
4. `start`
5. `acquire_models`
6. `configure`
7. `chat_smoke`
8. `image_smoke`
9. `stop`
10. `cleanup`
11. `second_plan`

The order above describes a complete scenario, not a mandatory implicit
pipeline. Any phase can be requested in isolation. If its verified prerequisite
state does not exist, it returns `BLOCKED`; it must not perform hidden mutation
to manufacture that state.

`TestRootMode` deliberately accepts only `ephemeral` or `configured`. A
configured root is resolved when the harness is constructed from trusted test
configuration. Transport clients cannot submit filesystem paths.

`TestProviderMode` accepts only `controlled` or `local-real`. Controlled
providers are deterministic test doubles at dependency seams. Local-real
providers are observed or controlled only through existing ownership contracts
and only when the caller explicitly enables the necessary network or GPU
capability.

`TestPhaseResult`, `TestRunReport`, and `TestEvent` are immutable values. A report
retains its validated `TestRunSpec` and sanitized run context. Their
`data` mappings must contain JSON-safe, sanitized values. References are opaque
or managed-root-relative identifiers, never arbitrary local paths or secrets.

The harness implements the synchronous `TestHarness.run()` protocol. Scheduling,
threading, HTTP, and event buffering belong to adapters outside the protocol.

## Harness contract

The harness owns scenario ordering, deterministic fixtures, failure injection,
timing, cleanup, and report assembly. It does not own the application behavior it
exercises.

### Mapping to existing domains

| Test concern | Existing source of truth |
|---|---|
| Hardware and GPU facts | `system.detect_system()` |
| Managed preflight | `integration.preflight_managed_stack()` |
| Install plan | `planning.build_install_plan()` and `integration.stage_managed_plan()` |
| Provisioning and plan actions | `integration.plan_executor()`, `default_provisioners()`, and `default_install_actions()` |
| Model availability | `models.ModelResolver` and `ResolvedModel` |
| Managed workflow/config | Existing workflow resolution and `ManagedConfigWriter` |
| Runtime detection | `integration.detect_integration()` |
| Lifecycle and ownership | `integration.build_service_controller()` and `ServiceController` |
| Logs | `logs.read_log_tail()` |

The harness may compose these services, but it must not copy their decisions.
Canonical component IDs, model IDs, runtime locations, workflow bindings,
verification, external classification, and process ownership remain in their
current domains.

### Phase behavior

| Phase | Contract |
|---|---|
| `preflight` | Detect system facts and validate the selected managed root and endpoints without mutation. |
| `plan` | Build and record the initial dry-run plan without executing it. |
| `provision` | Execute only the existing provisioning plan. Controlled mode replaces side-effect providers at their documented seams. |
| `start` | Start only verified managed handoffs through `ServiceController`; external services remain observation-only. |
| `acquire_models` | Execute existing `ACQUIRE_MODEL` actions. Network is a required explicit capability for real providers. |
| `configure` | Resolve required models/workflow and apply only managed configuration through existing actions. |
| `chat_smoke` | Exercise the configured chat route after readiness; record latency and sanitized response evidence. |
| `image_smoke` | Exercise the versioned T2I workflow after readiness; GPU use is a required explicit capability. |
| `stop` | Stop only processes with existing managed ownership proof. |
| `cleanup` | Release test-owned processes and ephemeral-root artifacts; never delete external or configured persistent data. |
| `second_plan` | Build the plan again and prove that verified current state produces no unintended mutation. |

Cleanup runs from a `finally` path after any mutating phase. A cleanup failure is
reported independently and cannot turn an earlier failure into a pass. The
harness uses registry and Job Object proof as-is; it must not add descendant
heuristics, `taskkill /T`, or PID-only authority.

### Failure injection

Injection is allowed only at replaceable dependency seams, including command
execution, HTTP/download transport, `OllamaModelClient`, runtime probes and
launching, managed configuration writes, clocks, and event sinks. It may model
timeouts, short writes, connection loss, checksum mismatch, readiness failure,
or a controlled process crash.

An injection must be named in the report, deterministic, and limited to the test
root or controlled process. It cannot patch ownership checks, substitute an
arbitrary executable, redirect a managed path, or mutate an external provider.

### Structured report

A report has a schema version, opaque run ID, final state, ordered phase results,
UTC start/end times, timings, sanitized evidence, and managed-relative log/result
references. It also records the profile ID, root/provider mode, enabled
capabilities, application commit, platform facts, manifest/workflow digests, and
named failure injection when applicable. Secrets and full model bytes are never
embedded.

## Local Test Control API

The smallest initial surface is:

| Method and path | Purpose |
|---|---|
| `GET /internal/test/v1/health` | Confirm that the opt-in test server itself is responsive. |
| `GET /internal/test/v1/status` | Return sanitized aggregate system, GPU, service, model, and active-run state from existing services. |
| `POST /internal/test/v1/runs` | Validate a `TestRunSpec`, allocate a run ID, and schedule the harness. |
| `GET /internal/test/v1/runs/{run_id}` | Return the current or completed `TestRunReport`. |
| `GET /internal/test/v1/runs/{run_id}/events` | Stream ordered events using SSE. |

Separate `/plan`, `/start`, `/stop`, and smoke command endpoints are intentionally
omitted in the first version. The corresponding typed phases can be submitted as
one-phase runs, so another command surface would duplicate validation and
authority. Additional read-only projections such as services/models/GPU can be
added later only if aggregate status proves insufficient.

The API maps requests to `TestRunSpec` and delegates to `TestHarness`. It may own
authentication, scheduling, serialization, event buffering, run lookup, and
HTTP-specific limits. It may not call a process launcher, provisioner, downloader,
configuration writer, or shell directly.

Only one mutating run may execute at a time. Read-only status and event clients
may be concurrent. An isolated phase uses exactly the same prerequisite and
ownership checks as a complete run.

## Event model

Each `TestEvent` contains an opaque run ID, a per-run sequence starting at 1, an
RFC 3339 UTC timestamp, kind, optional phase, sanitized message, and JSON-safe
data. Defined kinds are run state, phase state, progress, log reference, and
result.

For SSE:

- `id` is the decimal sequence;
- `event` is the `TestEventKind` value;
- `data` is the serialized complete event;
- `Last-Event-ID` resumes from the bounded per-run buffer when available;
- keep-alives carry no application state;
- a final `result` event precedes stream completion.

The harness is the single event producer for a run. The API assigns storage and
fan-out without rewriting event meaning. Logs remain files in the managed test
root; events contain references and bounded tails, not an unbounded log stream.
SSE is output-only and never accepts control commands.

## Security boundary

The Test Control API is safe only under all of these rules:

- it is absent and stopped by default, started through an explicit dev/test
  entry point, and binds to literal `127.0.0.1` by default;
- the initial implementation rejects a non-loopback bind rather than offering a
  dangerous override;
- every route, including health, requires a bearer token or session secret with
  at least 256 bits of entropy;
- tokens are read from a trusted environment/file source, compared in constant
  time, never accepted in a query string, logged, returned, or persisted in a
  report;
- request bodies have a small fixed limit, CORS is disabled, content type is
  checked, and unknown fields or enum values are rejected;
- requests select only known phases, configured root mode, controlled/local-real
  provider mode, known profiles, and existing canonical service IDs;
- requests cannot supply commands, shell fragments, executable paths, working
  directories, environment variable names/values, URLs, or filesystem paths;
- all path resolution remains beneath an already configured managed test root;
- external services are observable but never adopted, stopped, configured,
  overwritten, or used as deletion authority;
- model cleanup never removes an external model, and persistent managed roots
  are not recursively deleted by the API;
- runtime actions retain PID identity, registry, origin, and Job Object checks;
- HTTP errors and cancellation fail closed and do not trigger alternative kill,
  install, or cleanup mechanisms.

Remote use is out of scope. A future client must reach the loopback server through
an authenticated tunnel such as SSH or Tailscale. Direct LAN or public exposure,
port-forwarding the server, and a general-purpose automation API are forbidden.

## Prioritized test matrix

### P0: release-blocking

| Scenario | Controlled setup or injection | Required proof | Primary owner |
|---|---|---|---|
| Clean-root install | Empty ephemeral root, controlled providers | Plan orders valid actions; verified handoffs/config/workflow are created only after real verification | Claude 1 |
| Second run | Run `second_plan` after successful controlled install | No download, reinstall, rewrite, or restart is proposed for verified current state | Claude 1 |
| External Ollama coexistence | Healthy Ollama at configured endpoint with no managed registry | Classified `EXTERNAL`; no adoption, config write, start, stop, or model deletion | Claude 1 |
| External ComfyUI coexistence | Healthy ComfyUI at configured endpoint with no managed registry | Classified `EXTERNAL`; observation-only throughout run and cleanup | Claude 1 |
| Managed port collision | Unrelated listener owns a managed endpoint | Precondition fails before mutation; unrelated PID survives | Claude 1 |
| Corrupted model | Managed file has wrong SHA-256 | Resolver returns invalid/unavailable; workflow/config/smoke remain blocked | Claude 1 |
| Corrupted managed config | Change body while preserving header/marker | Verification fails; readiness/capability is not reported valid | Claude 1 |
| Interrupted model download | Transport stops during a controlled transfer | No partial file is promoted; retry is deterministic; external files untouched | Claude 1 |
| Readiness failure | Managed controlled process never becomes ready | Start reports failure/unknown as appropriate, preserves evidence, then performs safe cleanup | Claude 1 |
| Crash after start | Managed controlled process exits after readiness | State changes without PID reuse/adoption; cleanup releases registry and Job handle | Claude 1 |
| Stop and cleanup | Managed parent and descendants plus unrelated process | Proven Job members stop; unrelated process survives; registry/handle/root cleanup is correct | Claude 1 |
| Invalid workflow | Malformed graph or unresolved declarative binding | Resolver fails closed; image capability and configuration remain disabled | Claude 1 |
| Missing required model | Any required T2I role is `NOT_INSTALLED`, `INVALID`, or `EXTERNAL` | Workflow is not `READY`; no executable/config capability is invented | Claude 1 |
| CUDA unavailable | Controlled facts report no usable CUDA | GPU phase is blocked before workload while CPU/read-only phases stay available | Claude 1 |
| No managed process residual | Invariant checked after every mutating P0 scenario | No owned process, listener, registry entry, or Job handle remains; external processes survive | Claude 1 |
| API authentication and locality | Missing/wrong token and attempted non-loopback bind | Every request is denied without a valid token; server cannot start non-loopback | Claude 2 |
| API authority validation | Malicious path, executable, shell, URL, and unknown ID fields | Input is rejected before harness/domain invocation | Claude 2 |
| API-to-harness equivalence | Submit one-phase and full-run specs | Same domain result/report as direct harness, with no direct API side effects | Claude 2 |
| Event ordering and completion | Concurrent SSE readers, reconnect with `Last-Event-ID` | Monotonic events, no secret leakage, bounded replay, exactly one terminal result | Claude 2 |

All P0 mutating scenarios run with a watchdog and a final independent inventory
check. Any external process death, external write, escaped path, unverified
handoff, or managed residual is an immediate release blocker.

### P1: high-value robustness

- Low disk space before provisioning and during a controlled download.
- Catalog re-pin with old managed bytes present.
- Hub restart and conservative Job lifetime loss.
- PID reuse and stale/mismatched registry records.
- Deleted or stale managed markers and configuration files.
- Concurrent status/event polling during start, smoke, stop, and failure.
- Duplicate run submission, cancellation, client disconnect, and server restart.
- Expired/revoked token, oversized JSON, invalid content type, and replay attempts.
- Cold and warm model verification measurements, including the 12.45 GB artifact.
- Endpoint changes and a mixed managed/external component topology.

### P2: endurance and future-platform evidence

- Long-running chat and image inference, cancellation, and repeated-run soak.
- Large log volume and slow/disconnected SSE consumers.
- More than 1,024 Job members and other documented Windows limits.
- Shutdown, suspend/resume, and user-session termination during a run.
- Multiple supported Windows/Python/PyTorch/CUDA combinations.
- Authenticated tunnel experiments for a future remote client. These cannot
  change the loopback-only server contract.

## Performance baselines

Performance work in this phase records evidence; it does not optimize or define
unsupported pass/fail thresholds.

| Metric | Start | End | Required context |
|---|---|---|---|
| Doctor duration | Immediately before system/service detection | Structured Doctor result available | Cold/warm run, platform, component topology |
| Plan duration | Before profile/system/model resolution | Complete dry-run plan built | Profile, root state, manifest digests |
| Model resolve duration | Before resolving required profile models | All `ResolvedModel` results available | File count/size and verification mode |
| 12.45 GB re-hash | Before opening the file | SHA-256 comparison complete | File size, storage type, cold/warm state; known observation is about 26 seconds |
| Service startup | Launcher accepted a managed start | Existing readiness layer reports ready or timeout | Service ID, endpoint, cold/warm state |
| Chat latency | Smoke request issued | Valid final response received | Model reference and token/request metadata |
| Image latency | Prompt submitted | Valid image/result received | Workflow digest and generation parameters |
| Cleanup latency | Cleanup begins | Inventory proves no managed residual | Number of services/processes/artifacts |

Record monotonic durations per phase and preserve raw observations in the run
report. Summaries should show sample count, median, and maximum without hiding
individual failures.

A future hash cache must preserve these invariants:

- the expected manifest SHA-256 remains the source of truth;
- metadata alone can never make a model `AVAILABLE`;
- cache identity includes canonical model ID, expected digest, normalized managed
  path, file size, and stable file identity/change metadata;
- any identity uncertainty, re-pin, move, timestamp/size change, cache corruption,
  or schema mismatch falls back to a full hash;
- cache updates are versioned and atomic, and a failed update cannot hide an
  invalid model;
- external models are never promoted to managed availability through the cache;
- dry-run and read-only observation do not create or mutate cache state;
- tests corrupt bytes while preserving size and exercise coarse timestamp/file
  replacement cases before the cache can be accepted.

## Parallel ownership

### Claude 1: harness and regression

Exclusive initial files/directories:

- `src/local_ai_stack/e2e/`
- `tests/e2e/`
- `tests/test_e2e_*`
- `docs/testing/e2e-harness.md`

Claude 1 owns the `TestHarness` implementation, scenario runner, controlled
providers, failure injection, regression scenarios, cleanup proofs, and
performance measurement. It may import `test_contracts` and existing domains but
must not edit the Test Control API or shared contract.

### Claude 2: local Test Control API

Exclusive initial files/directories:

- `src/local_ai_stack/test_api/`
- `tests/test_test_api_*`
- `docs/testing/test-control-api.md`

Claude 2 owns loopback binding, opt-in startup, authentication, request/response
validation, harness scheduling, run storage, SSE fan-out/replay, and API mapping.
It may import the `TestHarness` protocol but must not implement phases or call
domain mutation services directly.

The first implementation should avoid a product runtime dependency. A dedicated
`python -m local_ai_stack.test_api` dev/test entry point can remain entirely in
the owned package. If a framework or optional dependency becomes necessary,
Claude 2 must propose the isolated dependency and coordinate the shared
`pyproject.toml` edit before making it.

### Codex: integration authority

Codex owns:

- `src/local_ai_stack/test_contracts.py`
- `tests/test_test_contracts.py`
- this contract and its architecture diagram;
- changes to shared files, security review, cross-branch integration, and final
  test-matrix acceptance.

Neither agent edits product/TUI behavior, `test_contracts.py`, or a shared
manifest merely to satisfy a test. A required shared-contract change is proposed
to Codex with a failing contract test and rationale.

## Integration gates

The branches can develop from the foundation commit in parallel. Merge the
harness first, run the complete suite, then merge the API and run the complete
suite again. Codex adds cross-integration tests only after both branches land.

Acceptance requires:

- all P0 controlled scenarios pass without network or GPU;
- real-provider scenarios remain separately gated and explicitly enabled;
- direct harness and API-triggered reports are semantically equivalent;
- security-negative tests prove rejection before application invocation;
- external processes and data survive every scenario;
- no managed process, registry entry, Job handle, temporary download, or
  ephemeral root remains;
- `pytest`, `ruff check .`, `ruff format --check .`, and `git diff --check` pass;
- product behavior remains unchanged unless a separately reviewed regression fix
  proves a real defect.
