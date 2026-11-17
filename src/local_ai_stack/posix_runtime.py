"""Persistent Linux ownership without historical PID/PGID guesses.

Opted-in foreground services run below two detached, single-threaded Linux
child subreapers. A live isolated session supplies graceful group signalling;
cleanup also handles double-fork/setsid workers: when their parent exits, Linux
adopts them into the nearest live subreaper. Only its *current direct children*
are signalled, through pidfds, before they are reaped. No ancestry walk grants
kill authority. The outer guardian cleans up if the control supervisor exits;
the supervisor cleans up if its guardian exits. Neither depends on the CLI,
its terminal, an open SSH connection, or a stale stored process-group number.

A random abstract Unix socket is authenticated with a capability, kernel peer
credentials, boot identity and exact /proc start ticks. Capabilities travel over
private sockets, never argv, service environment or logs. Registry records keep
them private (the registry is atomically created with mode 0600 on POSIX).

This module is also the stdlib-only supervisor entry point. Invoke its absolute
source path, not ``-m``: the service may have a different cwd, PYTHONPATH or venv.
"""

from __future__ import annotations

import ctypes
import hmac
import json
import math
import os
import secrets
import signal
import socket
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_PROTOCOL = 1
_MAX_MESSAGE = 1024 * 1024
_POLL = 0.05
INSTALL_CREDENTIAL_ENV = ("CIVITAI_TOKEN", "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN")


class SessionError(OSError):
    """A persistent session could not be authenticated or controlled."""


class SessionGone(SessionError):
    """The exact sentinel identity no longer exists, not merely its socket."""


def supported() -> bool:
    return (
        sys.platform == "linux"
        and hasattr(os, "pidfd_open")
        and hasattr(signal, "pidfd_send_signal")
        and hasattr(socket, "SO_PEERCRED")
    )


@dataclass(frozen=True)
class SessionProof:
    address: str
    token: str = field(repr=False)
    boot_id: str
    supervisor_pid: int
    supervisor_start: int
    guardian_pid: int
    guardian_start: int
    root_pid: int
    root_start: int

    def to_dict(self) -> dict[str, Any]:
        return dict(vars(self))

    @classmethod
    def from_dict(cls, value: Any) -> SessionProof | None:
        if not isinstance(value, dict):
            return None
        names = ("address", "token", "boot_id")
        if any(not isinstance(value.get(name), str) for name in names):
            return None
        if not value["address"].startswith("PepitosAIHub-") or len(value["address"]) != 45:
            return None
        if len(value["token"]) != 64 or any(c not in "0123456789abcdef" for c in value["token"]):
            return None
        if not value["boot_id"] or len(value["boot_id"]) > 64:
            return None
        numbers = (
            "supervisor_pid",
            "supervisor_start",
            "guardian_pid",
            "guardian_start",
            "root_pid",
            "root_start",
        )
        if any(type(value.get(name)) is not int or value[name] <= 0 for name in numbers):
            return None
        return cls(**{name: value[name] for name in (*names, *numbers)})


@dataclass(frozen=True)
class SessionSnapshot:
    pids: tuple[int, ...]
    root_alive: bool


@dataclass(frozen=True)
class SessionStop:
    stopped: bool
    forced: bool
    pids: tuple[int, ...]
    left_running: tuple[int, ...] = ()


@dataclass
class PendingSession:
    """A spawned service remains provisional until its registry save succeeds."""

    proof: SessionProof
    connection: socket.socket = field(repr=False)
    guardian: subprocess.Popen = field(repr=False)

    def commit(self) -> None:
        try:
            _send(self.connection, {"commit": True})
            reply = _receive(self.connection)
            if reply.get("committed") is not True:
                raise SessionError("Linux supervisor did not commit ownership")
        finally:
            self.connection.close()

    def abort(self) -> None:
        # EOF before commit asks the live guardian/supervisor to clean up.
        self.connection.close()
        try:
            stop(self.proof, timeout=5.0, allow_force=True)
        except SessionError:
            pass


@dataclass(frozen=True)
class _Process:
    pid: int
    parent: int
    group: int
    session: int
    start: int
    state: str


def _process(pid: int) -> _Process:
    text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    # comm can contain spaces and parentheses; fields after its final ')' are
    # the stable numeric stat fields (state is field 3, starttime field 22).
    fields = text[text.rfind(")") + 2 :].split()
    return _Process(pid, int(fields[1]), int(fields[2]), int(fields[3]), int(fields[19]), fields[0])


