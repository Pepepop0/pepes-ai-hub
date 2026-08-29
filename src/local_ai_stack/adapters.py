"""Concrete control specs per service. Ollama is the only launchable one yet.

Nothing here discovers, downloads or installs anything: the caller supplies the
executable it already has, and PATH is never modified. If the path is wrong,
starting fails with ``EXECUTABLE_NOT_FOUND`` rather than searching the machine.

Open WebUI and ComfyUI are declared observation-only in this milestone: their
status, health and process discovery all work, and start/stop refuse.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from .controller import ServiceControlSpec
from .launcher import DEFAULT_STARTUP_TIMEOUT, DEFAULT_STOP_TIMEOUT, ProcessLaunchSpec
from .logs import LogFiles, service_log_files
from .processes import COMFYUI_PROCESS, OLLAMA_PROCESS, OPEN_WEBUI_PROCESS
from .services import COMFYUI_SPEC, OLLAMA_SPEC, OPEN_WEBUI_SPEC

OLLAMA_SERVE_ARGS = ("serve",)


def ollama_launch_spec(
    executable: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 11434,
    models_dir: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    logs: LogFiles | None = None,
    cwd: Path | None = None,
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
    stop_timeout: float = DEFAULT_STOP_TIMEOUT,
    allow_force_kill: bool = False,
) -> ProcessLaunchSpec:
    """``ollama serve`` bound to an explicit host and port, with an explicit env."""
    child_env: dict[str, str] = {"OLLAMA_HOST": f"{host}:{port}"}
    if models_dir is not None:
        child_env["OLLAMA_MODELS"] = str(models_dir)
    if env:
        child_env.update(env)
    return ProcessLaunchSpec(
        service_id=OLLAMA_SPEC.id,
        argv=(str(executable), *OLLAMA_SERVE_ARGS),
        cwd=cwd,
        env=child_env,
        logs=logs,
        startup_timeout=startup_timeout,
        stop_timeout=stop_timeout,
        allow_force_kill=allow_force_kill,
    )


def ollama_control_spec(
    executable: str | Path,
    *,
    layout=None,
    host: str = "127.0.0.1",
    port: int = 11434,
    models_dir: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    logs: LogFiles | None = None,
    cwd: Path | None = None,
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
    stop_timeout: float = DEFAULT_STOP_TIMEOUT,
    allow_force_kill: bool = False,
) -> ServiceControlSpec:
    """Ollama, controllable: health spec, process spec and how to launch it."""
    if logs is None and layout is not None:
        logs = service_log_files(layout, OLLAMA_SPEC.id)
    service = OLLAMA_SPEC if port == OLLAMA_SPEC.port else replace(OLLAMA_SPEC, port=port)
    if host != service.host:
        service = replace(service, host=host)
    launch = ollama_launch_spec(
        executable,
        host=host,
        port=port,
        models_dir=models_dir,
        env=env,
        logs=logs,
        cwd=cwd,
        startup_timeout=startup_timeout,
        stop_timeout=stop_timeout,
        allow_force_kill=allow_force_kill,
    )
    process = OLLAMA_PROCESS if port == OLLAMA_PROCESS.port else replace(OLLAMA_PROCESS, port=port)
    return ServiceControlSpec(service, process, launch)


def observation_only_specs() -> tuple[ServiceControlSpec, ...]:
    """Services we watch but do not start yet."""
    return (
        ServiceControlSpec(OPEN_WEBUI_SPEC, OPEN_WEBUI_PROCESS),
        ServiceControlSpec(COMFYUI_SPEC, COMFYUI_PROCESS),
    )


def default_control_specs(
    *, ollama_executable: str | Path | None = None, layout=None, **ollama_options
) -> tuple[ServiceControlSpec, ...]:
    """Every known service; Ollama becomes controllable once given an executable."""
    ollama = (
        ServiceControlSpec(OLLAMA_SPEC, OLLAMA_PROCESS)
        if ollama_executable is None
        else ollama_control_spec(ollama_executable, layout=layout, **ollama_options)
    )
    return (ollama, *observation_only_specs())
