# Operation Contract Foundation

Status: frozen for Wave A under Codex authority. Base: master
`a0fa19741ae1049863d905f846e3026d9e93a89d` / `checkpoint-test-infra`.
The checkpoint remains unchanged. This foundation adds values, protocols and a
pure event reducer. It does not connect new behavior to the MVP entry point.
The additive transfer-progress extension is based on master
`740ec74c3e0d279e911542fcbae36a070cab7082` /
`checkpoint-external-conflict-fix`; it remains a contract-only change.
The additive install-observability extension is based on master
`6dcc8807e798fc4bf96ff1a660fc83c5cdbb3dd7` /
`checkpoint-download-progress`. It freezes values and reducer behavior only;
provisioner parsers and TUI rendering remain parallel implementation work.

## Authority and dependencies

The application is the source of truth. A future coordinator produces operation
observations; the application validates them with `advance_operation`, updates
its snapshot, then publishes through `OperationEventSink`. TUI, tests and logs
can consume the same immutable values. Neither a sink nor a transport decides
whether an operation succeeded.

The three contract modules depend only on the standard library and each other.
They contain no TUI import, provider IO, HTTP, process or filesystem control.
The existing `app.py` is the Textual composition entry point; it is not a backend
module to import from a coordinator. The TUI receives projections rather than
`ServiceStatus`, `RuntimeSnapshot`, `SystemFacts` or `WorkflowResolution` objects
with embedded paths, node bindings, endpoints or process information.

## Lifecycle, events and results

`OperationState`: `PREPARING`, `QUEUED`, `RUNNING`, `FINALIZING`, `SUCCEEDED`,
`FAILED`. Only the last two are terminal. No cancellation contract is introduced.
Idle means `current_operation() is None`; it is not an operation lifecycle state.
The last completed snapshot remains available until the application selects a
new operation. Operation IDs are fresh, application/caller-allocated opaque IDs;
a retry creates a new operation and cannot reopen a completed one.

`OperationPhase` names work within that lifecycle: `PREPARATION`,
`RESOURCE_RELEASE`, `SERVICE_START`, `SUBMISSION`, `EXECUTION`, `FINALIZATION`.
VRAM release and service start normally occur while `PREPARING`; they do not
need their own lifecycle states. A terminal event retains the phase where it
finished or failed. A producer reports observed stages, never simulated stages
or completion inferred from an elapsed timer.

| State / phase | Example safe message |
| --- | --- |
| PREPARING / PREPARATION | Preparando geração |
| PREPARING / RESOURCE_RELEASE | Liberando VRAM |
| PREPARING / SERVICE_START | Iniciando serviço |
| QUEUED / SUBMISSION | Imagem na fila |
| RUNNING / EXECUTION | Gerando imagem |
| FINALIZING / FINALIZATION | Finalizando resultado |
| SUCCEEDED / FINALIZATION | Imagem concluída |
| FAILED / phase of failure | Não foi possível concluir a geração |

`OperationEvent` contains operation ID, sequence, kind (`STATE`, `PROGRESS`,
`ACTIVITY`, `DETAIL`, `RESULT`), state, UTC timestamp, phase, message, optional
logical progress, diagnostic, final result, transfer progress, current activity
and timeline detail. The activity and detail fields are appended after every
previous field, preserving positional construction and old producers. Existing
consumers may ignore the additions. Absent progress in an active event means
the current observation is indeterminate; the snapshot's explicitly named
`last_progress` and `last_transfer` remain historical observations and must not
be presented as fresh progress.

The reducer enforces these rules:

1. Start at sequence 1 with `STATE / PREPARING / PREPARATION`.
2. Keep the same operation ID and increase sequence by exactly one. Reject
   duplicate, missing and reordered events before publication. The application
   serializes producers. This reducer provides no locking or worker scheduling.
3. Nonterminal lifecycle states can repeat or advance, including skipping a
   stage not observed by the provider; they cannot regress. Failure is possible
   from any active state. `PROGRESS`, `ACTIVITY` and `DETAIL` cannot change state
   or phase: emit `STATE` for that change first. Phases describe work, not a
   second ordered lifecycle.
