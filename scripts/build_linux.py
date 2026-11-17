#!/usr/bin/env python3
"""Build the allowlisted Linux source bundle; never install or load AI models.

Run with the checkout's Python 3.11+ environment (PyYAML is a project dependency).
The output is reproducible for the same allowlisted source bytes and Python/zlib
implementation. File times, owner identities, local paths and environment values
are deliberately absent from the archive and its manifest.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import stat
import tarfile
import tempfile
import tomllib
from pathlib import Path, PurePosixPath

import yaml

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "BUILD-MANIFEST.json"
# Only these source/resource locations are eligible. Never walk the checkout root.
TREES = {
    "src/local_ai_stack": frozenset({".py"}),
    "profiles": frozenset({".yaml", ".yml"}),
    "manifests": frozenset({".yaml", ".yml"}),
    "workflows": frozenset({".json", ".md"}),
    "schemas": frozenset({".json"}),
    "assets/open-webui": frozenset({".py", ".js", ".css"}),
    "assets/pets": frozenset({".yaml", ".yml", ".txt"}),
    "assets/themes": frozenset({".yaml", ".yml"}),
    "desktop-ui/dist": frozenset(
        {".html", ".js", ".css", ".svg", ".png", ".ico", ".woff", ".woff2"}
    ),
}
FILES = (
    "pyproject.toml",
    "README.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "CHANGELOG.md",
    "assets/app.ico",
    "scripts/build_linux.py",
    "scripts/apply_pack.py",
    "scripts/capella-linux.sh",
    "docs/PROVISIONING.md",
    "docs/OPERATION_CONTRACT.md",
    "docs/MODELS_WORKFLOWS_CONTRACT.md",
)
# Lab and runbook files that ride along when they exist: in the checkout, or in the pack
# being bundled (``--pack``). The public checkout may not carry some of them.
OPTIONAL_FILES = (
    "docs/testing/runbook-linux-1.17.md",
    "docs/research/edit-industry-review.md",
    "scripts/swap_lab.py",
    "scripts/lab_common.py",
    "scripts/lab_review.html",
    "scripts/blend_lab.py",
    "docs/testing/swap-lab.md",
    "docs/testing/runbook-skyrig-1.15.md",
)
PACK_OPTIONAL_FILES = (
    "docs/testing/edit-lab.md",
    "scripts/edit_lab.py",
    "scripts/edit_lab_review.html",
    "scripts/swap_lab_private.py",
    "scripts/swap_composite_private.py",
    "scripts/blend_lab_private.py",
    "scripts/swap_analysis_compose_private.py",
    "docs/research/pose-transfer.md",
    "docs/research/edit-root-cause.md",
)
REQUIRED = (
    "src/local_ai_stack/__main__.py",
    "src/local_ai_stack/cli.py",
    "profiles/capella.yaml",
    "profiles/pepe.yaml",
    "profiles/pepe-8gb.yaml",
    "manifests/artifacts.yaml",
    "manifests/packages.yaml",
    "manifests/repositories.yaml",
    "manifests/models.yaml",
    "manifests/workflows.yaml",
    "workflows/flux2-klein-t2i-api.json",
    "workflows/flux2-klein-edit-api.json",
    "desktop-ui/dist/index.html",
)
EXCLUDED = frozenset(
    {
        "__pycache__",
        "node_modules",
        "tests",
        "test",
        "cache",
        "caches",
        "screenshots",
        "secrets",
        "private",
        "user-data",
        "userdata",
        "user_data",
        "models",
        "downloads",
        "logs",
        "sessions",
        "exports",
        "uploads",
        "backups",
    }
)
TEXT_SUFFIXES = frozenset(
    {".py", ".toml", ".yaml", ".yml", ".json", ".md", ".txt", ".sh", ".html", ".js", ".css", ".svg"}
)


def checked_stat(path: Path) -> os.stat_result:
    """Refuse links and Windows junction/reparse points before following a path."""
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(
        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
    ):
        raise ValueError(f"Links/reparse points are not allowed: {path.relative_to(ROOT)}")
    return info


def safe_name(name: str) -> str:
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or path.as_posix() != name
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in name
        or ":" in name
        or any(ord(char) < 32 for char in name)
    ):
        raise ValueError(f"Unsafe archive name: {name!r}")
    return name


def pack_root(pack: str) -> Path:
    """The directory of an optional pack: the checkout's ``packs/<pack>``."""
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", pack):
        raise ValueError(f"Invalid pack name: {pack!r}")
    directory = ROOT / "packs" / pack
    if not directory.is_dir():
        raise ValueError(f"Pack not found: packs/{pack}")
    return directory


