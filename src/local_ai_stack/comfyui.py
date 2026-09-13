"""ComfyUI provisioner.

Ownership
---------
MANAGED means this hub cloned ComfyUI into ``<layout.apps>/comfyui`` and left a
marker. EXTERNAL covers the ComfyUI Desktop build and any other checkout found
outside the layout — reported, never modified, never removed. The Desktop
install in particular owns its own model tree, which this hub does not touch.

Install strategy
----------------
ComfyUI is distributed as source, so the managed install is a Git checkout
pinned to an exact revision plus a virtual environment beside it:

    <layout.apps>/comfyui          the checkout, detached at the pinned commit
    <layout.apps>/comfyui/.venv    its environment
    <layout.data>/comfyui          runtime data, owned by the runtime layer

The clone is pinned twice. The manifest records the tag *and* the commit that
tag pointed at when it was recorded, and the checkout is verified against that
commit afterwards, so a tag moved upstream is reported rather than followed.

Dependencies
------------
ComfyUI's own requirements.txt leaves torch, torchvision and torchaudio
unversioned, so installing it verbatim would resolve whatever those packages
happen to be today — and on an NVIDIA target, which build of torch to install
is a decision, not a default. Those three are pinned in the manifest instead,
with the index that serves the CUDA wheels; everything else still comes from
the repository's own file, which is itself fixed by the pinned commit.

A component with no dependency pin is still refused with PRECONDITION *before
cloning anything*, so nothing is created and no half-installed checkout is left
behind claiming to be usable.

verify() proves both halves independently: the checkout sits on the pinned
commit, and the environment really holds the pinned distribution at the pinned
version. Either can be true without the other.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .components import ComponentId
from .git_ops import GitClient, GitCloneEvent, GitCloneOutputParser, GitError
from .markers import ManagedMarker, fingerprint_of, marker_path, read_marker, write_marker
from .operation_contracts import OperationActivityCategory, TransferProgress
from .provisioning import (
    ComponentState,
    Detection,
    EventLevel,
    FailureReason,
    Ownership,
    ProvisionContext,
    Provisioner,
    ProvisionPlan,
    ProvisionResult,
    ProvisionStep,
    build_activity,
    environment_failure_message,
    reason_for_pip_failure,
)
from .pyenv import (
    PipEvent,
    PipOutputParser,
    PythonEnvironment,
    PythonEnvironmentError,
    create_environment,
    describe_range,
    distribution_version,
    environment_is_reusable,
    pip_install,
)
from .python_runtime import ManagedPythonRuntime, resolve_interpreter

COMPONENT_ID = str(ComponentId.COMFYUI)
ENTRY_POINT = "main.py"

PYTHON_MINIMUM = (3, 11)
PYTHON_BELOW = (3, 13)

# Where the ComfyUI Desktop build usually lands. Observed only.
EXTERNAL_HINTS = (
    ("LOCALAPPDATA", Path("Programs") / "comfyui-electron"),
    ("LOCALAPPDATA", Path("Comfy-Desktop")),
)


@dataclass(frozen=True)
class DependencyPin:
    """An explicit decision about what to install into the environment.

    `requirements` are passed to pip verbatim and are expected to be pinned by
    whoever wrote them. `index_url` and `extra_index_url` exist because a
    GPU-specific torch build does not come from the default index.
    """

    requirements: tuple[str, ...] = ()
    index_url: str | None = None
    extra_index_url: str | None = None
    use_repository_requirements: bool = False
    verify_distribution: str | None = None
    verify_version: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.requirements and not self.use_repository_requirements

    @classmethod
    def from_pin(cls, pinned) -> DependencyPin:
        """Adopt a catalog pin verbatim. The manifest already validated it."""
        if pinned is None:
            return cls()
        return cls(
            requirements=tuple(pinned.requirements),
            index_url=pinned.index_url,
            extra_index_url=pinned.extra_index_url,
            use_repository_requirements=pinned.use_repository_requirements,
            verify_distribution=pinned.verify_distribution,
            verify_version=pinned.verify_version,
        )

    def pip_arguments(self, checkout: Path) -> tuple[list[str], list[str]]:
        """(extra pip flags, requirement arguments)."""
        flags: list[str] = []
        if self.index_url:
            flags += ["--index-url", self.index_url]
        if self.extra_index_url:
            flags += ["--extra-index-url", self.extra_index_url]
        requirements = list(self.requirements)
        if self.use_repository_requirements:
            requirements += ["-r", str(checkout / "requirements.txt")]
        return flags, requirements


@dataclass
class ComfyUIProvisioner(Provisioner):
    """Clones a pinned ComfyUI and prepares its environment."""

    revision: str | None = None
    commit: str | None = None
    url: str | None = None
    dependencies: DependencyPin = field(default_factory=DependencyPin)
    python_minimum: tuple[int, int] = PYTHON_MINIMUM
    python_below: tuple[int, int] = PYTHON_BELOW
    install_alongside_external: bool = False
    git: GitClient | None = None
    # The packaged product's own Python; None means "search the host", which
    # is what a source checkout does. See python_runtime.py.
    python_runtime: ManagedPythonRuntime | None = None

    component_id = COMPONENT_ID
    display_name = "ComfyUI"
    supports_apply = True

    @property
    def is_pinned(self) -> bool:
        return bool(self.url) and bool(self.revision)

    def _git(self, context: ProvisionContext) -> GitClient:
        return self.git if self.git is not None else GitClient(context.runner)

    # --- identidade do pin -------------------------------------------------

    def dependency_fingerprint(self) -> str:
        """A stable hash of everything this install would be built from.

        Recorded in the marker, so a later run can tell an install produced by
        the current pin from one produced by an older one. Requirements are
        sorted, so reordering the manifest is not mistaken for a new pin, while
        adding, removing or re-versioning anything is.
        """
        return fingerprint_of(
            {
                "component": self.component_id,
                "url": self.url or "",
                "revision": self.revision or "",
                "commit": self.commit or "",
                "requirements": sorted(self.dependencies.requirements),
                "index_url": self.dependencies.index_url or "",
                "extra_index_url": self.dependencies.extra_index_url or "",
                "use_repository_requirements": self.dependencies.use_repository_requirements,
                "python": describe_range(self.python_minimum, self.python_below),
            }
        )

    def matches_pin(self, context: ProvisionContext) -> bool:
        """True when the marker on disk was written by the current pin."""
        marker = self._read_marker(context)
        return marker is not None and marker.matches(self.dependency_fingerprint())

    # --- paths -------------------------------------------------------------

    def managed_dir(self, context: ProvisionContext) -> Path:
        return context.layout.comfyui_app

    def environment(self, context: ProvisionContext) -> PythonEnvironment:
        return PythonEnvironment.inside(self.managed_dir(context))

    def managed_executable(self, context: ProvisionContext) -> Path:
        """The managed interpreter that runs this checkout's entry point."""
        return self.environment(context).python

    def entry_point(self, context: ProvisionContext) -> Path:
        return self.managed_dir(context) / ENTRY_POINT

    def data_dir(self, context: ProvisionContext) -> Path:
        return context.layout.comfyui_data

    def marker_path(self, context: ProvisionContext) -> Path:
        return marker_path(self.managed_dir(context))

    # --- deteccao ----------------------------------------------------------

    def detect(self, context: ProvisionContext) -> Detection:
        """Read-only. Clones nothing, creates nothing, installs nothing."""
        managed = self._detect_managed(context)
        if managed is not None:
            return managed
        external = self._detect_external(context)
        if external is not None:
            return external
        return Detection(
            component_id=self.component_id,
            state=ComponentState.NOT_INSTALLED,
            ownership=Ownership.NONE,
            detail="ComfyUI was not found, managed or external.",
        )

    def _detect_managed(self, context: ProvisionContext) -> Detection | None:
        directory = self.managed_dir(context)
        entry_point = self.entry_point(context)
        marker = self._read_marker(context)

        if marker is None and not entry_point.is_file():
            return None
        if marker is None:
            return Detection(
                component_id=self.component_id,
                state=ComponentState.INVALID,
                ownership=Ownership.MANAGED,
                path=directory,
                detail=(
                    "A managed ComfyUI checkout exists but has no marker file. "
                    "An install was probably interrupted; it is not usable."
                ),
                evidence=(str(directory),),
            )
        missing = [
            str(path)
            for path in (entry_point, self.environment(context).python)
            if not path.is_file()
        ]
        if missing:
            return Detection(
                component_id=self.component_id,
                state=ComponentState.INVALID,
                ownership=Ownership.MANAGED,
                path=directory,
                detail="The managed marker exists but the install is incomplete.",
                evidence=tuple(missing),
            )
        return Detection(
            component_id=self.component_id,
            state=ComponentState.INSTALLED,
            ownership=Ownership.MANAGED,
            version=marker.version,
            path=entry_point,
            detail="Managed by Pepito's AI Hub.",
            evidence=(str(entry_point), str(self.marker_path(context))),
        )

    def _detect_external(self, context: ProvisionContext) -> Detection | None:
        environ = context.runner.child_env()
        for variable, relative in EXTERNAL_HINTS:
            base = environ.get(variable)
            if not base:
                continue
            candidate = Path(base) / relative
            if candidate == self.managed_dir(context) or not candidate.is_dir():
                continue
            return Detection(
                component_id=self.component_id,
                state=ComponentState.INSTALLED,
                ownership=Ownership.EXTERNAL,
                path=candidate,
                detail=(
                    "ComfyUI is already installed outside this hub. "
                    "It is reported only; it is never modified, replaced or removed."
                ),
                evidence=(str(candidate),),
            )
        return None

    def _read_marker(self, context: ProvisionContext) -> ManagedMarker | None:
        return read_marker(self.managed_dir(context), component=self.component_id)

    # --- verificacao -------------------------------------------------------

    def verify(self, context: ProvisionContext) -> Detection:
        """Confirm the checkout sits on the pinned commit and the environment
        really holds the pinned dependency.

        Two independent proofs, because either can be true without the other: a
        correct checkout whose environment never finished installing, or a
        complete environment on a checkout somebody moved.
        """
        detection = self.detect(context)
        if not detection.is_usable or not detection.is_managed:
            return detection

        if self.commit is not None:
            try:
                head = self._git(context).head(self.managed_dir(context))
            except GitError as exc:
                return self._invalid(detection, f"Could not read the checkout revision: {exc}")
            if head != self.commit:
                return self._invalid(
                    detection,
                    f"The checkout is at {head[:12]} but the pin is {self.commit[:12]}. "
                    "It was moved outside this hub.",
                )

        distribution = self.dependencies.verify_distribution
        if distribution is None:
            return detection

        installed = distribution_version(context.runner, self.environment(context), distribution)
        if installed is None:
            return self._invalid(
                detection,
                f"{distribution} is not installed in the managed environment; "
                "the dependency install did not complete.",
            )
        expected = self.dependencies.verify_version
        # A CUDA wheel reports its build as a local version (2.11.0+cu130), so
        # the pin is compared against the release part, not the whole string.
        if expected is not None and installed.split("+")[0] != expected:
            return self._invalid(
                detection,
                f"{distribution} {installed} is installed but the pin is {expected}.",
            )
        return Detection(
            component_id=detection.component_id,
            state=ComponentState.INSTALLED,
            ownership=detection.ownership,
            version=detection.version,
            path=detection.path,
            detail=detection.detail,
            evidence=(*detection.evidence, f"{distribution}=={installed}"),
        )

    def _invalid(self, detection: Detection, detail: str) -> Detection:
        return Detection(
            component_id=self.component_id,
            state=ComponentState.INVALID,
            ownership=detection.ownership,
            path=detection.path,
            detail=detail,
            evidence=detection.evidence,
        )

    # --- plano -------------------------------------------------------------

    def plan(self, context: ProvisionContext) -> ProvisionPlan:
        detection = self.detect(context)
        # A managed install is only nothing-to-do when it came from this pin.
        # Under a changed pin it has work to do, and the plan must say so.
        if detection.is_managed and detection.is_usable and self.matches_pin(context):
            return ProvisionPlan(self.component_id, (), detection)
        if detection.is_external and not self.install_alongside_external:
            return ProvisionPlan(self.component_id, (), detection)

        blocker = self._blocker()
        if blocker is not None:
            return ProvisionPlan(
                self.component_id,
                (ProvisionStep(action="blocked", target=self.component_id, summary=blocker),),
                detection,
            )

        directory = self.managed_dir(context)
        return ProvisionPlan(
            self.component_id,
            (
                ProvisionStep(
                    action="clone",
                    target=str(directory),
                    summary=f"Clone {self.url} and check out {self.revision}.",
                ),
                ProvisionStep(
                    action="create-venv",
                    target=str(self.environment(context).root),
                    summary=f"Create a Python {describe_range(self.python_minimum, self.python_below)} environment.",
                ),
                ProvisionStep(
                    action="install-requirements",
                    target=self.component_id,
                    summary="Install the pinned dependencies into the environment.",
                ),
                ProvisionStep(
                    action="mark-managed",
                    target=str(self.marker_path(context)),
                    summary="Write the managed marker so this install is provably ours.",
                ),
            ),
            detection,
        )

    def _blocker(self) -> str | None:
        if not self.is_pinned:
            return (
                "No ComfyUI revision is pinned, so nothing can be cloned. "
                "Pin a url and a revision in the manifest."
            )
        if self.dependencies.is_empty:
            return (
                "ComfyUI's own requirements leave torch unpinned, so no dependency set "
                "is installed by default. Supply an explicit dependency pin."
            )
        return None

    # --- aplicacao ---------------------------------------------------------

    def apply(self, context: ProvisionContext, *, dry_run: bool = False) -> ProvisionResult:
        context.emit(
            self.component_id,
            "Checking existing installation",
            activity=build_activity(
                OperationActivityCategory.PREPARATION, "checking", self.display_name
            ),
        )
        plan = self.plan(context)
        detection = plan.detection

        reinstall_reason = ""
        if detection.is_managed and detection.is_usable:
            if self.matches_pin(context):
                # Same pin: only a passing verification earns a skip. A managed
                # install whose environment is broken is repaired, not accepted.
                verified = self.verify(context)
                if verified.is_usable:
                    return self.skipped(
                        verified, "A managed ComfyUI built from this pin is already installed."
                    )
                reinstall_reason = f"Reinstalling: {verified.detail}"
            else:
                # The pin changed. Skipping here would make a re-pin look like a
                # no-op, which is exactly how a version bump gets lost.
                marker = self._read_marker(context)
                previous = marker.version if marker is not None else "an unknown revision"
                reinstall_reason = (
                    f"The installed ComfyUI was built from {previous}, "
                    f"not from the current pin {self.revision}. Reinstalling."
                )

        if detection.is_external and not self.install_alongside_external:
            return self.skipped(
                detection,
                (
                    f"ComfyUI is already installed externally at {detection.path}. "
                    "Leaving it untouched; this hub takes no ownership of it."
                ),
            )

        blocker = self._blocker()
        if blocker is not None:
            # Refused before anything is cloned, so no partial checkout is left.
            return self.failed(
                detection,
                FailureReason.PRECONDITION,
                blocker,
                (
                    "Nothing was created. A managed install must be reproducible, "
                    "which an unpinned dependency set is not."
                ),
            )

        git = self._git(context)
        if not git.available():
            return self.failed(
                detection,
                FailureReason.PRECONDITION,
                "Git is required to install ComfyUI and was not found on PATH.",
            )

        context.emit(
            self.component_id,
            "Locating a supported Python interpreter",
            activity=build_activity(OperationActivityCategory.ENVIRONMENT, "locating", "Python"),
        )
        resolution = resolve_interpreter(
            context,
            self.component_id,
            minimum=self.python_minimum,
            below=self.python_below,
            runtime=self.python_runtime,
            dry_run=dry_run,
        )
        if resolution.interpreter is None:
            assert resolution.reason is not None
            return self.failed(
                detection,
                resolution.reason,
                resolution.message,
                resolution.detail
                or f"ComfyUI requires Python {describe_range(self.python_minimum, self.python_below)}.",
            )
        interpreter = resolution.interpreter

        if dry_run:
            intent = f"Would install ComfyUI {self.revision} into {self.managed_dir(context)}."
            return self.dry_run_result(
                detection,
                plan.steps,
                f"{reinstall_reason} {intent}".strip(),
            )

        if reinstall_reason:
            context.emit(self.component_id, reinstall_reason)
        return self._install(context, detection, plan, git, interpreter)

    def _install(self, context, detection, plan, git, interpreter) -> ProvisionResult:
        directory = self.managed_dir(context)
        environment = self.environment(context)
        repo_name = _repo_name(self.url)
        context.emit(
            self.component_id,
            f"Preparing the {self.display_name} repository",
            activity=build_activity(
                OperationActivityCategory.PREPARATION,
                "preparing",
                f"repositório {self.display_name}",
            ),
        )
        try:
            context.layout.ensure()
        except OSError as exc:
            return self.failed(
                detection, FailureReason.FILESYSTEM, "Could not prepare the layout.", str(exc)
            )

        context.emit(
            self.component_id,
            f"Cloning {self.url} at {self.revision}",
            activity=build_activity(OperationActivityCategory.TRANSFER, "cloning", repo_name),
        )
        on_clone_output = GitCloneOutputParser(self._git_progress(context, repo_name)).feed
        try:
            if not git.is_repository(directory):
                git.clone(self.url, directory, revision=self.revision, on_output=on_clone_output)
            else:
                # Re-pinning to a newer tag needs it fetched first. fetch()
                # refuses a dirty tree, so local edits are never discarded.
                git.fetch(directory)
                git.checkout(directory, self.revision)
        except GitError as exc:
            return self.failed(
                detection, FailureReason.COMMAND_FAILED, "Could not clone ComfyUI.", str(exc)
            )

        if self.commit is not None:
            try:
                head = git.head(directory)
            except GitError as exc:
                return self.failed(
                    detection,
                    FailureReason.COMMAND_FAILED,
                    "Could not read the checkout.",
                    str(exc),
                )
            if head != self.commit:
                return self.failed(
                    detection,
                    FailureReason.PRECONDITION,
                    f"{self.revision} resolved to {head[:12]}, not the pinned {self.commit[:12]}.",
                    "The tag moved upstream. No marker was written; re-pin deliberately.",
                )

        if environment_is_reusable(context.runner, environment, interpreter):
            # A retry over a partial install: the environment already works,
            # so it is kept, and only what is missing after it is redone.
            context.emit(
                self.component_id,
                "Reusing the existing virtual environment",
                activity=build_activity(
                    OperationActivityCategory.ENVIRONMENT, "reusing", "ambiente virtual"
                ),
            )
        else:
            context.emit(
                self.component_id,
                f"Creating the environment at {environment.root}",
                activity=build_activity(
                    OperationActivityCategory.ENVIRONMENT, "creating", "ambiente virtual"
                ),
            )
            try:
                create_environment(context.runner, environment, interpreter)
            except PythonEnvironmentError as exc:
                reason, message = environment_failure_message(
                    "Could not create the environment.", exc.failure
                )
                return self.failed(detection, reason, message, str(exc))
        context.emit(
            self.component_id,
            "Validating the virtual environment",
            activity=build_activity(
                OperationActivityCategory.ENVIRONMENT, "validating", "ambiente virtual"
            ),
        )
        if not environment.exists:
            return self.failed(
                detection,
                FailureReason.FILESYSTEM,
                "The virtual environment was not created.",
            )
        context.emit(
            self.component_id,
            "Virtual environment ready",
            activity=build_activity(
                OperationActivityCategory.ENVIRONMENT, "ready", "ambiente virtual"
            ),
            milestone=True,
        )

        flags, requirements = self.dependencies.pip_arguments(directory)
        context.emit(
            self.component_id,
            "Installing pinned dependencies",
            activity=build_activity(
                OperationActivityCategory.DEPENDENCY, "installing", "dependências"
            ),
        )
        start = context.monotonic()
        on_pip_output = PipOutputParser(self._pip_progress(context, start)).feed
        try:
            pip_install(
                context.runner, environment, requirements, extra_args=flags, on_output=on_pip_output
            )
        except PythonEnvironmentError as exc:
            message = "Could not install dependencies."
            if exc.failure is not None:
                message = f"{message} {exc.failure.summary}"
            return self.failed(
                detection,
                reason_for_pip_failure(exc.failure),
                message,
            )

        if not self.entry_point(context).is_file():
            return self.failed(
                detection,
                FailureReason.FILESYSTEM,
                f"The checkout has no {ENTRY_POINT}.",
                f"Looked in {directory}.",
            )

        context.emit(
            self.component_id,
            f"Finalizing {self.display_name} installation",
            activity=build_activity(
                OperationActivityCategory.PREPARATION, "finalizing", self.display_name
            ),
        )
        # Last: only a complete install may claim ownership.
        write_marker(
            directory,
            component=self.component_id,
            source_id=self.url or self.component_id,
            version=self.revision,
            checksum=self.commit,
            fingerprint=self.dependency_fingerprint(),
        )
        context.emit(
            self.component_id,
            f"Verifying the {self.display_name} installation",
            activity=build_activity(
                OperationActivityCategory.PREPARATION, "verifying", self.display_name
            ),
        )
        final = self.verify(context)
        if not final.is_usable:
            return self.failed(
                detection,
                FailureReason.COMMAND_FAILED,
                "ComfyUI was installed but did not verify.",
                final.detail,
            )
        context.emit(
            self.component_id,
            "Managed ComfyUI installed",
            EventLevel.INFO,
            activity=build_activity(
                OperationActivityCategory.PREPARATION, "installed", self.display_name
            ),
            milestone=True,
        )
        return self.installed(final, plan.steps, f"Installed into {directory}.")

    def _git_progress(self, context: ProvisionContext, repo_name: str):
        def on_event(event: GitCloneEvent) -> None:
            context.emit(
                self.component_id,
                f"{event.action.capitalize()} objects ({event.percent}%)",
                activity=build_activity(
                    OperationActivityCategory.TRANSFER,
                    event.action,
                    repo_name,
                    detail=f"{event.completed}/{event.total} objetos ({event.percent}%)",
                ),
            )

        return on_event

    def _pip_progress(self, context: ProvisionContext, start: float):
        def on_event(pip_event: PipEvent) -> None:
            elapsed = max(0.0, context.monotonic() - start)
            transfer = None
            if pip_event.completed_bytes is not None or pip_event.total_bytes is not None:
                transfer = TransferProgress(
                    completed_bytes=pip_event.completed_bytes or 0,
                    total_bytes=pip_event.total_bytes,
                    bytes_per_second=pip_event.bytes_per_second,
                )
            context.emit(
                self.component_id,
                f"{pip_event.action.capitalize()} {pip_event.package}",
                activity=build_activity(
                    OperationActivityCategory.DEPENDENCY,
                    pip_event.action,
                    pip_event.package,
                    elapsed_seconds=elapsed,
                ),
                transfer=transfer,
                milestone=pip_event.action == "installed",
            )

        return on_event

    # --- utilidades --------------------------------------------------------

    @staticmethod
    def _discard(directory: Path) -> None:  # pragma: no cover - defensive cleanup
        shutil.rmtree(directory, ignore_errors=True)