4. Exactly one `RESULT` event contains a terminal state and matching result
   identity/state. Neither a terminal state without result nor a premature
   result is valid. Reject **every** event after terminal, including another
   failure, log note or duplicate result. Diagnostic logs outside the operation
   stream can refer to the completed operation without reopening it.

Sequence, not wall-clock time, orders events. Timestamps must be timezone-aware
UTC and may go backwards after a clock adjustment. `OperationSnapshot` retains
the first timestamp, latest event, current activity, last bounded details and
the explicitly named `last_progress` / `last_transfer` observations required by
failure presentation. Duration, when supplied, is a
finite nonnegative number measured with a producer's monotonic clock. A consumer
can measure local elapsed time, but must not infer trustworthy duration by
subtracting adjusted wall-clock timestamps.

`OperationProgress(completed, total=None)` counts nonnegative integer work
units within the current phase. With a known positive total, completed cannot
exceed it and `fraction` is available. With unknown total, `fraction` is `None`;
with no reliable count, omit progress entirely. Counts may restart for a new
piece of phase work; they are not an overall monotonic percentage. Consumers
must not invent a percentage from phases, queue position, node count or timers.

`TransferProgress(completed_bytes, total_bytes=None, bytes_per_second=None)` is
an independent, optional observation for byte transfers. `OperationEvent` adds
it as its last optional field, so existing positional and keyword producers do
not need to change. A `PROGRESS` event may contain logical progress, transfer
progress or both. It must contain at least one of them. Existing consumers may
ignore transfer progress and continue to use the same reducer and terminal
semantics.

Completed bytes are a nonnegative integer. A present total is a positive integer
and cannot be smaller than completed bytes. A present speed is finite and
nonnegative. Percentage is not stored: consumers derive it from `fraction` only
when a total is present. ETA is also derived, as remaining bytes divided by speed,
only when a total and a positive speed are available. Zero or absent speed, or an
unknown total, yields no ETA. Logical work units and transfer bytes must never be
substituted for one another, and neither supplies a fabricated overall install
percentage.

Future UI consumers render these observations as follows; this foundation does
not implement that UI:

- Known total: a determinate bar and derived percentage, followed by transferred
  and total bytes, for example `68%`, `2.77 GiB / 4.07 GiB`; show an observed
  speed such as `24.8 MiB/s` and derived ETA such as `~53 s` only when available.
- Completed bytes without a total: indeterminate `Baixando...`, the transferred
  amount such as `1.73 GiB`, and speed only when available. Never show a bar,
  percentage or ETA for this case.
- No transfer observation: an indeterminate activity indicator such as
  `[>] Instalando dependências...`. Never infer a percentage from elapsed time.

## Install activity and detail timeline

`OperationActivity` is an immutable semantic observation with these fields:

- `category`: one typed `OperationActivityCategory` value;
- `action`: a short identifier such as `creating`, `installing`, `checking` or
  `waiting`;
- `item`: a sanitized display label such as `torch` or `ComfyUI`;
- optional `detail`: one sanitized, printable semantic summary;
- optional `elapsed_seconds`: finite nonnegative elapsed time already measured
  by the producer from a monotonic clock.

The minimum categories are `PREPARATION`, `TRANSFER`, `ENVIRONMENT`,
`DEPENDENCY`, `CONFIGURATION`, `SERVICE`, `READINESS`, `MODEL` and `WORKFLOW`.
They group stable user-facing work; actions remain small semantic identifiers,
not an enum containing every executable command. Together they cover directory
setup, repository preparation, virtual environments, dependencies and the
current package, observable bytes, model acquisition, configuration, service
initialization and readiness.

`activity` is the current item. `detail` is a completed or meaningful timeline
observation and uses the same safe value shape. `ACTIVITY` requires current
activity, and `DETAIL` requires detail. An activity event may also carry logical
progress, transfer progress or both; those observations remain independent and
retain their existing meaning. A `PROGRESS` event still requires logical or
transfer progress, so activity cannot disguise a progress event. No field stores
or derives an overall installation percentage.

