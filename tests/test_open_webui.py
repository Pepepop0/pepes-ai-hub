"""Tests for the Open WebUI provisioner.

Nothing here creates a real virtual environment, runs pip, reaches PyPI or
touches an existing Open WebUI. The CommandRunner is faked and every path is
inside tmp_path.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from local_ai_stack.installation import InstallLocation, resolve_installation
from local_ai_stack.markers import MARKER_NAME, write_marker
from local_ai_stack.open_webui import (
    COMPONENT_ID,
    DISTRIBUTION,
    OpenWebUIProvisioner,
)
from local_ai_stack.provisioning import (
    ComponentState,
    FailureReason,
    Ownership,
    ProvisionContext,
    ProvisionOutcome,
)
from local_ai_stack.pyenv import PythonEnvironment
from local_ai_stack.runner import CommandResult, CommandRunner

VERSION = "0.11.3"


class FakeRunner(CommandRunner):
    """Fakes interpreter discovery, venv creation, pip and metadata reads."""

    def __init__(
        self,
        *,
        on_path: str | None = None,
        installed_version: str | None = VERSION,
        venv_ok: bool = True,
        pip_ok: bool = True,
        create_files: bool = True,
    ):
        super().__init__()
        self.on_path = on_path
        self.installed_version = installed_version
        self.venv_ok = venv_ok
        self.pip_ok = pip_ok
        self.create_files = create_files
        self.calls: list[tuple[str, ...]] = []

    def which(self, name: str, extra_env: Mapping[str, str] | None = None) -> str | None:
        if name == "open-webui":
            return self.on_path
        return f"C:\\bin\\{name}.exe" if name in {"py", "python"} else None

    def run(self, argv: Sequence[str], **kwargs: object) -> CommandResult:
        items = tuple(str(a) for a in argv)
        self.calls.append(items)

        if "venv" in items:
            if not self.venv_ok:
                return CommandResult(argv=items, returncode=1, stderr="venv refused")
            if self.create_files:
                self._materialize(Path(items[-1]))
            return CommandResult(argv=items, returncode=0)

        if "pip" in items:
            if not self.pip_ok:
                return CommandResult(argv=items, returncode=1, stderr="pip failed")
            if self.create_files:
                self._install_script(Path(items[0]))
            return CommandResult(argv=items, returncode=0)

        if "-c" in items:
            code = items[items.index("-c") + 1]
            if "version_info" in code:
                return CommandResult(argv=items, returncode=0, stdout="3.12\n")
            if "importlib.metadata" in code:
                if self.installed_version is None:
                    return CommandResult(argv=items, returncode=0, stdout="")
                return CommandResult(argv=items, returncode=0, stdout=f"{self.installed_version}\n")
        return CommandResult(argv=items, returncode=0)

    @staticmethod
    def _materialize(venv_root: Path) -> None:
        environment = PythonEnvironment(root=venv_root)
        environment.bin_dir.mkdir(parents=True, exist_ok=True)
        environment.python.write_text("python", encoding="utf-8")

    @staticmethod
    def _install_script(python_path: Path) -> None:
        environment = PythonEnvironment(root=Path(python_path).parent.parent)
        environment.script("open-webui").write_text("console script", encoding="utf-8")

    def ran(self, needle: str) -> bool:
        return any(needle in " ".join(call) for call in self.calls)


def context_for(tmp_path: Path, runner: CommandRunner | None = None) -> ProvisionContext:
    resolved = resolve_installation(InstallLocation.custom(tmp_path / "stack"))
    return ProvisionContext(installation=resolved, runner=runner or FakeRunner())


def pinned(**kwargs) -> OpenWebUIProvisioner:
    return OpenWebUIProvisioner(version=VERSION, **kwargs)


def install_managed(context: ProvisionContext, *, marker: bool = True, script: bool = True):
    directory = context.layout.open_webui_app
    environment = PythonEnvironment.inside(directory)
    environment.bin_dir.mkdir(parents=True, exist_ok=True)
    environment.python.write_text("python", encoding="utf-8")
    if script:
        environment.script("open-webui").write_text("script", encoding="utf-8")
    if marker:
        write_marker(directory, component=COMPONENT_ID, source_id=DISTRIBUTION, version=VERSION)


# --- identidade ------------------------------------------------------------


def test_component_id_is_canonical():
    assert COMPONENT_ID == "open-webui"


def test_requirement_is_pinned_exactly():
    assert pinned().requirement == "open-webui==0.11.3"
    assert OpenWebUIProvisioner().requirement is None


# --- nao instalado ---------------------------------------------------------


def test_missing_open_webui_is_reported(tmp_path):
    detection = pinned().detect(context_for(tmp_path))
    assert detection.state is ComponentState.NOT_INSTALLED
    assert detection.ownership is Ownership.NONE


def test_detect_creates_nothing(tmp_path):
    context = context_for(tmp_path)
    pinned().detect(context)
    assert not context.layout.root.exists()
    assert list(tmp_path.iterdir()) == []


# --- external --------------------------------------------------------------


def test_external_open_webui_is_detected_but_not_owned(tmp_path):
    external = tmp_path / "elsewhere" / "open-webui.exe"
    external.parent.mkdir(parents=True)
    external.write_text("binary", encoding="utf-8")
    context = context_for(tmp_path, FakeRunner(on_path=str(external)))

    detection = pinned().detect(context)
    assert detection.state is ComponentState.INSTALLED
    assert detection.ownership is Ownership.EXTERNAL
    assert detection.path == external


def test_apply_never_takes_ownership_of_an_external_install(tmp_path):
    external = tmp_path / "elsewhere" / "open-webui.exe"
    external.parent.mkdir(parents=True)
    external.write_text("binary", encoding="utf-8")
    runner = FakeRunner(on_path=str(external))
    context = context_for(tmp_path, runner)

    result = pinned().apply(context)

    assert result.outcome is ProvisionOutcome.SKIPPED
    assert result.detection.ownership is Ownership.EXTERNAL
    assert external.read_text(encoding="utf-8") == "binary"
    assert not context.layout.open_webui_app.exists()
    assert not runner.ran("pip")
    assert not runner.ran("venv")


def test_plan_for_an_external_install_proposes_nothing(tmp_path):
    external = tmp_path / "elsewhere" / "open-webui.exe"
    external.parent.mkdir(parents=True)
    external.write_text("binary", encoding="utf-8")
    plan = pinned().plan(context_for(tmp_path, FakeRunner(on_path=str(external))))
    assert plan.is_noop
    assert plan.detection.is_external


def test_managed_install_can_coexist_when_explicitly_requested(tmp_path):
    external = tmp_path / "elsewhere" / "open-webui.exe"
    external.parent.mkdir(parents=True)
    external.write_text("binary", encoding="utf-8")
    context = context_for(tmp_path, FakeRunner(on_path=str(external)))
    result = pinned(install_alongside_external=True).apply(context)
    assert result.outcome is ProvisionOutcome.INSTALLED
    assert external.read_text(encoding="utf-8") == "binary"


# --- managed ---------------------------------------------------------------


def test_managed_install_is_detected(tmp_path):
    context = context_for(tmp_path)
    install_managed(context)
    detection = pinned().detect(context)
    assert detection.state is ComponentState.INSTALLED
    assert detection.ownership is Ownership.MANAGED
    assert detection.version == VERSION


def test_managed_wins_over_external(tmp_path):
    external = tmp_path / "elsewhere" / "open-webui.exe"
    external.parent.mkdir(parents=True)
    external.write_text("binary", encoding="utf-8")
    context = context_for(tmp_path, FakeRunner(on_path=str(external)))
    install_managed(context)
    assert pinned().detect(context).ownership is Ownership.MANAGED


def test_environment_without_a_marker_is_invalid(tmp_path):
    """An interrupted install must never look finished."""
    context = context_for(tmp_path)
    install_managed(context, marker=False)
    detection = pinned().detect(context)
    assert detection.state is ComponentState.INVALID
    assert "interrupted" in detection.detail


def test_marker_without_the_console_script_is_invalid(tmp_path):
    context = context_for(tmp_path)
    install_managed(context, script=False)
    assert pinned().detect(context).state is ComponentState.INVALID


def test_a_marker_from_another_component_is_ignored(tmp_path):
    context = context_for(tmp_path)
    install_managed(context)
    (context.layout.open_webui_app / MARKER_NAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "component": "ollama",
                "source_id": "x",
                "version": None,
                "checksum": None,
                "installed_at": 0.0,
            }
        ),
        encoding="utf-8",
    )
    assert pinned().detect(context).state is ComponentState.INVALID


# --- verify ----------------------------------------------------------------


def test_verify_reads_the_version_from_the_environment(tmp_path):
    context = context_for(tmp_path)
    install_managed(context)
    detection = pinned().verify(context)
    assert detection.state is ComponentState.INSTALLED
    assert detection.version == VERSION


def test_verify_fails_when_the_package_is_absent_from_the_environment(tmp_path):
    context = context_for(tmp_path, FakeRunner(installed_version=None))
    install_managed(context)
    detection = pinned().verify(context)
    assert detection.state is ComponentState.INVALID
    assert DISTRIBUTION in detection.detail


# --- plano e dry-run -------------------------------------------------------


def test_plan_describes_venv_install_and_marker(tmp_path):
    plan = pinned().plan(context_for(tmp_path))
    assert [step.action for step in plan.steps] == [
        "create-venv",
        "install-package",
        "mark-managed",
    ]
    assert plan.steps[1].target == "open-webui==0.11.3"


def test_plan_without_a_pin_is_blocked(tmp_path):
    plan = OpenWebUIProvisioner().plan(context_for(tmp_path))
    assert [step.action for step in plan.steps] == ["blocked"]


def test_apply_without_a_pin_refuses_with_a_precondition(tmp_path):
    context = context_for(tmp_path)
    result = OpenWebUIProvisioner().apply(context)
    assert result.outcome is ProvisionOutcome.FAILED
    assert result.failure is not None
    assert result.failure.reason is FailureReason.PRECONDITION
    assert not context.layout.root.exists()


def test_dry_run_runs_no_pip_and_creates_nothing(tmp_path):
    runner = FakeRunner()
    context = context_for(tmp_path, runner)
    result = pinned().apply(context, dry_run=True)
    assert result.outcome is ProvisionOutcome.DRY_RUN
    assert result.steps
    assert not runner.ran("pip")
    assert not runner.ran("venv")
    assert not context.layout.root.exists()
    assert list(tmp_path.iterdir()) == []


def test_plan_is_a_noop_once_managed(tmp_path):
    context = context_for(tmp_path)
    install_managed(context)
    assert pinned().plan(context).is_noop


# --- instalacao ------------------------------------------------------------


def test_apply_creates_the_environment_and_installs_the_pin(tmp_path):
    runner = FakeRunner()
    context = context_for(tmp_path, runner)

    result = pinned().apply(context)

    assert result.outcome is ProvisionOutcome.INSTALLED
    assert result.detection.ownership is Ownership.MANAGED
    assert runner.ran("-m venv")
    assert runner.ran("open-webui==0.11.3")
    marker = json.loads((context.layout.open_webui_app / MARKER_NAME).read_text(encoding="utf-8"))
    assert marker["component"] == "open-webui"
    assert marker["version"] == VERSION


def test_apply_is_idempotent(tmp_path):
    context = context_for(tmp_path)
    provisioner = pinned()
    assert provisioner.apply(context).outcome is ProvisionOutcome.INSTALLED
    assert provisioner.apply(context).outcome is ProvisionOutcome.SKIPPED


def test_pip_is_never_asked_for_a_floating_version(tmp_path):
    runner = FakeRunner()
    pinned().apply(context_for(tmp_path, runner))
    pip_calls = [c for c in runner.calls if "pip" in c]
    assert pip_calls
    for call in pip_calls:
        assert not any(arg == DISTRIBUTION for arg in call)
        assert "--upgrade" not in call


def test_failed_venv_creation_leaves_no_marker(tmp_path):
    context = context_for(tmp_path, FakeRunner(venv_ok=False))
    result = pinned().apply(context)
    assert result.outcome is ProvisionOutcome.FAILED
    assert result.failure is not None
    assert result.failure.reason is FailureReason.COMMAND_FAILED
    assert not (context.layout.open_webui_app / MARKER_NAME).exists()


def test_failed_pip_leaves_no_marker(tmp_path):
    context = context_for(tmp_path, FakeRunner(pip_ok=False))
    result = pinned().apply(context)
    assert result.outcome is ProvisionOutcome.FAILED
    assert not (context.layout.open_webui_app / MARKER_NAME).exists()
    assert pinned().detect(context).state is not ComponentState.INSTALLED


def test_missing_console_script_after_install_fails(tmp_path):
    class NoScript(FakeRunner):
        @staticmethod
        def _install_script(python_path):
            return None

    context = context_for(tmp_path, NoScript())
    result = pinned().apply(context)
    assert result.outcome is ProvisionOutcome.FAILED
    assert not (context.layout.open_webui_app / MARKER_NAME).exists()


def test_no_supported_interpreter_refuses(tmp_path):
    class NoPython(FakeRunner):
        def which(self, name, extra_env=None):
            return None

        def run(self, argv, **kwargs):
            items = tuple(str(a) for a in argv)
            self.calls.append(items)
            if "-c" in items and "version_info" in items[items.index("-c") + 1]:
                return CommandResult(argv=items, returncode=0, stdout="3.9\n")
            return CommandResult(argv=items, returncode=1)

    import local_ai_stack.pyenv as pyenv_module

    original = pyenv_module.sys.version_info
    context = context_for(tmp_path, NoPython())
    provisioner = pinned()

    # Pretend the running interpreter is out of range too.
    class FakeVersionInfo(tuple):
        major = 3
        minor = 9

    pyenv_module.sys.version_info = FakeVersionInfo((3, 9, 0))
    try:
        result = provisioner.apply(context)
    finally:
        pyenv_module.sys.version_info = original
    assert result.outcome is ProvisionOutcome.FAILED
    assert result.failure is not None
    assert result.failure.reason is FailureReason.PRECONDITION
    assert "3.11" in result.failure.detail


def test_install_stays_inside_the_install_root(tmp_path):
    context = context_for(tmp_path)
    pinned().apply(context)
    assert {entry.name for entry in tmp_path.iterdir()} == {"stack"}


def test_install_emits_progress_events(tmp_path):
    seen = []
    resolved = resolve_installation(InstallLocation.custom(tmp_path / "stack"))
    context = ProvisionContext(installation=resolved, runner=FakeRunner(), events=seen.append)
    pinned().apply(context)
    assert seen
    assert all(event.component_id == "open-webui" for event in seen)


# --- smoke real, somente leitura -------------------------------------------


def test_real_machine_detection_is_read_only(tmp_path):
    resolved = resolve_installation(InstallLocation.custom(tmp_path / "stack"))
    context = ProvisionContext(installation=resolved, runner=CommandRunner())
    detection = pinned().detect(context)
    assert detection.ownership in (Ownership.NONE, Ownership.EXTERNAL)
    assert not context.layout.root.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("component", ["open-webui"])
def test_data_dir_is_the_layout_data_dir(tmp_path, component):
    context = context_for(tmp_path)
    assert pinned().data_dir(context) == context.layout.data / component
