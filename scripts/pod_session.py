#!/usr/bin/env python3
"""One-command pod sessions: up, tunnel, sync-filter, collect (SPEC-200).

Drives the system ``ssh``/``scp`` and the bundle's ``scripts/capella-linux.sh`` that runs on
the rented pod. Recipe: docs/PLANO.md section 9; the day's script: docs/testing/pod-onda-2.md.

    python scripts/pod_session.py up --host H --port P --alias ID [--profile pepe] [--tar dist/X.tar.gz]
    python scripts/pod_session.py tunnel --host H --port P --alias ID
    python scripts/pod_session.py start --host H --port P --alias ID   (after creating the account)
    python scripts/pod_session.py sync-filter --host H --port P --alias ID
    python scripts/pod_session.py collect --out PASTA [--host H --port P --alias ID]

``--profile`` picks what is installed (default ``capella``; ``pepe``, ``pepe-8gb``, ...), and
its data lives in ``<BASE>/<profile>-data`` so profiles never share a root. ``host``, ``port``,
``alias`` and ``profile`` are remembered in ``<out>/pod-state.json`` by ``up`` and may be left
out afterwards. ``up`` records each finished step there and skips them when run
again. Tokens go to the remote shell's stdin only and are redacted from everything printed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import sys
import tarfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASE = "/mnt/pepitos-bench"
DEFAULT_PROFILE = "capella"
PROFILE_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
DEFAULT_RELEASE_GLOB = "PepitosAIHub-Capella-v*-linux-amd64.tar.gz"
STATE_FILE = "pod-state.json"
MANIFEST_FILE = "manifest.json"
STEPS = ("upload", "verify", "extract", "deps", "prepare", "check", "install", "start")
FILTER_SUFFIXES = (".py", ".js", ".css")
# Paths under $ROOT that every experiment needs (the Open WebUI database is added by discovery).
COLLECT_PATHS = (
    "data/open-webui/pepito-trace",
    "data/open-webui/pepito-characters",
    "data/open-webui/uploads",
    "apps/comfyui/output",
    "apps/comfyui/input",
    "logs/open-webui.log",
    "logs/comfyui.log",
    "logs/hub-events.log",
)
WEBUI_DB = "webui.db"
DB_SIDECARS = ("-wal", "-shm")


class PodError(Exception):
    """A step failed; the message is safe to print."""


@dataclass
class Result:
    code: int
    output: str = ""


@dataclass
class Pod:
    host: str
    port: int
    alias: str
    user: str = "root"
    release: str = ""
    profile: str = DEFAULT_PROFILE

    @property
    def bundle(self) -> str:
        return f"{BASE}/{self.release}"

    @property
    def root(self) -> str:
        return f"{BASE}/{self.profile}-data"

    @property
    def bench(self) -> str:
        """The bundle's Linux entry script, told which profile it works with."""
        return f"PEPITOS_BENCH_PROFILE={self.profile} bash scripts/capella-linux.sh"

    def ssh_options(self) -> list[str]:
        return [
            "-o",
            f"HostKeyAlias=lium-pod-{self.alias}",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "ServerAliveInterval=30",
        ]

    def ssh(self, remote: str | None = None, *extra: str) -> list[str]:
        command = [
            "ssh",
            *self.ssh_options(),
            "-p",
            str(self.port),
            *extra,
            f"{self.user}@{self.host}",
        ]
        return command + ([remote] if remote is not None else [])

    def scp(self, sources: Sequence[str | Path], destination: str) -> list[str]:
        return [
            "scp",
            *self.ssh_options(),
            "-P",
            str(self.port),
            *map(str, sources),
            f"{self.user}@{self.host}:{destination}",
        ]


# A runner executes one process: run(args, stdin, secrets, show, stdout_to) -> Result.
Runner = Callable[..., Result]