`OperationSnapshot.details` is the ordered timeline read model. The reducer adds
`OperationTimelineEntry(sequence, phase, detail)` and retains only the last
`DETAIL_HISTORY_LIMIT = 20` entries. Sequence is the ordering source; wall-clock
timestamps do not determine detail order. The current activity persists until
replaced. `SUCCEEDED` clears it while the bounded detail history may remain.
`FAILED` retains current activity and the last logical and transfer observations
when its result event has no replacements, plus a sanitized typed diagnostic.
Raw traceback text is never a diagnostic or detail.

Elapsed time has one authority: a producer samples its monotonic clock and emits
the resulting `elapsed_seconds`. A UI may instead maintain its own local
monotonic timer when the value is absent. Neither producer nor consumer derives
elapsed time from event wall-clock timestamps.

The future producer boundary is explicit:

`raw command output -> provisioner-specific parser/observer -> sanitized
OperationActivity/detail -> OperationEvent`

Only the provisioner-specific adapter sees subprocess output. It maps allowlisted
facts to categories, action identifiers, display labels and short summaries. It
must not forward secrets, tokens, sensitive absolute paths, full shell commands,
raw tracebacks or unbounded stdout. Shape validation bounds strings but cannot
recognize secrets; sanitization belongs at this adapter boundary. Events retain
no arbitrary payload or stdout collection.

Future TUI consumers implement this frozen presentation contract. Collapsed
installation shows the operation and current semantic work without inventing an
overall percentage, for example:

```text
[>] Instalando ComfyUI 1/3
Instalando dependências
torch
158 MiB transferidos
22.4 MiB/s
Tempo: 00:18
```

An expanded view renders the bounded semantic details as a checklist/timeline,
never raw stdout. Logical progress, transfer progress, indeterminate activity
and timeline details may appear together according to the observations actually
available. On success the active activity disappears and history may remain. On
failure the last activity and progress stay visible with the sanitized
diagnostic; no traceback is rendered.

`OperationResult` requires `SUCCEEDED` or `FAILED`, operation ID, optional opaque
output references, at most eight diagnostics and optional duration.
Failure requires at least one diagnostic. `ImageGenerationResult` specializes
this: success requires at least one completed image reference; failure has no
completed outputs. The coordinator's returned result must be the result carried
by its single terminal event. A completed provider job alone does not prove that
the application has resolved and validated its outputs.

Messages and diagnostic messages are printable single lines, at most 240
characters. Diagnostic codes, IDs and references use
`[A-Za-z0-9][A-Za-z0-9_-]{0,127}`. References are application-owned lookup keys;
they are not file paths, provider filenames, URLs, shell text or capabilities to
execute commands. The application will own resolution of these keys. Do not
copy prompts, raw provider payloads or exception strings into event messages.
Adapters must map errors to safe summaries and approved diagnostic references.
Validation bounds text and reference shape; it does **not** claim to redact
secrets from arbitrary prose. There is no arbitrary `data` dictionary in events.

## Image request and coordinator

`ImageGenerationRequest(profile_id, prompt, workflow="text_to_image", width=1024,
height=1024, steps=20, seed=0)` exposes caller choices already represented by
`WorkflowParameters`. Prompt is nonblank, dimensions and steps are positive
integers, seed is a nonnegative integer, and booleans are not integers here.
No random-seed sentinel or provider-specific dimension/step limits are invented.
Model/workflow compatibility and actual provider limits remain domain checks.

The workflow field names the **profile declaration** `text_to_image`, not an
arbitrary manifest or graph. Only that existing selection is accepted in this
wave. The application resolves
`profile.workflows[request.workflow].manifest_id`; models come from the profile's
existing `image.diffusion`, `image.text_encoder` and `image.vae` requirements and
the manifest's required roles. For Capella this selects `flux2_klein_t2i`.
The request's shape validation does not load or validate a profile's existence.
The coordinator must resolve the profile, model availability and structural
workflow validity with existing domain code before submission. No node IDs,
model override, image input or arbitrary path crosses the request boundary.