def _alive(pid: int, start: int) -> bool:
    try:
        process = _process(pid)
        return process.start == start and process.state not in {"Z", "X"}
    except (FileNotFoundError, ProcessLookupError):
        return False


def root_alive(proof: SessionProof) -> bool:
    """Read-only exact identity check used when an old sentinel is gone."""
    return _alive(proof.root_pid, proof.root_start)


def guardian_alive(proof: SessionProof) -> bool:
    """A dead supervisor may still have a live guardian finishing cleanup."""
    return _alive(proof.guardian_pid, proof.guardian_start)


def _boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()


def _send(connection: socket.socket, document: dict) -> None:
    payload = json.dumps(document, separators=(",", ":")).encode("utf-8") + b"\n"
    if len(payload) > _MAX_MESSAGE:
        raise SessionError("Linux supervisor message exceeds the size limit")
    connection.sendall(payload)


def _receive(connection: socket.socket) -> dict:
    payload = bytearray()
    while b"\n" not in payload:
        chunk = connection.recv(min(65536, _MAX_MESSAGE + 1 - len(payload)))
        if not chunk:
            raise SessionError("Linux supervisor connection closed")
        payload.extend(chunk)
        if len(payload) > _MAX_MESSAGE:
            raise SessionError("Linux supervisor message exceeds the size limit")
    try:
        result = json.loads(payload[: payload.index(b"\n")])
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise SessionError("Invalid Linux supervisor response") from exc
    if not isinstance(result, dict):
        raise SessionError("Invalid Linux supervisor response")
    return result


def _peer(connection: socket.socket) -> tuple[int, int]:
    pid, uid, _gid = struct.unpack(
        "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    )
    return pid, uid


def _request(proof: SessionProof, command: str, *, timeout: float = 2.0, **options) -> dict:
    if not supported():
        raise SessionError("Linux pidfd/subreaper lifecycle is unavailable on this host")
    try:
        if _boot_id() != proof.boot_id or not _alive(proof.supervisor_pid, proof.supervisor_start):
            raise SessionGone("The recorded Linux supervisor is gone")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect("\0" + proof.address)
            if _peer(connection) != (proof.supervisor_pid, os.getuid()):
                raise SessionError("Linux supervisor peer identity does not match")
            if not _alive(proof.supervisor_pid, proof.supervisor_start):
                raise SessionGone("The recorded Linux supervisor is gone")
            _send(
                connection,
                {
                    "version": _PROTOCOL,
                    "token": proof.token,
                    "command": command,
                    "root_pid": proof.root_pid,
                    "root_start": proof.root_start,
                    **options,
                },
            )
            reply = _receive(connection)
        if reply.get("ok") is not True:
            raise SessionError("Linux supervisor refused the ownership proof or request")
        return reply
    except SessionError:
        raise
    except (OSError, ValueError) as exc:
        raise SessionError(f"Linux supervisor unavailable: {exc.__class__.__name__}") from exc


def inspect(proof: SessionProof) -> SessionSnapshot:
    reply = _request(proof, "inspect")
    return SessionSnapshot(tuple(reply["pids"]), reply["root_alive"])


def stop(proof: SessionProof, *, timeout: float, allow_force: bool) -> SessionStop:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("stop timeout must be finite and > 0")
    reply = _request(
        proof,
        "stop",
        timeout=2 * timeout + 5.0,
        grace=timeout,
        allow_force=allow_force,
    )
    return SessionStop(
        reply["stopped"], reply["forced"], tuple(reply["pids"]), tuple(reply["left_running"])
    )


def launch(
    argv,
    *,
    cwd,
    env,
    stdout,
    stderr,
    startup_timeout: float,
) -> PendingSession:
    if not supported():
        raise SessionError("Linux managed trees require pidfd support (kernel 5.3+, Python 3.11+)")
    parent, child = socket.socketpair()
    guardian = None
    try:
        parent.settimeout(startup_timeout)
        guardian = subprocess.Popen(
            [sys.executable, "-I", str(Path(__file__).resolve()), str(child.fileno())],
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            close_fds=True,
            pass_fds=(child.fileno(),),
            start_new_session=True,
            env={
                name: value
                for name, value in os.environ.items()
                if name not in INSTALL_CREDENTIAL_ENV
            },
        )
        child.close()
        _send(
            parent,
            {
                "argv": list(argv),
                "cwd": cwd,
                "env": dict(env),
                "address": "PepitosAIHub-" + secrets.token_hex(16),
                "token": secrets.token_hex(32),
                "startup_timeout": startup_timeout,
            },
        )
        reply = _receive(parent)
        proof = SessionProof.from_dict(reply.get("proof"))
        if proof is None:
            raise SessionError(
                f"Linux supervisor startup failed: {reply.get('error', 'invalid proof')}"
            )
        if proof.guardian_pid != guardian.pid or _boot_id() != proof.boot_id:
            raise SessionError("Linux supervisor startup identity does not match")
        if not _alive(guardian.pid, proof.guardian_start):
            raise SessionError("Linux guardian exited during startup")
        return PendingSession(proof, parent, guardian)
    except (OSError, ValueError, RuntimeError, TypeError, KeyError, KeyboardInterrupt):
        parent.close()
        child.close()
        if guardian is not None:
            # The guardian is our unreaped Popen child, not a reconstructed PID.
            # Terminate requests its cleanup path; do not kill its safety anchor.
            guardian.terminate()
        raise


def _subreaper() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise SessionError("Linux child subreaper setup failed")


def _children() -> tuple[int, ...]:
    pid = os.getpid()
    text = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="ascii")
    return tuple(int(value) for value in text.split())


