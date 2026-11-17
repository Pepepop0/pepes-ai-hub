"""Real Linux services, synthetic HTTP payloads, and separate controller processes.

No models, GPUs, rented machines or user data are involved. Every process is a
fixture we launch, and finalizers retain exact pidfds rather than killing stale
numeric PIDs or collecting processes by executable name.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from local_ai_stack import posix_runtime
from local_ai_stack.jobs import Membership
from local_ai_stack.launcher import ProcessLauncher, ProcessLaunchSpec
from local_ai_stack.registry import IdentityVerdict, ManagedProcess, ManagedProcessRegistry

pytestmark = pytest.mark.skipif(
    not posix_runtime.supported(), reason="requires Linux kernel pidfds and subreapers"
)

CHILD = r"""
import json, os, signal, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

directory, port, mode, ignore = sys.argv[1:]
directory = Path(directory)
if mode == "doublefork" and os.fork():
    os._exit(0)

def terminate(_signal, _frame):
    (directory / "child.term").write_text("graceful", encoding="utf-8")
    raise SystemExit(0)

signal.signal(signal.SIGTERM, signal.SIG_IGN if ignore == "yes" else terminate)

class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"status":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *_args):
        pass

server = HTTPServer(("127.0.0.1", int(port)), Health)
fields = Path(f"/proc/{os.getpid()}/stat").read_text().rsplit(")", 1)[1].split()
info = {"pid": os.getpid(), "start": int(fields[19]), "port": server.server_port}
temporary = directory / "child.tmp"
temporary.write_text(json.dumps(info), encoding="utf-8")
temporary.replace(directory / "child.json")
print("synthetic child ready", flush=True)
server.serve_forever(poll_interval=0.05)
"""

ROOT = r"""
import json, os, signal, subprocess, sys, time
from pathlib import Path

directory, port, mode, ignore_root, ignore_child = sys.argv[1:]
directory = Path(directory)

def terminate(_signal, _frame):
    (directory / "root.term").write_text("graceful", encoding="utf-8")
    raise SystemExit(0)

signal.signal(signal.SIGTERM, signal.SIG_IGN if ignore_root == "yes" else terminate)
# The fixture intentionally receives ONLY a synthetic environment. Recording it
# cannot collect the test runner's real environment or any account credentials.
(directory / "service-env.json").write_text(json.dumps(dict(os.environ)), encoding="utf-8")
subprocess.Popen(
    [sys.executable, str(directory / "child.py"), str(directory), port, mode, ignore_child],
    start_new_session=mode != "normal",
)
print("synthetic root ready", flush=True)
while True:
    time.sleep(0.05)
"""

CONTROLLER = r"""
import json, sys
from pathlib import Path
from local_ai_stack.controller import ServiceController, ServiceControlSpec
from local_ai_stack.launcher import ProcessLauncher, ProcessLaunchSpec
from local_ai_stack.logs import LogFiles
from local_ai_stack.processes import RuntimeProcessSpec
from local_ai_stack.registry import ManagedProcessRegistry
from local_ai_stack.services import ServiceSpec

config_path, action, force = sys.argv[1:]
config = json.loads(Path(config_path).read_text(encoding="utf-8"))
directory = Path(config["directory"])
registry = ManagedProcessRegistry(directory / "state" / "managed.json")
launcher = ProcessLauncher(registry)
launch = ProcessLaunchSpec(
    "fixture", (sys.executable, str(directory / "root.py"), str(directory),
                str(config["port"]), config["mode"], config["ignore_root"], config["ignore_child"]),
    cwd=directory, env={"SYNTHETIC_SERVICE_ENV": "kept"}, inherit_env=False,
    logs=LogFiles(directory / "service.log"),
    startup_timeout=10, stop_timeout=0.3, allow_force_kill=True,
)
spec = ServiceControlSpec(
    ServiceSpec("fixture", "Synthetic service", config["port"], health_path="/health",
                expected_json_keys=("status",)),
    RuntimeProcessSpec("fixture", "Synthetic service", port=config["port"]),
    launch,
)
controller = ServiceController((spec,), registry, launcher, poll_interval=0.05)
if action == "start":
    result = controller.start("fixture", wait=True, timeout=10)
    output = {"outcome": result.outcome.value, "pid": result.pid, "ready": result.ready}
elif action in {"restart", "restart_no_force"}:
    no_force = action == "restart_no_force"
    result = controller.restart("fixture", wait=True, timeout=0.3 if no_force else 10,
                                allow_force=False if no_force else None)
    output = {"restarted": result.restarted,
              "old_pid": result.stop.pid,
              "pid": result.start.pid if result.start else None,
              "ready": result.start.ready if result.start else False}
