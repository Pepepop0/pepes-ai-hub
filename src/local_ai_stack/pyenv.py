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
from enum import StrEnum
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


class PipFailureKind(StrEnum):
    """What kind of thing went wrong, when pip's own output says so plainly.

    Only signals pip states unambiguously are classified. Anything else stays
    UNKNOWN rather than being guessed at: a wrong classification is worse than
    an honest "pip failed", because it sends whoever reads it the wrong way.
    """

    INVALID_INVOCATION = "invalid-invocation"
    NO_MATCHING_DISTRIBUTION = "no-matching-distribution"
    RESOLUTION_CONFLICT = "resolution-conflict"
    NETWORK = "network"
    CERTIFICATE = "certificate"
    BUILD_FAILED = "build-failed"
    NO_DISK_SPACE = "no-disk-space"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PipFailure:
    """One classified pip failure, already short and safe to show."""

    kind: PipFailureKind
    summary: str


class EnvironmentFailureKind(StrEnum):
    """Why `python -m venv` failed, when its own output says so plainly."""

    INTERPRETER_IN_USE = "interpreter-in-use"
    ACCESS_DENIED = "access-denied"
    PIP_BOOTSTRAP_FAILED = "pip-bootstrap-failed"
    UNCLASSIFIED = "environment-unclassified"


@dataclass(frozen=True)
class EnvironmentFailure:
    """One classified environment-creation failure, short and safe to show."""

    kind: EnvironmentFailureKind
    summary: str


class PythonEnvironmentError(RuntimeError):
    """No suitable interpreter, or an environment operation failed.

    ``failure`` is set when the cause could be classified -- a
    :class:`PipFailure` for an install, an :class:`EnvironmentFailure` for
    environment creation -- so a provisioner can choose the right failure
    reason and say why, instead of calling every problem a command failure.
    """

    def __init__(
        self, message: str, failure: PipFailure | EnvironmentFailure | None = None
    ) -> None:
        super().__init__(message)
        self.failure = failure


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


def _probe_version(
    runner: CommandRunner, prefix: Sequence[str], *, code: str = _VERSION_PROBE
) -> tuple[int, int] | None:
    result = runner.run([*prefix, "-c", code], timeout=PROBE_TIMEOUT, dry_run=False)
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


def running_interpreter_is_usable() -> bool:
    """False when ``sys.executable`` is a frozen application rather than a Python."""
    return not getattr(sys, "frozen", False)


def find_interpreter(
    runner: CommandRunner,
    *,
    minimum: tuple[int, int] = (3, 11),
    below: tuple[int, int] = (3, 13),
) -> tuple[str, ...] | None:
    """An interpreter command inside the supported range, or None.

    Returns an argv prefix rather than a path, because the Windows launcher
    selects a version through an argument (`py -3.12`).

    The running interpreter is only a candidate when it *is* an interpreter.
    Inside a frozen build ``sys.executable`` is the Hub itself, and
    ``PepitosAIHub.exe -m venv`` would open another Hub, not build an
    environment - so a frozen process never offers itself. The packaged
    product resolves its interpreter through :mod:`python_runtime` instead;
    this fallback exists so no other path can regress into that mistake.
    """
    running = (sys.version_info.major, sys.version_info.minor)
    if not running_interpreter_is_usable():
        pass
    elif _in_range(running, minimum, below):
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


# Proves an existing environment is worth keeping: its interpreter starts, it
# can import pip (so a later install has something to run), and it reports the
# version it was built with. A venv interrupted before ensurepip finished passes
# the first check and fails the second, which is exactly the one to rebuild.
_ENVIRONMENT_PROBE = "import sys, pip; print('%d.%d' % sys.version_info[:2])"


def environment_is_reusable(
    runner: CommandRunner, environment: PythonEnvironment, interpreter: Sequence[str]
) -> bool:
    """True when an existing environment already works and matches the interpreter.

    Read-only: it runs the environment's own Python, never writes to it. An
    environment built from a different minor version is not reused -- its
    compiled packages belong to that version -- and neither is one whose
    interpreter will not start or has no pip.
    """
    if not environment.exists:
        return False
    existing = _probe_version(runner, (str(environment.python),), code=_ENVIRONMENT_PROBE)
    if existing is None:
        return False
    return existing == _probe_version(runner, interpreter)


