"""pip as this project uses it: the invocation, its output, and its failures.

:class:`PipOutputParser` never sees a real network or a real download: it is
fed lines exactly as :meth:`CommandRunner.run`'s ``on_output`` would deliver
them, and is checked against the output shapes pip produces both when it is
reporting progress and when it is only narrating steps.

The argv tests are different in kind, and deliberately so: they hand what the
product really builds to a real pip, offline, because the rest of the suite
fakes the command runner and therefore cannot tell a valid option from an
invented one.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from local_ai_stack.pyenv import (
    UNCLASSIFIED_SUMMARY,
    PipEvent,
    PipFailureKind,
    PipOutputParser,
    PythonEnvironment,
    PythonEnvironmentError,
    _size_to_bytes,
    classify_pip_failure,
    pip_install,
    pip_install_argv,
)
from local_ai_stack.runner import CommandResult, CommandRunner


def events_for(*lines: str) -> list[PipEvent]:
    seen: list[PipEvent] = []
    parser = PipOutputParser(seen.append)
    for line in lines:
        parser.feed("stdout", line)
    return seen


def failure_for(stderr: str = "", stdout: str = "", **changes):
    return classify_pip_failure(
        CommandResult(argv=("pip",), returncode=1, stderr=stderr, stdout=stdout, **changes)
    )


def test_collecting_a_package_is_a_resolving_event():
    events = events_for("Collecting torch==2.4.0")
    assert events == [PipEvent("resolving", "torch")]


def test_a_download_size_line_reports_a_known_total_with_no_bytes_yet():
    events = events_for("Collecting torch==2.4.0", "  Downloading torch-2.4.0-cp311.whl (162.1 MB)")
    assert events[-1].action == "downloading"
    assert events[-1].package == "torch"
    assert events[-1].total_bytes == 162_100_000
    assert events[-1].completed_bytes is None


def test_a_download_size_line_stands_on_its_own_artifact_name():
    """No `Collecting` is needed: pip's payload phase never repeats one."""
    events = events_for("  Downloading torch-2.4.0-cp311.whl (162.1 MB)")
    assert events == [PipEvent("downloading", "torch", total_bytes=162_100_000)]


def test_a_progress_tick_reports_completed_total_and_speed():
    events = events_for(
        "Collecting torch==2.4.0",
        "     -------------------- ------------------- 41.9/162.1 MB 24.1 MB/s eta 0:00:05",
    )
    tick = events[-1]
    assert tick.action == "downloading"
    assert tick.completed_bytes == 41_900_000
    assert tick.total_bytes == 162_100_000
    assert tick.bytes_per_second == 24_100_000.0


def test_several_progress_ticks_for_the_same_package_accumulate_in_order():
    events = events_for(
        "Collecting torch==2.4.0",
        "     10.0/162.1 MB 20.0 MB/s eta 0:00:07",
        "     80.0/162.1 MB 22.0 MB/s eta 0:00:04",
        "     162.1/162.1 MB 21.0 MB/s eta 0:00:00",
    )
    ticks = [e for e in events if e.action == "downloading"]
    assert [t.completed_bytes for t in ticks] == [10_000_000, 80_000_000, 162_100_000]


def test_a_second_package_resets_the_current_package():
    events = events_for(
        "Collecting torch==2.4.0",
        "     162.1/162.1 MB 21.0 MB/s eta 0:00:00",
        "Collecting numpy==2.0.0",
        "     10.0/16.0 MB 5.0 MB/s eta 0:00:01",
    )
    ticks = [e for e in events if e.action == "downloading"]
    assert ticks[0].package == "torch"
    assert ticks[-1].package == "numpy"


def test_installing_collected_packages_reports_one_event_per_package():
    events = events_for("Installing collected packages: torch, numpy, filelock")
    assert [e.action for e in events] == ["installing"] * 3
    assert [e.package for e in events] == ["torch", "numpy", "filelock"]


def test_successfully_installed_reports_bare_package_names():
    events = events_for("Successfully installed torch-2.4.0 numpy-2.0.0 scikit-learn-1.2.0")
    assert [e.action for e in events] == ["installed"] * 3
    assert [e.package for e in events] == ["torch", "numpy", "scikit-learn"]


