"""SPEC-208: ComfyUI's input and output folders do not grow without limit.

The Hub removes, from ``input`` and ``output`` of the ComfyUI app folder, the files older
than the profile's ``runtime.comfyui_retention_hours`` that it (or its filter) wrote; it
never touches subfolders or files of another owner, and it waits while ComfyUI has a job.
The delivered pictures already live in Open WebUI's own storage.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx


def managed_output_prefixes(
    repo_root: Path,
) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    """The ``SaveImage`` prefixes of the loaded workflows, split in two.

    Read from the graphs the manifests (the shipped one and any ``workflows.d`` extra) name,
    so a pack's workflows are covered without being named here. The first part is the
    prefixes saved straight into ``output``; the second maps a subfolder (the analysis
    graphs write into their own, ``PepitoSwap/...``) to its prefixes. Only those are ours:
    any other file or subfolder is left alone. A manifest or graph that cannot be read adds
    nothing, so nothing it would have named is removed.
    """
    from .workflows import WorkflowError, load_workflow_manifest

    try:
        specs = load_workflow_manifest(repo_root / "manifests" / "workflows.yaml")
    except WorkflowError:
        return (), {}
    flat: set[str] = set()
    nested: dict[str, set[str]] = {}
    for spec in specs.values():
        try:
            graph = json.loads((repo_root / spec.file).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(graph, dict):
            continue
        for node in graph.values():
            if not isinstance(node, dict) or node.get("class_type") != "SaveImage":
                continue
            inputs = node.get("inputs") if isinstance(node, dict) else None
            prefix = inputs.get("filename_prefix") if isinstance(inputs, dict) else None
            if not isinstance(prefix, str) or not prefix:
                continue
            if (
                "\\" in prefix
                or ":" in prefix
                or any(part in {"", ".", ".."} for part in prefix.split("/"))
            ):
                continue
            folder, _, name = prefix.rpartition("/")
            if not name:
                continue
            if folder:
                nested.setdefault(folder, set()).add(name)
            else:
                flat.add(name)
    return tuple(sorted(flat)), {folder: tuple(sorted(names)) for folder, names in nested.items()}


_IMAGE = r"\.(?:png|jpe?g|webp)"


def _saved_by(prefixes) -> re.Pattern[str] | None:
    """ComfyUI's SaveImage writes ``<prefix>_00001_.png``. None when there is no prefix."""
    if not prefixes:
        return None
    return re.compile(
        r"(?:" + "|".join(re.escape(prefix) for prefix in prefixes) + r")_\d+_?" + _IMAGE,
        re.IGNORECASE,
    )


# ``pepito_swap_<hex>.png``: the filter's own uploads (``ComfyClient.upload``); and
# ``<uuid>.png``: Open WebUI's uploads of the pictures it edits ("a fresh uuid name").
_INPUT_NAME = re.compile(
    r"(?:pepito_swap_[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    + _IMAGE,
    re.IGNORECASE,
)

QUEUE_TIMEOUT = 3.0


@dataclass(frozen=True)
class RetentionReport:
    removed: int = 0
    freed_bytes: int = 0
    deferred: str | None = None

    def describe(self, hours: float) -> str:
        if self.deferred:
            return f"comfyui cleanup deferred: {self.deferred}"
        return (
            f"comfyui cleanup removed={self.removed} freed_bytes={self.freed_bytes} "
            f"older_than_h={hours:g}"
        )


def comfyui_queue_busy(base_url: str, *, timeout: float = QUEUE_TIMEOUT) -> bool:
    """Whether ComfyUI has a job running or waiting. Unreachable means idle; an answer
    that cannot be read means busy (when unsure, nothing is removed)."""
    try:
        response = httpx.get(f"{base_url.rstrip('/')}/queue", timeout=timeout)
    except httpx.TransportError:
        return False
    try:
        response.raise_for_status()
        data = response.json()
        return bool(data.get("queue_running")) or bool(data.get("queue_pending"))
    except (httpx.HTTPError, ValueError, AttributeError):
        return True


def _expired(
    folder: Path, pattern: re.Pattern[str] | None, cutoff: float
) -> Iterable[tuple[Path, int]]:
    if pattern is None:
        return
    try:
        if folder.is_symlink():
            return
        entries = list(folder.iterdir())
    except OSError:
        return
    for path in entries:
        if not pattern.fullmatch(path.name):
            continue
        try:
            if path.is_symlink() or not path.is_file():
                continue
            info = path.stat()
        except OSError:
            continue
        if info.st_mtime < cutoff:
            yield path, info.st_size


def clean_comfyui_folders(
    comfyui_app: Path,
    hours: float | None,
    *,
    now: datetime,
    busy: Callable[[], bool],
    repo_root: Path | None = None,
) -> RetentionReport:
    """Remove what is older than ``hours`` and ours; nothing without ``hours``.

    The output files that are ours are those the workflows of ``repo_root`` (the shipped
    resources by default) save; see :func:`managed_output_prefixes`.
    """
    if hours is None:
        return RetentionReport()
    from .resources import resource_root

    flat, nested = managed_output_prefixes(repo_root or resource_root())
    output_name = _saved_by(flat)
    cutoff = now.timestamp() - hours * 3600
    doomed = [
        *_expired(comfyui_app / "input", _INPUT_NAME, cutoff),
        *_expired(comfyui_app / "output", output_name, cutoff),
        *(
            entry
            for folder, names in nested.items()
            if (comfyui_app / "output" / folder).resolve().is_relative_to(comfyui_app.resolve())
            for entry in _expired(comfyui_app / "output" / folder, _saved_by(names), cutoff)
        ),
    ]
    if not doomed:
        return RetentionReport()
    if busy():
        return RetentionReport(deferred="ComfyUI is processing a job")
    removed = freed = 0
    for path, size in doomed:
        try:
            path.unlink()
        except OSError:
            continue  # in use or already gone: the next start tries again
        removed += 1
        freed += size
    return RetentionReport(removed, freed)