`ImageGenerationCoordinator.generate(operation_id, request, *, emit=None)` is
synchronous and returns `ImageGenerationResult`. A worker/composition adapter
supplies scheduling; TUI code must not run it on the render thread. Accepted
operations begin with the start event and end with one result. Expected
operational failures become FAILED results; invalid inputs/programming errors
may raise before acceptance. An observer exception cannot turn a successful
generation into a failed operation or suppress its terminal result.

Future flow: prepare using existing domains → coordinate resources → submit
rendered workflow → observe accepted job → finalize output references → result.
None of those actions is implemented by this foundation.

## Resource and job observation boundaries

`VramSnapshot(observed_at, total_mib, free_mib)` describes one selected GPU.
`used_mib` is derived when known; missing telemetry remains `None`.
`can_fit(required_mib)` returns false for unknown/insufficient free VRAM. It
compares a positive application-owned estimate; it is not a GPU reservation or
a guarantee against a later OOM. Profile minimum hardware capacity is not
automatically the workload's free-memory requirement. Choosing the estimate is
part of Claude 1's resource policy, not a new TUI input or hardware threshold
hardcoded in this foundation.

`ResourceCoordinator` exposes:

- `snapshot()` for the current single-GPU observation;
- `release_conflicting_workload(timeout_seconds=...) -> bool` to request release
  with a positive finite timeout; true means acknowledgement, not free memory;
- `wait_for_available(required_mib, timeout_seconds=...) -> VramSnapshot`, which
  returns the last observed state by its positive finite deadline, including
  insufficient or unknown VRAM. It must not wait indefinitely.

Claude 1's future policy is sufficient VRAM → proceed; insufficient VRAM →
request Ollama workload unload → wait bounded → check the returned snapshot →
proceed or fail with a diagnostic. A failed release or unknown telemetry cannot
be interpreted as permission to proceed. The coordinator applies one overall
monotonic budget across release/wait and passes the remaining budget to each
call. It must not reset the deadline on each retry. No arbitrary process kill,
service stop, Job Object operation, GPU scheduler or multi-GPU policy is exposed.
ComfyUI continues to manage its own resident models internally. Timeout and
unload behavior are protocol obligations to verify in Wave A implementations;
there is no real resource action in these modules.

`ImageJobReceipt(job_id)` means submission was accepted. `job_id` maps to the
existing controlled provider's `prompt_id`, retained only inside the application
and adapter. No submission method is introduced here. The future submit adapter
creates a receipt only after an accepted response.

`ImageJobObserver.observe(receipt, timeout_seconds=...)` performs one bounded
observation, returning `ImageJobObservation`: `QUEUED`, `RUNNING`, `COMPLETED` or
`FAILED`, with optional reliable progress, completed output references and a
diagnostic for failure. This fits the existing controlled submit/result split;
it does not assume a particular real HTTP/WebSocket transport. Missing or
ambiguous data raises `ImageJobObservationError` with a safe diagnostic; it must
not manufacture a queued/completed verdict or assert the provider job failed.
Actual provider job failure is a FAILED observation. A caller can fail the
operation on observation failure without claiming the remote job stopped.
Polling, real submission, result storage and resource release are Wave A work.

## Consumer contract and safe reuse

`ApplicationReadModel` is a protocol that Claude 2 can fake independently:

- `current_operation() -> OperationSnapshot | None`;
- `subscribe(OperationEventSink) -> OperationSubscription`;
- `services_snapshot() -> ServicesSnapshot`;
- `vram_snapshot() -> VramSnapshot`.

The application stores the authoritative snapshot before notifying observers.
Subscription atomically replays the latest event of the selected operation,
if any, then delivers future events in serialized producer order. It does not
replay full history. The initial replay may have sequence greater than 1 and
may be terminal: it seeds a consumer view, not a new producer stream passed to
`advance_operation(None, ...)`. Reading a snapshot then subscribing is safe
because subscription supplies this watermark; consumers deduplicate by
`(operation_id, sequence)`. A new operation ID replaces the selected view.