def create_environment(
    runner: CommandRunner,
    environment: PythonEnvironment,
    interpreter: Sequence[str],
    *,
    dry_run: bool = False,
    timeout: float = VENV_TIMEOUT,
    on_output: OutputSink | None = None,
) -> CommandResult:
    """Create the virtual environment, or keep one that already works.

    Re-running ``python -m venv`` over a working environment is not a harmless
    no-op. It rewrites ``pyvenv.cfg`` and then copies the interpreter over the
    existing one, and on Windows that copy fails with "Permission denied" for
    as long as any process started from that interpreter is still running -- a
    pip left over from an interrupted attempt, say. A retry would then fail on
    an environment that needed nothing. So a reusable environment is kept as
    it is, and only an absent or broken one is (re)created.
    """
    command = [*interpreter, "-m", "venv", str(environment.root)]
    if not dry_run and environment_is_reusable(runner, environment, interpreter):
        return CommandResult(argv=tuple(command), returncode=0, stdout="reused")
    result = runner.run(command, timeout=timeout, dry_run=dry_run, on_output=on_output)
    if not dry_run and not result.ok:
        failure = classify_environment_failure(result)
        raise PythonEnvironmentError(
            f"could not create the virtual environment: {failure.summary}", failure
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
# pip names the artifact it is fetching, which is how the payload downloads can
# be attributed at all: they arrive in a later phase with no `Collecting` line
# of their own, so the filename is the only thing that says which package they
# belong to.
_DOWNLOADING_SIZE_RE = re.compile(
    r"^\s*Downloading\s+(?P<artifact>\S+)\s+\(([\d.]+)\s*([kKmMgG]i?B)\)"
)
# `<name>-<version>-...`: everything up to the first hyphen is the distribution,
# normalized the way pip itself prints it back in "Installing collected
# packages". A metadata fetch is resolver chatter about a package already
# reported as `Collecting`, and its size is not the package's, so it is skipped.
_ARTIFACT_NAME_RE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*?)-\d")
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

    Stateful only across one call: it tracks which package pip is currently
    talking about, because a progress tick names no package at all. That is
    also why a download is attributed from the artifact filename rather than
    from the last ``Collecting`` line: pip resolves everything first and only
    then fetches the payloads, so by download time the last package collected
    is whichever one happened to be resolved last -- not the one arriving.

    Unrecognized lines (pip's own warnings, resolver chatter) are silently
    ignored rather than forwarded: nothing here hands raw pip text to its
    observer.
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
        if match:
            artifact = match.group("artifact")
            if _artifact_filename(artifact).endswith(".metadata"):
                return
            package = _package_of(artifact)
            if package is None:
                # The last Collecting line belongs to resolver order, not
                # necessarily to this payload. Clear it so a following bare
                # progress tick cannot inherit the wrong package either.
                self._current = None
                return
            self._current = package
            total = _size_to_bytes(match.group(2), match.group(3))
            self._on_event(PipEvent("downloading", package, total_bytes=total))
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


def _package_of(artifact: str) -> str | None:
    """The distribution an artifact filename belongs to, or None if unclear.

    A URL is reduced to its last segment first, since pip prints a full index
    URL when it is fetching from one; query and fragment data are excluded
    before parsing. An unparseable name yields None rather than a guess.
    """
    name = _artifact_filename(artifact)
    match = _ARTIFACT_NAME_RE.match(name)
    return match.group("name").replace("_", "-").lower() if match else None


def _artifact_filename(artifact: str) -> str:
    """A URL/path's filename, without query or fragment data."""
    return artifact.rsplit("/", 1)[-1].split("?", 1)[0].split("#", 1)[0]


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
        pip_install_argv(environment, requirements, extra_args=extra_args),
        timeout=timeout,
        dry_run=dry_run,
        on_output=on_output,
    )
    if not dry_run and not result.ok:
        failure = classify_pip_failure(result)
        raise PythonEnvironmentError(
            f"pip install failed in {environment.root}: {failure.summary}", failure
        )
    return result