def source_files(pack: str | None = None) -> dict[str, Path]:
    """The files of the bundle by archive name. The public bundle never contains ``packs/``;
    with ``pack``, that pack's files are laid over the checkout's, as ``apply_pack.py`` does."""
    selected: dict[str, Path] = {}
    base = ROOT

    def walk(directory: Path, suffixes: frozenset[str]) -> None:
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            info = checked_stat(path)
            if (
                path.name.startswith(".")
                or path.name.casefold() in EXCLUDED
                or path.stem.casefold()
                in {
                    "secret",
                    "secrets",
                    "private",
                    "credentials",
                    "token",
                    "tokens",
                    "userdata",
                    "user_data",
                    "user-data",
                }
            ):
                continue
            if stat.S_ISDIR(info.st_mode):
                walk(path, suffixes)
            elif stat.S_ISREG(info.st_mode):
                if path.suffix.casefold() in suffixes:
                    name = safe_name(path.relative_to(base).as_posix())
                    if name in selected:
                        if file_bytes(selected[name]) != file_bytes(path):
                            raise ValueError(f"Pack file collides with a checkout file: {name}")
                        continue
                    selected[name] = path
            else:
                raise ValueError(f"Not a regular source file/directory: {path.relative_to(ROOT)}")

    for relative, suffixes in TREES.items():
        directory = ROOT
        for part in PurePosixPath(relative).parts:
            directory = directory / part
            if not stat.S_ISDIR(checked_stat(directory).st_mode):
                raise ValueError(f"Required source directory missing: {relative}")
        walk(directory, suffixes)
    if pack is not None:
        base = pack_root(pack)
        checked_stat(base)
        for relative, suffixes in TREES.items():
            directory = base
            for part in PurePosixPath(relative).parts:
                directory = directory / part
                try:
                    info = checked_stat(directory)
                except FileNotFoundError:
                    break
                if not stat.S_ISDIR(info.st_mode):
                    raise ValueError(f"Pack source directory is not a directory: {relative}")
            else:
                walk(directory, suffixes)
    roots = [ROOT] if base == ROOT else [ROOT, base]
    optional = (*OPTIONAL_FILES, *PACK_OPTIONAL_FILES) if pack else OPTIONAL_FILES
    for relative in (*FILES, *optional):
        for root in roots:
            path = root
            for part in PurePosixPath(relative).parts:
                path = path / part
            if path.is_file():
                break
        else:
            if relative in FILES:
                raise ValueError(f"Required regular file missing: {relative}")
            continue
        if not stat.S_ISREG(checked_stat(path).st_mode):
            raise ValueError(f"Required regular file missing: {relative}")
        selected[safe_name(relative)] = path
    missing = sorted(set(REQUIRED) - selected.keys())
    if missing:
        raise ValueError(f"Required bundle resources missing: {', '.join(missing)}")
    if not any(name.startswith("desktop-ui/dist/assets/") for name in selected):
        raise ValueError("Build desktop-ui/dist with its production assets before packaging.")
    return dict(sorted(selected.items()))


def file_bytes(path: Path) -> bytes:
    if not stat.S_ISREG(checked_stat(path).st_mode):
        raise ValueError(f"Source changed to a nonregular file: {path.relative_to(ROOT)}")
    data = path.read_bytes()
    # A Windows checkout and a Linux checkout of the same text have equal payloads.
    if path.suffix.casefold() in TEXT_SUFFIXES:
        data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return data