Consumers return promptly, marshal work onto their own UI thread and do not
raise. The source isolates subscriber exceptions. `close()` is idempotent and
guarantees no callbacks after it returns, including queued callbacks; future
concurrent adapters must prove these synchronization obligations in their tests.
This foundation supplies no broker, durable event log, thread or polling loop.

`ServicesSnapshot` is timestamped and contains only service IDs and the existing
`ServiceState.value` health vocabulary (UP/DOWN/DEGRADED/UNKNOWN). This is a
projection, not a new runtime state machine. A test checks that vocabulary
against the domain enum without importing infrastructure into the contracts.
Services, VRAM and operation snapshots are independently timestamped; the
protocol does not promise an atomic system-wide observation.

Reuse is deliberate: `ProvisionEvent` and `TestEvent` already use callable sinks,
and `DownloadProgress` already supports unknown totals. Their actual models
remain separate: provisioning has component semantics, test events describe
test phases, and download events contain URLs/paths. None should be copied
unfiltered into an operation event. The Test Control API and its 19 integrated
P0 cases remain unchanged. A future bridge may map these operation values
explicitly without replacing the Test API schema.

## Wave A ownership

These paths follow the repository's existing flat backend layout. Planned files
below are reserved names, not implementations created in this foundation.
Use separate worktrees based on the foundation commit; no two agents share a
working tree. For Wave A this table supersedes the earlier MVP ownership table
in `AGENT_WORKFLOW.md`.

| Owner | Exclusive scope |
| --- | --- |
| Codex | Frozen `src/local_ai_stack/operation_contracts.py`, `image_generation_contracts.py`, `application_contracts.py`; `tests/test_operation_contracts.py`; this document; final integration and contract decisions |
| Claude 1 | New `src/local_ai_stack/image_generation.py`, `resource_coordination.py`, `comfyui_jobs.py` if needed; `tests/test_image_generation.py`, `tests/test_resource_coordination.py`, `tests/test_comfyui_jobs.py` |
| Claude 2 | Existing `src/local_ai_stack/tui/**`; `src/local_ai_stack/app.py` for consumer wiring/injection; `tests/test_app.py`, `tests/test_tui_appearance.py`, new `tests/test_tui_operations.py` |

Claude 1 implements the coordinator/resource policy and private provider adapter,
using existing runtime/models/workflows APIs without moving their ownership.
Backend modules must not import `app.py`, `tui`, Textual or Rich. Claude 2 codes
against `ApplicationReadModel` and an injected `ImageGenerationCoordinator`
using fakes first; no backend internals or parallel edits to contract files.
Codex owns concrete cross-branch assembly and any required change to existing
domain files outside the table. Contract changes return to Codex rather than
being implemented independently by both agents.

## Evidence and deferred work

`tests/test_operation_contracts.py` exercises stream ordering, unique terminal
results, immutable shapes, indeterminate progress, request validation against
the current profile/workflow selection, resource observations, receipt/result
distinction and independent fake consumers with replay/unsubscribe/error
isolation. An AST dependency check protects the foundation's import boundary.
These tests prove the foundation and protocol usability, not a concrete
coordinator, subscription scheduler or kernel/provider execution.

Foundation gates on master (2026-09-11): full pytest **1847 passed** in 86.32s,
including 66 new contract tests and the existing integrated 19/19 P0 coverage;
**zero warnings**. Harness temporary roots before/after: **0 → 0**; tracking
ledger **0 → 0**, with no pre-existing directory removed. `ruff check .`,
`ruff format --check .` and `git diff --check` passed. Existing `.serena/`
untracked files were preserved and excluded from the foundation commit.

Deferred: real generation/submission/observation adapters, Ollama unload and
bounded-wait implementation, TUI integration/polish, local-real evidence and
real Windows Job Object proof (P1/local-real). Image edit, packaging, maintenance,
multi-GPU and cancellation are outside this foundation. Neither checkpoint tag
is moved. Stop after this foundation's gates; do not implement Wave A here.