def pip_install_argv(
    environment: PythonEnvironment,
    requirements: Sequence[str],
    *,
    extra_args: Sequence[str] = (),
) -> list[str]:
    """The exact argv an install would run. Useful for plans and for tests.

    Every option here has to exist on the pip that ``python -m venv`` puts in
    a fresh environment, which is whatever the host interpreter bundles -- not
    the newest pip. Nothing probes pip's version first, so an option whose
    spelling or accepted values changed between releases does not belong here:
    pip rejects the whole invocation during argument parsing, before doing any
    work, and the install fails with nothing installed and nothing attempted.
    """
    return [
        str(environment.python),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        *extra_args,
        *requirements,
    ]


# Signals pip states plainly enough to classify. Each is matched against pip's
# own combined output; the first match wins, so the more specific patterns
# come first. Nothing here parses a traceback or keeps raw output.
_PIP_SIGNALS: tuple[tuple[PipFailureKind, re.Pattern[str], str], ...] = (
    (
        PipFailureKind.INVALID_INVOCATION,
        re.compile(
            r"(?:option\s+--[A-Za-z0-9][A-Za-z0-9-]*:\s+invalid choice"
            r"|no such option:\s+--[A-Za-z0-9][A-Za-z0-9-]*"
            r"|unrecognized arguments?:\s+--[A-Za-z0-9][A-Za-z0-9-]*)",
            re.IGNORECASE,
        ),
        "pip refused the install options; the installer asked for something this pip does not accept",
    ),
    (
        PipFailureKind.NO_MATCHING_DISTRIBUTION,
        re.compile(
            r"(?:No matching distribution found for|Could not find a version that satisfies"
            r" the requirement)\s*(?P<item>[^\s;(]+)?",
            re.IGNORECASE,
        ),
        "no distribution matches {item} for this Python",
    ),
    (
        PipFailureKind.RESOLUTION_CONFLICT,
        re.compile(r"(?:ResolutionImpossible|conflict is caused by)", re.IGNORECASE),
        "the pinned requirements conflict with each other",
    ),
    (
        PipFailureKind.CERTIFICATE,
        re.compile(
            r"(?:CERTIFICATE_VERIFY_FAILED|SSLError|SSLCertVerificationError)", re.IGNORECASE
        ),
        "the package index could not be verified over TLS",
    ),
    (
        PipFailureKind.NETWORK,
        re.compile(
            r"(?:Failed to establish a new connection|Temporary failure in name resolution"
            r"|Network is unreachable|Read timed out|ConnectionResetError|ProxyError"
            r"|Connection aborted|Could not fetch URL|retries exceeded)",
            re.IGNORECASE,
        ),
        "the package index could not be reached",
    ),
    (
        PipFailureKind.NO_DISK_SPACE,
        re.compile(r"(?:No space left on device|not enough space|ENOSPC)", re.IGNORECASE),
        "there is not enough disk space to install the dependencies",
    ),
    (
        PipFailureKind.BUILD_FAILED,
        re.compile(
            r"(?:metadata-generation-failed|Failed building wheel for\s*(?P<item>\S+))",
            re.IGNORECASE,
        ),
        "a dependency failed to build from source",
    ),
)

# A requirement as pip names one back to us: a distribution name with an
# optional version specifier and extras. Anything else a capture group picks up
# -- a path, a URL, an index with credentials in it -- does not match, and is
# dropped rather than repeated, so no part of pip's output can ride along.
_REQUIREMENT_TOKEN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._+\-\[\]]{0,59}"
    r"(?:[=<>!~]=?[A-Za-z0-9.*+!_\-]{1,31})?$"
)

# What an unclassified failure says. Pip's own last line is deliberately not
# here: on a real failure it is as likely to be a traceback, a cache path or
# the whole index URL as it is to be the cause, and the event stream is public.
UNCLASSIFIED_SUMMARY = "pip failed without a cause it stated plainly"


