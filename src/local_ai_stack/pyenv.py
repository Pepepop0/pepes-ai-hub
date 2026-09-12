"""Managed Python environments.

Two components in the stack are Python programs rather than downloadable
binaries: Open WebUI is a published package and ComfyUI is a source checkout
with requirements. Both are installed the same way — a virtual environment
inside the component's own directory under StackLayout, with pinned
requirements installed into it.

Everything here goes through CommandRunner, so there is no shell, argv is a
list, timeouts apply and output is sanitized. The global interpreter, the user
PATH and any system site-packages are never modified: a virtual environment is
precisely how an install stays contained and removable.

Interpreter choice is explicit. A component states the version range it
supports and this module finds an interpreter inside it, preferring the one
already running. When none matches, the caller gets a clear refusal rather than
an install against an unsupported Python.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .runner import CommandResult, CommandRunner, OutputSink

VENV_DIR_NAME = ".venv"
PROBE_TIMEOUT = 30.0
VENV_TIMEOUT = 300.0
PIP_TIMEOUT = 3600.0

_IS_WINDOWS = os.name == "nt"
_VERSION_PROBE = "import sys; print('%d.%d' % sys.version_info[:2])"

# Interpreters worth trying, in order, after the one already running. The
# Windows launcher first because it is the reliable way to reach a specific
# version without touching PATH.
_CANDIDATE_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("py", "-3.12"),
    ("py", "-3.11"),
    ("python3.12",),
    ("python3.11",),
    ("python3",),
    ("python",),
)


class PythonEnvironmentError(RuntimeError):
    """No suitable interpreter, or an environment operation failed."""


@dataclass(frozen=True)
class PythonEnvironment:
    """A virtual environment owned by one component."""

    root: Path

    @classmethod
    def inside(cls, component_dir: Path) -> PythonEnvironment:
        return cls(root=Path(component_dir) / VENV_DIR_NAME)

    @property
    def bin_dir(self) -> Path:
        return self.root / ("Scripts" if _IS_WINDOWS else "bin")

    @property
    def python(self) -> Path:
        return self.bin_dir / ("python.exe" if _IS_WINDOWS else "python")

    def script(self, name: str) -> Path:
        """The console script a package installs, e.g. `open-webui`."""
        return self.bin_dir / (f"{name}.exe" if _IS_WINDOWS else name)

    @property
    def exists(self) -> bool:
        return self.python.is_file()


def _probe_version(runner: CommandRunner, prefix: Sequence[str]) -> tuple[int, int] | None:
    result = runner.run([*prefix, "-c", _VERSION_PROBE], timeout=PROBE_TIMEOUT, dry_run=False)
    if not result.ok:
        return None
    text = result.stdout.strip().splitlines()
    if not text:
        return None
    parts = text[-1].strip().split(".")
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def _in_range(version: tuple[int, int], minimum: tuple[int, int], below: tuple[int, int]) -> bool:
    return minimum <= version < below


def find_interpreter(
    runner: CommandRunner,
    *,
    minimum: tuple[int, int] = (3, 11),
    below: tuple[int, int] = (3, 13),
) -> tuple[str, ...] | None:
    """An interpreter command inside the supported range, or None.

    Returns an argv prefix rather than a path, because the Windows launcher
    selects a version through an argument (`py -3.12`).
    """
    running = (sys.version_info.major, sys.version_info.minor)
    if _in_range(running, minimum, below):
        return (sys.executable,)
    for prefix in _CANDIDATE_PREFIXES:
        if runner.which(prefix[0]) is None:
            continue
        version = _probe_version(runner, prefix)
        if version is not None and _in_range(version, minimum, below):
            return prefix
    return None


def describe_range(minimum: tuple[int, int], below: tuple[int, int]) -> str:
    return f">={minimum[0]}.{minimum[1]},<{below[0]}.{below[1]}"


def create_environment(
    runner: CommandRunner,
    environment: PythonEnvironment,
    interpreter: Sequence[str],
    *,
    dry_run: bool = False,
    timeout: float = VENV_TIMEOUT,
    on_output: OutputSink | None = None,
) -> CommandResult:
    """Create the virtual environment. Idempotent: an existing one is reused."""
    result = runner.run(
        [*interpreter, "-m", "venv", str(environment.root)],
        timeout=timeout,
        dry_run=dry_run,
        on_output=on_output,
    )
    if not dry_run and not result.ok:
        raise PythonEnvironmentError(
            f"could not create the virtual environment at {environment.root}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result


# --- observacao de pip ------------------------------------------------------

_SIZE_UNITS = {
    "b": 1,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
}
_COLLECTING_RE = re.compile(r"^Collecting\s+([A-Za-z0-9][A-Za-z0-9_.\-\[\],]*)")
_DOWNLOADING_SIZE_RE = re.compile(r"^\s*Downloading\s+\S+\s+\(([\d.]+)\s*([kKmMgG]i?B)\)")
_PROGRESS_TICK_RE = re.compile(
    r"([\d.]+)\s*/\s*([\d.]+)\s*([kKmMgG]i?B)\s+([\d.]+)\s*([kKmMgG]i?B)/s"
)
_INSTALLING_RE = re.compile(r"^Installing collected packages:\s*(.+)$")
_SUCCESS_RE = re.compile(r"^Successfully installed\s+(.+)$")


def _size_to_bytes(value: str, unit: str) -> int | None:
    scale = _SIZE_UNITS.get(unit.lower())
    if scale is None:
        return None
    try:
        return round(float(value) * scale)
    except ValueError:
        return None


@dataclass(frozen=True)
class PipEvent:
    """One sanitized fact parsed from a pip invocation's own output.

    Deliberately not an operation-contract type: this module has no reason to
    know about ``OperationActivity``. It is a plain domain fact; the caller
    decides how, or whether, to publish it.
    """

    action: str  # "resolving" | "downloading" | "installing" | "installed"
    package: str
    completed_bytes: int | None = None
    total_bytes: int | None = None
    bytes_per_second: float | None = None


PipObserver = Callable[[PipEvent], None]


class PipOutputParser:
    """Turns one pip invocation's line-by-line output into :class:`PipEvent`.

    Stateful only across one call: it remembers which package the most recent
    ``Collecting`` line named, since a progress tick or a download-size line
    does not repeat it. Unrecognized lines (pip's own warnings, resolver
    chatter) are silently ignored rather than forwarded -- nothing here ever
    hands raw pip text to its observer.
    """

    def __init__(self, on_event: PipObserver) -> None:
        self._on_event = on_event
        self._current: str | None = None

    def feed(self, _stream: str, line: str) -> None:
        text = line.strip()
        if not text:
            return
        match = _COLLECTING_RE.match(text)
        if match:
            self._current = match.group(1)
            self._on_event(PipEvent("resolving", self._current))
            return
        match = _DOWNLOADING_SIZE_RE.match(text)
        if match and self._current:
            total = _size_to_bytes(match.group(1), match.group(2))
            self._on_event(PipEvent("downloading", self._current, total_bytes=total))
            return
        match = _PROGRESS_TICK_RE.search(text)
        if match and self._current:
            completed = _size_to_bytes(match.group(1), match.group(3))
            total = _size_to_bytes(match.group(2), match.group(3))
            speed = _size_to_bytes(match.group(4), match.group(5))
            self._on_event(
                PipEvent(
                    "downloading",
                    self._current,
                    completed_bytes=completed,
                    total_bytes=total,
                    bytes_per_second=float(speed) if speed is not None else None,
                )
            )
            return
        match = _INSTALLING_RE.match(text)
        if match:
            for name in _split_names(match.group(1)):
                self._on_event(PipEvent("installing", name))
            return
        match = _SUCCESS_RE.match(text)
        if match:
            for token in match.group(1).split():
                self._on_event(PipEvent("installed", token.rsplit("-", 1)[0]))


def _split_names(joined: str) -> list[str]:
    return [name.strip() for name in joined.split(",") if name.strip()]


def pip_install(
    runner: CommandRunner,
    environment: PythonEnvironment,
    requirements: Sequence[str],
    *,
    dry_run: bool = False,
    timeout: float = PIP_TIMEOUT,
    extra_args: Sequence[str] = (),
    on_output: OutputSink | None = None,
) -> CommandResult:
    """Install pinned requirements into the environment.

    Requirements are passed verbatim and are expected to be pinned by the
    caller; nothing here resolves a floating version.
    """
    if not requirements:
        raise PythonEnvironmentError("no requirements given")
    result = runner.run(
        [
            str(environment.python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--progress-bar",
            "ascii",
            *extra_args,
            *requirements,
        ],
        timeout=timeout,
        dry_run=dry_run,
        on_output=on_output,
    )
    if not dry_run and not result.ok:
        raise PythonEnvironmentError(
            f"pip install failed in {environment.root}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def distribution_version(
    runner: CommandRunner, environment: PythonEnvironment, distribution: str
) -> str | None:
    """The installed version of a package, read from inside the environment.

    Uses importlib.metadata rather than a `--version` flag, so it does not
    depend on any command-line contract the package may change.
    """
    if not environment.exists:
        return None
    code = (
        "import importlib.metadata as m\n"
        "try:\n"
        f"    print(m.version({distribution!r}))\n"
        "except Exception:\n"
        "    pass\n"
    )
    result = runner.run([str(environment.python), "-c", code], timeout=PROBE_TIMEOUT, dry_run=False)
    if not result.ok:
        return None
    text = result.stdout.strip()
    return text.splitlines()[-1].strip() if text else None
