"""Tests for the Ollama provisioner.

Nothing here installs Ollama, downloads an archive, touches PATH or the
registry, kills a process or reads the real Ollama install. The CommandRunner
is faked, HTTP is served by an httpx MockTransport, and every path is inside
tmp_path.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx

from local_ai_stack.artifacts import Artifact
from local_ai_stack.download import Downloader
from local_ai_stack.installation import InstallLocation, resolve_installation
from local_ai_stack.markers import MARKER_NAME, write_marker
from local_ai_stack.ollama import EXECUTABLE, OllamaProvisioner
from local_ai_stack.provisioning import (
    ComponentState,
    FailureReason,
    Ownership,
    ProvisionContext,
    ProvisionOutcome,
)
from local_ai_stack.runner import CommandResult, CommandRunner

VERSION_OUTPUT = "ollama version is 0.5.4\n"


def make_archive(*, include_executable: bool = True, extra: dict | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        if include_executable:
            bundle.writestr(EXECUTABLE, "binary-ish content")
        bundle.writestr("lib/runner.dll", "library")
        for name, content in (extra or {}).items():
            bundle.writestr(name, content)
    return buffer.getvalue()


ARCHIVE = make_archive()
ARCHIVE_SHA = hashlib.sha256(ARCHIVE).hexdigest()


def artifact(sha: str | None = ARCHIVE_SHA) -> Artifact:
    return Artifact(
        id="ollama-windows",
        url="https://example.invalid/ollama-windows-amd64.zip",
        filename="ollama-windows-amd64.zip",
        sha256=sha,
        version="0.5.4",
    )


class FakeRunner(CommandRunner):
    """Controls PATH lookups and `--version` answers. Runs nothing."""

    def __init__(
        self,
        *,
        on_path: str | None = None,
        env: Mapping[str, str] | None = None,
        version_output: str | None = VERSION_OUTPUT,
    ):
        super().__init__()
        self.on_path = on_path
        self.fake_env = dict(env or {})
        self.version_output = version_output
        self.calls: list[tuple[str, ...]] = []

    def which(self, name: str, extra_env: Mapping[str, str] | None = None) -> str | None:
        return self.on_path if name == "ollama" else None

    def child_env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        return dict(self.fake_env)

    def run(self, argv: Sequence[str], **kwargs: object) -> CommandResult:
        items = tuple(str(a) for a in argv)
        self.calls.append(items)
        if self.version_output is None:
            return CommandResult(argv=items, returncode=1, stderr="not runnable")
        return CommandResult(argv=items, returncode=0, stdout=self.version_output)


def context_for(tmp_path: Path, runner: CommandRunner | None = None) -> ProvisionContext:
    resolved = resolve_installation(InstallLocation.custom(tmp_path / "stack"))
    return ProvisionContext(installation=resolved, runner=runner or FakeRunner())


def install_managed(context: ProvisionContext, *, marker: bool = True, executable: bool = True):
    directory = context.layout.ollama_app
    directory.mkdir(parents=True, exist_ok=True)
    if executable:
        (directory / EXECUTABLE).write_text("binary", encoding="utf-8")
    if marker:
        write_marker(
            directory,
            component="ollama",
            source_id="ollama-windows",
            version="0.5.4",
            checksum=ARCHIVE_SHA,
        )


def downloader_serving(body: bytes = ARCHIVE, **kwargs) -> Downloader:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    return Downloader(client_factory=lambda: httpx.Client(transport=transport), **kwargs)


# --- nao instalado ---------------------------------------------------------


def test_missing_ollama_is_reported(tmp_path):
    detection = OllamaProvisioner().detect(context_for(tmp_path))
    assert detection.state is ComponentState.NOT_INSTALLED
    assert detection.ownership is Ownership.NONE
    assert detection.path is None


def test_detect_creates_nothing(tmp_path):
    context = context_for(tmp_path)
    OllamaProvisioner().detect(context)
    assert not context.layout.root.exists()
    assert list(tmp_path.iterdir()) == []


# --- deteccao externa ------------------------------------------------------


def test_external_ollama_on_path_is_detected_but_not_owned(tmp_path):
    external = tmp_path / "Programs" / "Ollama" / EXECUTABLE
    external.parent.mkdir(parents=True)
    external.write_text("binary", encoding="utf-8")
    context = context_for(tmp_path, FakeRunner(on_path=str(external)))

    detection = OllamaProvisioner().detect(context)
    assert detection.state is ComponentState.INSTALLED
    assert detection.ownership is Ownership.EXTERNAL
    assert detection.is_external and not detection.is_managed
    assert detection.path == external
    assert detection.version == "0.5.4"


def test_external_ollama_found_through_a_known_install_location(tmp_path):
    local_appdata = tmp_path / "AppData" / "Local"
    external = local_appdata / "Programs" / "Ollama" / EXECUTABLE
    external.parent.mkdir(parents=True)
    external.write_text("binary", encoding="utf-8")
    runner = FakeRunner(env={"LOCALAPPDATA": str(local_appdata)})
    detection = OllamaProvisioner().detect(context_for(tmp_path, runner))
    assert detection.ownership is Ownership.EXTERNAL
    assert detection.path == external


def test_apply_never_takes_ownership_of_an_external_install(tmp_path):
    external = tmp_path / "external" / EXECUTABLE
    external.parent.mkdir(parents=True)
    external.write_text("binary", encoding="utf-8")
    context = context_for(tmp_path, FakeRunner(on_path=str(external)))

    result = OllamaProvisioner(artifact=artifact()).apply(context)

    assert result.outcome is ProvisionOutcome.SKIPPED
    assert result.detection.ownership is Ownership.EXTERNAL
    assert external.read_text(encoding="utf-8") == "binary"
    assert not context.layout.ollama_app.exists()
    assert not (context.layout.root / "apps").exists()


def test_plan_for_an_external_install_proposes_nothing(tmp_path):
    external = tmp_path / "external" / EXECUTABLE
    external.parent.mkdir(parents=True)
    external.write_text("binary", encoding="utf-8")
    context = context_for(tmp_path, FakeRunner(on_path=str(external)))
    plan = OllamaProvisioner(artifact=artifact()).plan(context)
    assert plan.is_noop
    assert plan.detection.is_external


def test_managed_install_can_coexist_when_explicitly_requested(tmp_path):
    external = tmp_path / "external" / EXECUTABLE
    external.parent.mkdir(parents=True)
    external.write_text("binary", encoding="utf-8")
    context = context_for(tmp_path, FakeRunner(on_path=str(external)))

    provisioner = OllamaProvisioner(
        artifact=artifact(),
        downloader=downloader_serving(),
        install_alongside_external=True,
    )
    result = provisioner.apply(context)

    assert result.outcome is ProvisionOutcome.INSTALLED
    assert external.read_text(encoding="utf-8") == "binary"  # externo intocado


# --- deteccao managed ------------------------------------------------------


def test_managed_install_is_detected(tmp_path):
    context = context_for(tmp_path)
    install_managed(context)
    detection = OllamaProvisioner().detect(context)
    assert detection.state is ComponentState.INSTALLED
    assert detection.ownership is Ownership.MANAGED
    assert detection.version == "0.5.4"
    assert detection.path == context.layout.ollama_app / EXECUTABLE


def test_managed_install_wins_over_an_external_one(tmp_path):
    external = tmp_path / "external" / EXECUTABLE
    external.parent.mkdir(parents=True)
    external.write_text("binary", encoding="utf-8")
    context = context_for(tmp_path, FakeRunner(on_path=str(external)))
    install_managed(context)
    assert OllamaProvisioner().detect(context).ownership is Ownership.MANAGED


def test_files_without_a_marker_are_invalid_not_installed(tmp_path):
    """An interrupted install must never look finished."""
    context = context_for(tmp_path)
    install_managed(context, marker=False)
    detection = OllamaProvisioner().detect(context)
    assert detection.state is ComponentState.INVALID
    assert detection.is_usable is False
    assert "interrupted" in detection.detail


def test_marker_without_the_executable_is_invalid(tmp_path):
    context = context_for(tmp_path)
    install_managed(context, executable=False)
    detection = OllamaProvisioner().detect(context)
    assert detection.state is ComponentState.INVALID


def test_corrupt_marker_is_treated_as_no_marker(tmp_path):
    context = context_for(tmp_path)
    install_managed(context)
    (context.layout.ollama_app / MARKER_NAME).write_text("{not json", encoding="utf-8")
    assert OllamaProvisioner().detect(context).state is ComponentState.INVALID


def test_marker_with_a_future_schema_is_rejected(tmp_path):
    context = context_for(tmp_path)
    install_managed(context)
    (context.layout.ollama_app / MARKER_NAME).write_text(
        json.dumps({"schema_version": 99, "component": "ollama", "source_id": "x"}),
        encoding="utf-8",
    )
    assert OllamaProvisioner().detect(context).state is ComponentState.INVALID


# --- verify ----------------------------------------------------------------


def test_verify_confirms_the_executable_answers(tmp_path):
    context = context_for(tmp_path)
    install_managed(context)
    detection = OllamaProvisioner().verify(context)
    assert detection.state is ComponentState.INSTALLED
    assert detection.version == "0.5.4"


def test_verify_marks_a_non_answering_executable_invalid(tmp_path):
    context = context_for(tmp_path, FakeRunner(version_output=None))
    install_managed(context)
    detection = OllamaProvisioner().verify(context)
    assert detection.state is ComponentState.INVALID
    assert "did not answer" in detection.detail


def test_verify_of_a_missing_install_stays_not_installed(tmp_path):
    assert OllamaProvisioner().verify(context_for(tmp_path)).state is ComponentState.NOT_INSTALLED


# --- plano e dry-run -------------------------------------------------------


def test_plan_describes_download_extract_and_marker(tmp_path):
    plan = OllamaProvisioner(artifact=artifact()).plan(context_for(tmp_path))
    assert [step.action for step in plan.steps] == ["download", "extract", "mark-managed"]


def test_plan_without_an_artifact_is_blocked_not_silent(tmp_path):
    plan = OllamaProvisioner().plan(context_for(tmp_path))
    assert [step.action for step in plan.steps] == ["blocked"]
    assert "SHA-256" in plan.steps[0].summary


def test_apply_without_an_artifact_fails_with_a_precondition(tmp_path):
    context = context_for(tmp_path)
    result = OllamaProvisioner().apply(context)
    assert result.outcome is ProvisionOutcome.FAILED
    assert result.failure is not None
    assert result.failure.reason is FailureReason.PRECONDITION
    assert "registry" in result.failure.detail
    assert not context.layout.root.exists()


def test_dry_run_downloads_nothing_and_creates_nothing(tmp_path):
    context = context_for(tmp_path)
    provisioner = OllamaProvisioner(artifact=artifact(), downloader=downloader_serving())
    result = provisioner.apply(context, dry_run=True)
    assert result.outcome is ProvisionOutcome.DRY_RUN
    assert result.steps
    assert not context.layout.root.exists()
    assert list(tmp_path.iterdir()) == []


def test_plan_is_a_noop_once_managed(tmp_path):
    context = context_for(tmp_path)
    install_managed(context)
    assert OllamaProvisioner(artifact=artifact()).plan(context).is_noop


# --- instalacao gerenciada -------------------------------------------------


def test_managed_install_extracts_and_marks(tmp_path):
    context = context_for(tmp_path)
    provisioner = OllamaProvisioner(artifact=artifact(), downloader=downloader_serving())

    result = provisioner.apply(context)

    assert result.outcome is ProvisionOutcome.INSTALLED
    assert result.detection.ownership is Ownership.MANAGED
    assert (context.layout.ollama_app / EXECUTABLE).is_file()
    assert (context.layout.ollama_app / "lib" / "runner.dll").is_file()
    marker = json.loads((context.layout.ollama_app / MARKER_NAME).read_text(encoding="utf-8"))
    assert marker["source_id"] == "ollama-windows"
    assert marker["checksum"] == ARCHIVE_SHA
    assert marker["version"] == "0.5.4"


def test_managed_install_is_idempotent(tmp_path):
    context = context_for(tmp_path)
    provisioner = OllamaProvisioner(artifact=artifact(), downloader=downloader_serving())
    assert provisioner.apply(context).outcome is ProvisionOutcome.INSTALLED
    second = provisioner.apply(context)
    assert second.outcome is ProvisionOutcome.SKIPPED


def test_bad_checksum_installs_nothing(tmp_path):
    context = context_for(tmp_path)
    provisioner = OllamaProvisioner(
        artifact=artifact(sha="f" * 64), downloader=downloader_serving()
    )
    result = provisioner.apply(context)
    assert result.outcome is ProvisionOutcome.FAILED
    assert result.failure is not None
    assert result.failure.reason is FailureReason.CHECKSUM
    assert not (context.layout.ollama_app / EXECUTABLE).exists()
    assert not (context.layout.ollama_app / MARKER_NAME).exists()


def test_network_failure_is_reported_structurally(tmp_path):
    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    downloader = Downloader(
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(explode)),
        max_attempts=1,
    )
    result = OllamaProvisioner(artifact=artifact(), downloader=downloader).apply(
        context_for(tmp_path)
    )
    assert result.outcome is ProvisionOutcome.FAILED
    assert result.failure is not None
    assert result.failure.reason is FailureReason.NETWORK


def test_archive_without_the_executable_fails_without_a_marker(tmp_path):
    body = make_archive(include_executable=False)
    context = context_for(tmp_path)
    provisioner = OllamaProvisioner(
        artifact=Artifact(
            id="ollama-windows",
            url="https://example.invalid/o.zip",
            filename="o.zip",
            sha256=hashlib.sha256(body).hexdigest(),
        ),
        downloader=downloader_serving(body),
    )
    result = provisioner.apply(context)
    assert result.outcome is ProvisionOutcome.FAILED
    assert not (context.layout.ollama_app / MARKER_NAME).exists()
    assert OllamaProvisioner().detect(context).state is not ComponentState.INSTALLED


def test_archive_escaping_the_destination_is_refused(tmp_path):
    body = make_archive(extra={"../../escaped.txt": "nope"})
    context = context_for(tmp_path)
    provisioner = OllamaProvisioner(
        artifact=Artifact(
            id="evil",
            url="https://example.invalid/o.zip",
            filename="o.zip",
            sha256=hashlib.sha256(body).hexdigest(),
        ),
        downloader=downloader_serving(body),
    )
    result = provisioner.apply(context)
    assert result.outcome is ProvisionOutcome.FAILED
    assert result.failure is not None
    assert result.failure.reason is FailureReason.FILESYSTEM
    assert not (tmp_path / "escaped.txt").exists()
    assert not (context.layout.ollama_app / MARKER_NAME).exists()


def test_install_emits_progress_events(tmp_path):
    seen = []
    resolved = resolve_installation(InstallLocation.custom(tmp_path / "stack"))
    context = ProvisionContext(installation=resolved, runner=FakeRunner(), events=seen.append)
    OllamaProvisioner(artifact=artifact(), downloader=downloader_serving()).apply(context)
    assert [event.component_id for event in seen] == ["ollama"] * len(seen)
    assert any("Downloading" in event.message for event in seen)


def test_apply_never_writes_outside_the_install_root(tmp_path):
    context = context_for(tmp_path)
    OllamaProvisioner(artifact=artifact(), downloader=downloader_serving()).apply(context)
    assert {entry.name for entry in tmp_path.iterdir()} == {"stack"}


# --- smoke real, somente leitura -------------------------------------------


def test_real_machine_detection_is_read_only(tmp_path):
    """Detects whatever Ollama this machine really has. Installs nothing."""
    resolved = resolve_installation(InstallLocation.custom(tmp_path / "stack"))
    context = ProvisionContext(installation=resolved, runner=CommandRunner())
    detection = OllamaProvisioner().detect(context)
    assert detection.ownership in (Ownership.NONE, Ownership.EXTERNAL)
    assert not context.layout.root.exists()
    assert list(tmp_path.iterdir()) == []
