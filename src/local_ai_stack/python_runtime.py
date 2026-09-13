"""The Python that managed environments are built from.

Open WebUI and ComfyUI each live in a virtual environment, and a virtual
environment needs a real interpreter to be created from. Where that
interpreter comes from depends on how the Hub itself is running:

- from a source checkout, the Hub's own venv Python is a fine parent, and
  :func:`pyenv.find_interpreter` picks it (or a ``py -3.11`` on the machine);
- from a frozen build, there is no such thing: ``sys.executable`` is the Hub,
  and the person running it may have no Python installed at all.

So the packaged product provisions its own: one pinned, checksummed CPython
archive from ``manifests/artifacts.yaml`` (``python-windows-amd64``), unpacked
into ``<root>/runtime/python`` under the same marker discipline as Ollama.
Every managed venv is then created from that interpreter, whose ``home``
lives inside the install root - so the install stays self-contained and
survives the Hub being updated, moved or removed.

Selection is explicit, not magic: :func:`managed_python_runtime` returns a
runtime only when the process is frozen, and provisioners receive it at
construction, which is what lets a test hand them a controlled artifact and a
fake downloader without pretending to be frozen. :func:`resolve_interpreter`
is the single seam both provisioners call.
"""

from __future__ import annotations

import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path

from .artifacts import Artifact
from .download import (
    ChecksumMismatchError,
    DownloadCancelledError,
    Downloader,
    DownloadError,
    DownloadProgress,
    DownloadSpec,
)
from .layout import StackLayout
from .markers import read_marker, write_marker
from .operation_contracts import OperationActivityCategory
from .provisioning import FailureReason, ProvisionContext, build_activity
from .pyenv import describe_range, find_interpreter
from .resources import is_frozen
from .transfer_progress import TransferSampler

COMPONENT_ID = "python-runtime"
ARTIFACT_ID = "python-windows-amd64"
RUNTIME_DIR_NAME = "python"
# The archive's single top-level directory; everything the interpreter needs
# is under it, and it is flattened into RUNTIME_DIR_NAME on extraction.
ARCHIVE_TOP_LEVEL = "python"
EXECUTABLE = "python.exe"


@dataclass(frozen=True)
class InterpreterResolution:
    """Either an argv prefix that starts a supported Python, or why there is none."""

    interpreter: tuple[str, ...] | None
    reason: FailureReason | None = None
    message: str = ""
    detail: str = ""

    @classmethod
    def found(cls, interpreter: tuple[str, ...]) -> InterpreterResolution:
        return cls(interpreter)

    @classmethod
    def refused(
        cls, reason: FailureReason, message: str, detail: str = ""
    ) -> InterpreterResolution:
        return cls(None, reason, message, detail)