elif action == "status":
    status = controller.status("fixture")
    output = {"health": status.health.state.value, "origin": status.origin.value,
              "managed": status.managed is not None, "stoppable": status.stoppable,
              "tree": launcher.tree_pids("fixture", status.managed.pid)
                      if status.managed else None}
else:
    result = controller.stop("fixture", allow_force=force == "yes", timeout=0.3)
    output = {"outcome": result.outcome.value, "pid": result.pid, "forced": result.forced,
              "tree": result.tree_pids, "left_running": result.left_running}
launcher.close_all()
print(json.dumps(output))
"""


def _wait(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.025)
    raise AssertionError("synthetic process did not reach its expected state")


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _port_freed(port: int) -> bool:
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


class RuntimeFixture:
    def __init__(self, directory: Path):
        self.directory = directory
        self.config_path = directory / "fixture.json"
        self.registry = ManagedProcessRegistry(directory / "state" / "managed.json")
        self.config = {
            "directory": str(directory),
            "port": _free_port(),
            "mode": "normal",
            "ignore_root": "no",
            "ignore_child": "no",
        }
        self.descriptors: list[int] = []
        self.proofs: list[posix_runtime.SessionProof] = []
        (directory / "root.py").write_text(ROOT, encoding="utf-8")
        (directory / "child.py").write_text(CHILD, encoding="utf-8")

    def configure(self, **options):
        self.config.update(options)

    def control(self, action: str, *, force=False) -> dict:
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        environment = dict(os.environ)
        source = str(Path(__file__).resolve().parents[1] / "src")
        environment["PYTHONPATH"] = source + os.pathsep + environment.get("PYTHONPATH", "")
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                CONTROLLER,
                str(self.config_path),
                action,
                "yes" if force else "no",
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        output = json.loads(result.stdout)
        entry = self.registry.load().get("fixture")
        if entry is not None and entry.posix_session is not None:
            proof = entry.posix_session
            if proof not in self.proofs:
                self.proofs.append(proof)
                for pid, start in (
                    (proof.root_pid, proof.root_start),
                    (proof.supervisor_pid, proof.supervisor_start),
                    (proof.guardian_pid, proof.guardian_start),
                ):
                    self.track(pid, start)
            assert proof.token not in result.stdout + result.stderr
        if action in {"start", "restart"} and output.get("ready"):
            self.track(self.child["pid"], self.child["start"])
        return output

    def track(self, pid: int, start: int):
        try:
            descriptor = os.pidfd_open(pid)
        except ProcessLookupError:
            return
        if posix_runtime._alive(pid, start):
            self.descriptors.append(descriptor)
        else:
            os.close(descriptor)

    @property
    def proof(self) -> posix_runtime.SessionProof:
        return self.registry.load().get("fixture").posix_session

    @property
    def child(self) -> dict:
        return json.loads((self.directory / "child.json").read_text(encoding="utf-8"))

    def cleanup(self):
        for proof in self.proofs:
            try:
                posix_runtime.stop(proof, timeout=1, allow_force=True)
            except posix_runtime.SessionError:
                pass
        for descriptor in self.descriptors:
            try:
                signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            except ProcessLookupError:
                pass
            finally:
                os.close(descriptor)


@pytest.fixture
def runtime(tmp_path):
    fixture = RuntimeFixture(tmp_path)
    try:
        yield fixture
    finally:
        fixture.cleanup()


def _gone(pid: int, start: int) -> bool:
    return not posix_runtime._alive(pid, start)


def _kill_exact(pid: int, start: int):
    descriptor = os.pidfd_open(pid)
    try:
        assert posix_runtime._alive(pid, start)
        signal.pidfd_send_signal(descriptor, signal.SIGKILL)
    finally:
        os.close(descriptor)


def test_real_listener_survives_controller_exit_and_stops_gracefully(runtime):
    # A different private session owns this unrelated listener. Stop must not
    # widen its authority to another listener, executable, or scan result.
    with socket.socket() as unrelated:
        unrelated.bind(("127.0.0.1", 0))
        unrelated.listen()
        external_port = unrelated.getsockname()[1]
        started = runtime.control("start")
        assert started["outcome"] == "started" and started["ready"]
        proof, child = runtime.proof, runtime.child
        assert started["pid"] == proof.root_pid
        assert stat.S_IMODE(runtime.registry.path.stat().st_mode) == 0o600
        assert os.getsid(proof.root_pid) != os.getsid(0)
        assert os.getpgrp() != os.getpgid(proof.root_pid)
        status = runtime.control("status")  # a genuinely different interpreter
        assert status["health"] == "UP" and status["origin"] == "managed"
        assert status["managed"] and status["stoppable"]
        assert {proof.root_pid, child["pid"]} <= set(status["tree"])
        assert proof.token not in repr(runtime.registry.load().get("fixture"))
        assert proof.token not in (runtime.directory / "service.log").read_text()
        service_env = json.loads((runtime.directory / "service-env.json").read_text())
        assert service_env["SYNTHETIC_SERVICE_ENV"] == "kept"
        assert proof.token not in json.dumps(service_env)
        stopped = runtime.control("stop", force=False)
        assert stopped["outcome"] == "stopped" and stopped["forced"] is False
        assert {proof.root_pid, child["pid"]} <= set(stopped["tree"])
        assert (runtime.directory / "root.term").read_text() == "graceful"
        assert (runtime.directory / "child.term").read_text() == "graceful"
        assert _gone(proof.root_pid, proof.root_start)
        assert _gone(child["pid"], child["start"])
        _wait(lambda: _gone(proof.supervisor_pid, proof.supervisor_start))
        _wait(lambda: _gone(proof.guardian_pid, proof.guardian_start))
        assert _port_freed(runtime.config["port"])
        assert runtime.registry.load().entries == {}
        with socket.create_connection(("127.0.0.1", external_port), timeout=1):
            pass


def test_separate_controller_restart_reclaims_port_and_replaces_real_root(runtime):
    first = runtime.control("start")
    old_proof, old_child = runtime.proof, runtime.child
    restarted = runtime.control("restart")
    assert restarted["restarted"] and restarted["ready"]
    assert restarted["old_pid"] == first["pid"]
    assert restarted["pid"] != first["pid"]
    assert _gone(old_proof.root_pid, old_proof.root_start)
    assert _gone(old_child["pid"], old_child["start"])
    assert runtime.control("status")["stoppable"]
    assert runtime.control("stop")["outcome"] == "stopped"


@pytest.mark.parametrize("field", ["supervisor_start", "boot_id", "token", "root_start"])
def test_stale_or_invalid_proof_refuses_even_a_matching_live_root(runtime, field):
    assert runtime.control("start")["ready"]
    original = runtime.registry.load().get("fixture")
    proof = original.posix_session
    child = runtime.child
    if field.endswith("start"):
        bad = replace(proof, **{field: getattr(proof, field) + 1})
    else:
        bad = replace(proof, **{field: "0" * len(getattr(proof, field))})
    runtime.registry.register(replace(original, posix_session=bad))
    with socket.socket() as unrelated:
        unrelated.bind(("127.0.0.1", 0))
        unrelated.listen()
        stopped = runtime.control("stop", force=True)
        with socket.create_connection(unrelated.getsockname(), timeout=1):
            pass
    assert stopped["outcome"] not in {"stopped", "not_running"}
    refused = ProcessLauncher(runtime.registry).start(
        ProcessLaunchSpec("fixture", original.argv, allow_force_kill=True)
    )
    assert refused.outcome.value == "spawn_failed"
    assert runtime.registry.load().get("fixture").posix_session == bad
    assert posix_runtime.root_alive(proof)
    assert not _gone(child["pid"], child["start"])
    assert not _port_freed(runtime.config["port"])
    runtime.registry.register(original)
    assert runtime.control("stop", force=True)["outcome"] == "stopped"


def test_malformed_persisted_proof_is_not_a_legacy_single_pid_record(runtime):
    assert runtime.control("start")["ready"]
    original = runtime.registry.path.read_text(encoding="utf-8")
    document = json.loads(original)
    document["processes"][0]["posix_session"]["token"] = "invalid"
    runtime.registry.path.write_text(json.dumps(document), encoding="utf-8")
    assert runtime.control("stop", force=True)["outcome"] == "not_managed"
    assert not _port_freed(runtime.config["port"])
    runtime.registry.path.write_text(original, encoding="utf-8")
    assert runtime.control("stop", force=True)["outcome"] == "stopped"


@pytest.mark.parametrize("mode", ["session", "doublefork"])
def test_escaped_session_and_double_fork_listener_are_stopped(runtime, mode):
    runtime.configure(mode=mode)
    assert runtime.control("start")["ready"]
    proof, child = runtime.proof, runtime.child
    assert os.getsid(child["pid"]) != os.getsid(proof.root_pid)
    status = runtime.control("status")
    assert status["health"] == "UP" and status["stoppable"]
    launcher = ProcessLauncher(runtime.registry)
    if mode == "doublefork":
        _wait(lambda: launcher.owns("fixture", child["pid"]) is Membership.MEMBER)
    else:
        assert launcher.owns("fixture", child["pid"]) is Membership.UNPROVABLE
    stopped = runtime.control("stop", force=False)
    assert stopped["outcome"] == "stopped" and not stopped["forced"]
    assert child["pid"] in stopped["tree"]
    assert _gone(child["pid"], child["start"])
    assert _port_freed(runtime.config["port"])


@pytest.mark.parametrize("anchor", ["root", "supervisor", "guardian"])
def test_service_or_either_anchor_exit_cleans_escaped_listener(runtime, anchor):
    runtime.configure(mode="session", ignore_root="yes", ignore_child="yes")
    assert runtime.control("start")["ready"]
    proof, child = runtime.proof, runtime.child
    _kill_exact(getattr(proof, anchor + "_pid"), getattr(proof, anchor + "_start"))
    _wait(lambda: _gone(proof.root_pid, proof.root_start))
    _wait(lambda: _gone(child["pid"], child["start"]))
    _wait(lambda: _gone(proof.supervisor_pid, proof.supervisor_start))
    _wait(lambda: _gone(proof.guardian_pid, proof.guardian_start))
    assert _port_freed(runtime.config["port"])
    assert runtime.control("stop", force=True)["outcome"] == "not_running"


@pytest.mark.parametrize("ignore_root", ["no", "yes"])
def test_non_force_timeout_keeps_proof_for_later_force_and_blocks_restart(runtime, ignore_root):
    runtime.configure(mode="session", ignore_root=ignore_root, ignore_child="yes")
    assert runtime.control("start")["ready"]
    proof, child = runtime.proof, runtime.child
    stopped = runtime.control("stop", force=False)
    assert stopped["outcome"] == "timeout" and not stopped["forced"]
    known_survivor = proof.root_pid if ignore_root == "yes" else child["pid"]
    assert known_survivor in stopped["left_running"]
    assert not _port_freed(runtime.config["port"])
    assert runtime.registry.status("fixture").verdict is IdentityVerdict.LIVE
    assert runtime.control("status")["stoppable"]
    assert not runtime.control("restart_no_force")["restarted"]
    later = runtime.control("stop", force=True)
    assert later["outcome"] == "stopped" and later["forced"]
    assert {proof.root_pid, child["pid"]} <= set(stopped["tree"]) | set(later["tree"])
    assert _gone(child["pid"], child["start"])
    assert _port_freed(runtime.config["port"])


def test_external_listener_is_never_adopted_or_stopped(runtime):
    with socket.socket() as unrelated:
        unrelated.bind(("127.0.0.1", runtime.config["port"]))
        unrelated.listen()
        assert runtime.control("start")["outcome"] == "already_running_external"
        assert runtime.registry.load().entries == {}
        assert runtime.control("stop", force=True)["outcome"] == "not_managed"
        with socket.create_connection(("127.0.0.1", runtime.config["port"]), timeout=1):
            pass


def test_registry_failure_aborts_provisional_tree_before_returning(tmp_path, monkeypatch):
    registry = ManagedProcessRegistry(tmp_path / "state" / "managed.json")
    launched = []
    original = posix_runtime.launch

    def capture(*args, **kwargs):
        pending = original(*args, **kwargs)
        launched.append(pending)
        return pending

    def refuse(_entry):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(posix_runtime, "launch", capture)
    monkeypatch.setattr(registry, "register", refuse)
    launcher = ProcessLauncher(registry)
    try:
        with pytest.raises(OSError, match="synthetic disk failure"):
            launcher.start(
                ProcessLaunchSpec(
                    "fixture",
                    (sys.executable, "-c", "import time; time.sleep(60)"),
                    allow_force_kill=True,
                )
            )
        assert len(launched) == 1
        proof = launched[0].proof
        _wait(lambda: _gone(proof.root_pid, proof.root_start))
        _wait(lambda: _gone(proof.supervisor_pid, proof.supervisor_start))
        _wait(lambda: _gone(proof.guardian_pid, proof.guardian_start))
        assert registry.load().entries == {}
    finally:
        for pending in launched:
            pending.abort()
            pending.guardian.wait(timeout=10)
        launcher.close_all()


def test_schema_one_windows_and_pre_supervisor_records_keep_their_identity_contract():
    legacy = ManagedProcess("fixture", 1234, "old-managed.exe", create_time=1.0, job_name="old-job")
    assert "posix_session" not in legacy.to_dict()
    assert ManagedProcess.from_dict(legacy.to_dict()) == legacy
    malformed = legacy.to_dict() | {"posix_session": {"root_pid": legacy.pid}}
    assert ManagedProcess.from_dict(malformed) is None
    assert ManagedProcess.from_dict(legacy.to_dict() | {"posix_session": None}) is None


def test_linux_app_environment_keeps_install_credentials_in_the_controller_only():
    base = {name: "synthetic-install-only" for name in posix_runtime.INSTALL_CREDENTIAL_ENV}
    base["UNCHANGED_SETTING"] = "kept"
    original = dict(base)
    ordinary = ProcessLaunchSpec("fixture", (sys.executable,)).child_env(base)
    assert ordinary == {"UNCHANGED_SETTING": "kept"}
    explicit = ProcessLaunchSpec(
        "fixture", (sys.executable,), env={"HF_TOKEN": "synthetic-explicit-service-token"}
    ).child_env(base)
    assert explicit == {"UNCHANGED_SETTING": "kept", "HF_TOKEN": "synthetic-explicit-service-token"}
    assert base == original


def test_windows_app_environment_keeps_existing_inheritance(monkeypatch):
    from types import SimpleNamespace

    from local_ai_stack import launcher

    monkeypatch.setattr(launcher, "sys", SimpleNamespace(platform="win32"))
    base = {name: "synthetic-windows-value" for name in posix_runtime.INSTALL_CREDENTIAL_ENV}
    assert ProcessLaunchSpec("fixture", ("python.exe",)).child_env(base) == base


@pytest.mark.parametrize("malformed", ["non_ascii", "surrogate", "nested_json"])
def test_unauthenticated_malformed_clients_cannot_end_the_service(runtime, malformed):
    assert runtime.control("start")["ready"]
    proof, child = runtime.proof, runtime.child
    if malformed == "nested_json":
        payload = b'{"token":' + b"[" * 3000 + b"0" + b"]" * 3000 + b"}\n"
    else:
        token = ("é" if malformed == "non_ascii" else "\ud800") * 64
        payload = (
            json.dumps(
                {
                    "version": 1,
                    "command": "stop",
                    "token": token,
                    "root_pid": proof.root_pid,
                    "root_start": proof.root_start,
                    "grace": 0.1,
                    "allow_force": True,
                }
            ).encode("utf-8")
            + b"\n"
        )
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as attacker:
        attacker.settimeout(3)
        attacker.connect("\0" + proof.address)
        try:
            attacker.sendall(payload)
            attacker.recv(4096)
        except (BrokenPipeError, ConnectionResetError):
            pass
    assert posix_runtime.root_alive(proof)
    assert not _gone(child["pid"], child["start"])
    assert not _port_freed(runtime.config["port"])
    assert runtime.control("status")["stoppable"]
    assert runtime.control("stop")["outcome"] == "stopped"


CONCURRENT_START = r"""
import json, sys, time
from pathlib import Path
from local_ai_stack.launcher import ProcessLauncher, ProcessLaunchSpec
from local_ai_stack.registry import ManagedProcessRegistry