def redact(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def run_process(
    args: Sequence[str],
    stdin: str | None = None,
    secrets: Sequence[str] = (),
    show: bool = True,
    stdout_to: Path | None = None,
) -> Result:
    """The real runner: live, redacted output; ``stdout_to`` streams stdout to a file instead."""
    if stdout_to is not None:
        with stdout_to.open("wb") as sink:
            process = subprocess.Popen(
                list(args),
                stdin=subprocess.PIPE if stdin is not None else None,
                stdout=sink,
                stderr=subprocess.PIPE,
            )
            _, err = process.communicate(stdin.encode() if stdin is not None else None)
        message = redact(err.decode("utf-8", "replace"), secrets)
        if message and show:
            print(message, end="" if message.endswith("\n") else "\n", file=sys.stderr)
        return Result(process.returncode, message)
    process = subprocess.Popen(
        list(args),
        stdin=subprocess.PIPE if stdin is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if stdin is not None and process.stdin is not None:
        process.stdin.write(stdin)
        process.stdin.close()
    lines: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        line = redact(line, secrets)
        lines.append(line)
        if show:
            print(line, end="", flush=True)
    return Result(process.wait(), "".join(lines))


def local_tokens() -> tuple[str, str]:
    """The Civitai and Hugging Face tokens from the local store (never from arguments)."""
    sys.path.insert(0, str(REPO / "src"))
    from local_ai_stack import civitai_auth, huggingface_auth

    return civitai_auth.load_token()[0] or "", huggingface_auth.load_token()[0] or ""


# ---------------------------------------------------------------- state


def read_state(out: Path) -> dict:
    try:
        data = json.loads((out / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(out: Path, state: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / STATE_FILE).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def newest_release_tar() -> Path | None:
    found = sorted((REPO / "dist").glob(DEFAULT_RELEASE_GLOB), key=lambda p: p.stat().st_mtime)
    return found[-1] if found else None


def release_of(tar: Path) -> str:
    return tar.name.removesuffix(".tar.gz")


def profile_needs_civitai(profile_id: str) -> bool:
    """Whether the profile downloads from Civitai (decided from this checkout's profiles)."""
    sys.path.insert(0, str(REPO / "src"))
    from local_ai_stack.linux_bench import BenchProfileError, bench_profile, needs_civitai

    try:
        return needs_civitai(bench_profile(profile_id))
    except BenchProfileError as error:
        raise PodError(str(error)) from error


def pod_from(args: argparse.Namespace, state: dict) -> Pod:
    target = state.get("target") or {}
    host = args.host or target.get("host")
    port = args.port or target.get("port")
    alias = args.alias or target.get("alias")
    if not (host and port and alias):
        raise PodError("Give --host, --port and --alias (or run `up` with this --out first).")
    release = getattr(args, "release", None) or target.get("release") or ""
    if not release:
        tar = newest_release_tar()
        release = release_of(tar) if tar else ""
    if not release:
        raise PodError("Release unknown: pass --release or --tar, or build one into dist/.")
    profile = getattr(args, "profile", None) or target.get("profile") or DEFAULT_PROFILE
    if not PROFILE_ID.fullmatch(profile):
        raise PodError(f"Invalid profile id: {profile!r}")
    return Pod(host, int(port), alias, args.user or target.get("user") or "root", release, profile)


# ---------------------------------------------------------------- up


def ensure(result: Result, what: str) -> None:
    if result.code != 0:
        raise PodError(f"{what} failed (exit {result.code}). Run the same command again to resume.")


def up(pod: Pod, tar: Path, out: Path, run: Runner, tokens: Callable[[], tuple[str, str]]) -> None:
    target = {
        "host": pod.host,
        "port": pod.port,
        "alias": pod.alias,
        "user": pod.user,
        "release": pod.release,
        "profile": pod.profile,
    }
    state = read_state(out)
    recorded = state.get("target")
    if isinstance(recorded, dict):
        recorded = {"profile": DEFAULT_PROFILE, **recorded}  # sessions saved before --profile
    if recorded != target:
        state = {"target": target, "done": []}
    done: list[str] = state.setdefault("done", [])
    state["tar"] = str(tar.resolve())
    write_state(out, state)
    sha = tar.with_name(tar.name + ".sha256")
    if not tar.is_file() or not sha.is_file():
        raise PodError(f"Missing {tar} or {sha.name}: build the Linux bundle first.")
    in_bundle = f"cd {shlex.quote(pod.bundle)} && {pod.bench}"
    root = shlex.quote(pod.root)
    remote_tar = f"/tmp/{tar.name}"
    plain: dict[str, Callable[[], Result]] = {
        "upload": lambda: run(pod.scp([tar, sha], "/tmp/")),
        "verify": lambda: run(pod.ssh(f"cd /tmp && sha256sum -c {shlex.quote(sha.name)}")),
        "extract": lambda: run(
            pod.ssh(f"mkdir -p {BASE} && tar -xzf {shlex.quote(remote_tar)} -C {BASE}")
        ),
        "deps": lambda: run(pod.ssh(f"{in_bundle} deps")),
        "prepare": lambda: run(pod.ssh(f"{in_bundle} prepare")),
        "check": lambda: run(pod.ssh(f"{in_bundle} check {root}")),
        "start": lambda: run(pod.ssh(f"{in_bundle} start {root}")),
    }
    for step in STEPS:
        if step in done:
            print(f"[{step}] already done, skipping.")
            continue
        print(f"[{step}]")
        if step == "install":
            result = install(pod, run, tokens)
        else:
            result = plain[step]()
        ensure(result, step)
        done.append(step)
        write_state(out, state)
    print(
        "Pod is up. Next: `tunnel`, open http://127.0.0.1:18080, create the account, then run `start`"
    )
    print(f"once more so the {pod.profile} models and the Image button are synced.")


def install(pod: Pod, run: Runner, tokens: Callable[[], tuple[str, str]]) -> Result:
    civitai, hugging_face = tokens()
    if not civitai and profile_needs_civitai(pod.profile):
        raise PodError("No Civitai token in the local store: save it in the Hub first.")
    remote = (
        "IFS= read -r CIVITAI_TOKEN; IFS= read -r HF_TOKEN; "
        'if [ -n "$CIVITAI_TOKEN" ]; then export CIVITAI_TOKEN; fi; '
        'if [ -n "$HF_TOKEN" ]; then export HF_TOKEN; fi; '
        f"cd {shlex.quote(pod.bundle)} && {pod.bench} install {shlex.quote(pod.root)} < /dev/null"
    )
    return run(
        pod.ssh(remote),
        stdin=f"{civitai}\n{hugging_face}\n",
        secrets=(civitai, hugging_face),
    )


# ---------------------------------------------------------------- tunnel


def tunnel(
    pod: Pod,
    local_port: int,
    remote_port: int,
    run: Runner,
    sleep: Callable[[float], None] = time.sleep,
    attempts: int | None = None,
) -> None:
    """Keep the forward alive: reconnect two seconds after every drop until Ctrl+C."""
    forward = f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}"
    command = pod.ssh(None, "-N", "-o", "ExitOnForwardFailure=yes", "-L", forward)
    print(f"Tunnel to {pod.alias}: Open WebUI at http://127.0.0.1:{local_port} (Ctrl+C ends it).")
    count = 0
    try:
        while attempts is None or count < attempts:
            count += 1
            result = run(command)
            print(f"Tunnel dropped (exit {result.code}); reconnecting in 2 s.", file=sys.stderr)
            sleep(2)
    except KeyboardInterrupt:
        print("Tunnel closed.")


def start(pod: Pod, run: Runner) -> None:
    """Step 5 of the recipe: start again after the account exists."""
    command = f"cd {shlex.quote(pod.bundle)} && {pod.bench} start {shlex.quote(pod.root)}"
    ensure(run(pod.ssh(command)), "start")


# ---------------------------------------------------------------- sync-filter


def filter_files(assets: Path) -> list[Path]:
    return sorted(p for p in assets.iterdir() if p.is_file() and p.suffix in FILTER_SUFFIXES)


def sync_filter(pod: Pod, assets: Path, run: Runner) -> None:
    files = filter_files(assets)
    if not files:
        raise PodError(f"No filter files in {assets}.")
    bundle = shlex.quote(pod.bundle)
    root = shlex.quote(pod.root)
    ensure(run(pod.scp(files, f"{pod.bundle}/assets/open-webui/")), "upload of the filter files")
    names = " ".join(shlex.quote(f.name) for f in files)
    remote = (
        f"cd {bundle} && n=0; for f in {names}; do "
        f'for d in $(find {root}/apps/open-webui -name "$f" -type f); do '
        'cp "assets/open-webui/$f" "$d" && n=$((n+1)) && echo "updated $d"; done; done; '
        'echo "$n copies"; [ "$n" -gt 0 ] && '
        f"{pod.bench} stop {root} && {pod.bench} start {root}"
    )
    ensure(run(pod.ssh(remote)), "filter update and restart")
    print("Done. In the browser press Ctrl+F5.")


# ---------------------------------------------------------------- collect


def find_database(pod: Pod, run: Runner) -> str:
    """Path of webui.db relative to the pod root, discovered on the pod."""
    root = shlex.quote(pod.root)
    data = shlex.quote(f"{pod.root}/data/open-webui")
    remote = (
        f"p=$(find {data} -name {WEBUI_DB} -type f 2>/dev/null | head -n 1); "
        f'if [ -z "$p" ]; then p=$(find {root} -name {WEBUI_DB} -type f 2>/dev/null | head -n 1); fi; '
        'echo "$p"'
    )
    result = run(pod.ssh(remote), show=False)
    ensure(result, "database discovery")
    found = [line.strip() for line in result.output.splitlines() if line.strip()]
    if not found or not found[-1].startswith(pod.root + "/"):
        raise PodError(f"{WEBUI_DB} not found under {pod.root} on the pod.")
    return found[-1][len(pod.root) + 1 :]


def collect_paths(database: str) -> list[str]:
    return [*COLLECT_PATHS, database, *(database + suffix for suffix in DB_SIDECARS)]


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def extract(archive: Path, out: Path, skipped: dict[str, str] | None = None) -> list[str]:
    """Unpack the session tar into ``out``; returns the relative paths of the regular files."""
    files: list[str] = []
    with tarfile.open(archive) as bundle:
        members = bundle.getmembers()
        base = out.resolve()
        for member in members:
            if not (base / member.name).resolve().is_relative_to(base):
                raise PodError(f"Unsafe path in the session tar: {member.name}")
            if not (member.isfile() or member.isdir()):
                raise PodError(f"Unexpected entry in the session tar: {member.name}")
        for member in members:
            try:
                bundle.extract(member, out)
            except OSError as error:
                if not member.isfile() or Path(member.name).name not in (
                    WEBUI_DB + "-wal",
                    WEBUI_DB + "-shm",
                ):
                    raise
                message = f"{type(error).__name__}: {error}"
                if skipped is not None:
                    skipped[member.name] = message
                print(
                    f"Skipped local database sidecar {member.name}: {message}. "
                    "Close the local Open WebUI/database and collect again for a complete copy.",
                    file=sys.stderr,
                )
                continue
            if member.isfile():
                files.append(member.name)
    return files


def collect(pod: Pod, out: Path, run: Runner) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    database = find_database(pod, run)
    print(f"Database on the pod: {pod.root}/{database}")
    paths = collect_paths(database)
    quoted = " ".join(shlex.quote(p) for p in paths)
    remote = f"cd {shlex.quote(pod.root)} && tar --ignore-failed-read -cf - {quoted}"
    archive = out / ".session.tar"
    skipped: dict[str, str] = {}
    try:
        ensure(run(pod.ssh(remote), stdout_to=archive), "session download")
        names = extract(archive, out, skipped)
    finally:
        archive.unlink(missing_ok=True)
    entries = {}
    for name in sorted(names):
        path = out / name
        entries[name] = {"bytes": path.stat().st_size, "sha256": sha256_of(path)}
    missing = [p for p in paths if p not in skipped and not (out / p).exists()]
    manifest = {
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "pod": {"host": pod.host, "port": pod.port, "alias": pod.alias, "root": pod.root},
        "database": database,
        "requested": paths,
        "missing": missing,
        "files": entries,
        "skipped": skipped,
    }
    target = out / MANIFEST_FILE
    target.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    total = sum(item["bytes"] for item in entries.values())
    print(f"Collected {len(entries)} files, {total / 1e6:.1f} MB into {out}; manifest: {target}")
    if missing:
        print("Not present on the pod: " + ", ".join(missing))
    return target


# ---------------------------------------------------------------- command line


def parser() -> argparse.ArgumentParser:
    main = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = main.add_subparsers(dest="command", required=True)

    def common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--host")
        sub.add_argument("--port", type=int)
        sub.add_argument("--alias", help="pod id; HostKeyAlias becomes lium-pod-<alias>")
        sub.add_argument("--user", default=None, help="ssh user (default root)")
        sub.add_argument(
            "--profile",
            default=None,
            help=f"profile to install and operate (default {DEFAULT_PROFILE}; remembered by up)",
        )
        sub.add_argument(
            "--out", type=Path, default=Path("pod-session"), help="folder with pod-state.json"
        )

    sub = commands.add_parser("up", help="steps 1-5 of the recipe, resumable")
    common(sub)
    sub.add_argument("--tar", type=Path, help="bundle .tar.gz (default: newest in dist/)")
    sub = commands.add_parser("tunnel", help="ssh forward with reconnection")
    common(sub)
    sub.add_argument("--release")
    sub.add_argument("--local-port", type=int, default=18080)
    sub.add_argument("--remote-port", type=int, default=8080)
    sub = commands.add_parser("start", help="start the services again (after creating the account)")
    common(sub)
    sub.add_argument("--release")
    sub = commands.add_parser(
        "sync-filter", help="update the filter and JS without a new tar, restart"
    )
    common(sub)
    sub.add_argument("--release")
    sub = commands.add_parser(
        "collect", help="bring the session back, with the Open WebUI database"
    )
    common(sub)
    sub.add_argument("--release")
    return main


def main(argv: Sequence[str] | None = None, run: Runner = run_process, tokens=local_tokens) -> int:
    args = parser().parse_args(argv)
    try:
        state = read_state(args.out)
        if args.command == "up":
            tar = args.tar or (Path(state["tar"]) if state.get("tar") else newest_release_tar())
            if tar is None:
                raise PodError("No bundle in dist/: build it or pass --tar.")
            args.release = release_of(tar)
            up(pod_from(args, state), tar, args.out, run, tokens)
        elif args.command == "tunnel":
            tunnel(pod_from(args, state), args.local_port, args.remote_port, run)
        elif args.command == "start":
            start(pod_from(args, state), run)
        elif args.command == "sync-filter":
            sync_filter(pod_from(args, state), REPO / "assets" / "open-webui", run)
        else:
            collect(pod_from(args, state), args.out, run)
    except PodError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
