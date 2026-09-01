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
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .runner import CommandResult, CommandRunner

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
) -> CommandResult:
    """Create the virtual environment. Idempotent: an existing one is reused."""
    result = runner.run(
        [*interpreter, "-m", "venv", str(environment.root)],
        timeout=timeout,
        dry_run=dry_run,
    )
    if not dry_run and not result.ok:
        raise PythonEnvironmentError(
            f"could not create the virtual environment at {environment.root}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def pip_install(
    runner: CommandRunner,
    environment: PythonEnvironment,
    requirements: Sequence[str],
    *,
    dry_run: bool = False,
    timeout: float = PIP_TIMEOUT,
    extra_args: Sequence[str] = (),
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
            *extra_args,
            *requirements,
        ],
        timeout=timeout,
        dry_run=dry_run,
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
