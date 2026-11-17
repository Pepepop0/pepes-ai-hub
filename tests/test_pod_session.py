"""The pod session driver (SPEC-200), with a fake runner instead of ssh/scp."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "pod_session.py"
spec = importlib.util.spec_from_file_location("pod_session", SCRIPT)
pod_session = importlib.util.module_from_spec(spec)
sys.modules["pod_session"] = pod_session
spec.loader.exec_module(pod_session)

CIVITAI = "civitai-secret-123"
HUGGING_FACE = "hf-secret-456"
RELEASE = "PepitosAIHub-Capella-v9.9.9-linux-amd64"
ROOT = "/mnt/pepitos-bench/capella-data"
TARGET = ["--host", "203.0.113.7", "--port", "2222", "--alias", "abc123"]


class FakeRun:
    """Records every process; ``fail`` names a substring whose command exits 1 once."""

    def __init__(self, fail: str | None = None, database: str | None = None, session: bytes = b""):
        self.calls: list[dict] = []
        self.fail = fail
        self.database = database if database is not None else f"{ROOT}/data/open-webui/webui.db"
        self.session = session

    def __call__(self, args, stdin=None, secrets=(), show=True, stdout_to=None):
        self.calls.append({"args": list(args), "stdin": stdin, "secrets": tuple(secrets)})
        text = " ".join(args)
        if self.fail and self.fail in text:
            self.fail = None
            return pod_session.Result(1, "boom")
        if "find " in text and "webui.db" in text:
            return pod_session.Result(0, self.database + "\n")
        if stdout_to is not None:
            stdout_to.write_bytes(self.session)
        return pod_session.Result(0, "")

    def commands(self) -> list[str]:
        return [" ".join(call["args"]) for call in self.calls]


@pytest.fixture
def bundle(tmp_path):
    tar = tmp_path / f"{RELEASE}.tar.gz"
    tar.write_bytes(b"bundle")
    tar.with_name(tar.name + ".sha256").write_text(f"{'0' * 64}  {tar.name}\n")
    return tar


def tokens():
    return CIVITAI, HUGGING_FACE


def up(tmp_path, bundle, run):
    return pod_session.main(
        ["up", *TARGET, "--tar", str(bundle), "--out", str(tmp_path / "out")],
        run=run,
        tokens=tokens,
    )


def test_up_runs_the_recipe_in_order(tmp_path, bundle):
    run = FakeRun()
    assert up(tmp_path, bundle, run) == 0
    commands = run.commands()
    assert len(commands) == 8
    assert (
        commands[0].startswith("scp ") and bundle.name in commands[0] and ".sha256" in commands[0]
    )
    assert "-P 2222" in commands[0] and "root@203.0.113.7:/tmp/" in commands[0]
    assert "sha256sum -c" in commands[1]
    assert f"tar -xzf /tmp/{bundle.name} -C /mnt/pepitos-bench" in commands[2]
    assert [c.rsplit(" ", 1)[-1] for c in commands[3:5]] == ["deps", "prepare"]
    assert commands[5].endswith(f"check {ROOT}")
    assert f"capella-linux.sh install {ROOT}" in commands[6]
    assert commands[7].endswith(f"start {ROOT}")
    for command in commands:
        if command.startswith("ssh "):
            assert "-p 2222" in command
            assert "HostKeyAlias=lium-pod-abc123" in command
            assert "StrictHostKeyChecking=accept-new" in command
            assert "ServerAliveInterval=30" in command


def test_tokens_only_travel_on_stdin(tmp_path, bundle, capsys):
    run = FakeRun()
    up(tmp_path, bundle, run)
    for call in run.calls:
        joined = " ".join(call["args"])
        assert CIVITAI not in joined and HUGGING_FACE not in joined
    with_stdin = [call for call in run.calls if call["stdin"] is not None]
    assert len(with_stdin) == 1
    assert with_stdin[0]["stdin"] == f"{CIVITAI}\n{HUGGING_FACE}\n"
    assert set(with_stdin[0]["secrets"]) == {CIVITAI, HUGGING_FACE}
    shown = capsys.readouterr()
    assert CIVITAI not in shown.out + shown.err
    assert CIVITAI not in (tmp_path / "out" / "pod-state.json").read_text()


def test_the_runner_hides_tokens_from_output():
    assert (
        pod_session.redact(f"x {CIVITAI} y {HUGGING_FACE}", (CIVITAI, HUGGING_FACE))
        == "x *** y ***"
    )
    assert pod_session.redact("nothing", ("",)) == "nothing"


def test_up_without_a_civitai_token_stops_before_install(tmp_path, bundle, capsys, monkeypatch):
    monkeypatch.setattr(pod_session, "profile_needs_civitai", lambda _profile: True)
    run = FakeRun()
    code = pod_session.main(
        ["up", *TARGET, "--tar", str(bundle), "--out", str(tmp_path / "out")],
        run=run,
        tokens=lambda: ("", ""),
    )
    assert code == 1
    assert not any("install" in c for c in run.commands())
    assert "Civitai" in capsys.readouterr().err


def test_up_resumes_where_it_stopped(tmp_path, bundle):
    first = FakeRun(fail="install")
    assert up(tmp_path, bundle, first) == 1
    state = json.loads((tmp_path / "out" / "pod-state.json").read_text())
    assert state["done"] == ["upload", "verify", "extract", "deps", "prepare", "check"]
    second = FakeRun()
    assert up(tmp_path, bundle, second) == 0
    commands = second.commands()
    assert len(commands) == 2
    assert "install" in commands[0] and commands[1].endswith(f"start {ROOT}")
    third = FakeRun()
    assert up(tmp_path, bundle, third) == 0
    assert third.calls == []


def test_up_for_another_pod_starts_over(tmp_path, bundle):
    up(tmp_path, bundle, FakeRun())
    other = FakeRun()
    code = pod_session.main(
        ["up", "--host", "198.51.100.1", "--port", "22", "--alias", "zzz", "--tar", str(bundle)]
        + ["--out", str(tmp_path / "out")],
        run=other,
        tokens=tokens,
    )
    assert code == 0 and len(other.calls) == 8


def test_tunnel_reconnects_with_the_recipe_forward():
    run = FakeRun()
    pod = pod_session.Pod("203.0.113.7", 2222, "abc123", release=RELEASE)
    pauses: list[float] = []
    pod_session.tunnel(pod, 18080, 8080, run, sleep=pauses.append, attempts=3)
    assert len(run.calls) == 3 and pauses == [2, 2, 2]
    command = run.commands()[0]
    assert "-N" in command and "ExitOnForwardFailure=yes" in command
    assert "-L 127.0.0.1:18080:127.0.0.1:8080" in command and command.endswith("root@203.0.113.7")


def test_tunnel_prints_the_local_address(capsys):
    pod = pod_session.Pod("h", 22, "a", release=RELEASE)
    pod_session.tunnel(pod, 18080, 8080, FakeRun(), sleep=lambda _: None, attempts=1)
    assert "http://127.0.0.1:18080" in capsys.readouterr().out


def test_sync_filter_copies_assets_and_restarts(tmp_path):
    run = FakeRun()
    out = tmp_path / "out"
    pod_session.write_state(
        out,
        {
            "target": {"host": "h", "port": 22, "alias": "a", "user": "root", "release": RELEASE},
            "done": [],
        },
    )
    assert pod_session.main(["sync-filter", "--out", str(out)], run=run) == 0
    upload, remote = run.commands()
    assert (
        upload.startswith("scp ") and f"/mnt/pepitos-bench/{RELEASE}/assets/open-webui/" in upload
    )
    assert "pepito_image_ui.js" in upload and "pepito_swap.py" in upload
    assert "find /mnt/pepitos-bench/capella-data/apps/open-webui" in remote
    assert remote.index("capella-linux.sh stop") < remote.index("capella-linux.sh start")


def test_collect_brings_everything_and_the_discovered_database(tmp_path):
    session = io.BytesIO()
    with tarfile.open(fileobj=session, mode="w") as bundle:
        for name, data in {
            "data/open-webui/pepito-trace/turns.jsonl": b'{"a":1}\n',
            "data/open-webui/webui.db": b"sqlite",
            "logs/comfyui.log": b"log",
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
    run = FakeRun(database=f"{ROOT}/data/open-webui/deep/webui.db", session=session.getvalue())
    out = tmp_path / "got"
    code = pod_session.main(["collect", *TARGET, "--release", RELEASE, "--out", str(out)], run=run)
    assert code == 0
    find, download = run.commands()
    assert "find" in find and "/data/open-webui" in find
    for wanted in (
        "data/open-webui/pepito-trace",
        "data/open-webui/pepito-characters",
        "data/open-webui/uploads",
        "data/open-webui/deep/webui.db",
        "apps/comfyui/output",
        "apps/comfyui/input",
        "logs/open-webui.log",
        "logs/comfyui.log",
    ):
        assert wanted in download
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["database"] == "data/open-webui/deep/webui.db"
    assert manifest["files"]["data/open-webui/webui.db"] == {
        "bytes": 6,
        "sha256": hashlib.sha256(b"sqlite").hexdigest(),
    }
    assert "logs/open-webui.log" in manifest["missing"]
    assert (out / "logs" / "comfyui.log").read_bytes() == b"log"
    assert not (out / ".session.tar").exists()


def test_collect_fails_clearly_when_the_database_is_nowhere(tmp_path, capsys):
    run = FakeRun(database="")
    code = pod_session.main(
        ["collect", *TARGET, "--release", RELEASE, "--out", str(tmp_path)], run=run
    )
    assert code == 1 and "webui.db not found" in capsys.readouterr().err


def test_collect_refuses_paths_that_escape_the_folder(tmp_path, capsys):
    session = io.BytesIO()
    with tarfile.open(fileobj=session, mode="w") as bundle:
        info = tarfile.TarInfo("../evil")
        info.size = 1
        bundle.addfile(info, io.BytesIO(b"x"))
    run = FakeRun(session=session.getvalue())
    out = tmp_path / "got"
    assert (
        pod_session.main(["collect", *TARGET, "--release", RELEASE, "--out", str(out)], run=run)
        == 1
    )
    assert not (tmp_path / "evil").exists()
    assert "Unsafe path" in capsys.readouterr().err


def test_up_remembers_target_before_first_failure_and_resumes_without_arguments(tmp_path, bundle):
    out = tmp_path / "out"
    first = FakeRun(fail="scp")

    def failing_upload(*args, **kwargs):
        state = pod_session.read_state(out)
        assert state["target"]["host"] == "203.0.113.7"
        assert state["target"]["port"] == 2222
        assert state["target"]["alias"] == "abc123"
        assert state["done"] == []
        return first(*args, **kwargs)

    assert up(tmp_path, bundle, failing_upload) == 1
    assert pod_session.read_state(out)["done"] == []
    resumed = FakeRun()
    assert pod_session.main(["up", "--out", str(out)], run=resumed, tokens=tokens) == 0
    assert len(resumed.calls) == 8
    assert pod_session.read_state(out)["done"] == list(pod_session.STEPS)


def test_up_remembers_target_even_when_bundle_is_missing(tmp_path, bundle):
    bundle.unlink()
    run = FakeRun()
    assert up(tmp_path, bundle, run) == 1
    assert pod_session.read_state(tmp_path / "out")["target"]["alias"] == "abc123"
    assert run.calls == []


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_collect_skips_locked_local_sidecars(tmp_path, monkeypatch, capsys, suffix):
    database = "data/open-webui/webui.db"
    sidecar = database + suffix
    out = tmp_path / "got"
    (out / sidecar).parent.mkdir(parents=True)
    (out / sidecar).write_bytes(b"local locked sidecar")
    session = io.BytesIO()
    with tarfile.open(fileobj=session, mode="w") as bundle:
        for name in [database, sidecar, "logs/comfyui.log"]:
            info = tarfile.TarInfo(name)
            info.size = 6
            bundle.addfile(info, io.BytesIO(b"remote"))
    original = tarfile.TarFile.extract

    def locked_extract(self, member, *args, **kwargs):
        if member.name == sidecar:
            raise OSError(22, "Invalid argument", str(out / sidecar))
        return original(self, member, *args, **kwargs)

    monkeypatch.setattr(tarfile.TarFile, "extract", locked_extract)
    run = FakeRun(session=session.getvalue())
    assert (
        pod_session.main(["collect", *TARGET, "--release", RELEASE, "--out", str(out)], run=run)
        == 0
    )
    manifest = json.loads((out / "manifest.json").read_text())
    assert (out / database).read_bytes() == b"remote"
    assert (out / "logs/comfyui.log").read_bytes() == b"remote"
    assert (out / sidecar).read_bytes() == b"local locked sidecar"
    assert sidecar in manifest["skipped"] and sidecar not in manifest["files"]
    assert sidecar not in manifest["missing"]
    assert "Close the local Open WebUI/database" in capsys.readouterr().err
    assert not (out / ".session.tar").exists()


def test_extract_does_not_hide_regular_file_errors(tmp_path, monkeypatch):
    archive = tmp_path / "session.tar"
    with tarfile.open(archive, "w") as bundle:
        info = tarfile.TarInfo("webui.db")
        info.size = 1
        bundle.addfile(info, io.BytesIO(b"x"))

    def broken(*args, **kwargs):
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(tarfile.TarFile, "extract", broken)
    with pytest.raises(OSError, match="Invalid argument"):
        pod_session.extract(archive, tmp_path / "out")


PEPE_ROOT = "/mnt/pepitos-bench/pepe-data"


def up_pepe(tmp_path, bundle, run, token_source=tokens):
    return pod_session.main(
        ["up", *TARGET, "--profile", "pepe", "--tar", str(bundle), "--out", str(tmp_path / "out")],
        run=run,
        tokens=token_source,
    )


def test_up_with_a_profile_installs_it_in_its_own_data_folder(tmp_path, bundle):
    run = FakeRun()
    assert up_pepe(tmp_path, bundle, run) == 0
    commands = run.commands()
    for command in commands[3:]:
        assert "PEPITOS_BENCH_PROFILE=pepe bash scripts/capella-linux.sh" in command
        assert "capella-data" not in command
    assert commands[5].endswith(f"check {PEPE_ROOT}")
    assert f"capella-linux.sh install {PEPE_ROOT}" in commands[6]
    assert commands[7].endswith(f"start {PEPE_ROOT}")


def test_the_default_profile_is_capella_with_its_old_data_folder(tmp_path, bundle):
    run = FakeRun()
    assert up(tmp_path, bundle, run) == 0
    assert all("PEPITOS_BENCH_PROFILE=capella " in c for c in run.commands()[3:])
    assert run.commands()[5].endswith(f"check {ROOT}")


def test_a_profile_without_civitai_models_installs_without_the_token(tmp_path, bundle):
    run = FakeRun()
    assert up_pepe(tmp_path, bundle, run, token_source=lambda: ("", "")) == 0
    install = next(c for c in run.calls if "install" in " ".join(c["args"]))
    assert install["stdin"] == "\n\n"


def test_the_profile_is_remembered_by_later_commands(tmp_path, bundle):
    up_pepe(tmp_path, bundle, FakeRun())
    state = json.loads((tmp_path / "out" / "pod-state.json").read_text())
    assert state["target"]["profile"] == "pepe"
    run = FakeRun()
    assert pod_session.main(["start", "--out", str(tmp_path / "out")], run=run) == 0
    assert run.commands()[0].endswith(
        f"PEPITOS_BENCH_PROFILE=pepe bash scripts/capella-linux.sh start {PEPE_ROOT}"
    )
    other = FakeRun()
    pod_session.main(["start", "--profile", "capella", "--out", str(tmp_path / "out")], run=other)
    assert other.commands()[0].endswith(f"start {ROOT}")


def test_a_session_saved_before_profiles_existed_stays_capella(tmp_path, bundle):
    up(tmp_path, bundle, FakeRun())
    state_file = tmp_path / "out" / "pod-state.json"
    state = json.loads(state_file.read_text())
    del state["target"]["profile"]
    state_file.write_text(json.dumps(state))
    again = FakeRun()
    assert up(tmp_path, bundle, again) == 0
    assert again.calls == []


@pytest.mark.parametrize("profile", ["Pepe", "pepe;rm -rf /", "-x"])
def test_invalid_profile_ids_are_refused_before_any_command(tmp_path, bundle, capsys, profile):
    run = FakeRun()
    code = pod_session.main(
        [
            "up",
            *TARGET,
            f"--profile={profile}",
            "--tar",
            str(bundle),
            "--out",
            str(tmp_path / "out"),
        ],
        run=run,
        tokens=tokens,
    )
    assert code == 1 and run.calls == [] and "Invalid profile" in capsys.readouterr().err


def test_up_help_lists_the_profile_option(capsys):
    with pytest.raises(SystemExit):
        pod_session.main(["up", "--help"])
    assert "--profile" in capsys.readouterr().out
