#!/usr/bin/env python3
"""Lay an optional pack over a checkout: ``packs/<name>/`` copied onto the root.

A pack is a tree shaped like the repository (profiles, manifests, workflows, tests, docs)
made only of files the checkout does not have; the Hub finds its profile overlays and
manifest extras by where they land. Applying never overwrites a file: one that already
exists with other bytes is a collision and nothing is copied. Applying twice is harmless
(identical files are left as they are). Plain files directly under ``packs/<name>/`` are
the pack's own notes, not part of the tree, and are not copied. ``archive/`` preserves
private originals for reference only and is never installed.

    python scripts/apply_pack.py <name> [--target DIR]

``--target`` is the checkout to apply to (default: this one), the pack is read from
``<target>/packs/<name>``; a temporary copy of the checkout is the way to try a pack.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACK_NAME = re.compile(r"[a-z][a-z0-9_-]*")
INSTALL_TREES = frozenset(
    {
        "profiles",
        "manifests",
        "workflows",
        "tests",
        "docs",
        "scripts",
        "schemas",
        "assets",
        "src",
        "desktop-ui",
    }
)


class PackError(ValueError):
    pass


def checked_path(path: Path) -> os.stat_result:
    """Refuse symlinks and Windows junctions before traversing either tree."""
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(
        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
    ):
        raise PackError(f"links/reparse points are not allowed: {path}")
    return info


def pack_files(pack: Path) -> list[Path]:
    """Installable regular files, excluding archival originals and pack notes."""
    files: list[Path] = []

    def walk(directory: Path) -> None:
        for path in sorted(directory.iterdir()):
            info = checked_path(path)
            if path.name == "__pycache__":
                continue
            if stat.S_ISDIR(info.st_mode):
                walk(path)
            elif stat.S_ISREG(info.st_mode):
                files.append(path.relative_to(pack))
            else:
                raise PackError(f"not a regular pack file: {path}")

    checked_path(pack)
    for entry in sorted(pack.iterdir()):
        if entry.name in INSTALL_TREES:
            if not stat.S_ISDIR(checked_path(entry).st_mode):
                raise PackError(f"pack tree is not a directory: {entry}")
            walk(entry)
    return files


def apply_pack(name: str, target: Path = ROOT) -> list[Path]:
    """Copy ``packs/<name>`` over ``target``; return the files written (relative paths)."""
    if not PACK_NAME.fullmatch(name):
        raise PackError(f"invalid pack name: {name!r}")
    target = target.resolve()
    checked_path(target)
    if (target / "packs").exists():
        checked_path(target / "packs")
    pack = target / "packs" / name
    if not pack.is_dir():
        raise PackError(f"pack not found: {pack}")
    files = pack_files(pack)
    if not files:
        raise PackError(f"pack is empty: {pack}")
    collisions: list[str] = []
    for relative in files:
        destination = target
        for part in relative.parts[:-1]:
            destination /= part
            if (destination.exists() or destination.is_symlink()) and not stat.S_ISDIR(
                checked_path(destination).st_mode
            ):
                collisions.append(relative.as_posix())
                break
        else:
            destination = target / relative
            if destination.exists() or destination.is_symlink():
                info = checked_path(destination)
                if not stat.S_ISREG(info.st_mode) or (
                    destination.read_bytes() != (pack / relative).read_bytes()
                ):
                    collisions.append(relative.as_posix())
    if collisions:
        listed = "\n  ".join(collisions)
        raise PackError(f"pack {name!r} collides with existing files:\n  {listed}")
    written: list[Path] = []
    for relative in files:
        destination = target / relative
        if destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation also refuses a file that appeared after preflight.
        with destination.open("xb") as output, (pack / relative).open("rb") as source:
            while chunk := source.read(1024 * 1024):
                output.write(chunk)
        written.append(relative)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("name", help="pack name: the directory under packs/")
    parser.add_argument("--target", type=Path, default=ROOT, help="checkout to apply to")
    args = parser.parse_args()
    try:
        written = apply_pack(args.name, args.target.resolve())
    except (PackError, OSError) as exc:
        print(f"apply_pack: {exc}", file=sys.stderr)
        return 1
    print(f"Applied pack {args.name!r}: {len(written)} files written to {args.target.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
