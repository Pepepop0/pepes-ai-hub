"""Where a managed service writes its output, and how to read a little of it back.

A managed process must not stream through this program: its stdout and stderr
are redirected straight to files under ``StackLayout.logs`` by the OS, so
nothing accumulates in memory and the output survives a manager restart.

Reading is deliberately a tail, not a load: a future Dashboard > Logs view asks
for the last N lines and gets exactly that, whatever the file size.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULT_TAIL_LINES = 200
TAIL_CHUNK = 8192


@dataclass(frozen=True)
class LogFiles:
    """The files one service writes to. ``stderr`` may be merged into stdout."""

    stdout: Path
    stderr: Path | None = None

    @property
    def merged(self) -> bool:
        return self.stderr is None or self.stderr == self.stdout

    @property
    def paths(self) -> tuple[Path, ...]:
        return (self.stdout,) if self.merged else (self.stdout, self.stderr)

    def ensure_parents(self) -> None:
        for path in self.paths:
            path.parent.mkdir(parents=True, exist_ok=True)


def service_log_files(layout, service_id: str, *, split_stderr: bool = True) -> LogFiles:
    """Standard log paths for a service, derived from the stack layout."""
    stdout = layout.log_file(service_id)
    if not split_stderr:
        return LogFiles(stdout)
    return LogFiles(stdout, layout.log_file(f"{service_id}-stderr"))


def read_log_tail(path: Path, lines: int = DEFAULT_TAIL_LINES) -> tuple[str, ...]:
    """Last ``lines`` lines of a log file, reading only the tail of the file."""
    if lines <= 0 or not Path(path).is_file():
        return ()
    collected = b""
    with open(path, "rb") as stream:
        stream.seek(0, 2)
        position = stream.tell()
        while position > 0 and collected.count(b"\n") <= lines:
            step = min(TAIL_CHUNK, position)
            position -= step
            stream.seek(position)
            collected = stream.read(step) + collected
    text = collected.decode("utf-8", errors="replace").replace("\r\n", "\n")
    return tuple(text.splitlines()[-lines:])