def _reap() -> set[int]:
    reaped: set[int] = set()
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return reaped
        if pid == 0:
            return reaped
        reaped.add(pid)


def _signal_children(sig: int, seen: set[tuple[int, int]] | None = None) -> None:
    # This process is live and single-threaded. It does not reap between the
    # kernel's direct-child list and pidfd_open: an exiting child remains our
    # unreaped zombie and its PID cannot be recycled. Rechecking current PPID
    # after opening the pidfd is an additional fail-closed guard, not ancestry.
    for pid in _children():
        try:
            descriptor = os.pidfd_open(pid)
            try:
                process = _process(pid)
                identity = (pid, process.start)
                if process.parent != os.getpid() or (seen is not None and identity in seen):
                    continue
                signal.pidfd_send_signal(descriptor, sig)
                if seen is not None:
                    seen.add(identity)
            finally:
                os.close(descriptor)
        except ProcessLookupError:
            continue


def _cleanup(deadline: float | None = None, proved: set[int] | None = None) -> bool:
    while True:
        _reap()
        if not _children():
            return True
        if proved is not None:
            proved.update(_children())
        _signal_children(signal.SIGKILL)
        if deadline is not None and time.monotonic() >= deadline:
            return False
        time.sleep(_POLL)


def _members(guardian_pid: int) -> tuple[int, ...]:
    group = os.getpgrp()
    session = os.getsid(0)
    members = set(_children())
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            process = _process(int(entry.name))
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            # Hidden processes are never claimed as members. Cleanup does not
            # depend on a complete /proc scan: live subreaper children are its
            # authority, including workers outside this session/group.
            continue
        if (
            process.group == group
            and process.session == session
            and process.state not in {"Z", "X"}
        ):
            members.add(process.pid)
    members.difference_update((os.getpid(), guardian_pid))
    return tuple(sorted(members))


def _stop_tree(guardian_pid: int, grace: float, allow_force: bool) -> dict:
    proved = set(_members(guardian_pid))
    seen: set[tuple[int, int]] = set()
    # The caller of killpg is ITSELF a member of this private live group. Its
    # numeric identifier cannot have become another group's ID. Other sessions
    # cannot join it. Both sentinels ignore this graceful group signal.
    os.killpg(os.getpgrp(), signal.SIGTERM)
    deadline = time.monotonic() + grace
    while True:
        _reap()
        proved.update(_members(guardian_pid))
        if not _children():
            return {
                "ok": True,
                "stopped": True,
                "forced": False,
                "pids": sorted(proved),
                "left_running": [],
            }
        _signal_children(signal.SIGTERM, seen)
        if time.monotonic() >= deadline:
            break
        time.sleep(_POLL)
    if allow_force:
        stopped = _cleanup(time.monotonic() + grace, proved)
        return {
            "ok": True,
            "stopped": stopped,
            "forced": True,
            "pids": sorted(proved),
            "left_running": [] if stopped else list(_members(guardian_pid)),
        }
    return {
        "ok": True,
        "stopped": False,
        "forced": False,
        "pids": sorted(proved),
        "left_running": list(_members(guardian_pid)),
    }


