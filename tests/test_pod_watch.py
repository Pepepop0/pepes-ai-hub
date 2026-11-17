"""TOOL-03: the live view of a pod install reads the `up` log, not the pod."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/pod_watch.py"
_spec = importlib.util.spec_from_file_location("pod_watch_under_test", SCRIPT)
watch = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = watch
_spec.loader.exec_module(watch)

T0 = datetime(2026, 11, 15, 16, 0, 0, tzinfo=UTC)
SIZES = {"comfyui/a": 4_000_000_000, "comfyui/b": 6_000_000_000}


def event(seconds, kind, message, **fields):
    stamp = (T0 + timedelta(seconds=seconds)).isoformat()
    return {"kind": kind, "message": message, "timestamp": stamp, **fields}


def feed(state, *events):
    for item in events:
        state.feed(item)
    return state


def download(seconds, model, done, total, speed=50e6):
    return event(
        seconds,
        "progress",
        f"Baixando {model}",
        progress={"completed": 0, "total": 2},
        transfer={"completed_bytes": done, "total_bytes": total, "bytes_per_second": speed},
    )


def test_a_line_is_an_event_a_step_marker_or_nothing():
    line = json.dumps(event(1, "activity", "Baixando comfyui/a"))
    assert watch.parse_line(line)["message"] == "Baixando comfyui/a"
    assert watch.parse_line("[install]") == {"marker": "install", "skipped": False}
    assert watch.parse_line("[deps] already done, skipping.") == {
        "marker": "deps",
        "skipped": True,
    }
    for noise in ("", "[unknown]", '{"torn": ', '{"no_message": 1}', "plain text", "[1, 2]"):
        assert watch.parse_line(noise) is None


def test_the_percentage_counts_bytes_of_finished_models_and_the_one_in_progress():
    state = feed(
        watch.Install(sizes=SIZES),
        event(0, "progress", "Baixando modelos", progress={"completed": 0, "total": 2}),
        download(10, "comfyui/a", 2_000_000_000, 4_000_000_000),
    )
    assert state.models_total == 2
    assert round(state.percent(), 1) == 20.0  # 2 of 10 GB
    feed(
        state,
        event(20, "detail", "Modelo instalado: comfyui/a"),
        download(30, "comfyui/b", 3e9, 6e9),
    )
    assert round(state.percent(), 1) == 70.0  # 4 GB done + 3 GB of the second
    assert list(state.models_done) == ["comfyui/a"] and state.current == "comfyui/b"


def test_the_time_left_uses_the_recent_speed_of_the_current_download():
    state = feed(
        watch.Install(sizes=SIZES),
        download(0, "comfyui/a", 1e9, 4e9, speed=100e6),
        download(5, "comfyui/a", 1.5e9, 4e9, speed=100e6),
    )
    # 10 GB in total, 1.5 GB done, 8.5 GB left at 100 MB/s
    assert round(state.eta_seconds()) == 85
    assert state.speed() == 100e6
    state.feed(event(60, "result", "Tudo pronto", state="succeeded", result={"state": "succeeded"}))
    assert state.eta_seconds() is None and state.percent() == 100.0


def test_a_log_that_stops_growing_is_shown_as_stalled_until_the_install_ends():
    state = feed(watch.Install(sizes=SIZES), download(100, "comfyui/a", 1e9, 4e9))
    assert state.stalled_for(T0 + timedelta(seconds=130)) == 30
    assert state.stalled_for(T0 + timedelta(seconds=100 + 18 * 60)) == 18 * 60
    state.feed(event(101, "result", "Tudo pronto", state="succeeded"))
    assert state.stalled_for(T0 + timedelta(hours=3)) is None
    assert watch.Install().stalled_for(T0) is None  # nothing read yet


def test_a_new_log_is_a_new_run_so_the_unfinished_step_is_not_marked_done():
    state = watch.Install()
    state.feed({"marker": "deps", "skipped": False})
    state.feed({"marker": "install", "skipped": False})
    state.restart()  # the second log starts
    state.feed({"marker": "deps", "skipped": True})
    state.feed({"marker": "install", "skipped": False})
    assert state.steps_done == {"deps"} and state.step == "install"


def test_a_line_still_being_written_waits_for_its_end(tmp_path):
    log = tmp_path / "up.log"
    first = json.dumps(
        event(1, "progress", "Instalando ComfyUI", progress={"completed": 0, "total": 3})
    )
    second = json.dumps(
        event(2, "progress", "Instalando Ollama", progress={"completed": 1, "total": 3})
    )
    log.write_text(first + "\n" + second[:30], encoding="utf-8")
    state, offsets = watch.read_logs([log], {})
    assert state.stage == "Instalando ComfyUI"
    with log.open("a", encoding="utf-8") as handle:
        handle.write(second[30:] + "\n")
    watch._advance(state, [log], offsets)
    assert state.stage == "Instalando Ollama" and state.stage_progress == (1, 3)


def test_only_the_models_a_profile_installs_have_a_size(tmp_path):
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "models:\n  chat:\n    primary: ollama/chat\n    alternatives: [ollama/other]\n"
        "  image:\n    diffusion: comfyui/a\n",
        encoding="utf-8",
    )
    catalog = tmp_path / "models.yaml"
    catalog.write_text(
        "ollama:\n  chat: {size_bytes: 20}\n  other: {size_bytes: 99}\n"
        "comfyui:\n  a: {size_bytes: 5}\n  unused: {size_bytes: 7}\n",
        encoding="utf-8",
    )
    assert watch.model_sizes(profile, catalog) == {"ollama/chat": 20, "comfyui/a": 5}
    assert watch.model_sizes(tmp_path / "missing.yaml", catalog) == {}


def draw(state, now, stall_after=90):
    from rich.console import Console

    out = io.StringIO()
    Console(file=out, width=100, force_terminal=False).print(
        watch.render(state, now, stall_after, "up.log")
    )
    return out.getvalue()


def test_the_screen_names_a_stall_a_failure_and_a_finished_install():
    state = feed(
        watch.Install(sizes=SIZES),
        event(0, "progress", "Baixando modelos", progress={"completed": 0, "total": 2}),
        download(10, "comfyui/a", 1e9, 4e9),
    )
    quiet = draw(state, T0 + timedelta(seconds=40))
    assert "SEM PROGRESSO" not in quiet and "comfyui/a" in quiet and "10.0%" in quiet
    assert "SEM PROGRESSO HÁ 18min" in draw(state, T0 + timedelta(seconds=10 + 18 * 60))
    feed(state, event(20, "state", "Falha ao baixar", state="failed"))
    assert "Falha: Falha ao baixar" in draw(state, T0 + timedelta(hours=1))
    done = feed(watch.Install(sizes=SIZES), event(5, "result", "Tudo pronto", state="succeeded"))
    assert "Instalação concluída" in draw(done, T0 + timedelta(hours=1))


def test_the_exit_status_tells_a_script_what_happened(tmp_path, capsys):
    log = tmp_path / "up.log"
    old = {**event(0, "progress", "Baixando comfyui/a"), "timestamp": "2000-01-01T00:00:00+00:00"}
    log.write_text(json.dumps(old) + "\n", encoding="utf-8")
    nothing = ["--profile", str(tmp_path / "x"), "--catalog", str(tmp_path / "y")]
    assert watch.main([str(log), "--once", *nothing]) == 2  # an event from 2000: stalled
    log.write_text(json.dumps(event(1, "result", "Tudo pronto", state="succeeded")) + "\n")
    assert watch.main([str(log), "--once", *nothing]) == 0
    log.write_text(json.dumps(event(1, "state", "Falha", state="failed")) + "\n")
    assert watch.main([str(log), "--once", *nothing]) == 1


def test_following_a_finished_log_returns_at_once_and_a_dead_one_stops_on_request(tmp_path):
    log = tmp_path / "up.log"
    nothing = ["--profile", str(tmp_path / "x"), "--catalog", str(tmp_path / "y")]
    log.write_text(json.dumps(event(1, "result", "Tudo pronto", state="succeeded")) + "\n")
    assert watch.main([str(log), "--refresh", "0.01", *nothing]) == 0
    dead = {**event(0, "progress", "Baixando comfyui/a"), "timestamp": "2000-01-01T00:00:00+00:00"}
    log.write_text(json.dumps(dead) + "\n")
    assert (
        watch.main([str(log), "--refresh", "0.01", "--exit-on-stall", "--no-bell", *nothing]) == 2
    )


def test_default_profile_comes_from_the_pod_state_beside_the_log(tmp_path):
    repo_profiles = watch.REPO / "profiles"
    assert watch.default_profile(tmp_path) == repo_profiles / "capella.yaml"
    (tmp_path / "pod-state.json").write_text(
        json.dumps({"target": {"profile": "pepe-8gb"}}), encoding="utf-8"
    )
    assert watch.default_profile(tmp_path) == repo_profiles / "pepe-8gb.yaml"
    (tmp_path / "pod-state.json").write_text("{broken", encoding="utf-8")
    assert watch.default_profile(tmp_path) == repo_profiles / "capella.yaml"