@dataclass(frozen=True)
class ManagedPythonRuntime:
    """A pinned CPython, provisioned into the install root on first use."""

    artifact: Artifact
    downloader: Downloader | None = None

    # --- paths ------------------------------------------------------------

    def install_dir(self, layout: StackLayout) -> Path:
        return layout.runtime / RUNTIME_DIR_NAME

    def interpreter(self, layout: StackLayout) -> Path:
        return self.install_dir(layout) / EXECUTABLE

    def version(self) -> tuple[int, int]:
        major, minor = self.artifact.version.split(".")[:2]
        return int(major), int(minor)

    # --- state ------------------------------------------------------------

    def is_ready(self, layout: StackLayout) -> bool:
        """True when the pinned interpreter is on disk and the marker proves it is ours."""
        if not self.interpreter(layout).is_file():
            return False
        marker = read_marker(self.install_dir(layout), component=COMPONENT_ID)
        return (
            marker is not None
            and marker.source_id == self.artifact.id
            and marker.version == self.artifact.version
            and marker.checksum == self.artifact.sha256
        )

    # --- provisioning -------------------------------------------------------

    def ensure(self, context: ProvisionContext, component_id: str) -> InterpreterResolution:
        """Download, verify, unpack and mark the runtime, unless it is already there.

        Progress is reported under ``component_id`` - the component that needed
        a Python - since that is the step the person is watching.
        """
        layout = context.layout
        if self.is_ready(layout):
            return InterpreterResolution.found((str(self.interpreter(layout)),))

        label = f"Python {self.artifact.version}"
        context.emit(
            component_id,
            f"Preparing the managed {label}",
            activity=build_activity(OperationActivityCategory.ENVIRONMENT, "preparing", label),
        )
        try:
            layout.ensure()
        except OSError as exc:
            return InterpreterResolution.refused(
                FailureReason.FILESYSTEM, "Could not prepare the layout.", str(exc)
            )

        downloader = self.downloader or Downloader(layout=layout)
        spec = DownloadSpec.from_artifact(self.artifact, layout.downloads)
        sampler = TransferSampler(monotonic=context.monotonic)

        def report(transfer=None) -> None:
            context.emit(
                component_id,
                f"Downloading {self.artifact.filename}",
                activity=build_activity(
                    OperationActivityCategory.TRANSFER, "downloading", self.artifact.filename
                ),
                transfer=transfer,
            )

        def on_progress(raw: DownloadProgress) -> None:
            update = sampler.sample(raw.downloaded_bytes, raw.total_bytes)
            if update is not None:
                report(update)

        report()
        try:
            download = downloader.fetch(spec, progress=on_progress)
        except ChecksumMismatchError as exc:
            return InterpreterResolution.refused(FailureReason.CHECKSUM, str(exc))
        except DownloadCancelledError as exc:
            return InterpreterResolution.refused(FailureReason.CANCELLED, str(exc))
        except DownloadError as exc:
            return InterpreterResolution.refused(FailureReason.NETWORK, str(exc))
        final = sampler.finish(download.downloaded_bytes, self.artifact.size_bytes)
        if final is not None:
            report(final)

        target = self.install_dir(layout)
        staging = layout.cache / f"{COMPONENT_ID}-{self.artifact.id}"
        context.emit(
            component_id,
            f"Extracting {self.artifact.filename}",
            activity=build_activity(
                OperationActivityCategory.PREPARATION, "extracting", self.artifact.filename
            ),
        )
        try:
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True, exist_ok=True)
            _extract_tar(download.path, staging)
            unpacked = staging / ARCHIVE_TOP_LEVEL
            if not (unpacked / EXECUTABLE).is_file():
                raise ValueError(f"the archive did not contain {ARCHIVE_TOP_LEVEL}/{EXECUTABLE}")
            # Replace, never merge: two interpreters' files interleaved is not
            # an interpreter. The marker is gone with the old tree, so an
            # interruption here reports as "not ready", which is the truth.
            shutil.rmtree(target, ignore_errors=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(unpacked), str(target))
            shutil.rmtree(staging, ignore_errors=True)
        except (OSError, tarfile.TarError, ValueError) as exc:
            shutil.rmtree(staging, ignore_errors=True)
            return InterpreterResolution.refused(
                FailureReason.FILESYSTEM, f"Could not extract {self.artifact.filename}.", str(exc)
            )

        write_marker(
            target,
            component=COMPONENT_ID,
            source_id=self.artifact.id,
            version=self.artifact.version,
            checksum=self.artifact.sha256,
        )
        context.emit(
            component_id,
            f"Managed {label} ready",
            activity=build_activity(OperationActivityCategory.ENVIRONMENT, "ready", label),
            milestone=True,
        )
        return InterpreterResolution.found((str(self.interpreter(layout)),))


def managed_python_runtime(artifact: Artifact | None) -> ManagedPythonRuntime | None:
    """The product's runtime when this process is frozen; None means "use the host's".

    Only a frozen build gets a managed runtime by default: a checkout keeps
    building environments from its own venv, as it always has, and tests
    construct one explicitly with a controlled artifact.
    """
    if artifact is None or not is_frozen():
        return None
    return ManagedPythonRuntime(artifact)


def resolve_interpreter(
    context: ProvisionContext,
    component_id: str,
    *,
    minimum: tuple[int, int],
    below: tuple[int, int],
    runtime: ManagedPythonRuntime | None,
    dry_run: bool = False,
) -> InterpreterResolution:
    """The one place a provisioner asks for a Python.

    With a managed runtime the answer is always that runtime's interpreter -
    provisioned on the spot when missing, only *planned* on a dry run, and
    refused with the pin's own range when it would not satisfy the component.
    Without one, the host is searched the way it always was.
    """
    if runtime is None:
        interpreter = find_interpreter(context.runner, minimum=minimum, below=below)
        if interpreter is None:
            return InterpreterResolution.refused(
                FailureReason.PRECONDITION,
                "No supported Python interpreter was found.",
                f"A supported Python is {describe_range(minimum, below)}.",
            )
        return InterpreterResolution.found(interpreter)

    version = runtime.version()
    if not minimum <= version < below:
        return InterpreterResolution.refused(
            FailureReason.PRECONDITION,
            "The managed Python does not satisfy this component.",
            f"Pinned {runtime.artifact.version}; required {describe_range(minimum, below)}.",
        )
    if dry_run:
        return InterpreterResolution.found((str(runtime.interpreter(context.layout)),))
    return runtime.ensure(context, component_id)


def _extract_tar(archive: Path, destination: Path) -> None:
    """Unpack with the stdlib's data filter: no absolute paths, no links out, no traversal."""
    destination = destination.resolve()
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            name = member.name
            if name.startswith("/") or ".." in Path(name).parts or Path(name).is_absolute():
                raise ValueError(f"refusing archive entry outside the destination: {name}")
            if member.issym() or member.islnk():
                raise ValueError(f"refusing link entry in a runtime archive: {name}")
        bundle.extractall(destination, filter="data")


__all__ = [
    "ARTIFACT_ID",
    "COMPONENT_ID",
    "EXECUTABLE",
    "InterpreterResolution",
    "ManagedPythonRuntime",
    "managed_python_runtime",
    "resolve_interpreter",
]
