"""Ollama provisioner: the first real proof of the provisioner contract.

Ownership model
---------------
Three outcomes, and they are not interchangeable:

- MANAGED   we installed it under <layout.apps>/ollama and left a marker file
            proving it; only this one may be modified or replaced;
- EXTERNAL  Ollama exists on the machine (PATH or a known install location) but
            we did not put it there. It is read and reported, never adopted,
            never uninstalled, never overwritten;
- NONE      nothing found.

An external install is never silently promoted to managed. Installing a managed
copy alongside one is possible, but only when the caller asks for it, because
two copies on one machine is a decision, not a default.

Install strategy and its limitation
-----------------------------------
The official Windows distribution is an installer executable, which writes to
the registry and to the user PATH. Running it unattended would mutate machine
state this project promises not to touch, so it is not used.

The managed path uses the portable Windows archive instead: download, verify
the SHA-256, extract into the layout, write the marker. That keeps PATH, the
registry and any external install untouched.

Because the archive URL and its checksum change with every upstream release,
this module ships no hardcoded artifact. `apply()` requires one to be supplied,
and refuses with a PRECONDITION failure when it is missing, instead of guessing
a URL. Pinning is therefore explicit and verifiable: whoever supplies the
artifact supplies the version and the checksum.

The marker is written last. An interrupted install leaves files without a
marker, which reports as INVALID rather than pretending to be installed.
"""

from __future__ import annotations

import os
import shutil
import zipfile
from pathlib import Path

from .artifacts import Artifact
from .components import ComponentId
from .download import (
    ChecksumMismatchError,
    DownloadCancelledError,
    Downloader,
    DownloadError,
    DownloadSpec,
)
from .markers import MARKER_NAME, ManagedMarker, marker_path, read_marker, write_marker
from .provisioning import (
    ComponentState,
    Detection,
    EventLevel,
    FailureReason,
    Ownership,
    ProvisionContext,
    Provisioner,
    ProvisionOutcome,
    ProvisionPlan,
    ProvisionResult,
    ProvisionStep,
)

COMPONENT_ID = str(ComponentId.OLLAMA)
EXECUTABLE = "ollama.exe" if os.name == "nt" else "ollama"
VERSION_TIMEOUT = 15.0

# Locais onde o instalador oficial do Windows costuma deixar o binario.
EXTERNAL_HINTS = (
    ("LOCALAPPDATA", Path("Programs") / "Ollama" / EXECUTABLE),
    ("PROGRAMFILES", Path("Ollama") / EXECUTABLE),
)