def versions(payload: dict[str, bytes]) -> dict:
    project = tomllib.loads(payload["pyproject.toml"].decode("utf-8"))["project"]
    profiles = {}
    for name, data in payload.items():
        if name.startswith("profiles/") and name.endswith((".yaml", ".yml")):
            record = yaml.safe_load(data)
            if isinstance(record, dict) and "id" in record:
                profiles[str(record["id"])] = str(record["version"])
    artifacts = yaml.safe_load(payload["manifests/artifacts.yaml"])["artifacts"]
    packages = yaml.safe_load(payload["manifests/packages.yaml"])["packages"]
    repositories = yaml.safe_load(payload["manifests/repositories.yaml"])["repositories"]
    comfy = repositories["comfyui"]
    return {
        "package": {
            "name": project["name"],
            "version": project["version"],
            "requires_python": project["requires-python"],
        },
        "profiles": dict(sorted(profiles.items())),
        "artifacts": {item["id"]: str(item["version"]) for item in artifacts},
        "packages": {name: str(item["version"]) for name, item in packages.items()},
        "repositories": {
            name: {"revision": item["revision"], "commit": item["commit"]}
            for name, item in repositories.items()
        },
        "comfyui_dependencies": comfy["dependencies"]["requirements"],
        "comfyui_wheel_index": comfy["dependencies"]["extra_index_url"],
    }


def add_member(archive: tarfile.TarFile, name: str, data: bytes | None, mode: int) -> None:
    member = tarfile.TarInfo(safe_name(name))
    member.mode = mode
    member.mtime = member.uid = member.gid = 0
    member.uname = member.gname = ""
    if data is None:
        member.type = tarfile.DIRTYPE
        archive.addfile(member)
    else:
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))


def build(output: Path, pack: str | None = None) -> tuple[Path, Path]:
    payload = {name: file_bytes(path) for name, path in source_files(pack).items()}
    declared_versions = versions(payload)
    version = declared_versions["package"]["version"]
    if not isinstance(version, str) or not re.fullmatch(
        r"[0-9]+(?:\.[0-9]+)+(?:[A-Za-z0-9.+-]*)", version
    ):
        raise ValueError("Package metadata must declare a safe release version.")
    suffix = f"-{pack}" if pack else ""
    folder = f"PepitosAIHub-Capella-v{version}{suffix}-linux-amd64"
    manifest = {
        "schema_version": 1,
        "kind": "linux-source-bundle",
        "archive_root": folder,
        "target": {"os": "linux", "architecture": "amd64"},
        "versions": declared_versions,
        "pack": pack,
        "validation": {"cuda": "not_run", "inference": "not_run"},
        "hash_scope": "All payload files except BUILD-MANIFEST.json itself; hashes cover archived bytes after LF normalization.",
        "files": [
            {
                "path": name,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "mode": "0755" if name.endswith(".sh") else "0644",
            }
            for name, data in payload.items()
        ],
    }
    payload[MANIFEST] = (
        json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f"{folder}.tar.gz"
    receipt = output / f"{destination.name}.sha256"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{folder}-", suffix=".tmp", dir=output)
    temporary_path = Path(temporary)
    try:
        with (
            os.fdopen(descriptor, "wb") as stream,
            gzip.GzipFile(
                filename="", mode="wb", fileobj=stream, compresslevel=9, mtime=0
            ) as compressed,
            tarfile.open(fileobj=compressed, mode="w|", format=tarfile.USTAR_FORMAT) as archive,
        ):
            directories = {folder}
            for name in payload:
                parent = PurePosixPath(folder, name).parent
                while parent != PurePosixPath("."):
                    directories.add(parent.as_posix())
                    parent = parent.parent
            for name in sorted(directories):
                add_member(archive, name, None, 0o755)
            for name, data in sorted(payload.items()):
                add_member(
                    archive,
                    f"{folder}/{name}",
                    data,
                    0o755 if name.endswith(".sh") else 0o644,
                )
        with temporary_path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        os.replace(temporary_path, destination)
        receipt.write_text(f"{digest}  {destination.name}\n", encoding="ascii", newline="\n")
    finally:
        temporary_path.unlink(missing_ok=True)
    return destination, receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist",
        help="Output directory (default: checkout/dist; explicit relative paths use the caller's directory).",
    )
    parser.add_argument(
        "--pack",
        help="Lay packs/<name> over the checkout in the bundle (the public bundle has no packs).",
    )
    args = parser.parse_args()
    try:
        archive, receipt = build(args.output, args.pack)
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, yaml.YAMLError) as exc:
        parser.exit(1, f"Linux source bundle failed: {exc}\n")
    print(f"Archive: {archive}")
    print(f"SHA256: {receipt}")
    print("Source packaging only; CUDA and inference were not validated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
