"""Virtual environments as they really behave, not as a fake answers for them.

Every other environment test fakes the command runner, and the fake answers
``python -m venv`` by writing an interpreter file and returning 0 -- whether or
not an environment is already there, and whether or not something is running
from it. That is how a retry could fail on a real machine and pass every test:
re-running ``venv`` over a working environment is not a no-op. It rewrites
``pyvenv.cfg`` and copies the interpreter over the existing one, and on Windows
that copy is refused while any process started from it is still alive.

These tests build real environments with this interpreter and hold a real
process open on them. Nothing leaves ``tmp_path``; nothing reaches a network.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from local_ai_stack.pyenv import (
    UNCLASSIFIED_ENVIRONMENT_SUMMARY,
    EnvironmentFailureKind,
    PythonEnvironment,
    classify_environment_failure,
    create_environment,
    environment_is_reusable,
)
from local_ai_stack.runner import CommandResult, CommandRunner

WINDOWS_ONLY = pytest.mark.skipif(
    os.name != "nt", reason="the interpreter lock is Windows-specific"
)
INTERPRETER = (sys.executable,)


def build_venv(root: Path, *extra: str) -> PythonEnvironment:
    result = CommandRunner().run([sys.executable, "-m", "venv", *extra, str(root)], timeout=300.0)
    assert result.ok, result.stderr
    return PythonEnvironment(root=root)


@contextmanager
def process_running_from(environment: PythonEnvironment) -> Iterator[None]:
    """Keep the environment's interpreter in use, as a leftover pip would."""
    child = subprocess.Popen(
        [str(environment.python), "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(1.0)
        assert child.poll() is None, "the holding process exited before the test ran"
        yield
    finally:
        child.kill()
        child.wait()


@pytest.fixture(scope="module")
def venv_root(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("environments")


# --- o perigo, observado --------------------------------------------------------


@WINDOWS_ONLY
def test_recreating_a_venv_over_an_interpreter_in_use_is_refused(venv_root):
    """The hazard itself: what the provisioner used to do on every retry."""
    environment = build_venv(venv_root / "hazard")
    config = environment.root / "pyvenv.cfg"
    before = config.stat().st_mtime_ns

    with process_running_from(environment):
        result = CommandRunner().run(
            [sys.executable, "-m", "venv", str(environment.root)], timeout=300.0
        )

    assert result.returncode != 0
    assert "Permission denied" in result.stderr
    # venv writes its configuration before it copies the interpreter, so even
    # a refused run leaves a rewritten pyvenv.cfg -- the fingerprint the real
    # partial install carried.
    assert config.stat().st_mtime_ns != before
    failure = classify_environment_failure(result)
    assert failure.kind is EnvironmentFailureKind.INTERPRETER_IN_USE


# --- a regressao ---------------------------------------------------------------


def test_a_working_venv_in_use_is_reused_instead_of_recreated(venv_root):
    """Regression: a retry over a partial install failed creating its venv.

    The environment was complete and pinned -- only the marker was missing --
    yet the provisioner re-ran `venv` over it while a process from the earlier
    attempt still held the interpreter, and the whole install failed.
    """
    environment = build_venv(venv_root / "retry")
    config = environment.root / "pyvenv.cfg"
    before = config.stat().st_mtime_ns

    with process_running_from(environment):
        assert environment_is_reusable(CommandRunner(), environment, INTERPRETER)
        result = create_environment(CommandRunner(), environment, INTERPRETER)

    assert result.ok
    assert config.stat().st_mtime_ns == before, "a reusable environment was recreated"
    assert environment_is_reusable(CommandRunner(), environment, INTERPRETER)


def test_a_venv_whose_interpreter_will_not_start_is_rebuilt(venv_root):
    environment = build_venv(venv_root / "broken")
    environment.python.write_bytes(b"not an interpreter")

    assert not environment_is_reusable(CommandRunner(), environment, INTERPRETER)
    create_environment(CommandRunner(), environment, INTERPRETER)

    assert environment_is_reusable(CommandRunner(), environment, INTERPRETER)


def test_a_venv_interrupted_before_pip_was_installed_is_rebuilt(venv_root):
    """Its interpreter starts, but a later install would have nothing to run."""
    environment = build_venv(venv_root / "no-pip", "--without-pip")

    assert environment.exists
    assert not environment_is_reusable(CommandRunner(), environment, INTERPRETER)
    create_environment(CommandRunner(), environment, INTERPRETER)

    assert environment_is_reusable(CommandRunner(), environment, INTERPRETER)


def test_an_absent_venv_is_not_reusable(venv_root):
    environment = PythonEnvironment(root=venv_root / "never-created")
    assert not environment_is_reusable(CommandRunner(), environment, INTERPRETER)


def test_a_venv_built_by_another_python_minor_is_not_reused(venv_root):
    environment = build_venv(venv_root / "other-minor")

    class OtherMinor(CommandRunner):
        """Reports a different minor for the selected interpreter only."""

        def run(self, argv, **kwargs):
            items = tuple(str(a) for a in argv)
            if items[0] == sys.executable and "-c" in items:
                return CommandResult(argv=items, returncode=0, stdout="3.99\n")
            return super().run(argv, **kwargs)

    assert not environment_is_reusable(OtherMinor(), environment, INTERPRETER)


def test_a_dry_run_never_probes_or_reuses(tmp_path):
    calls: list[tuple[str, ...]] = []

    class Recording(CommandRunner):
        def run(self, argv, **kwargs):
            calls.append(tuple(str(a) for a in argv))
            return super().run(argv, **kwargs)

    environment = PythonEnvironment(root=tmp_path / "dry")
    result = create_environment(Recording(dry_run=True), environment, INTERPRETER, dry_run=True)

    assert result.dry_run
    assert not environment.root.exists()
    assert not any("-c" in call for call in calls), "a dry run probed an interpreter"


# --- classificacao ---------------------------------------------------------------


def failure_for(stderr: str = "", **changes):
    return classify_environment_failure(
        CommandResult(argv=("python",), returncode=1, stderr=stderr, **changes)
    )


def test_the_observed_lock_error_is_an_interpreter_in_use():
    observed = (
        "Error: [Errno 13] Permission denied: "
        "'C:\\\\Users\\\\Someone\\\\stack\\\\apps\\\\open-webui\\\\.venv\\\\Scripts\\\\python.exe'"
    )
    failure = failure_for(observed)
    assert failure.kind is EnvironmentFailureKind.INTERPRETER_IN_USE
    assert "Someone" not in failure.summary and "Scripts" not in failure.summary


def test_a_sharing_violation_is_an_interpreter_in_use():
    failure = failure_for(
        "[WinError 32] The process cannot access the file because it is being used by another"
        " process: 'C:\\\\x\\\\Scripts\\\\python.exe'"
    )
    assert failure.kind is EnvironmentFailureKind.INTERPRETER_IN_USE


def test_a_denied_directory_is_not_mistaken_for_a_locked_interpreter():
    failure = failure_for("Error: [Errno 13] Permission denied: 'C:\\\\Program Files\\\\stack'")
    assert failure.kind is EnvironmentFailureKind.ACCESS_DENIED
    assert "Program Files" not in failure.summary


def test_a_failed_pip_bootstrap_is_classified():
    failure = failure_for(
        "Error: Command '['C:\\\\x\\\\Scripts\\\\python.exe', '-m', 'ensurepip', '--upgrade',"
        " '--default-pip']' returned non-zero exit status 1."
    )
    assert failure.kind is EnvironmentFailureKind.PIP_BOOTSTRAP_FAILED
    assert "ensurepip" not in failure.summary


def test_an_unrecognized_failure_repeats_nothing_of_the_output():
    failure = failure_for("Traceback (most recent call last):\n  File 'C:\\\\private\\\\x.py'")
    assert failure.kind is EnvironmentFailureKind.UNCLASSIFIED
    assert failure.summary == UNCLASSIFIED_ENVIRONMENT_SUMMARY


def test_a_timeout_is_reported_without_reading_output():
    assert failure_for(timed_out=True).kind is EnvironmentFailureKind.UNCLASSIFIED
