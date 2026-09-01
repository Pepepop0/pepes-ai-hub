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

Why apply() still refuses by default
------------------------------------
ComfyUI's own requirements.txt leaves torch, torchvision and torchaudio
unpinned, so installing it verbatim would resolve whatever those packages
happen to be today — and on an NVIDIA target, which build of torch to install
is a real decision, not a default. Rather than resolve it silently, the
dependency step needs an explicit pin: a requirement list and, where the wheels
do not come from PyPI, an index to take them from.

Without that pin, apply() refuses with PRECONDITION *before cloning anything*,
so nothing is created and no half-installed checkout is left behind claiming to
be usable. Detection, planning, verification and dry-run all work regardless.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .components import ComponentId
from .git_ops import GitClient, GitError
from .markers import ManagedMarker, marker_path, read_marker, write_marker
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
)
from .pyenv import (
    PythonEnvironment,
    PythonEnvironmentError,
    create_environment,
    describe_range,
    find_interpreter,
    pip_install,
)

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

    @property
    def is_empty(self) -> bool:
        return not self.requirements and not self.use_repository_requirements

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
    install_alongside_external: bool = False
    git: GitClient | None = None

    component_id = COMPONENT_ID
    display_name = "ComfyUI"
    supports_apply = True

    @property
    def is_pinned(self) -> bool:
        return bool(self.url) and bool(self.revision)

    def _git(self, context: ProvisionContext) -> GitClient:
        return self.git if self.git is not None else GitClient(context.runner)

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
        """Confirm the checkout still sits on the pinned commit."""
        detection = self.detect(context)
        if not detection.is_usable or not detection.is_managed:
            return detection
        if self.commit is None:
            return detection
        try:
            head = self._git(context).head(self.managed_dir(context))
        except GitError as exc:
            return Detection(
                component_id=self.component_id,
                state=ComponentState.INVALID,
                ownership=detection.ownership,
                path=detection.path,
                detail=f"Could not read the checkout revision: {exc}",
                evidence=detection.evidence,
            )
        if head != self.commit:
            return Detection(
                component_id=self.component_id,
                state=ComponentState.INVALID,
                ownership=detection.ownership,
                path=detection.path,
                detail=(
                    f"The checkout is at {head[:12]} but the pin is {self.commit[:12]}. "
                    "It was moved outside this hub."
                ),
                evidence=detection.evidence,
            )
        return detection

    # --- plano -------------------------------------------------------------

    def plan(self, context: ProvisionContext) -> ProvisionPlan:
        detection = self.detect(context)
        if detection.is_managed and detection.is_usable:
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
                    summary=f"Create a Python {describe_range(PYTHON_MINIMUM, PYTHON_BELOW)} environment.",
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
        plan = self.plan(context)
        detection = plan.detection

        if detection.is_managed and detection.is_usable:
            return self.skipped(detection, "A managed ComfyUI is already installed.")

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

        interpreter = find_interpreter(context.runner, minimum=PYTHON_MINIMUM, below=PYTHON_BELOW)
        if interpreter is None:
            return self.failed(
                detection,
                FailureReason.PRECONDITION,
                "No supported Python interpreter was found.",
                f"ComfyUI requires Python {describe_range(PYTHON_MINIMUM, PYTHON_BELOW)}.",
            )

        if dry_run:
            return self.dry_run_result(
                detection,
                plan.steps,
                f"Would install ComfyUI {self.revision} into {self.managed_dir(context)}.",
            )

        return self._install(context, detection, plan, git, interpreter)

    def _install(self, context, detection, plan, git, interpreter) -> ProvisionResult:
        directory = self.managed_dir(context)
        environment = self.environment(context)
        try:
            context.layout.ensure()
        except OSError as exc:
            return self.failed(
                detection, FailureReason.FILESYSTEM, "Could not prepare the layout.", str(exc)
            )

        context.emit(self.component_id, f"Cloning {self.url} at {self.revision}")
        try:
            if not git.is_repository(directory):
                git.clone(self.url, directory, revision=self.revision)
            else:
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

        context.emit(self.component_id, f"Creating the environment at {environment.root}")
        try:
            create_environment(context.runner, environment, interpreter)
        except PythonEnvironmentError as exc:
            return self.failed(
                detection,
                FailureReason.COMMAND_FAILED,
                "Could not create the environment.",
                str(exc),
            )

        flags, requirements = self.dependencies.pip_arguments(directory)
        context.emit(self.component_id, "Installing pinned dependencies")
        try:
            pip_install(context.runner, environment, requirements, extra_args=flags)
        except PythonEnvironmentError as exc:
            return self.failed(
                detection, FailureReason.COMMAND_FAILED, "Could not install dependencies.", str(exc)
            )

        if not self.entry_point(context).is_file():
            return self.failed(
                detection,
                FailureReason.FILESYSTEM,
                f"The checkout has no {ENTRY_POINT}.",
                f"Looked in {directory}.",
            )

        # Last: only a complete install may claim ownership.
        write_marker(
            directory,
            component=self.component_id,
            source_id=self.url or self.component_id,
            version=self.revision,
            checksum=self.commit,
        )
        final = self.verify(context)
        if not final.is_usable:
            return self.failed(
                detection,
                FailureReason.COMMAND_FAILED,
                "ComfyUI was installed but did not verify.",
                final.detail,
            )
        context.emit(self.component_id, "Managed ComfyUI installed", EventLevel.INFO)
        return self.installed(final, plan.steps, f"Installed into {directory}.")

    # --- utilidades --------------------------------------------------------

    @staticmethod
    def _discard(directory: Path) -> None:  # pragma: no cover - defensive cleanup
        shutil.rmtree(directory, ignore_errors=True)


def provisioner_from_pin(
    pin,
    *,
    dependencies: DependencyPin | None = None,
    requirements: Sequence[str] = (),
) -> ComfyUIProvisioner:
    """Build a provisioner from a catalog pin, or an unpinned one from None."""
    resolved = dependencies if dependencies is not None else DependencyPin(tuple(requirements))
    if pin is None:
        return ComfyUIProvisioner(dependencies=resolved)
    return ComfyUIProvisioner(
        revision=pin.revision,
        commit=pin.commit,
        url=pin.url,
        dependencies=resolved,
    )


__all__ = ["COMPONENT_ID", "ComfyUIProvisioner", "DependencyPin", "provisioner_from_pin"]
