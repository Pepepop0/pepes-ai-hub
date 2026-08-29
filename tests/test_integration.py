"""Tests for the provisioning -> runtime seam.

Nothing here installs, downloads, starts or stops anything real. Every path is
under tmp_path, the CommandRunner is faked, process and listener scans are
injected, and the only executable ever named is sys.executable, which is a real
file precisely so the launcher's "does it exist" check is exercised without a
process ever being spawned.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from helpers import NOTHING_LISTENING, http_failing, running, tcp_answering

from local_ai_stack.execution import PlanExecutor
from local_ai_stack.installation import InstallLocation, resolve_installation
from local_ai_stack.integration import (
    build_service_controller,
    control_specs_for,
    default_provisioners,
    detect_integration,
    plan_executor,
    provision_context,
)
from local_ai_stack.inventory import LookupOutcome, ProcessInfo, ProcessLookup
from local_ai_stack.launcher import (
    LaunchOutcome,
    ProcessLauncher,
    SpawnedProcess,
    StopOutcome,
)
from local_ai_stack.layout import StackLayout
from local_ai_stack.ollama import EXECUTABLE, MARKER_NAME, ManagedMarker, OllamaProvisioner
from local_ai_stack.planning import PlanAction, build_install_plan
from local_ai_stack.probes import ProbeOutcome
from local_ai_stack.processes import ProcessOrigin, RuntimeProcessState
from local_ai_stack.profiles import load_profile
from local_ai_stack.provisioning import (
    ComponentState,
    FailureReason,
    LaunchAvailability,
    Ownership,
    ProvisionOutcome,
)
from local_ai_stack.registry import ManagedProcessRegistry
from local_ai_stack.runner import CommandResult, CommandRunner
from local_ai_stack.services import OLLAMA_SPEC
from local_ai_stack.stacks import STANDARD_STACK

REPO = Path(__file__).resolve().parents[1]

# A real file, so "the executable exists" is true. It is never started.
REAL_FILE = sys.executable

EXTERNAL_PROC = ProcessInfo(
    4242,
    "ollama.exe",
    r"C:\Users\Someone\AppData\Local\Programs\Ollama\ollama.EXE",
    "ollama.EXE serve",
    100.0,
)


class FakeRunner(CommandRunner):
    """Answers PATH lookups and `--version`. Never runs a real command."""

    def __init__(self, *, on_path: str | None = None, env: Mapping[str, str] | None = None):
        super().__init__()
        self.on_path = on_path
        self.fake_env = dict(env or {})
        self.calls: list[tuple[str, ...]] = []

    def which(self, name: str, extra_env: Mapping[str, str] | None = None) -> str | None:
        return self.on_path if name == "ollama" else None

    def child_env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        return dict(self.fake_env)

    def run(self, argv: Sequence[str], **kwargs: object) -> CommandResult:
        items = tuple(str(a) for a in argv)
        self.calls.append(items)
        return CommandResult(argv=items, returncode=0, stdout="ollama version is 0.5.4\n")


@pytest.fixture
def profile():
    return load_profile(REPO / "profiles" / "pepe.yaml")


def installation_for(tmp_path):
    return resolve_installation(InstallLocation.custom(tmp_path / "stack"))


def context_for(tmp_path, runner: CommandRunner | None = None):
    return provision_context(installation_for(tmp_path), runner=runner or FakeRunner())


def external_context(tmp_path):
    """A context where Ollama exists on this machine but is not ours."""
    external = tmp_path / "Programs" / "Ollama" / EXECUTABLE
    external.parent.mkdir(parents=True, exist_ok=True)
    external.write_text("someone else's binary", encoding="utf-8")
    return context_for(tmp_path, FakeRunner(on_path=str(external))), external


def install_managed(context, *, executable: bool = True, marker: bool = True) -> Path:
    directory = context.layout.ollama_app
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / EXECUTABLE
    if executable:
        target.write_bytes(Path(REAL_FILE).read_bytes()[:64])
    if marker:
        (directory / MARKER_NAME).write_text(
            json.dumps(
                ManagedMarker(
                    schema_version=1,
                    artifact_id="ollama-windows",
                    version="0.5.4",
                    sha256=None,
                    installed_at=0.0,
                ).to_dict()
            ),
            encoding="utf-8",
        )
    return target


# --- A) Ollama externo -----------------------------------------------------


def test_external_ollama_is_external_on_both_sides(tmp_path):
    context, external = external_context(tmp_path)
    provisioner = OllamaProvisioner()

    detection = provisioner.detect(context)
    assert detection.state is ComponentState.INSTALLED
    assert detection.ownership is Ownership.EXTERNAL
    assert detection.path == external

    handoff = provisioner.runtime_handoff(context)
    assert handoff.availability is LaunchAvailability.EXTERNAL
    assert handoff.ownership is Ownership.EXTERNAL
    # The runtime side is never handed a path it could launch.
    assert handoff.executable is None

    # And the runtime's own discovery agrees, with no managed pid claimed.
    controller = make_controller(tmp_path, context, processes=running(EXTERNAL_PROC))
    status = controller.status(OLLAMA_SPEC.id)
    assert status.runtime.state is RuntimeProcessState.RUNNING
    assert status.origin is ProcessOrigin.EXTERNAL
    assert not status.controllable
    assert not status.stoppable


def test_external_ollama_is_never_adopted_as_managed(tmp_path):
    context, _ = external_context(tmp_path)
    snapshot = detect_integration(context)
    ollama = snapshot.by_component("ollama")
    assert ollama is not None
    assert ollama.ownership is Ownership.EXTERNAL
    assert not ollama.controllable
    assert snapshot.controllable_ids == ()

    # No launch spec exists, so nothing about the external binary is launchable.
    specs = control_specs_for(snapshot, layout=context.layout)
    ollama_spec = next(spec for spec in specs if spec.service_id == OLLAMA_SPEC.id)
    assert ollama_spec.launch is None
    assert not ollama_spec.controllable


def test_starting_managed_while_external_runs_is_refused(tmp_path):
    context, _ = external_context(tmp_path)
    controller = make_controller(tmp_path, context, processes=running(EXTERNAL_PROC))
    report = controller.start(OLLAMA_SPEC.id)
    assert report.outcome is LaunchOutcome.SPAWN_FAILED
    assert "no launch spec" in report.launch.detail
    assert report.pid is None


def test_managed_start_conflicts_with_an_already_running_external_ollama(tmp_path):
    """The real conflict: we could launch ours, but theirs already holds the port."""
    context = context_for(tmp_path)
    install_managed(context)
    registry = ManagedProcessRegistry(tmp_path / "state" / "managed-processes.json")
    controller = make_controller(
        tmp_path, context, registry=registry, processes=running(EXTERNAL_PROC)
    )

    report = controller.start(OLLAMA_SPEC.id)
    assert report.outcome is LaunchOutcome.ALREADY_RUNNING_EXTERNAL
    assert report.pid == EXTERNAL_PROC.pid
    assert "refusing to start a second one" in report.launch.detail
    # Nothing was spawned, so nothing was claimed as ours.
    assert registry.load().get(OLLAMA_SPEC.id) is None


def test_stopping_external_ollama_is_refused(tmp_path):
    context, _ = external_context(tmp_path)
    controller = make_controller(tmp_path, context, processes=running(EXTERNAL_PROC))
    result = controller.stop(OLLAMA_SPEC.id)
    assert result.outcome is StopOutcome.NOT_MANAGED
    assert result.pid == EXTERNAL_PROC.pid
    assert "refusing to stop" in result.detail


def test_external_detection_mutates_nothing(tmp_path):
    context, external = external_context(tmp_path)
    before = external.read_text(encoding="utf-8")
    snapshot = detect_integration(context)
    control_specs_for(snapshot, layout=context.layout)
    assert external.read_text(encoding="utf-8") == before
    assert not context.layout.root.exists()


# --- B) Ollama managed -----------------------------------------------------


def test_managed_install_hands_its_executable_to_the_runtime(tmp_path):
    context = context_for(tmp_path)
    executable = install_managed(context)

    handoff = OllamaProvisioner().runtime_handoff(context)
    assert handoff.availability is LaunchAvailability.AVAILABLE
    assert handoff.ownership is Ownership.MANAGED
    assert handoff.executable == executable

    snapshot = detect_integration(context)
    specs = control_specs_for(snapshot, layout=context.layout)
    ollama_spec = next(spec for spec in specs if spec.service_id == OLLAMA_SPEC.id)
    assert ollama_spec.controllable
    assert ollama_spec.launch is not None
    assert ollama_spec.launch.argv == (str(executable), "serve")
    assert ollama_spec.launch.env["OLLAMA_HOST"] == "127.0.0.1:11434"
    # A managed install keeps its models inside the managed layout.
    assert ollama_spec.launch.env["OLLAMA_MODELS"] == str(context.layout.models)
    assert snapshot.controllable_ids == (OLLAMA_SPEC.id,)


def test_managed_start_registers_the_process_as_managed(tmp_path):
    context = context_for(tmp_path)
    install_managed(context)
    registry = ManagedProcessRegistry(tmp_path / "state" / "managed-processes.json")
    controller = make_controller(
        tmp_path, context, registry=registry, processes=running(), spawn_pid=7007
    )

    report = controller.start(OLLAMA_SPEC.id)
    assert report.outcome is LaunchOutcome.STARTED
    assert report.pid == 7007
    entry = registry.load().get(OLLAMA_SPEC.id)
    assert entry is not None
    assert entry.pid == 7007
    assert entry.argv[1] == "serve"


def test_managed_install_without_its_executable_is_not_launchable(tmp_path):
    context = context_for(tmp_path)
    install_managed(context, executable=False)

    handoff = OllamaProvisioner().runtime_handoff(context)
    assert handoff.availability is LaunchAvailability.UNAVAILABLE
    assert handoff.executable is None
    assert "executable is missing" in handoff.detail

    specs = control_specs_for(detect_integration(context), layout=context.layout)
    assert next(s for s in specs if s.service_id == OLLAMA_SPEC.id).launch is None


# --- C) Ollama ausente -----------------------------------------------------


def test_missing_ollama_reports_not_installed_and_invents_no_executable(tmp_path):
    context = context_for(tmp_path)
    handoff = OllamaProvisioner().runtime_handoff(context)
    assert handoff.availability is LaunchAvailability.NOT_INSTALLED
    assert handoff.ownership is Ownership.NONE
    assert handoff.executable is None
    assert "not installed" in handoff.detail


def test_start_is_unavailable_when_ollama_is_not_installed(tmp_path):
    context = context_for(tmp_path)
    controller = make_controller(tmp_path, context, processes=running())
    report = controller.start(OLLAMA_SPEC.id)
    assert report.outcome is LaunchOutcome.SPAWN_FAILED
    assert "no launch spec" in report.launch.detail
    assert report.process_state is RuntimeProcessState.UNKNOWN


def test_observation_only_services_stay_observation_only(tmp_path):
    context = context_for(tmp_path)
    specs = control_specs_for(detect_integration(context), layout=context.layout)
    watched = {spec.service_id: spec for spec in specs}
    assert set(watched) == {"ollama", "open-webui", "comfyui"}
    assert watched["open-webui"].launch is None
    assert watched["comfyui"].launch is None


# --- plan executor ---------------------------------------------------------


def supported_actions(executor: PlanExecutor, plan):
    return {(e.step.action, e.step.target): e.supported for e in executor.resolve(plan)}


def plan_for(profile, tmp_path):
    return build_install_plan(
        profile, installation=installation_for(tmp_path), stack=STANDARD_STACK
    )


def test_prepare_layout_and_ollama_are_the_supported_steps(profile, tmp_path):
    context = context_for(tmp_path)
    executor = plan_executor(context)
    plan = plan_for(profile, tmp_path)
    resolved = supported_actions(executor, plan)

    assert resolved[(PlanAction.PREPARE_LAYOUT, str(context.layout.root))] is True
    assert resolved[(PlanAction.INSTALL, "ollama")] is True
    # Plan targets are profile service keys, which is why Open WebUI appears
    # here as "open_webui" and not as the "open-webui" service id.
    assert resolved[(PlanAction.INSTALL, "open_webui")] is False
    assert resolved[(PlanAction.INSTALL, "comfyui")] is False
    assert resolved[(PlanAction.CHECK, "system")] is False
    assert resolved[(PlanAction.CONFIGURE, profile.id)] is False


def test_unsupported_steps_stay_explicit_and_cover_the_whole_plan(profile, tmp_path):
    executor = plan_executor(context_for(tmp_path))
    plan = plan_for(profile, tmp_path)
    report = executor.execute(plan, dry_run=True)

    assert len(executor.resolve(plan)) == len(plan.steps)
    assert report.unsupported
    for entry in report.unsupported:
        assert entry.reason
    unsupported_targets = {e.step.target for e in report.unsupported}
    # Models and workflows are named, not silently dropped as if they were done.
    assert {"open_webui", "comfyui"} <= unsupported_targets
    assert any(e.action is PlanAction.ACQUIRE_MODEL for e in report.unsupported)
    assert any(e.action is PlanAction.APPLY_WORKFLOW for e in report.unsupported)


def test_dry_run_is_deterministic_and_creates_nothing(profile, tmp_path):
    context = context_for(tmp_path)
    executor = plan_executor(context)
    plan = plan_for(profile, tmp_path)

    first = executor.execute(plan, dry_run=True)
    second = executor.execute(plan, dry_run=True)
    assert first == second
    assert first.dry_run
    assert first.created_directories == context.layout.all_directories
    assert not context.layout.root.exists()
    assert list(tmp_path.iterdir()) == []


def test_dry_run_without_a_pinned_artifact_refuses_with_a_precondition(profile, tmp_path):
    executor = plan_executor(context_for(tmp_path))
    report = executor.execute(plan_for(profile, tmp_path), dry_run=True)
    ollama = next(r for r in report.results if r.component_id == "ollama")
    assert ollama.outcome is ProvisionOutcome.FAILED
    assert ollama.failure is not None
    assert ollama.failure.reason is FailureReason.PRECONDITION
    assert "artifact" in ollama.failure.message


def test_dry_run_leaves_an_external_ollama_untouched(profile, tmp_path):
    context, external = external_context(tmp_path)
    executor = plan_executor(context)
    report = executor.execute(plan_for(profile, tmp_path), dry_run=True)
    ollama = next(r for r in report.results if r.component_id == "ollama")
    assert ollama.outcome is ProvisionOutcome.SKIPPED
    assert "never" in ollama.detection.detail or "untouched" in ollama.detail
    assert external.is_file()
    assert not context.layout.root.exists()


def test_default_registry_ships_ollama_without_an_artifact():
    provisioners = default_provisioners()
    assert set(provisioners) == {"ollama"}
    assert provisioners["ollama"].artifact is None


# --- helpers ---------------------------------------------------------------


def make_controller(
    tmp_path,
    context,
    *,
    registry: ManagedProcessRegistry | None = None,
    processes=None,
    listeners=None,
    spawn_pid: int = 7007,
):
    """A controller wired exactly the way the application wires it."""
    registry = registry or ManagedProcessRegistry(tmp_path / "state" / "managed-processes.json")
    launcher = ProcessLauncher(
        registry,
        spawn=lambda argv, **kwargs: SpawnedProcess(spawn_pid),
        clock=lambda: 10.0,
        monotonic=lambda: 0.0,
        sleep=lambda seconds: None,
    )
    return build_service_controller(
        detect_integration(context),
        registry,
        layout=context.layout,
        launcher=launcher,
        http=http_failing(ProbeOutcome.REFUSED),
        tcp=tcp_answering(ProbeOutcome.REFUSED),
        list_processes=lambda: processes if processes is not None else running(),
        list_listeners_fn=lambda: listeners if listeners is not None else NOTHING_LISTENING,
        lookup_process=lambda pid: ProcessLookup(
            LookupOutcome.FOUND, ProcessInfo(pid, "python.exe", REAL_FILE, "", 100.0)
        ),
        monotonic=lambda: 0.0,
        sleep=lambda seconds: None,
        poll_interval=0.0,
    )


def test_layout_helper_is_not_reimplemented(tmp_path):
    """The seam composes StackLayout; it never re-derives a path itself."""
    context = context_for(tmp_path)
    assert context.layout == StackLayout.from_root(tmp_path / "stack")
    assert OllamaProvisioner().managed_executable(context) == (
        context.layout.ollama_app / EXECUTABLE
    )