def test_a_full_pip_transcript_produces_the_expected_event_sequence():
    events = events_for(
        "Collecting torch==2.4.0",
        "  Downloading torch-2.4.0-cp311.whl (162.1 MB)",
        "     80.0/162.1 MB 22.0 MB/s eta 0:00:04",
        "     162.1/162.1 MB 21.0 MB/s eta 0:00:00",
        "Collecting numpy==2.0.0",
        "  Downloading numpy-2.0.0-cp311.whl (16.0 MB)",
        "     16.0/16.0 MB 8.0 MB/s eta 0:00:00",
        "Installing collected packages: torch, numpy",
        "Successfully installed torch-2.4.0 numpy-2.0.0",
    )
    assert [e.action for e in events] == [
        "resolving",
        "downloading",
        "downloading",
        "downloading",
        "resolving",
        "downloading",
        "downloading",
        "installing",
        "installing",
        "installed",
        "installed",
    ]


def test_a_payload_download_is_attributed_from_its_artifact_not_the_last_collected():
    """Regression, found against real pip: the phases are separate.

    pip resolves everything first (`Collecting` + a `.metadata` fetch per
    package) and only then downloads the payloads, with no `Collecting` line
    in between. Attributing those downloads to the last collected package
    labelled every byte count with whichever package happened to resolve last.
    """
    events = events_for(
        "Collecting rich==15.0.0",
        "  Downloading rich-15.0.0-py3-none-any.whl.metadata (18 kB)",
        "Collecting markdown-it-py>=2.2.0",
        "  Downloading markdown_it_py-4.2.0-py3-none-any.whl.metadata (7.4 kB)",
        "Collecting mdurl~=0.1",
        "  Downloading mdurl-0.1.2-py3-none-any.whl.metadata (1.6 kB)",
        "Downloading rich-15.0.0-py3-none-any.whl (310 kB)",
        "   ---------------------------------------- 310.7/310.7 kB 2.6 MB/s eta 0:00:00",
        "Downloading markdown_it_py-4.2.0-py3-none-any.whl (91 kB)",
    )
    downloads = [e for e in events if e.action == "downloading"]
    assert [e.package for e in downloads] == ["rich", "rich", "markdown-it-py"]
    assert downloads[0].total_bytes == 310_000
    assert downloads[1].completed_bytes == 310_700
    assert downloads[2].total_bytes == 91_000


def test_a_resolver_metadata_fetch_is_not_reported_as_a_package_download():
    """Its size is the metadata's, not the package's; reporting it misleads."""
    events = events_for(
        "Collecting torch==2.11.0+cu130",
        "  Downloading torch-2.11.0%2Bcu130-cp311-cp311-win_amd64.whl.metadata (29 kB)",
    )
    assert [e.action for e in events] == ["resolving"]


def test_an_artifact_name_is_normalized_the_way_pip_prints_it_back():
    events = events_for("Downloading markdown_it_py-4.2.0-py3-none-any.whl (91 kB)")
    assert [e.package for e in events] == ["markdown-it-py"]


def test_a_full_index_url_is_reduced_to_the_artifact_it_points_at():
    events = events_for(
        "Downloading https://download.pytorch.org/whl/cu130/torch-2.11.0%2Bcu130"
        "-cp311-cp311-win_amd64.whl (1915.2 MB)"
    )
    assert [e.package for e in events] == ["torch"]
    assert events[0].total_bytes == 1_915_200_000


def test_an_unparseable_artifact_does_not_inherit_the_last_collected_package():
    events = events_for(
        "Collecting spandrel",
        "  Downloading somefile.tar.gz (4.0 MB)",
        "     2.0/4.0 MB 1.0 MB/s eta 0:00:02",
    )
    assert events == [PipEvent("resolving", "spandrel")]


def test_a_download_with_no_package_context_at_all_is_ignored():
    assert events_for("  Downloading somefile.tar.gz (4.0 MB)") == []


def test_a_hyphenated_sdist_name_is_inferred_from_its_artifact():
    events = events_for("Downloading my-package-1.2.0.tar.gz (4.0 MB)")
    assert events == [PipEvent("downloading", "my-package", total_bytes=4_000_000)]


def test_metadata_with_a_query_string_is_ignored_without_exposing_the_query():
    events = events_for(
        "Collecting torch==2.11.0+cu130",
        "Downloading https://packages.invalid/torch-2.11.0.whl.metadata?token=secret (29 kB)",
    )
    assert events == [PipEvent("resolving", "torch")]


def test_unrecognized_lines_are_silently_ignored_never_forwarded_raw():
    events = events_for(
        "Requirement already satisfied: pip in c:\\some\\path",
        "WARNING: a new release of pip is available",
        "",
        "   ",
    )
    assert events == []


