"""Turning pip's own output into sanitized, structured facts.

:class:`PipOutputParser` never sees a real network or a real download: it is
fed lines exactly as :meth:`CommandRunner.run`'s ``on_output`` would deliver
them, and is checked against the very output shapes pip is known to produce
in both the informative (no ``--progress-bar``) and the progress-bar-enabled
cases.
"""

from __future__ import annotations

from local_ai_stack.pyenv import PipEvent, PipOutputParser, _size_to_bytes


def events_for(*lines: str) -> list[PipEvent]:
    seen: list[PipEvent] = []
    parser = PipOutputParser(seen.append)
    for line in lines:
        parser.feed("stdout", line)
    return seen


def test_collecting_a_package_is_a_resolving_event():
    events = events_for("Collecting torch==2.4.0")
    assert events == [PipEvent("resolving", "torch")]


def test_a_download_size_line_reports_a_known_total_with_no_bytes_yet():
    events = events_for("Collecting torch==2.4.0", "  Downloading torch-2.4.0-cp311.whl (162.1 MB)")
    assert events[-1].action == "downloading"
    assert events[-1].package == "torch"
    assert events[-1].total_bytes == 162_100_000
    assert events[-1].completed_bytes is None


def test_a_download_size_line_with_no_prior_collecting_line_is_ignored():
    events = events_for("  Downloading torch-2.4.0-cp311.whl (162.1 MB)")
    assert events == []


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
