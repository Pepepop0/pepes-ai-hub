"""Open WebUI provisioner.

Ownership
---------
Same three outcomes as every component. MANAGED means this hub created the
virtual environment under ``<layout.apps>/open-webui`` and left a marker; only
that one may be modified. EXTERNAL means an ``open-webui`` command already
exists on PATH — reported, never adopted, never uninstalled, never upgraded.

Install strategy
----------------
Open WebUI is a published Python package, so there is no archive to download
and no repository to clone: the managed install is a virtual environment with
one pinned requirement installed into it.

    <layout.apps>/open-webui/.venv     the environment
    <layout.data>/open-webui           DATA_DIR, owned by the runtime

A virtual environment rather than ``uvx`` or a user-site install, because it is
self-contained: nothing is added to PATH, no system site-packages are touched,
and removing the directory removes the install.

Pinning is explicit. The version comes from the shipped manifest, never from
"latest", and is installed as ``open-webui==<version>``. With nothing pinned,
``apply()`` refuses with PRECONDITION instead of resolving whatever PyPI serves
today.

Upstream supports Python >=3.11,<3.13. An interpreter inside that range is
located without touching PATH; when none exists the install refuses rather than
building an environment upstream does not support.

The marker is written last, so an interrupted install reads as INVALID.
"""

from __future__ import annotations

from pathlib import Path

from .components import ComponentId
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
    distribution_version,
    find_interpreter,
    pip_install,
)

COMPONENT_ID = str(ComponentId.OPEN_WEBUI)
DISTRIBUTION = "open-webui"
CONSOLE_SCRIPT = "open-webui"

# Upstream's own requires-python for the package.
PYTHON_MINIMUM = (3, 11)
PYTHON_BELOW = (3, 13)