def test_blank_and_whitespace_only_lines_are_ignored():
    assert events_for("", "   ", "\t") == []


# --- unidades de tamanho -----------------------------------------------------


def test_decimal_units_convert_to_bytes():
    assert _size_to_bytes("1", "MB") == 1_000_000
    assert _size_to_bytes("1", "GB") == 1_000_000_000
    assert _size_to_bytes("1", "kB") == 1000


def test_binary_units_convert_to_bytes():
    assert _size_to_bytes("1", "MiB") == 1024**2
    assert _size_to_bytes("1", "GiB") == 1024**3


def test_an_unknown_unit_is_reported_as_none_not_a_guess():
    assert _size_to_bytes("1", "furlongs") is None


# --- a invocacao tem de existir no pip real ---------------------------------
#
# This is the regression that matters. Every other test in the suite replaces
# CommandRunner with a fake that answers "pip" with returncode 0, so an option
# pip does not accept is invisible: the install fails on a real machine during
# pip's own argument parsing, before a single package is looked at, and no
# controlled test notices. These tests hand the argv the product really builds
# to the real pip, which validates options before it touches the network.


def real_pip_rejects(argv: list[str]) -> tuple[int, str]:
    """Run a real pip offline and report (exit code, output).

    ``--no-index`` and ``--dry-run`` keep this entirely local: pip parses its
    options first, so an invalid option still exits 2 with "invalid choice"
    while a valid one gets as far as refusing to resolve without an index.
    """
    runner = CommandRunner()
    result = runner.run([*argv, "--no-index", "--dry-run"], timeout=120.0)
    return result.returncode, f"{result.stderr}\n{result.stdout}"


def test_the_install_argv_is_accepted_by_a_real_pip():
    """Regression: `--progress-bar ascii` made every real install exit 2.

    pip removed that value in 23.1; the pip a fresh `python -m venv` ships
    rejects it outright, so provisioning failed with nothing installed and
    nothing even attempted.
    """
    environment = PythonEnvironment(root=Path(sys.prefix))
    argv = pip_install_argv(environment, ["definitely-not-a-real-package-xyz"])
    # Run with this interpreter's own pip rather than the environment's, so the
    # test needs no venv of its own; the options under test are the product's.
    argv = [sys.executable, *argv[1:]]

    code, output = real_pip_rejects(argv)

    assert "invalid choice" not in output, output
    assert "no such option" not in output, output
    assert code != 2, output


@pytest.mark.parametrize(
    "option",
    [
        "--disable-pip-version-check",
        "--no-input",
    ],
)
def test_every_option_the_product_passes_exists_in_the_real_pip(option):
    environment = PythonEnvironment(root=Path(sys.prefix))
    assert option in pip_install_argv(environment, ["anything"])

    code, output = real_pip_rejects([sys.executable, "-m", "pip", "install", option, "anything"])

    assert "invalid choice" not in output and "no such option" not in output, output
    assert code != 2, output


def test_the_install_argv_carries_extra_args_before_requirements():
    environment = PythonEnvironment(root=Path("X:/env"))
    argv = pip_install_argv(
        environment, ["torch==2.11.0+cu130"], extra_args=["--extra-index-url", "https://example"]
    )
    assert argv[-1] == "torch==2.11.0+cu130"
    assert argv.index("--extra-index-url") < argv.index("torch==2.11.0+cu130")
    assert "--progress-bar" not in argv


# --- classificacao de falha do pip ------------------------------------------


def test_an_invalid_option_is_classified_as_an_invalid_invocation():
    """The exact output the real failed install produced."""
    observed = (
        "Usage:\n"
        "  python.exe -m pip install [options] <requirement specifier> ...\n"
        "\n"
        "option --progress-bar: invalid choice: 'ascii' (choose from 'on', 'off')\n"
    )
    failure = failure_for(stderr=observed)
    assert failure.kind is PipFailureKind.INVALID_INVOCATION
    assert "options" in failure.summary


def test_invalid_choice_from_a_build_subprocess_is_not_called_a_pip_invocation_error():
    failure = failure_for(stderr="setup.py: error: invalid choice: 'native'")
    assert failure.kind is PipFailureKind.UNKNOWN
    assert failure.summary == UNCLASSIFIED_SUMMARY


def test_a_missing_distribution_is_classified_and_names_the_requirement():
    failure = failure_for(stderr="ERROR: No matching distribution found for torch==2.11.0+cu130\n")
    assert failure.kind is PipFailureKind.NO_MATCHING_DISTRIBUTION
    assert "torch==2.11.0+cu130" in failure.summary


