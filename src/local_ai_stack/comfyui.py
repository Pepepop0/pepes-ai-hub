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
ComfyUI's own requirements.txt leaves torch and torchvision unversioned, so
installing it verbatim would resolve whatever those packages happen to be
today — and on an NVIDIA target, which build of torch to install is a
decision, not a default. The torch triple is pinned in the manifest instead,
with the index that serves the CUDA wheels; everything else still comes from
the repository's own file, which is itself fixed by the pinned commit.

A component with no dependency pin is still refused with PRECONDITION *before
cloning anything*, so nothing is created and no half-installed checkout is left
behind claiming to be usable.

verify() proves both halves independently: the checkout sits on the pinned
commit, and the environment really holds the pinned distribution at the pinned
version. Either can be true without the other.

Custom nodes
------------
Node packs the manifest pins are checked out into ``custom_nodes/<id>`` inside
the checkout, which upstream's .gitignore already excludes, and each is
verified against its own commit. The Hub claims a node folder with its marker
the moment it clones it, so it only ever moves or removes folders it created:
a user's own node is never touched, and a same-named one blocks the install
instead of being replaced. A pack that declares pip dependencies is refused,
because the Hub installs none for nodes and a node missing its packages would
only fail later, inside ComfyUI.
"""

from __future__ import annotations

import shutil
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .catalog import PinnedCustomNode
from .components import ComponentId
from .deletion import delete_tree, first_link_inside
from .git_ops import (
    GitClient,
    GitCloneEvent,
    GitCloneOutputParser,
    GitError,
    describe_git_failure,
)
from .markers import (
    MARKER_NAME,
    ManagedMarker,
    fingerprint_of,
    marker_path,
    read_marker,
    write_marker,
)
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
    VENV_DIR_NAME,
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
# What the Hub itself writes inside the checkout: its ownership marker (and the
# temporary file it is atomically written through) and the managed virtual
# environment. Git sees them as untracked files, but they are not local edits,
# so they must never make a repair refuse to update the checkout. The marker
# family is matched by prefix, covering `.tmp` and a renamed copy alike.
# custom_nodes/ needs no entry: upstream's own .gitignore excludes it.
HUB_OWNED_CHECKOUT_ENTRIES = (f"{MARKER_NAME}*", VENV_DIR_NAME)

CUSTOM_NODES_DIR = "custom_nodes"
# The marker component that proves a custom_nodes/<id> folder is the Hub's.
CUSTOM_NODE_COMPONENT = f"{COMPONENT_ID}-custom-node"
# What may appear untracked in a node checkout without being a local edit:
# the Hub's marker and the bytecode ComfyUI writes when it imports the node.
HUB_OWNED_NODE_ENTRIES = (f"{MARKER_NAME}*", "__pycache__")

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
    custom_nodes: tuple[PinnedCustomNode, ...] = ()
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
                "custom_nodes": [
                    [node.id, node.url, node.revision, node.commit]
                    for node in sorted(self.custom_nodes, key=lambda node: node.id)
                ],
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

    def custom_node_dir(self, context: ProvisionContext, node: PinnedCustomNode) -> Path:
        return self.managed_dir(context) / CUSTOM_NODES_DIR / node.id

    def _at_pinned_commit(self, git: GitClient, directory: Path) -> bool:
        """True when the checkout already sits on the pinned commit: nothing to update."""
        return self.commit is not None and _head_is(git, directory, self.commit)

    def _stale_custom_nodes(self, context: ProvisionContext) -> list[Path]:
        """Node folders the Hub installed that the pin no longer lists. Read-only."""
        root = self.managed_dir(context) / CUSTOM_NODES_DIR
        if not root.is_dir():
            return []
        pinned = {node.id.lower() for node in self.custom_nodes}
        return sorted(
            path
            for path in root.iterdir()
            if path.is_dir()
            and not path.is_symlink()
            and path.name.lower() not in pinned
            and _owns_custom_node(path)
        )

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

        git = self._git(context)
        if self.commit is not None:
            try:
                head = git.head(self.managed_dir(context))
            except GitError as exc:
                return self._invalid(detection, f"Could not read the checkout revision: {exc}")
            if head != self.commit:
                return self._invalid(
                    detection,
                    f"The checkout is at {head[:12]} but the pin is {self.commit[:12]}. "
                    "It was moved outside this hub.",
                )

        for node in self.custom_nodes:
            problem = _custom_node_problem(git, self.custom_node_dir(context, node), node)
            if problem is not None:
                return self._invalid(detection, problem)

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
                *(
                    ProvisionStep(
                        action="remove-custom-node",
                        target=str(stale),
                        summary=(
                            f"Remove the custom node {stale.name}: "
                            "this hub installed it and the pin no longer lists it."
                        ),
                    )
                    for stale in self._stale_custom_nodes(context)
                ),
                *(
                    ProvisionStep(
                        action="install-custom-node",
                        target=str(self.custom_node_dir(context, node)),
                        summary=(
                            f"Check out {node.url} at {node.revision} "
                            f"into {CUSTOM_NODES_DIR}/{node.id}."
                        ),
                    )
                    for node in self.custom_nodes
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

        existing = False
        try:
            existing = git.is_repository(directory)
            if not existing:
                context.emit(
                    self.component_id,
                    f"Cloning {self.url} at {self.revision}",
                    activity=build_activity(
                        OperationActivityCategory.TRANSFER, "cloning", repo_name
                    ),
                )
                on_clone_output = GitCloneOutputParser(self._git_progress(context, repo_name)).feed
                git.clone(self.url, directory, revision=self.revision, on_output=on_clone_output)
            elif not self._at_pinned_commit(git, directory):
                # Re-pinning to a newer tag needs it fetched first. fetch()
                # refuses a dirty tree, so local edits are never discarded --
                # but the Hub's own files in the checkout are not edits.
                context.emit(
                    self.component_id,
                    f"Updating the checkout to {self.revision}",
                    activity=build_activity(
                        OperationActivityCategory.TRANSFER, "fetching", repo_name
                    ),
                )
                git.fetch(directory, ignore=HUB_OWNED_CHECKOUT_ENTRIES)
                git.checkout(directory, self.revision, ignore=HUB_OWNED_CHECKOUT_ENTRIES)
        except GitError as exc:
            action = "update the ComfyUI checkout" if existing else "clone ComfyUI"
            return self.failed(
                detection,
                FailureReason.COMMAND_FAILED,
                f"Could not {action}: {describe_git_failure(exc)}.",
                str(exc),
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

        failure = self._install_custom_nodes(context, detection, git)
        if failure is not None:
            return failure

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

    def _install_custom_nodes(self, context, detection, git) -> ProvisionResult | None:
        """Bring custom_nodes/ to the pin. None on success, else the failure."""
        for stale in self._stale_custom_nodes(context):
            context.emit(
                self.component_id,
                f"Removing the custom node {stale.name}, no longer pinned",
                activity=build_activity(
                    OperationActivityCategory.PREPARATION, "removing", stale.name
                ),
            )
            problem = _remove_owned(stale)
            if problem is not None:
                return self.failed(
                    detection,
                    FailureReason.FILESYSTEM,
                    f"Could not remove the custom node {stale.name}, which is no longer pinned.",
                    problem,
                )
        for node in self.custom_nodes:
            failure = self._install_custom_node(context, detection, git, node)
            if failure is not None:
                return failure
        return None

    def _install_custom_node(self, context, detection, git, node) -> ProvisionResult | None:
        target = self.custom_node_dir(context, node)
        label = f"{CUSTOM_NODES_DIR}/{node.id}"
        owned = _owns_custom_node(target)
        if target.exists() and not owned:
            return self.failed(
                detection,
                FailureReason.PRECONDITION,
                f"{label} already exists and was not installed by this hub.",
                "It was left untouched. Move it out of custom_nodes so the pinned node "
                "can be installed there.",
            )
        if owned and not (target / ".git").exists():
            # Ours but no longer a checkout, as an interrupted clone leaves it.
            problem = _remove_owned(target)
            if problem is not None:
                return self.failed(
                    detection,
                    FailureReason.FILESYSTEM,
                    f"Could not replace the incomplete custom node {node.id}.",
                    problem,
                )

        fresh = not target.exists()
        repo_name = _repo_name(node.url)
        try:
            if fresh:
                context.emit(
                    self.component_id,
                    f"Cloning {node.url} at {node.revision}",
                    activity=build_activity(
                        OperationActivityCategory.TRANSFER, "cloning", repo_name
                    ),
                )
                on_clone_output = GitCloneOutputParser(self._git_progress(context, repo_name)).feed
                git.clone(node.url, target, on_output=on_clone_output)
            # Claimed before anything else happens in it, so a failure from here
            # on leaves a folder a later run knows it may move or replace.
            write_marker(
                target,
                component=CUSTOM_NODE_COMPONENT,
                source_id=node.url,
                version=node.revision,
                checksum=node.commit,
            )
            if not _head_is(git, target, node.commit):
                if not fresh:
                    context.emit(
                        self.component_id,
                        f"Updating {label} to {node.revision}",
                        activity=build_activity(
                            OperationActivityCategory.TRANSFER, "fetching", repo_name
                        ),
                    )
                    git.fetch(target, ignore=HUB_OWNED_NODE_ENTRIES)
                git.checkout(target, node.revision, ignore=HUB_OWNED_NODE_ENTRIES)
            head = git.head(target)
        except GitError as exc:
            return self.failed(
                detection,
                FailureReason.COMMAND_FAILED,
                f"Could not install the custom node {node.id}: {describe_git_failure(exc)}.",
                str(exc),
            )
        if head != node.commit:
            return self.failed(
                detection,
                FailureReason.PRECONDITION,
                f"{node.id} {node.revision} resolved to {head[:12]}, "
                f"not the pinned {node.commit[:12]}.",
                "The tag moved upstream. No ComfyUI marker was written; re-pin deliberately.",
            )

        declared = _declared_dependencies(target)
        if declared is not None:
            # The folder is ours and unusable without packages nobody will
            # install, so it goes rather than being loaded half-working.
            problem = _remove_owned(target)
            detail = (
                "This hub installs no pip dependencies for custom nodes. "
                "Pin a revision without them, or remove the node from the manifest."
            )
            if problem is not None:
                detail = f"{detail} The folder could not be removed: {problem}"
            return self.failed(
                detection,
                FailureReason.PRECONDITION,
                f"The custom node {node.id} declares Python dependencies in {declared}.",
                detail,
            )
        return None

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
                pip_event.message,
                activity=build_activity(
                    OperationActivityCategory.DEPENDENCY,
                    pip_event.action,
                    pip_event.item,
                    detail="pip (Python)",
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


def _head_is(git: GitClient, directory: Path, commit: str) -> bool:
    try:
        return git.head(directory) == commit
    except GitError:
        return False


def _owns_custom_node(directory: Path) -> bool:
    return read_marker(directory, component=CUSTOM_NODE_COMPONENT) is not None


def _custom_node_problem(git: GitClient, directory: Path, node: PinnedCustomNode) -> str | None:
    """Why a pinned node does not verify, or None when it does."""
    label = f"{CUSTOM_NODES_DIR}/{node.id}"
    if not _owns_custom_node(directory):
        if directory.exists():
            return f"{label} exists but was not installed by this hub."
        return f"The custom node {node.id} is not installed."
    if not (directory / ".git").exists():
        return f"{label} is not a checkout; its install did not complete."
    try:
        head = git.head(directory)
    except GitError as exc:
        return f"Could not read the revision of {label}: {exc}"
    if head != node.commit:
        return f"{label} is at {head[:12]} but the pin is {node.commit[:12]}."
    return None


def _declared_dependencies(directory: Path) -> str | None:
    """Where a node checkout declares pip dependencies, or None when it declares none.

    An empty or comment-only requirements.txt declares nothing. A file that
    cannot be read is reported as declaring something: it cannot be shown not to.
    """
    requirements = directory / "requirements.txt"
    if requirements.is_file():
        try:
            lines = requirements.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return "requirements.txt (unreadable)"
        if any(line.strip() and not line.lstrip().startswith("#") for line in lines):
            return "requirements.txt"
    pyproject = directory / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            return "pyproject.toml (unreadable)"
        project = data.get("project")
        if isinstance(project, dict) and project.get("dependencies"):
            return "pyproject.toml [project].dependencies"
    return None


def _remove_owned(directory: Path) -> str | None:
    """Delete a folder the Hub created. None on success, else why it was not."""
    if directory.is_symlink():
        return f"{directory} is a link; it was not followed or deleted."
    link = first_link_inside(directory)
    if link is not None:
        return f"{link} is a link; {directory} was left in place rather than delete through it."
    failures = delete_tree(directory)
    return "; ".join(failures) if failures else None


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
        custom_nodes=tuple(pin.custom_nodes),
        python_minimum=pin.python_minimum,
        python_below=pin.python_below,
        install_alongside_external=install_alongside_external,
        python_runtime=python_runtime,
    )


__all__ = ["COMPONENT_ID", "ComfyUIProvisioner", "DependencyPin", "provisioner_from_pin"]
