"""``hub`` subcommands: the desktop controller in-process, JSON in and out, for agents.

Every command goes through :class:`DesktopController`, exactly as the window's
requests do; nothing here decides what a command means or whether it is safe.
This module only adds what a terminal needs: argument parsing errors as JSON,
the single-writer lock, and following an operation to its end.
"""

from __future__ import annotations

import json
import queue
import time
from collections.abc import Callable

from ..hub_application import HubApplicationAdapter
from ..operation_contracts import OperationState
from ..tui.application import HubError
from .api import (
    BUSY_SAFE_COMMANDS,
    COMMANDS,
    READ_ONLY_COMMANDS,
    DesktopController,
    DesktopError,
    serialize,
)
from .instance_lock import InstanceLock

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2

# Commands that need a person or the session that started them; never run from the CLI.
CLI_UNAVAILABLE = {
    "choose_folder": "Opens a native folder dialog. Pass absolute paths in --args instead.",
    "install_git": "Installs machine software and requires confirmation in the desktop window.",
    "resolve_conflicts": "Answers a decision held by the session that started the "
    "installation. Use the window or `hub serve`.",
    "cancel_pending": "Cancels a decision held by the session that started the "
    "installation. Use the window or `hub serve`.",
    "pause_installation": "Controls an installation held by the session that started it. "
    "Use the window or `hub serve`.",
    "resume_installation": "Controls an installation held by the session that started it. "
    "Use the window or `hub serve`.",
    "cancel_installation": "Controls an installation held by the session that started it. "
    "Use the window or `hub serve`.",
    "retry_installation": "Retries an installation held by the session that started it. "
    "Use the window or `hub serve`.",
}

# How often a blocked wait wakes, so Ctrl+C stays responsive on Windows.
_POLL_SECONDS = 0.5

AppFactory = Callable[[], HubApplicationAdapter]


class _RefusingFolderPicker:
    """The CLI has no person to pick a folder; the controller never gets a dialog."""

    def choose_folder(self, initial_path=None):
        raise DesktopError(CLI_UNAVAILABLE["choose_folder"], "interactive_only")


def _emit(payload: object, *, indent: int | None = None) -> None:
    # ASCII keeps the output valid JSON on any console code page.
    print(json.dumps(payload, ensure_ascii=True, allow_nan=False, indent=indent), flush=True)


def _error(message: str, code: str, details: object = None) -> dict:
    return DesktopError(message, code, details=details).payload()


def _default_app() -> HubApplicationAdapter:
    from ..hub_application import create_hub_application
    from ..resources import resource_root

    return create_hub_application(resource_root())


def _controller(app_factory: AppFactory | None) -> DesktopController:
    return DesktopController((app_factory or _default_app)(), folder_picker=_RefusingFolderPicker())


def commands_catalog() -> dict:
    """Every allowlisted command, as the controller itself defines it."""
    rows = []
    for name, (required, optional) in sorted(COMMANDS.items()):
        row = {
            "name": name,
            "required": sorted(required),
            "optional": sorted(optional),
            "read_only": name in READ_ONLY_COMMANDS,
            "busy_safe": name in BUSY_SAFE_COMMANDS,
            "cli": name not in CLI_UNAVAILABLE,
        }
        if name in CLI_UNAVAILABLE:
            row["unavailable_reason"] = CLI_UNAVAILABLE[name]
        rows.append(row)
    return {"commands": rows}


def hub_commands() -> int:
    _emit(commands_catalog(), indent=2)
    return EXIT_OK


def hub_state(*, app_factory: AppFactory | None = None) -> int:
    """Read-only: runs while the window is open."""
    controller = _controller(app_factory)
    try:
        state = controller.state()
    except HubError as exc:
        _emit(_error(str(exc), "unavailable"))
        return EXIT_FAILED
    finally:
        controller.close()
    _emit(state, indent=2)
    return EXIT_OK