class OllamaProvisioner(Provisioner):
    component_id = COMPONENT_ID
    display_name = "Ollama"
    supports_apply = True

    def __init__(
        self,
        *,
        artifact: Artifact | None = None,
        downloader: Downloader | None = None,
        install_alongside_external: bool = False,
    ) -> None:
        self.artifact = artifact
        self.downloader = downloader
        self.install_alongside_external = install_alongside_external

    # --- paths -------------------------------------------------------------

    def managed_dir(self, context: ProvisionContext) -> Path:
        return context.layout.ollama_app

    def managed_executable(self, context: ProvisionContext) -> Path:
        return self.managed_dir(context) / EXECUTABLE

    def marker_path(self, context: ProvisionContext) -> Path:
        return marker_path(self.managed_dir(context))

    # --- deteccao ----------------------------------------------------------

    def detect(self, context: ProvisionContext) -> Detection:
        """Read-only. Creates nothing, runs nothing that mutates."""
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
            detail="Ollama was not found, managed or external.",
        )

    def _detect_managed(self, context: ProvisionContext) -> Detection | None:
        executable = self.managed_executable(context)
        marker = self._read_marker(context)
        if marker is None and not executable.is_file():
            return None
        if marker is None:
            # Ficheiros sem marcador: instalacao interrompida, nao confiavel.
            return Detection(
                component_id=self.component_id,
                state=ComponentState.INVALID,
                ownership=Ownership.MANAGED,
                path=executable,
                detail=(
                    "A managed Ollama directory exists but has no marker file. "
                    "An install was probably interrupted; it is not usable."
                ),
                evidence=(str(executable),),
            )
        if not executable.is_file():
            return Detection(
                component_id=self.component_id,
                state=ComponentState.INVALID,
                ownership=Ownership.MANAGED,
                path=executable,
                detail="The managed marker exists but the executable is missing.",
                evidence=(str(self.marker_path(context)),),
            )
        return Detection(
            component_id=self.component_id,
            state=ComponentState.INSTALLED,
            ownership=Ownership.MANAGED,
            version=marker.version or self._probe_version(context, executable),
            path=executable,
            detail="Managed by Pepito's AI Hub.",
            evidence=(str(executable), str(self.marker_path(context))),
        )

    def _detect_external(self, context: ProvisionContext) -> Detection | None:
        for candidate in self._external_candidates(context):
            if not candidate.is_file():
                continue
            return Detection(
                component_id=self.component_id,
                state=ComponentState.INSTALLED,
                ownership=Ownership.EXTERNAL,
                version=self._probe_version(context, candidate),
                path=candidate,
                detail=(
                    "Ollama is already installed outside this hub. "
                    "It is reported only; it is never modified, replaced or removed."
                ),
                evidence=(str(candidate),),
            )
        return None

    def _external_candidates(self, context: ProvisionContext) -> list[Path]:
        candidates: list[Path] = []
        on_path = context.runner.which(COMPONENT_ID)
        if on_path:
            candidates.append(Path(on_path))
        environ = context.runner.child_env()
        for variable, relative in EXTERNAL_HINTS:
            base = environ.get(variable)
            if base:
                candidates.append(Path(base) / relative)
        managed = self.managed_executable(context)
        return [c for c in candidates if c != managed]

    def _read_marker(self, context: ProvisionContext) -> ManagedMarker | None:
        return read_marker(self.managed_dir(context), component=self.component_id)

    def _probe_version(self, context: ProvisionContext, executable: Path) -> str | None:
        result = context.runner.run(
            [str(executable), "--version"], timeout=VERSION_TIMEOUT, dry_run=False
        )
        if not result.ok:
            return None
        text = (result.stdout or result.stderr).strip()
        for token in text.replace(",", " ").split():
            if token and token[0].isdigit():
                return token
        return text.splitlines()[0] if text else None

    # --- verificacao -------------------------------------------------------

    def verify(self, context: ProvisionContext) -> Detection:
        """Re-detect and confirm the executable actually answers."""
        detection = self.detect(context)
        if detection.path is None or not detection.is_usable:
            return detection
        version = self._probe_version(context, detection.path)
        if version is None:
            return Detection(
                component_id=detection.component_id,
                state=ComponentState.INVALID,
                ownership=detection.ownership,
                path=detection.path,
                detail="The executable exists but did not answer --version.",
                evidence=detection.evidence,
            )
        return Detection(
            component_id=detection.component_id,
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
        steps: list[ProvisionStep] = []

        if detection.is_managed and detection.is_usable:
            return ProvisionPlan(self.component_id, (), detection)

        if detection.is_external and not self.install_alongside_external:
            return ProvisionPlan(self.component_id, (), detection)

        target = self.managed_dir(context)
        if self.artifact is None:
            steps.append(
                ProvisionStep(
                    action="blocked",
                    target=self.component_id,
                    summary=(
                        "No Ollama artifact is configured, so nothing can be downloaded. "
                        "Supply a pinned archive with its SHA-256."
                    ),
                )
            )
            return ProvisionPlan(self.component_id, tuple(steps), detection)

        steps.append(
            ProvisionStep(
                action="download",
                target=self.artifact.id,
                summary=f"Download {self.artifact.filename} and verify its SHA-256.",
            )
        )
        steps.append(
            ProvisionStep(
                action="extract",
                target=str(target),
                summary=f"Extract the archive into {target}.",
            )
        )
        steps.append(
            ProvisionStep(
                action="mark-managed",
                target=str(self.marker_path(context)),
                summary="Write the managed marker so this install is provably ours.",
            )
        )
        return ProvisionPlan(self.component_id, tuple(steps), detection)

    # --- aplicacao ---------------------------------------------------------

    def apply(self, context: ProvisionContext, *, dry_run: bool = False) -> ProvisionResult:
        plan = self.plan(context)
        detection = plan.detection

        if detection.is_managed and detection.is_usable:
            return self.skipped(detection, "A managed Ollama is already installed.")

        if detection.is_external and not self.install_alongside_external:
            return self.skipped(
                detection,
                (
                    f"Ollama is already installed externally at {detection.path}. "
                    "Leaving it untouched; this hub takes no ownership of it."
                ),
            )

        if self.artifact is None:
            return self.failed(
                detection,
                FailureReason.PRECONDITION,
                "No Ollama artifact is configured.",
                (
                    "The Windows installer is not run because it writes to PATH and the "
                    "registry. A managed install needs a pinned portable archive with its "
                    "SHA-256 supplied explicitly."
                ),
            )

        if dry_run:
            return self.dry_run_result(
                detection,
                plan.steps,
                f"Would install a managed Ollama into {self.managed_dir(context)}.",
            )

        return self._install(context, detection, plan)

    def _install(
        self, context: ProvisionContext, detection: Detection, plan: ProvisionPlan
    ) -> ProvisionResult:
        assert self.artifact is not None
        layout = context.layout
        downloader = self.downloader or Downloader(layout=layout)
        target = self.managed_dir(context)

        try:
            layout.ensure()
        except OSError as exc:
            return self.failed(
                detection, FailureReason.FILESYSTEM, "Could not prepare the layout.", str(exc)
            )

        context.emit(self.component_id, f"Downloading {self.artifact.filename}")
        spec = DownloadSpec.from_artifact(self.artifact, layout.downloads)
        try:
            download = downloader.fetch(spec)
        except ChecksumMismatchError as exc:
            return self.failed(detection, FailureReason.CHECKSUM, str(exc))
        except DownloadCancelledError as exc:
            return self.failed(detection, FailureReason.CANCELLED, str(exc))
        except DownloadError as exc:
            return self.failed(detection, FailureReason.NETWORK, str(exc))

        staging = layout.cache / f"{COMPONENT_ID}-{self.artifact.id}"
        context.emit(self.component_id, f"Extracting into {target}")
        try:
            _reset_directory(staging)
            _extract_zip(download.path, staging)
            _merge_directory(staging, target)
            shutil.rmtree(staging, ignore_errors=True)
        except (OSError, zipfile.BadZipFile, ValueError) as exc:
            shutil.rmtree(staging, ignore_errors=True)
            return self.failed(
                detection,
                FailureReason.FILESYSTEM,
                f"Could not extract {self.artifact.filename}.",
                str(exc),
            )

        executable = self.managed_executable(context)
        if not executable.is_file():
            return self.failed(
                detection,
                FailureReason.FILESYSTEM,
                f"The archive did not contain {EXECUTABLE}.",
                f"Looked in {target}.",
            )

        # O marcador vai por ultimo: antes dele, a instalacao nao se diz nossa.
        self._write_marker(context)
        final = self.verify(context)
        if not final.is_usable:
            return self.failed(
                detection,
                FailureReason.COMMAND_FAILED,
                "Ollama was installed but did not verify.",
                final.detail,
            )
        context.emit(self.component_id, "Managed Ollama installed", EventLevel.INFO)
        return self.installed(final, plan.steps, f"Installed into {target}.")

    def _write_marker(self, context: ProvisionContext) -> None:
        assert self.artifact is not None
        write_marker(
            self.managed_dir(context),
            component=self.component_id,
            source_id=self.artifact.id,
            version=self.artifact.version,
            checksum=self.artifact.sha256,
        )


def _reset_directory(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)


def _extract_zip(archive: Path, destination: Path) -> None:
    """Extract with an explicit traversal guard: no member may escape."""
    destination = destination.resolve()
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            name = member.filename
            if name.startswith("/") or ".." in Path(name).parts or Path(name).is_absolute():
                raise ValueError(f"refusing archive entry outside the destination: {name}")
            resolved = (destination / name).resolve()
            if destination != resolved and destination not in resolved.parents:
                raise ValueError(f"refusing archive entry outside the destination: {name}")
        bundle.extractall(destination)


def _merge_directory(source: Path, destination: Path) -> None:
    """Copy a staged tree into place, creating parents as needed."""
    destination.mkdir(parents=True, exist_ok=True)
    for entry in source.iterdir():
        target = destination / entry.name
        if entry.is_dir():
            shutil.copytree(entry, target, dirs_exist_ok=True)
        else:
            shutil.copy2(entry, target)


__all__ = [
    "COMPONENT_ID",
    "EXECUTABLE",
    "MARKER_NAME",
    "ManagedMarker",
    "OllamaProvisioner",
    "ProvisionOutcome",
]