def _repo_name(url: str | None) -> str:
    """A short display label for a repository URL. Never the whole URL."""
    if not url:
        return "repositório"
    trimmed = url.rstrip("/")
    name = trimmed.rsplit("/", 1)[-1]
    return name[: -len(".git")] if name.endswith(".git") else name or "repositório"


def provisioner_from_pin(
    pin,
    *,
    dependencies: DependencyPin | None = None,
    requirements: Sequence[str] = (),
    install_alongside_external: bool = False,
    python_runtime: ManagedPythonRuntime | None = None,
) -> ComfyUIProvisioner:
    """Build a provisioner from a catalog pin, or an unpinned one from None.

    The manifest's dependency decision is adopted unless a caller overrides it,
    which is what makes a pinned checkout installable without any code change.
    """
    if pin is None:
        resolved = dependencies if dependencies is not None else DependencyPin(tuple(requirements))
        return ComfyUIProvisioner(
            dependencies=resolved,
            install_alongside_external=install_alongside_external,
            python_runtime=python_runtime,
        )
    if dependencies is not None:
        resolved = dependencies
    elif requirements:
        resolved = DependencyPin(tuple(requirements))
    else:
        resolved = DependencyPin.from_pin(pin.dependencies)
    return ComfyUIProvisioner(
        revision=pin.revision,
        commit=pin.commit,
        url=pin.url,
        dependencies=resolved,
        python_minimum=pin.python_minimum,
        python_below=pin.python_below,
        install_alongside_external=install_alongside_external,
        python_runtime=python_runtime,
    )


__all__ = ["COMPONENT_ID", "ComfyUIProvisioner", "DependencyPin", "provisioner_from_pin"]
