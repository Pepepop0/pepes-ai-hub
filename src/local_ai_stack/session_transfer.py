"""Collect remote evidence over verified OpenSSH and publish only a verified local ZIP."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
import uuid
from pathlib import Path, PurePosixPath

from .runner import sanitize
from .session_export import verify_session_archive


def _remote_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or path == PurePosixPath("/")
        or not re.fullmatch(r"/[A-Za-z0-9_./-]+", value)
    ):
        raise ValueError(
            "Use dedicated absolute remote paths with ASCII letters, digits, '/', '.', '_' or '-'."
        )
    return path


def _run(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "SSH collection/download timed out; no local ZIP was published."
        ) from exc
    if result.returncode:
        detail = sanitize((result.stderr or result.stdout).strip())
        raise RuntimeError(f"Remote transfer failed ({result.returncode}): {detail[-2000:]}")
    return result


def fetch_session(
    *,
    host: str,
    port: int,
    identity_file: str | None,
    bundle: str,
    root: str,
    destination: Path,
) -> Path:
    """Use a configured/verified host; never disable host-key checks or request key contents."""
    if not host or host.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.@:%\[\]-]+", host):
        raise ValueError("Invalid OpenSSH host/alias.")
    if not 1 <= port <= 65535:
        raise ValueError("SSH port must be between 1 and 65535.")
    destination = destination.expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("Choose a new local ZIP path; existing evidence is never overwritten.")
    bundle_path, root_path = _remote_path(bundle), _remote_path(root)
    remote_zip = str(root_path / "exports" / f"session-{uuid.uuid4().hex}.zip")
    options = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=15"]
    if identity_file:
        key = Path(identity_file).expanduser()
        if not key.is_file():
            raise ValueError("The SSH identity file does not exist.")
        options += ["-i", str(key)]
    command = shlex.join(
        [
            str(bundle_path / ".venv" / "bin" / "python"),
            "-m",
            "local_ai_stack",
            "hub",
            "export-session",
            "--root",
            str(root_path),
            "--output",
            remote_zip,
        ]
    )
    result = _run(["ssh", *options, "-p", str(port), "--", host, command], timeout=600)
    try:
        response = json.loads(result.stdout.strip().splitlines()[-1])
        expected = response["sha256"]
        size = response["bytes"]
        if response["archive"] != remote_zip or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("Invalid archive identity.")
        if type(size) is not int or size < 1:
            raise ValueError("Invalid archive length.")
    except (ValueError, IndexError, KeyError, TypeError) as exc:
        raise RuntimeError(
            "The remote collector did not return a complete archive receipt."
        ) from exc

    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".session-", suffix=".part", dir=destination.parent
    )
    os.close(descriptor)
    temporary_path = Path(temporary)
    try:
        _run(
            ["scp", *options, "-P", str(port), f"{host}:{remote_zip}", str(temporary_path)],
            timeout=3600,
        )
        with temporary_path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if temporary_path.stat().st_size != size or actual != expected:
            raise RuntimeError("Downloaded evidence did not match the remote SHA256/size.")
        verify_session_archive(temporary_path)
        # Same-directory hard link publishes atomically without replacing a raced-in user file.
        os.link(temporary_path, destination)
        return destination
    finally:
        temporary_path.unlink(missing_ok=True)
