from __future__ import annotations

import pytest
from helpers import NO_LISTENER_SCAN, NO_PROCESS_SCAN, NOTHING_LISTENING, listening, running

from local_ai_stack.inventory import Listener, ListenerReport, ProcessInfo, ScanOutcome
from local_ai_stack.processes import (
    COMFYUI_PROCESS,
    DEFAULT_PROCESS_SPECS,
    OLLAMA_PROCESS,
    OPEN_WEBUI_PROCESS,
    ProcessOrigin,
    RuntimeProcessSpec,
    RuntimeProcessState,
    discover_process,
    discover_processes,
    process_spec_by_id,
)

OLLAMA_SERVE = ProcessInfo(
    1001,
    "ollama.exe",
    r"C:\Users\pepe\AppData\Local\Programs\Ollama\ollama.exe",
    "ollama.exe serve",
)
OLLAMA_TRAY = ProcessInfo(1002, "ollama app.exe", r"C:\Programs\Ollama\ollama app.exe", "")
OPEN_WEBUI_PY = ProcessInfo(
    2001,
    "python.exe",
    r"C:\Python311\python.exe",
    r"C:\Python311\python.exe C:\uv\cache\open_webui\main.py serve",
)
COMFY_DESKTOP = ProcessInfo(
    3001, "ComfyUI.exe", r"C:\Users\pepe\AppData\Local\Programs\ComfyUI\ComfyUI.exe", ""
)
UNRELATED = ProcessInfo(
    4001, "chrome.exe", r"C:\Program Files\Chrome\chrome.exe", "chrome.exe --tab"
)


def test_default_specs_cover_the_three_services():
    assert [s.service_id for s in DEFAULT_PROCESS_SPECS] == ["ollama", "open-webui", "comfyui"]


def test_process_spec_by_id_returns_spec_or_raises():
    assert process_spec_by_id("comfyui") is COMFYUI_PROCESS
    with pytest.raises(KeyError):
        process_spec_by_id("flux")


@pytest.mark.parametrize(
    ("spec", "process"),
    [
        (OLLAMA_PROCESS, OLLAMA_SERVE),
        (OLLAMA_PROCESS, OLLAMA_TRAY),
        (OPEN_WEBUI_PROCESS, OPEN_WEBUI_PY),
        (COMFYUI_PROCESS, COMFY_DESKTOP),
    ],
)
def test_specs_recognise_their_process(spec, process):
    assert spec.matches(process)


def test_specs_ignore_unrelated_processes():
    assert not any(spec.matches(UNRELATED) for spec in DEFAULT_PROCESS_SPECS)


def test_externally_started_process_is_detected():
    status = discover_process(
        OLLAMA_PROCESS,
        processes=running(OLLAMA_SERVE, UNRELATED),
        listeners=listening(11434, pid=OLLAMA_SERVE.pid),
    )
    assert status.state is RuntimeProcessState.RUNNING
    assert status.origin is ProcessOrigin.EXTERNAL
    assert status.pid == OLLAMA_SERVE.pid
    assert status.running


def test_process_started_by_us_is_reported_as_managed():
    status = discover_process(
        OLLAMA_PROCESS,
        processes=running(OLLAMA_SERVE),
        listeners=NOTHING_LISTENING,
        managed_pids=frozenset({OLLAMA_SERVE.pid}),
    )
    assert status.origin is ProcessOrigin.MANAGED


def test_no_process_found_is_stopped():
    status = discover_process(
        COMFYUI_PROCESS, processes=running(UNRELATED), listeners=NOTHING_LISTENING
    )
    assert status.state is RuntimeProcessState.STOPPED
    assert status.processes == ()
    assert status.origin is ProcessOrigin.UNKNOWN
    assert status.pid is None


def test_port_scoped_discovery_reports_only_the_process_serving_that_endpoint():
    status = discover_process(
        OLLAMA_PROCESS,
        processes=running(OLLAMA_TRAY, OLLAMA_SERVE, UNRELATED),
        listeners=listening(11434, pid=OLLAMA_SERVE.pid),
    )
    assert status.state is RuntimeProcessState.RUNNING
    assert status.pids == (OLLAMA_SERVE.pid,)


