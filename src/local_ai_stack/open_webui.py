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
        python_runtime: ManagedPythonRuntime | None = None,
    ) -> None:
        self.version = version
        self.install_alongside_external = install_alongside_external
        # The packaged product's own Python; None means "search the host",
        # which is what a source checkout does. See python_runtime.py.
        self.python_runtime = python_runtime

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

    def _installed_version(self, context: ProvisionContext) -> str | None:
        return distribution_version(context.runner, self.environment(context), DISTRIBUTION)

    def _is_at_pin(self, context: ProvisionContext, detection: Detection) -> bool:
        return (
            detection.is_managed
            and detection.is_usable
            and self.version is not None
            and self._installed_version(context) == self.version
        )

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

        if self._is_at_pin(context, detection):
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
        upgrading = detection.is_managed and detection.is_usable
        steps = []
        if not upgrading:
            steps.append(
                ProvisionStep(
                    action="create-venv",
                    target=str(environment.root),
                    summary=f"Create a Python {describe_range(PYTHON_MINIMUM, PYTHON_BELOW)} environment.",
                )
            )
        steps.extend(
            (
                ProvisionStep(
                    action="upgrade-package" if upgrading else "install-package",
                    target=requirement,
                    summary=f"Install {requirement} into the managed environment.",
                ),
                ProvisionStep(
                    action="mark-managed",
                    target=str(self.marker_path(context)),
                    summary="Record the exact installed pin after verification.",
                ),
            )
        )
        return ProvisionPlan(self.component_id, tuple(steps), detection)

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

        if self._is_at_pin(context, detection):
            return self.skipped(
                detection, f"Managed Open WebUI {self.version} is already installed."
            )

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

        context.emit(
            self.component_id,
            "Locating a supported Python interpreter",
            activity=build_activity(OperationActivityCategory.ENVIRONMENT, "locating", "Python"),
        )
        resolution = resolve_interpreter(
            context,
            self.component_id,
            minimum=PYTHON_MINIMUM,
            below=PYTHON_BELOW,
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
                or f"Open WebUI requires Python {describe_range(PYTHON_MINIMUM, PYTHON_BELOW)}.",
            )
        interpreter = resolution.interpreter

        if dry_run:
            return self.dry_run_result(
                detection,
                plan.steps,
                f"Would install {requirement} into {self.managed_dir(context)}.",
            )

        return self._install(context, detection, plan, requirement, interpreter)

    def _install(self, context, detection, plan, requirement, interpreter) -> ProvisionResult:
        environment = self.environment(context)
        context.emit(
            self.component_id,
            f"Preparing to install {self.display_name}",
            activity=build_activity(
                OperationActivityCategory.PREPARATION, "preparing", self.display_name
            ),
        )
        try:
            context.layout.ensure()
        except OSError as exc:
            return self.failed(
                detection, FailureReason.FILESYSTEM, "Could not prepare the layout.", str(exc)
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

        context.emit(
            self.component_id,
            f"Installing {requirement}",
            activity=build_activity(
                OperationActivityCategory.DEPENDENCY, "installing", DISTRIBUTION
            ),
        )
        start = context.monotonic()
        on_pip_output = PipOutputParser(self._pip_progress(context, start)).feed
        try:
            pip_install(context.runner, environment, [requirement], on_output=on_pip_output)
        except PythonEnvironmentError as exc:
            message = f"Could not install {requirement}."
            if exc.failure is not None:
                message = f"{message} {exc.failure.summary}"
            return self.failed(
                detection,
                reason_for_pip_failure(exc.failure),
                message,
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
                "Open WebUI was installed but did not verify.",
                final.detail,
            )
        context.emit(
            self.component_id,
            "Managed Open WebUI installed",
            EventLevel.INFO,
            activity=build_activity(
                OperationActivityCategory.PREPARATION, "installed", self.display_name
            ),
            milestone=True,
        )
        return self.installed(final, plan.steps, f"Installed into {environment.root}.")

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


__all__ = ["COMPONENT_ID", "DISTRIBUTION", "OpenWebUIProvisioner"]
