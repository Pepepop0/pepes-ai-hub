#!/usr/bin/env python3
"""Live terminal view of a pod install (TOOL-03): stage, models, speed, ETA and stall alert.

``pod_session.py up`` writes one JSON event per line (plus ``[step]`` markers) to its log.
This reads that log (one or more files, in order), follows it while it grows and draws
what is happening: the recipe steps, the install stage, each model with its size, the
current download with speed and time left, the whole model download as a percentage and
the age of the last event. A log that stops growing for ``--stall`` seconds is shown in
red and rings the bell once, because a dead ``up`` process looks exactly like a slow one
in the raw log (15/11: 18 minutes went unnoticed).

    python scripts/pod_watch.py Downloads/pod-onda-5/up.log Downloads/pod-onda-5/up-resume-01.log
    python scripts/pod_watch.py LOG --once        # draw once and exit (finished or not)

Read-only: it never touches the pod, never restarts anything. Exit status when it stops
on its own: 0 install finished, 1 install failed, 2 stalled (``--once`` or ``--exit-on-stall``).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STEPS = ("upload", "verify", "extract", "deps", "prepare", "check", "install", "start")
STEP_LABELS = {
    "upload": "Enviar o pacote",
    "verify": "Conferir o pacote",
    "extract": "Extrair",
    "deps": "Dependências do sistema",
    "prepare": "Preparar o Hub",
    "check": "Verificar o ambiente",
    "install": "Instalar a stack e os modelos",
    "start": "Iniciar os serviços",
}
MARKER = re.compile(r"^\[(\w+)\](?:\s+already done, skipping\.)?$")
MODEL_ID = re.compile(r"((?:comfyui|ollama)/[\w.\-]+)")
SPEED_WINDOW = 12  # samples of the current download kept for the average


def parse_timestamp(text: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(text) if text else None
    except ValueError:
        return None


def parse_line(line: str) -> dict | None:
    """One log line: an event (dict), a ``[step]`` marker (``{"marker": name, "skipped": bool}``)
    or None for anything else, a torn or foreign line included."""
    line = line.strip()
    if not line:
        return None
    marker = MARKER.match(line)
    if marker and marker.group(1) in STEPS:
        return {"marker": marker.group(1), "skipped": "already done" in line}
    if line.startswith("{") and line.endswith("}"):
        try:
            event = json.loads(line)
        except ValueError:
            return None
        if isinstance(event, dict) and isinstance(event.get("message"), str):
            return event
    return None


def model_sizes(profile: Path, catalog: Path) -> dict[str, int]:
    """Model id -> size in bytes for the models a profile installs (its alternatives are not
    installed, so they are left out). Empty when the manifests cannot be read: the view then
    counts models instead of bytes."""
    try:
        import yaml

        prof = yaml.safe_load(profile.read_text(encoding="utf-8")) or {}
        cat = yaml.safe_load(catalog.read_text(encoding="utf-8")) or {}
    except (OSError, ImportError, ValueError):
        return {}
    ids: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key not in ("alternatives", "alternative"):
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str) and MODEL_ID.fullmatch(node):
            ids.add(node)

    walk(prof.get("models", prof))
    sizes = {}
    for provider in ("ollama", "comfyui"):
        for key, entry in (cat.get(provider) or {}).items():
            size = entry.get("size_bytes") if isinstance(entry, dict) else None
            if f"{provider}/{key}" in ids and isinstance(size, int):
                sizes[f"{provider}/{key}"] = size
    return sizes


@dataclass
class Install:
    sizes: dict[str, int] = field(default_factory=dict)
    first: datetime | None = None
    last: datetime | None = None
    events: int = 0
    steps_done: set[str] = field(default_factory=set)
    step: str | None = None
    stage: str = ""
    stage_progress: tuple[int, int] | None = None
    models_total: int | None = None
    models_started: datetime | None = None
    models_done: dict[str, int] = field(default_factory=dict)  # id -> bytes
    order: list[str] = field(default_factory=list)
    current: str | None = None
    transfer: tuple[int, int] = (0, 0)
    speeds: deque = field(default_factory=lambda: deque(maxlen=SPEED_WINDOW))
    activity: str = ""
    failed: str | None = None
    result: str | None = None
    duration: float | None = None

    def feed(self, item: dict) -> None:
        if "marker" in item:
            name = item["marker"]
            if self.step and self.step != name:
                self.steps_done.add(self.step)
            if item["skipped"]:
                self.steps_done.add(name)
                self.step = None
            else:
                self.step = name
            return
        self.events += 1
        stamp = parse_timestamp(item.get("timestamp"))
        if stamp:
            self.first = self.first or stamp
            self.last = stamp
        kind, message = item.get("kind"), item["message"]
        found = MODEL_ID.search(message)
        if item.get("state") == "failed":
            self.failed = message
        if kind == "result":
            self.result = item.get("state") or "succeeded"
            self.duration = (item.get("result") or {}).get("duration_seconds")
            if self.step:
                self.steps_done.add(self.step)
            return
        progress, transfer = item.get("progress"), item.get("transfer")
        if kind == "progress" and progress and message.startswith("Instalando"):
            self.stage = message
            self.stage_progress = (progress.get("completed", 0), progress.get("total", 0))
        if message == "Baixando modelos" or (
            kind == "progress" and progress and "modelos" in message.lower()
        ):
            if progress and isinstance(progress.get("total"), int):
                self.models_total = progress["total"]
            self.models_started = self.models_started or stamp
        if found:
            model = found.group(1)
            if message.startswith("Modelo instalado"):
                size = self.sizes.get(model) or self.transfer[1] or 0
                self.models_done[model] = size
                self.current = None
                self.transfer = (0, 0)
                self.speeds.clear()
            elif message.startswith(("Baixando", "Verificando")):
                if model != self.current:
                    self.speeds.clear()
                self.current = model
                if model not in self.order:
                    self.order.append(model)
                if transfer and transfer.get("total_bytes"):
                    self.transfer = (transfer.get("completed_bytes") or 0, transfer["total_bytes"])
                    if transfer.get("bytes_per_second"):
                        self.speeds.append(transfer["bytes_per_second"])
        elif item.get("activity"):
            act = item["activity"]
            if act.get("category") in ("dependency", "transfer", "workflow", "environment"):
                self.activity = f"{act.get('action', '')} {act.get('item', '')}".strip()
        if kind == "activity" and transfer and transfer.get("total_bytes") and not found:
            self.activity = f"{message} ({_gb(transfer.get('completed_bytes') or 0)} de {_gb(transfer['total_bytes'])})"

    def restart(self) -> None:
        """A new log is a new run of ``up``: the step it was in is not finished."""
        self.step = None
        self.current = None
        self.transfer = (0, 0)
        self.speeds.clear()

    # -- derived figures -------------------------------------------------------------

    @property
    def total_bytes(self) -> int | None:
        if not self.sizes:
            return None
        return sum(self.sizes.values())

    def done_bytes(self) -> int:
        known = sum(self.models_done.get(m) or self.sizes.get(m, 0) for m in self.models_done)
        return known + (self.transfer[0] if self.current else 0)

    def percent(self) -> float | None:
        if self.result == "succeeded":
            return 100.0
        total = self.total_bytes
        if total:
            return min(100.0, 100 * self.done_bytes() / total)
        if self.models_total:
            return 100 * len(self.models_done) / self.models_total
        return None

    def speed(self) -> float | None:
        return sum(self.speeds) / len(self.speeds) if self.speeds else None

    def eta_seconds(self) -> float | None:
        speed, total = self.speed(), self.total_bytes
        if not speed or not total or self.result:
            return None
        return max(0.0, (total - self.done_bytes()) / speed)

    def stalled_for(self, now: datetime) -> float | None:
        """Seconds since the last event, None for a finished install or an empty log."""
        if self.last is None or self.result is not None:
            return None
        return max(0.0, (now - self.last).total_seconds())


def _gb(value: float) -> str:
    return f"{value / 1e9:.1f} GB" if value >= 1e8 else f"{value / 1e6:.0f} MB"


def _clock(seconds: float | None) -> str:
    if seconds is None:
        return "calculando…"
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}h{minutes:02d}min" if hours else f"{minutes}min{secs:02d}s"


def read_logs(paths: list[Path], sizes: dict[str, int]) -> tuple[Install, dict[Path, int]]:
    state, offsets = Install(sizes=sizes), {}
    for path in paths:
        offsets[path] = 0
    state, offsets = _advance(state, paths, offsets)
    return state, offsets


def _advance(state: Install, paths: list[Path], offsets: dict[Path, int]):
    """Feed what each file gained since the last call (a torn last line waits for its end)."""
    for number, path in enumerate(paths):
        try:
            with path.open("rb") as handle:
                handle.seek(offsets.get(path, 0))
                data = handle.read()
        except OSError:
            continue
        if number and offsets.get(path, 0) == 0 and data:
            state.restart()
        end = data.rfind(b"\n") + 1
        for raw in data[:end].decode("utf-8", "replace").splitlines():
            item = parse_line(raw)
            if item is not None:
                state.feed(item)
        offsets[path] = offsets.get(path, 0) + end
    return state, offsets


# ---------------------------------------------------------------------------- drawing


def render(state: Install, now: datetime, stall_after: float, name: str = "") -> object:
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    stall = state.stalled_for(now)
    head = Table.grid(padding=(0, 2))
    head.add_row("Registro", name or "—")
    elapsed = (state.last - state.first).total_seconds() if state.first and state.last else None
    head.add_row("Tempo de instalação", _clock(elapsed))
    head.add_row("Último evento", _ago(stall, state.result))
    head.add_row("Fase", state.stage or "—")
    if state.stage_progress and state.stage_progress[1]:
        done, total = state.stage_progress
        head.add_row("Etapa", f"{min(done + 1, total)} de {total}")
    if state.activity:
        head.add_row("Agora", state.activity[:90])

    steps = Table.grid(padding=(0, 1))
    for step in STEPS:
        done, running = step in state.steps_done, step == state.step
        mark = "[green]✓[/]" if done else "[yellow]▶[/]" if running else "[dim]·[/]"
        steps.add_row(
            mark,
            STEP_LABELS[step]
            if not (done or running)
            else f"[bold]{STEP_LABELS[step]}[/]"
            if running
            else STEP_LABELS[step],
        )

    models = Table.grid(padding=(0, 2))
    names = state.order or sorted(state.sizes)
    for model in names:
        size = state.sizes.get(model) or state.models_done.get(model) or 0
        if model in state.models_done:
            models.add_row("[green]✓[/]", model, _gb(size) if size else "")
        elif model == state.current:
            done, total = state.transfer
            models.add_row(
                "[yellow]▶[/]", f"[bold]{model}[/]", f"{_gb(done)} de {_gb(total or size)}"
            )
        else:
            models.add_row("[dim]·[/]", f"[dim]{model}[/]", f"[dim]{_gb(size)}[/]" if size else "")
    percent = state.percent()
    bar = _bar(percent)
    speed, eta = state.speed(), state.eta_seconds()
    count = f"{len(state.models_done)} de {state.models_total or '?'} modelos"
    summary = Text.assemble(
        (f"{percent:5.1f}%  " if percent is not None else "  ?%  ", "bold"),
        f"{count}",
        (
            f"  ·  {_gb(state.done_bytes())} de {_gb(state.total_bytes)}"
            if state.total_bytes
            else ""
        ),
    )
    pace = Text(
        f"Velocidade {speed / 1e6:.0f} MB/s  ·  faltam {_clock(eta)}"
        if speed
        else "Velocidade —  ·  faltam calculando…"
    )
    parts: list = [
        Panel(head, title="Instalação do pod", border_style="cyan"),
        Panel(steps, title="Receita", border_style="blue"),
    ]
    if names or state.models_total:
        parts += [
            Panel(Group(models, "", bar, summary, pace), title="Modelos", border_style="magenta")
        ]
    if state.result == "succeeded":
        parts.append(
            Panel(
                f"[bold green]Instalação concluída[/] (relógio: {_clock(elapsed)}).",
                border_style="green",
            )
        )
    elif state.failed:
        parts.append(Panel(f"[bold red]Falha:[/] {state.failed}", border_style="red"))
    elif stall is not None and stall >= stall_after:
        parts.append(
            Panel(
                f"[bold red]SEM PROGRESSO HÁ {_clock(stall)}.[/] O processo `up` pode ter parado: "
                "rode o mesmo comando `up` de novo (ele retoma de onde parou).",
                border_style="red",
            )
        )
    return Group(*parts)


def _bar(percent: float | None, width: int = 40):
    from rich.text import Text

    full = round(width * (percent or 0) / 100)
    return Text.assemble(("█" * full, "green"), ("░" * (width - full), "dim"))


def _ago(seconds: float | None, result: str | None) -> str:
    if result:
        return "instalação encerrada"
    return "—" if seconds is None else f"há {_clock(seconds)}"


def default_profile(folder: Path) -> Path:
    """profiles/<id>.yaml for the profile in ``folder/pod-state.json`` (capella when absent)."""
    profile_id = "capella"
    try:
        state = json.loads((folder / "pod-state.json").read_text(encoding="utf-8"))
        target = state.get("target") if isinstance(state, dict) else None
        found = target.get("profile") if isinstance(target, dict) else None
        if isinstance(found, str) and re.fullmatch(r"[\w.-]+", found):
            profile_id = found
    except (OSError, ValueError):
        pass
    return REPO / "profiles" / f"{profile_id}.yaml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "logs", nargs="+", type=Path, help="log(s) of `pod_session.py up`, in order"
    )
    parser.add_argument("--once", action="store_true", help="draw once and exit")
    parser.add_argument(
        "--stall", type=float, default=90.0, help="seconds without events that count as stalled"
    )
    parser.add_argument("--exit-on-stall", action="store_true")
    parser.add_argument("--no-bell", action="store_true")
    parser.add_argument("--refresh", type=float, default=2.0)
    parser.add_argument(
        "--profile",
        type=Path,
        default=None,
        help="profile yaml (default: target.profile of pod-state.json beside the log)",
    )
    parser.add_argument("--catalog", type=Path, default=REPO / "manifests/models.yaml")
    args = parser.parse_args(argv)
    from rich.console import Console
    from rich.live import Live

    console = Console()
    sizes = model_sizes(args.profile or default_profile(args.logs[0].parent), args.catalog)
    state, offsets = read_logs(args.logs, sizes)
    name = ", ".join(path.name for path in args.logs)

    def now() -> datetime:
        return datetime.now(UTC)

    def code() -> int:
        if state.result == "succeeded":
            return 0
        if state.failed:
            return 1
        stalled = state.stalled_for(now())
        return 2 if stalled is not None and stalled >= args.stall else 0

    if args.once:
        console.print(render(state, now(), args.stall, name))
        return code()
    belled = False
    with Live(
        render(state, now(), args.stall, name), console=console, refresh_per_second=4
    ) as live:
        try:
            while True:
                state, offsets = _advance(state, args.logs, offsets)
                live.update(render(state, now(), args.stall, name))
                if state.result or state.failed:
                    break
                stalled = state.stalled_for(now())
                if stalled is not None and stalled >= args.stall:
                    if not belled and not args.no_bell:
                        console.bell()
                        belled = True
                    if args.exit_on_stall:
                        break
                else:
                    belled = False
                time.sleep(args.refresh)
        except KeyboardInterrupt:
            pass
    return code()


if __name__ == "__main__":
    sys.exit(main())