def _supervise(
    bootstrap: socket.socket, config: dict, guardian_pid: int, guardian_start: int
) -> None:
    _subreaper()
    manual_stop = False
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind("\0" + config["address"])
        listener.listen(8)
        listener.settimeout(_POLL)
        process = subprocess.Popen(
            config["argv"],
            cwd=config["cwd"],
            env=config["env"],
            stdin=subprocess.DEVNULL,
            close_fds=True,
        )
        root_start = _process(process.pid).start
        proof = SessionProof(
            config["address"],
            config["token"],
            _boot_id(),
            os.getpid(),
            _process(os.getpid()).start,
            guardian_pid,
            guardian_start,
            process.pid,
            root_start,
        )
        _send(bootstrap, {"proof": proof.to_dict()})
        bootstrap.settimeout(config["startup_timeout"])
        if _receive(bootstrap).get("commit") is not True:
            return
        _send(bootstrap, {"committed": True})
        bootstrap.close()
        while True:
            exited = _reap()
            if not _alive(guardian_pid, guardian_start) or os.getppid() != guardian_pid:
                return
            if process.pid in exited and not manual_stop:
                return
            if not _children():
                return
            try:
                connection, _address = listener.accept()
            except TimeoutError:
                continue
            with connection:
                connection.settimeout(2.0)
                try:
                    if _peer(connection)[1] != os.getuid():
                        continue
                    request = _receive(connection)
                    if (
                        request.get("version") != _PROTOCOL
                        or not isinstance(request.get("token"), str)
                        or len(request["token"]) != 64
                        or any(c not in "0123456789abcdef" for c in request["token"])
                        or not hmac.compare_digest(request["token"], proof.token)
                        or request.get("root_pid") != proof.root_pid
                        or request.get("root_start") != proof.root_start
                    ):
                        _send(connection, {"ok": False})
                        continue
                    if request.get("command") == "inspect":
                        _send(
                            connection,
                            {
                                "ok": True,
                                "pids": list(_members(guardian_pid)),
                                "root_alive": root_alive(proof),
                            },
                        )
                    elif request.get("command") == "stop":
                        grace = request.get("grace")
                        if (
                            type(grace) not in (int, float)
                            or not math.isfinite(grace)
                            or grace <= 0
                            or type(request.get("allow_force")) is not bool
                        ):
                            _send(connection, {"ok": False})
                            continue
                        manual_stop = True
                        result = _stop_tree(guardian_pid, grace, request["allow_force"])
                        _send(connection, result)
                        if result["stopped"]:
                            return
                    else:
                        _send(connection, {"ok": False})
                except (OSError, ValueError, TypeError, RecursionError, OverflowError):
                    # A disconnected controller is not a request to abandon
                    # ownership, nor a reason to expose a token in a traceback.
                    continue
    finally:
        bootstrap.close()
        listener.close()
        _cleanup()


def _main() -> int:
    bootstrap = socket.socket(fileno=int(sys.argv[1]))
    # These handlers are inherited by the forked sentinels. subprocess restores
    # caught handlers on exec; SIG_IGN would incorrectly make services immune
    # to SIGTERM/SIGINT, so use a no-op caught handler instead.
    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda _sig, _frame: None)
    # Direct children must remain unreaped until this single-threaded owner
    # explicitly waits; inherited SIGCHLD=SIG_IGN would break that guarantee.
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    try:
        config = _receive(bootstrap)
        _subreaper()
        # Fail before spawning any workload if the kernel or a container
        # seccomp policy cannot provide the exact-handle signalling we need.
        descriptor = os.pidfd_open(os.getpid())
        try:
            signal.pidfd_send_signal(descriptor, 0)
        finally:
            os.close(descriptor)
        guardian_pid = os.getpid()
        guardian_start = _process(guardian_pid).start
        supervisor = os.fork()
        if supervisor == 0:
            try:
                _supervise(bootstrap, config, guardian_pid, guardian_start)
            except (OSError, ValueError, RuntimeError, TypeError, KeyError) as exc:
                try:
                    _send(bootstrap, {"error": exc.__class__.__name__})
                except OSError:
                    pass
            os._exit(0)
        bootstrap.close()
        while True:
            if supervisor in _reap():
                break
            time.sleep(_POLL)
    except (OSError, ValueError, RuntimeError, TypeError, KeyError) as exc:
        try:
            _send(bootstrap, {"error": exc.__class__.__name__})
        except OSError:
            pass
    finally:
        bootstrap.close()
        _cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