def classify_pip_failure(result: CommandResult) -> PipFailure:
    """Turn one failed pip run into a short, safe, classified summary.

    Deliberately not a parser of pip's whole output: it looks for a handful of
    phrases pip itself uses and says nothing beyond them. What leaves here is
    this module's own wording, plus at most one requirement pip named back --
    never a line of pip's output, a path, a URL or a traceback.
    """
    if result.timed_out:
        return PipFailure(PipFailureKind.NETWORK, "pip timed out while installing dependencies")
    if result.not_found:
        return PipFailure(
            PipFailureKind.INVALID_INVOCATION, "the managed Python could not be executed"
        )
    combined = f"{result.stderr}\n{result.stdout}"
    for kind, pattern, template in _PIP_SIGNALS:
        match = pattern.search(combined)
        if match is None:
            continue
        return PipFailure(kind, template.format(item=_requirement(match)))
    return PipFailure(PipFailureKind.UNKNOWN, UNCLASSIFIED_SUMMARY)


def _requirement(match: re.Match[str]) -> str:
    """The requirement a pip message named, when it really is one."""
    captured = (match.groupdict().get("item") or "").strip().strip(",;:'\"")
    return captured if _REQUIREMENT_TOKEN.match(captured) else "it"


# --- falhas de criacao de ambiente -----------------------------------------

# `venv` reports a failed copy as "[Errno 13] Permission denied: '<path>'" and a
# sharing violation as "[WinError 32]"; which file it names is what separates
# a locked interpreter from an unwritable directory. The path itself is only
# inspected, never repeated.
_DENIED_RE = re.compile(
    r"(?:\[Errno 13\] Permission denied|\[WinError 5\] Access is denied"
    r"|\[WinError 32\][^:\r\n]*)(?::\s*['\"](?P<path>[^'\"]*)['\"])?",
    re.IGNORECASE,
)
_INTERPRETER_NAME_RE = re.compile(
    r"[\\/](?:python|pythonw)(?:\d+(?:\.\d+)?)?(?:\.exe)?$", re.IGNORECASE
)
_SHARING_VIOLATION_RE = re.compile(r"\[WinError 32\]", re.IGNORECASE)
_ENSUREPIP_RE = re.compile(r"ensurepip", re.IGNORECASE)

UNCLASSIFIED_ENVIRONMENT_SUMMARY = "Python could not create it, without a cause it stated plainly"


def classify_environment_failure(result: CommandResult) -> EnvironmentFailure:
    """Turn one failed `python -m venv` run into a short, safe summary.

    Same rule as for pip: a handful of plain signals, this module's own words,
    and nothing of the output itself -- no path, no traceback.
    """
    if result.timed_out:
        return EnvironmentFailure(
            EnvironmentFailureKind.UNCLASSIFIED, "Python took too long to create it"
        )
    if result.not_found:
        return EnvironmentFailure(
            EnvironmentFailureKind.UNCLASSIFIED, "the selected Python could not be started"
        )
    combined = f"{result.stderr}\n{result.stdout}"
    denied = _DENIED_RE.search(combined)
    if denied is not None:
        path = denied.group("path") or ""
        if _INTERPRETER_NAME_RE.search(path):
            return EnvironmentFailure(
                EnvironmentFailureKind.INTERPRETER_IN_USE,
                "its Python is locked, usually by a process still running from it",
            )
        if _SHARING_VIOLATION_RE.search(denied.group(0)):
            return EnvironmentFailure(
                EnvironmentFailureKind.UNCLASSIFIED, UNCLASSIFIED_ENVIRONMENT_SUMMARY
            )
        return EnvironmentFailure(
            EnvironmentFailureKind.ACCESS_DENIED, "its directory could not be written to"
        )
    if _ENSUREPIP_RE.search(combined):
        return EnvironmentFailure(
            EnvironmentFailureKind.PIP_BOOTSTRAP_FAILED, "Python could not install pip into it"
        )
    return EnvironmentFailure(EnvironmentFailureKind.UNCLASSIFIED, UNCLASSIFIED_ENVIRONMENT_SUMMARY)


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