class OpenWebUIProvisioner(Provisioner):
    component_id = COMPONENT_ID
    display_name = "Open WebUI"
    supports_apply = True

    def __init__(
        self,
        *,
        version: str | None = None,
        install_alongside_external: bool = False,
    ) -> None:
        self.version = version
        self.install_alongside_external = install_alongside_external

    @property
    def requirement(self) -> str | None:
        """The exact pinned requirement, or None when nothing is pinned."""
        return None if self.version is None else f"{DISTRIBUTION}=={self.version}"

    # --- paths -------------------------------------------------------------

    def managed_dir(self, context: ProvisionContext) -> Path:
        return context.layout.open_webui_app

    def environment(self, context: ProvisionContext) -> PythonEnvironment:
        return PythonEnvironment.inside(self.managed_dir(context))

    def managed_executable(self, context: ProvisionContext) -> Path:
        return self.environment(context).script(CONSOLE_SCRIPT)

    def data_dir(self, context: ProvisionContext) -> Path:
        return context.layout.open_webui_data

    def marker_path(self, context: ProvisionContext) -> Path:
        return marker_path(self.managed_dir(context))

    # --- deteccao ----------------------------------------------------------

    def detect(self, context: ProvisionContext) -> Detection:
        """Read-only. Creates nothing and installs nothing."""
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
            detail="Open WebUI was not found, managed or external.",
        )

    def _detect_managed(self, context: ProvisionContext) -> Detection | None:
        environment = self.environment(context)
        executable = self.managed_executable(context)
        marker = self._read_marker(context)

        if marker is None and not environment.exists:
            return None
        if marker is None:
            return Detection(
                component_id=self.component_id,
                state=ComponentState.INVALID,
                ownership=Ownership.MANAGED,
                path=environment.root,
                detail=(
                    "A managed Open WebUI environment exists but has no marker file. "
                    "An install was probably interrupted; it is not usable."
                ),
                evidence=(str(environment.root),),
            )
        if not executable.is_file():
            return Detection(
                component_id=self.component_id,
                state=ComponentState.INVALID,
                ownership=Ownership.MANAGED,
                path=executable,
                detail="The managed marker exists but the Open WebUI command is missing.",
                evidence=(str(self.marker_path(context)),),
            )
        return Detection(
            component_id=self.component_id,
            state=ComponentState.INSTALLED,
            ownership=Ownership.MANAGED,
            version=marker.version
            or distribution_version(context.runner, environment, DISTRIBUTION),
            path=executable,
            detail="Managed by Pepito's AI Hub.",
            evidence=(str(executable), str(self.marker_path(context))),
        )

    def _detect_external(self, context: ProvisionContext) -> Detection | None:
        on_path = context.runner.which(CONSOLE_SCRIPT)
        if not on_path:
            return None
        candidate = Path(on_path)
        if candidate == self.managed_executable(context):
            return None
        return Detection(
            component_id=self.component_id,
            state=ComponentState.INSTALLED,
            ownership=Ownership.EXTERNAL,
            path=candidate,
            detail=(
                "Open WebUI is already installed outside this hub. "
                "It is reported only; it is never modified, replaced or removed."
            ),
            evidence=(str(candidate),),
        )

    def _read_marker(self, context: ProvisionContext) -> ManagedMarker | None:
        return read_marker(self.managed_dir(context), component=self.component_id)

    # --- verificacao -------------------------------------------------------

    def verify(self, context: ProvisionContext) -> Detection:
        """Confirm the package is really importable in its environment."""
        detection = self.detect(context)
        if not detection.is_usable or not detection.is_managed:
            return detection
        version = distribution_version(context.runner, self.environment(context), DISTRIBUTION)
        if version is None:
            return Detection(
                component_id=self.component_id,
                state=ComponentState.INVALID,
                ownership=detection.ownership,
                path=detection.path,
                detail=f"{DISTRIBUTION} is not installed in the managed environment.",
                evidence=detection.evidence,
            )
        return Detection(
            component_id=self.component_id,
            state=ComponentState.INSTALLED,
            ownership=detection.ownership,
            version=version,
            path=detection.path,
            detail=detection.detail,
            evidence=detection.evidence,
        )

    # --- plano -------------------------------------------------------------

    def plan(self, context: ProvisionContext) -> ProvisionPlan:
        detection = self.detect(context)

        if detection.is_managed and detection.is_usable:
            return ProvisionPlan(self.component_id, (), detection)
        if detection.is_external and not self.install_alongside_external:
            return ProvisionPlan(self.component_id, (), detection)

        requirement = self.requirement
        if requirement is None:
            return ProvisionPlan(
                self.component_id,
                (
                    ProvisionStep(
                        action="blocked",
                        target=self.component_id,
                        summary=(
                            "No Open WebUI version is pinned, so nothing can be installed. "
                            "Pin an exact version in the manifest."
                        ),
                    ),
                ),
                detection,
            )

        environment = self.environment(context)
        return ProvisionPlan(
            self.component_id,
            (
                ProvisionStep(
                    action="create-venv",
                    target=str(environment.root),
                    summary=f"Create a Python {describe_range(PYTHON_MINIMUM, PYTHON_BELOW)} environment.",
                ),
                ProvisionStep(
                    action="install-package",
                    target=requirement,
                    summary=f"Install {requirement} into the environment.",
                ),
                ProvisionStep(
                    action="mark-managed",
                    target=str(self.marker_path(context)),
                    summary="Write the managed marker so this install is provably ours.",
                ),
            ),
            detection,
        )

    # --- aplicacao ---------------------------------------------------------

    def apply(self, context: ProvisionContext, *, dry_run: bool = False) -> ProvisionResult:
        plan = self.plan(context)
        detection = plan.detection

        if detection.is_managed and detection.is_usable:
            return self.skipped(detection, "A managed Open WebUI is already installed.")

        if detection.is_external and not self.install_alongside_external:
            return self.skipped(
                detection,
                (
                    f"Open WebUI is already installed externally at {detection.path}. "
                    "Leaving it untouched; this hub takes no ownership of it."
                ),
            )

        requirement = self.requirement
        if requirement is None:
            return self.failed(
                detection,
                FailureReason.PRECONDITION,
                "No Open WebUI version is pinned.",
                (
                    "A managed install needs an exact version so it is reproducible. "
                    "Nothing is resolved from PyPI at install time."
                ),
            )

        interpreter = find_interpreter(context.runner, minimum=PYTHON_MINIMUM, below=PYTHON_BELOW)
        if interpreter is None:
            return self.failed(
                detection,
                FailureReason.PRECONDITION,
                "No supported Python interpreter was found.",
                f"Open WebUI requires Python {describe_range(PYTHON_MINIMUM, PYTHON_BELOW)}.",
            )

        if dry_run:
            return self.dry_run_result(
                detection,
                plan.steps,
                f"Would install {requirement} into {self.managed_dir(context)}.",
            )

        return self._install(context, detection, plan, requirement, interpreter)

    def _install(self, context, detection, plan, requirement, interpreter) -> ProvisionResult:
        environment = self.environment(context)
        try:
            context.layout.ensure()
        except OSError as exc:
            return self.failed(
                detection, FailureReason.FILESYSTEM, "Could not prepare the layout.", str(exc)
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

        context.emit(self.component_id, f"Installing {requirement}")
        try:
            pip_install(context.runner, environment, [requirement])
        except PythonEnvironmentError as exc:
            return self.failed(
                detection,
                FailureReason.COMMAND_FAILED,
                f"Could not install {requirement}.",
                str(exc),
            )

        if not self.managed_executable(context).is_file():
            return self.failed(
                detection,
                FailureReason.FILESYSTEM,
                f"{DISTRIBUTION} installed but the {CONSOLE_SCRIPT} command is missing.",
                f"Looked in {environment.bin_dir}.",
            )

        # Last, and only now: the install is complete, so it may claim ownership.
        write_marker(
            self.managed_dir(context),
            component=self.component_id,
            source_id=DISTRIBUTION,
            version=self.version,
        )
        final = self.verify(context)
        if not final.is_usable:
            return self.failed(
                detection,
                FailureReason.COMMAND_FAILED,
                "Open WebUI was installed but did not verify.",
                final.detail,
            )
        context.emit(self.component_id, "Managed Open WebUI installed", EventLevel.INFO)
        return self.installed(final, plan.steps, f"Installed into {environment.root}.")


__all__ = ["COMPONENT_ID", "DISTRIBUTION", "OpenWebUIProvisioner"]