def test_same_executable_on_another_port_does_not_occupy_managed_endpoint():
    managed = RuntimeProcessSpec(
        service_id=OLLAMA_PROCESS.service_id,
        name=OLLAMA_PROCESS.name,
        executables=OLLAMA_PROCESS.executables,
        cmdline_markers=OLLAMA_PROCESS.cmdline_markers,
        port=12434,
    )

    status = discover_process(
        managed,
        processes=running(OLLAMA_TRAY, OLLAMA_SERVE),
        listeners=listening(11434, pid=OLLAMA_SERVE.pid),
    )

    assert status.state is RuntimeProcessState.STOPPED
    assert status.processes == ()


def test_managed_root_remains_visible_before_its_listener_opens():
    managed = RuntimeProcessSpec(
        service_id=OLLAMA_PROCESS.service_id,
        name=OLLAMA_PROCESS.name,
        executables=OLLAMA_PROCESS.executables,
        cmdline_markers=OLLAMA_PROCESS.cmdline_markers,
        port=12434,
    )

    status = discover_process(
        managed,
        processes=running(OLLAMA_SERVE),
        listeners=NOTHING_LISTENING,
        managed_pids=frozenset({OLLAMA_SERVE.pid}),
    )

    assert status.state is RuntimeProcessState.RUNNING
    assert status.origin is ProcessOrigin.MANAGED


def test_process_holding_the_port_counts_even_under_an_unknown_name():
    renamed = ProcessInfo(5001, "server.exe", r"C:\custom\server.exe", "server.exe --serve")
    status = discover_process(
        COMFYUI_PROCESS, processes=running(renamed), listeners=listening(8188, pid=renamed.pid)
    )
    assert status.state is RuntimeProcessState.RUNNING
    assert status.pids == (renamed.pid,)
    assert "holding port 8188" in status.detail


def test_listener_pid_without_process_details_still_counts():
    status = discover_process(
        COMFYUI_PROCESS, processes=NO_PROCESS_SCAN, listeners=listening(8188, pid=7777)
    )
    assert status.state is RuntimeProcessState.RUNNING
    assert status.pids == (7777,)


def test_listener_scan_alone_can_prove_a_service_is_stopped():
    status = discover_process(
        OPEN_WEBUI_PROCESS, processes=NO_PROCESS_SCAN, listeners=NOTHING_LISTENING
    )
    assert status.state is RuntimeProcessState.STOPPED
    assert "nothing listening on port 8080" in status.detail


def test_without_psutil_the_process_state_is_unknown():
    status = discover_process(
        OPEN_WEBUI_PROCESS, processes=NO_PROCESS_SCAN, listeners=NO_LISTENER_SCAN
    )
    assert status.state is RuntimeProcessState.UNKNOWN
    assert "psutil" in status.detail
    assert status.origin is ProcessOrigin.UNKNOWN


def test_spec_without_port_falls_back_to_process_matching_only():
    spec = RuntimeProcessSpec("thing", "Thing", executables=("thing.exe",))
    found = discover_process(
        spec,
        processes=running(ProcessInfo(6001, "thing.exe")),
        listeners=NOTHING_LISTENING,
    )
    missing = discover_process(spec, processes=NO_PROCESS_SCAN, listeners=NOTHING_LISTENING)
    assert found.state is RuntimeProcessState.RUNNING
    assert missing.state is RuntimeProcessState.UNKNOWN


def test_discover_processes_returns_one_status_per_spec():
    statuses = discover_processes(
        processes=running(OLLAMA_SERVE, COMFY_DESKTOP),
        listeners=ListenerReport(
            ScanOutcome.OK,
            (
                Listener("127.0.0.1", 11434, OLLAMA_SERVE.pid),
                Listener("127.0.0.1", 8188, COMFY_DESKTOP.pid),
            ),
        ),
    )
    by_id = {s.service_id: s for s in statuses}
    assert by_id["ollama"].state is RuntimeProcessState.RUNNING
    assert by_id["comfyui"].state is RuntimeProcessState.RUNNING
    assert by_id["open-webui"].state is RuntimeProcessState.STOPPED


def test_discovery_against_the_real_machine_does_not_crash():
    statuses = discover_processes()
    assert len(statuses) == len(DEFAULT_PROCESS_SPECS)
    assert all(s.state in set(RuntimeProcessState) for s in statuses)