path, backup, gate, service_id = sys.argv[1:]
registry = ManagedProcessRegistry(Path(path))
original_load, original_register = registry.load, registry.register
def slow_load():
    snapshot = original_load()
    time.sleep(0.1)  # Widen the old unprotected read/replace race.
    return snapshot
def slow_register(*args, **kwargs):
    time.sleep(0.2)  # Widen the old check/spawn/register race.
    return original_register(*args, **kwargs)
registry.load, registry.register = slow_load, slow_register
while not Path(gate).exists():
    time.sleep(0.01)
launcher = ProcessLauncher(registry)
result = launcher.start(ProcessLaunchSpec(
    service_id, (sys.executable, "-c", "import time; time.sleep(120)"),
    allow_force_kill=True,
))
if result.entry is not None:
    # Recovery capability stays in a separate 0600 state file, never stdout.
    ManagedProcessRegistry(Path(backup)).register(result.entry)
launcher.close_all()
print(json.dumps({"outcome": result.outcome.value, "pid": result.pid}))
"""


@contextmanager
def _concurrent_starts(directory: Path, service_ids: tuple[str, ...]):
    registry = ManagedProcessRegistry(directory / "state" / "managed.json")
    gate = directory / "start.gate"
    backups = [directory / f"recovery-{index}.json" for index in range(len(service_ids))]
    environment = dict(os.environ)
    source = str(Path(__file__).resolve().parents[1] / "src")
    environment["PYTHONPATH"] = source + os.pathsep + environment.get("PYTHONPATH", "")
    processes = []
    try:
        for index, service_id in enumerate(service_ids):
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        CONCURRENT_START,
                        str(registry.path),
                        str(backups[index]),
                        str(gate),
                        service_id,
                    ],
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        gate.touch()
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=40)
            assert process.returncode == 0, stderr
            results.append(json.loads(stdout))
        yield registry, results
    finally:
        gate.touch(exist_ok=True)
        for process in processes:
            if process.poll() is None:
                process.terminate()  # Our unreaped Popen child, never a stale PID.
            process.communicate(timeout=10)
        recoveries = list(registry.load().entries.values())
        for backup in backups:
            recoveries.extend(ManagedProcessRegistry(backup).load().entries.values())
        for entry in recoveries:
            if entry.posix_session is not None:
                try:
                    posix_runtime.stop(entry.posix_session, timeout=1, allow_force=True)
                except posix_runtime.SessionError:
                    pass


def test_cross_process_mutations_preserve_every_live_service_capability(tmp_path):
    service_ids = ("service-a", "service-b", "service-c", "service-d")
    with _concurrent_starts(tmp_path, service_ids) as (registry, results):
        assert all(result["outcome"] == "started" for result in results)
        entries = registry.load().entries
        assert set(entries) == set(service_ids)
        assert len({entry.posix_session.token for entry in entries.values()}) == len(service_ids)
        for entry in entries.values():
            assert posix_runtime.root_alive(entry.posix_session)
            stopped = ProcessLauncher(registry).stop(entry.service_id, timeout=2, allow_force=True)
            assert stopped.stopped
            assert not posix_runtime.root_alive(entry.posix_session)
        assert registry.load().entries == {}


def test_same_service_start_is_serialized_through_registry_commit(tmp_path):
    with _concurrent_starts(tmp_path, ("fixture", "fixture")) as (registry, results):
        assert sorted(result["outcome"] for result in results) == ["already_managed", "started"]
        assert len({result["pid"] for result in results}) == 1
        assert set(registry.load().entries) == {"fixture"}
        assert posix_runtime.root_alive(registry.load().get("fixture").posix_session)


def test_old_stop_cannot_unregister_a_live_replacement_session(runtime, tmp_path, monkeypatch):
    assert runtime.control("start")["ready"]
    old_proof = runtime.proof
    replacement_directory = tmp_path / "replacement"
    replacement_directory.mkdir()
    replacement = RuntimeFixture(replacement_directory)
    try:
        assert replacement.control("start")["ready"]
        new_entry = replacement.registry.load().get("fixture")
        original_stop = posix_runtime.stop

        def replace_after_stop(proof, **options):
            result = original_stop(proof, **options)
            runtime.registry.register(new_entry)
            return result

        with monkeypatch.context() as patch:
            patch.setattr(posix_runtime, "stop", replace_after_stop)
            assert ProcessLauncher(runtime.registry).stop("fixture", allow_force=True).stopped
        assert not posix_runtime.root_alive(old_proof)
        assert runtime.registry.load().get("fixture") == new_entry
        assert posix_runtime.root_alive(new_entry.posix_session)
        assert replacement.control("status")["stoppable"]
    finally:
        replacement.cleanup()


def test_stale_stop_and_prune_cannot_remove_a_replacement_record(runtime, monkeypatch):
    from local_ai_stack.registry import EntryStatus

    assert runtime.control("start")["ready"]
    current = runtime.registry.load().get("fixture")
    old = replace(current, started_at=current.started_at - 1)
    stale = EntryStatus(old, IdentityVerdict.STALE, detail="old controller snapshot")
    with monkeypatch.context() as patch:
        patch.setattr(runtime.registry, "status", lambda *_args, **_kwargs: stale)
        assert ProcessLauncher(runtime.registry).stop("fixture", allow_force=True).stopped
    assert runtime.registry.load().get("fixture") == current
    with monkeypatch.context() as patch:
        patch.setattr(runtime.registry, "statuses", lambda: {"fixture": stale})
        assert runtime.registry.prune() == ()
    assert runtime.registry.load().get("fixture") == current
    assert posix_runtime.root_alive(current.posix_session)