def _parse_args(command: str, raw: str | None) -> dict | str:
    """The decoded arguments, or the reason they cannot be sent as given."""
    if raw is None:
        args = {}
    else:
        try:
            args = json.loads(raw)
        except ValueError as exc:
            return f"--args is not valid JSON: {exc}"
        if not isinstance(args, dict):
            return "--args must be a JSON object."
    required, optional = COMMANDS[command]
    if not required <= args.keys() or args.keys() - required - optional:
        return (
            f"Arguments do not match `{command}`: required {sorted(required)}, "
            f"optional {sorted(optional)}."
        )
    return args


def hub_run(
    command: str,
    raw_args: str | None = None,
    *,
    wait: bool = False,
    timeout: float | None = None,
    app_factory: AppFactory | None = None,
    lock: InstanceLock | None = None,
) -> int:
    """Run one command; exit 0 ok, 1 refused or failed, 2 usage.

    A mutating command holds the single-writer lock until its operation ends,
    so the window cannot open midway; while the window holds it, it is refused.
    """
    if command not in COMMANDS:
        _emit(
            _error(
                f"Unknown command `{command}`. Run `hub commands` for the list.",
                "unknown_command",
                {"commands": sorted(set(COMMANDS) - set(CLI_UNAVAILABLE))},
            )
        )
        return EXIT_USAGE
    if command in CLI_UNAVAILABLE:
        _emit(_error(CLI_UNAVAILABLE[command], "interactive_only"))
        return EXIT_USAGE
    args = _parse_args(command, raw_args)
    if isinstance(args, str):
        required, optional = COMMANDS[command]
        _emit(
            _error(
                args,
                "invalid_args",
                {"required": sorted(required), "optional": sorted(optional)},
            )
        )
        return EXIT_USAGE
    mutating = command not in READ_ONLY_COMMANDS
    if mutating:
        lock = lock or InstanceLock()
        if not lock.acquire():
            _emit(
                _error(
                    "The Hub window (or another hub command) is open and owns changes. "
                    "Close it, or use a read-only command.",
                    "desktop_running",
                    {"lock": str(lock.path)},
                )
            )
            return EXIT_FAILED
    try:
        controller = _controller(app_factory)
        try:
            return _execute(controller, command, args, wait=wait, timeout=timeout)
        finally:
            # Waits for a running operation: it lives in this process.
            controller.close()
    finally:
        if mutating:
            lock.release()


def _execute(
    controller: DesktopController,
    command: str,
    args: dict,
    *,
    wait: bool,
    timeout: float | None,
) -> int:
    events: queue.SimpleQueue = queue.SimpleQueue()
    # Subscribed before the command, so the operation's first event is not missed.
    subscription = controller.app.subscribe(events.put) if wait else None
    try:
        try:
            result = controller.command(command, args)
        except DesktopError as exc:
            _emit(exc.payload())
            return EXIT_FAILED
        _emit({"ok": True, "result": result})
        operation_id = result.get("operation_id") if isinstance(result, dict) else None
        if subscription is None or operation_id is None:
            return EXIT_OK
        return _follow(events, operation_id, timeout)
    finally:
        if subscription is not None:
            subscription.close()


def _follow(events: queue.SimpleQueue, operation_id: str, timeout: float | None) -> int:
    """One JSON line per event of this operation, until its single terminal event."""
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            _emit(
                _error(
                    f"Operation did not finish within {timeout:g}s. It keeps running; "
                    "this process exits when it ends.",
                    "timeout",
                    {"operation_id": operation_id},
                )
            )
            return EXIT_FAILED
        try:
            event = events.get(
                timeout=_POLL_SECONDS if remaining is None else min(_POLL_SECONDS, remaining)
            )
        except queue.Empty:
            continue
        if event.operation_id != operation_id:
            continue
        _emit(serialize(event))
        if event.state.terminal:
            return EXIT_OK if event.state is OperationState.SUCCEEDED else EXIT_FAILED


def hub_serve(*, open_browser: bool) -> int:
    from . import run_desktop

    return run_desktop(browser=True, open_browser=open_browser)