def test_an_unsatisfiable_requirement_is_also_a_missing_distribution():
    failure = failure_for(
        stderr="ERROR: Could not find a version that satisfies the requirement torch==9.9.9\n"
    )
    assert failure.kind is PipFailureKind.NO_MATCHING_DISTRIBUTION
    assert "torch==9.9.9" in failure.summary


def test_a_resolver_conflict_is_classified():
    failure = failure_for(stderr="ERROR: ResolutionImpossible: for help visit ...\n")
    assert failure.kind is PipFailureKind.RESOLUTION_CONFLICT


@pytest.mark.parametrize(
    "text",
    [
        "Failed to establish a new connection: [Errno 11001] getaddrinfo failed",
        "ERROR: Could not fetch URL https://pypi.org/simple/torch/",
        "Read timed out. (read timeout=15)",
        "Max retries exceeded with url: /simple/torch/",
    ],
)
def test_network_problems_are_classified_as_network(text):
    assert failure_for(stderr=text).kind is PipFailureKind.NETWORK


def test_a_tls_problem_is_classified_separately_from_a_plain_outage():
    failure = failure_for(stderr="SSLError(SSLCertVerificationError(CERTIFICATE_VERIFY_FAILED))")
    assert failure.kind is PipFailureKind.CERTIFICATE


def test_a_full_disk_is_classified_as_disk_space():
    assert failure_for(stderr="OSError: [Errno 28] No space left on device").kind is (
        PipFailureKind.NO_DISK_SPACE
    )


def test_a_build_failure_is_classified():
    failure = failure_for(
        stderr="error: metadata-generation-failed\nEncountered error while generating package"
    )
    assert failure.kind is PipFailureKind.BUILD_FAILED


def test_a_timeout_is_classified_without_reading_output():
    assert failure_for(timed_out=True).kind is PipFailureKind.NETWORK


def test_an_unclassifiable_failure_repeats_none_of_pips_output():
    """A traceback, a cache path and the index URL are all public-event poison."""
    failure = failure_for(
        stderr=(
            r"Traceback at C:\Users\Private Person\AppData\Local\pip-cache; "
            "token=sk-0123456789abcdef0123456789; "
            "--extra-index-url https://private-user:private-pass@packages.invalid/simple"
        )
    )
    assert failure.kind is PipFailureKind.UNKNOWN
    assert failure.summary == UNCLASSIFIED_SUMMARY
    for forbidden in ("Traceback", "pip-cache", "sk-0123", "--extra-index-url", "private-pass"):
        assert forbidden not in failure.summary


def test_a_classified_failure_drops_a_captured_value_that_is_not_a_requirement():
    """The capture group is pip's text, so only a real requirement may be shown."""
    failure = failure_for(
        stderr=(
            "ERROR: No matching distribution found for "
            r"https://private-user:private-pass@packages.invalid/simple"
        )
    )
    assert failure.kind is PipFailureKind.NO_MATCHING_DISTRIBUTION
    assert "private-pass" not in failure.summary
    assert "packages.invalid" not in failure.summary
    assert "it" in failure.summary


def test_a_direct_url_disguised_as_a_version_is_not_repeated():
    failure = failure_for(
        stderr=(
            "ERROR: No matching distribution found for "
            "torch==https://private-user:private-pass@packages.invalid/wheel"
        )
    )
    assert failure.kind is PipFailureKind.NO_MATCHING_DISTRIBUTION
    assert "private-pass" not in failure.summary
    assert "packages.invalid" not in failure.summary
    assert "it" in failure.summary


def test_a_classified_summary_stays_short():
    failure = failure_for(stderr="ERROR: No matching distribution found for " + "y" * 400)
    assert len(failure.summary) <= 160


def test_a_failed_install_raises_with_the_classified_failure_attached():
    class RefusingRunner(CommandRunner):
        def run(self, argv, **kwargs):
            return CommandResult(
                argv=tuple(str(a) for a in argv),
                returncode=1,
                stderr="ERROR: No matching distribution found for torch==2.11.0+cu130",
            )

    environment = PythonEnvironment(root=Path("X:/env"))
    with pytest.raises(PythonEnvironmentError) as exc:
        pip_install(RefusingRunner(), environment, ["torch==2.11.0+cu130"])

    assert exc.value.failure is not None
    assert exc.value.failure.kind is PipFailureKind.NO_MATCHING_DISTRIBUTION
    assert "torch==2.11.0+cu130" in str(exc.value)
