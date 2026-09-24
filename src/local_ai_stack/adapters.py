"""Concrete control specs, one per service the runtime knows how to launch.

Nothing here discovers, downloads or installs anything: the caller supplies the
executable it already has, which in practice means it came from a
:class:`~local_ai_stack.provisioning.RuntimeHandoff` for a MANAGED install. If
the path is wrong, starting fails with ``EXECUTABLE_NOT_FOUND`` rather than
searching the machine, and PATH is never modified.

Every service is configured through its environment rather than through
command-line flags wherever upstream documents an environment variable for it.
Flags differ between releases; the documented variables are stable, and SPEC
already asks for configuration through supported mechanisms. A caller that
needs a different invocation passes its own ``args``.

A service with no executable stays observation-only: status, health and process
discovery all work, and start/stop have nothing to act on.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from .controller import ServiceControlSpec
from .launcher import DEFAULT_STOP_TIMEOUT, ProcessLaunchSpec
from .logs import LogFiles, service_log_files
from .processes import (
    COMFYUI_PROCESS,
    DEFAULT_PROCESS_SPECS,
    OLLAMA_PROCESS,
    OPEN_WEBUI_PROCESS,
    RuntimeProcessSpec,
)
from .services import (
    COMFYUI_SPEC,
    DEFAULT_SERVICE_SPECS,
    OLLAMA_SPEC,
    OPEN_WEBUI_SPEC,
    ServiceSpec,
)

OLLAMA_SERVE_ARGS = ("serve",)
OPEN_WEBUI_SERVE_ARGS = ("serve",)
COMFYUI_SERVE_ARGS: tuple[str, ...] = ()

# Both web apps take noticeably longer to answer than Ollama does: Open WebUI
# builds its database on first boot and ComfyUI imports torch before it listens.
# A readiness wait that gives up sooner would report FAILED on a healthy start.
OPEN_WEBUI_STARTUP_TIMEOUT = 120.0
COMFYUI_STARTUP_TIMEOUT = 180.0
# Ollama listens within a second, but answers no request until it has probed
# every GPU. On a machine with a second adapter -- an integrated AMD GPU it
# probes through Vulkan and then drops -- that took 13 s on one start and 27 s
# on the next on the reference machine. The wait ends as soon as it answers.
OLLAMA_STARTUP_TIMEOUT = 90.0


def _aligned(spec: ServiceSpec, host: str, port: int) -> ServiceSpec:
    """The health spec, moved to wherever the caller says the service listens."""
    if port != spec.port:
        spec = replace(spec, port=port)
    if host != spec.host:
        spec = replace(spec, host=host)
    return spec


def _aligned_process(spec: RuntimeProcessSpec, port: int) -> RuntimeProcessSpec:
    return spec if port == spec.port else replace(spec, port=port)


def ollama_launch_spec(
    executable: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 11434,
    models_dir: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    logs: LogFiles | None = None,
    cwd: Path | None = None,
    startup_timeout: float = OLLAMA_STARTUP_TIMEOUT,
    stop_timeout: float = DEFAULT_STOP_TIMEOUT,
    allow_force_kill: bool = True,
) -> ProcessLaunchSpec:
    """``ollama serve`` bound to an explicit host and port, with an explicit env.

    Ollama starts one or more ``llama-server`` children.  If the root exits
    before those runners do, the launcher must be allowed to terminate the
    members its Windows Job Object proves belong to this launch.  The launcher
    still refuses unrelated processes and never widens the stop without that
    kernel ownership proof.
    """
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
    startup_timeout: float = OLLAMA_STARTUP_TIMEOUT,
    stop_timeout: float = DEFAULT_STOP_TIMEOUT,
    allow_force_kill: bool = True,
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


def open_webui_launch_spec(
    executable: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    data_dir: str | Path | None = None,
    args: Sequence[str] = OPEN_WEBUI_SERVE_ARGS,
    env: Mapping[str, str] | None = None,
    logs: LogFiles | None = None,
    cwd: Path | None = None,
    startup_timeout: float = OPEN_WEBUI_STARTUP_TIMEOUT,
    stop_timeout: float = DEFAULT_STOP_TIMEOUT,
    allow_force_kill: bool = False,
) -> ProcessLaunchSpec:
    """``open-webui serve`` bound to an explicit endpoint and data directory.

    Open WebUI 0.11.3's Typer command accepts host and port as CLI options; it
    does not read the same-named environment variables for those parameters.
    Keep the variables for compatible launchers, but make the supported CLI
    contract authoritative. ``args`` also covers the uvx prefix form.
    """
    child_env: dict[str, str] = {"HOST": host, "PORT": str(port)}
    if data_dir is not None:
        child_env["DATA_DIR"] = str(data_dir)
    if env:
        child_env.update(env)
    return ProcessLaunchSpec(
        service_id=OPEN_WEBUI_SPEC.id,
        argv=(str(executable), *args, "--host", host, "--port", str(port)),
        cwd=cwd,
        env=child_env,
        logs=logs,
        startup_timeout=startup_timeout,
        stop_timeout=stop_timeout,
        allow_force_kill=allow_force_kill,
    )


def open_webui_control_spec(
    executable: str | Path,
    *,
    layout=None,
    host: str = "127.0.0.1",
    port: int = 8080,
    data_dir: str | Path | None = None,
    args: Sequence[str] = OPEN_WEBUI_SERVE_ARGS,
    env: Mapping[str, str] | None = None,
    logs: LogFiles | None = None,
    cwd: Path | None = None,
    startup_timeout: float = OPEN_WEBUI_STARTUP_TIMEOUT,
    stop_timeout: float = DEFAULT_STOP_TIMEOUT,
    allow_force_kill: bool = False,
) -> ServiceControlSpec:
    """Open WebUI, controllable: health spec, process spec and how to launch it."""
    if logs is None and layout is not None:
        logs = service_log_files(layout, OPEN_WEBUI_SPEC.id)
    if data_dir is None and layout is not None:
        data_dir = layout.open_webui_data
    if cwd is None and layout is not None:
        # Open WebUI 0.11.3 stores .webui_secret_key under Path.cwd(), not
        # DATA_DIR. Keep that runtime state inside the managed data tree so a
        # launch from a checkout cannot write into or depend on the repo.
        cwd = layout.open_webui_data
    launch = open_webui_launch_spec(
        executable,
        host=host,
        port=port,
        data_dir=data_dir,
        args=args,
        env=env,
        logs=logs,
        cwd=cwd,
        startup_timeout=startup_timeout,
        stop_timeout=stop_timeout,
        allow_force_kill=allow_force_kill,
    )
    return ServiceControlSpec(
        _aligned(OPEN_WEBUI_SPEC, host, port),
        _aligned_process(OPEN_WEBUI_PROCESS, port),
        launch,
    )


def comfyui_launch_spec(
    executable: str | Path,
    *,
    script: str | Path | None = None,
    extra_model_paths_config: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8188,
    args: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    logs: LogFiles | None = None,
    cwd: Path | None = None,
    startup_timeout: float = COMFYUI_STARTUP_TIMEOUT,
    stop_timeout: float = DEFAULT_STOP_TIMEOUT,
    allow_force_kill: bool = True,
) -> ProcessLaunchSpec:
    """ComfyUI bound to one address.

    ComfyUI has no environment variable for its listen address, so ``--listen``
    and ``--port`` are passed: they are the two flags its README has documented
    for years. ``script`` covers the common shape where the executable is a
    Python interpreter and the entry point is ``main.py``; a packaged build
    passes the executable alone. Anything else goes through ``args``.
    """
    entry = () if script is None else (str(script),)
    address = ("--listen", host, "--port", str(port)) if args is None else tuple(args)
    model_paths = (
        ()
        if extra_model_paths_config is None
        else ("--extra-model-paths-config", str(extra_model_paths_config))
    )
    return ProcessLaunchSpec(
        service_id=COMFYUI_SPEC.id,
        argv=(str(executable), *entry, *address, *model_paths),
        cwd=cwd,
        env=dict(env) if env else {},
        logs=logs,
        startup_timeout=startup_timeout,
        stop_timeout=stop_timeout,
        allow_force_kill=allow_force_kill,
    )


def comfyui_control_spec(
    executable: str | Path,
    *,
    layout=None,
    script: str | Path | None = None,
    extra_model_paths_config: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8188,
    args: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    logs: LogFiles | None = None,
    cwd: Path | None = None,
    startup_timeout: float = COMFYUI_STARTUP_TIMEOUT,
    stop_timeout: float = DEFAULT_STOP_TIMEOUT,
    allow_force_kill: bool = True,
) -> ServiceControlSpec:
    """ComfyUI, controllable: health spec, process spec and how to launch it."""
    if logs is None and layout is not None:
        logs = service_log_files(layout, COMFYUI_SPEC.id)
    if cwd is None and layout is not None:
        # ComfyUI resolves its model and output directories relative to where it
        # runs, so a managed install runs from its own app directory.
        cwd = layout.comfyui_app
    launch = comfyui_launch_spec(
        executable,
        script=script,
        extra_model_paths_config=extra_model_paths_config,
        host=host,
        port=port,
        args=args,
        env=env,
        logs=logs,
        cwd=cwd,
        startup_timeout=startup_timeout,
        stop_timeout=stop_timeout,
        allow_force_kill=allow_force_kill,
    )
    return ServiceControlSpec(
        _aligned(COMFYUI_SPEC, host, port),
        _aligned_process(COMFYUI_PROCESS, port),
        launch,
    )


ControlSpecBuilder = Callable[..., ServiceControlSpec]

# The one place a service id maps to the way that service is launched. Adding a
# fourth service means adding a builder here, not branching anywhere else.
CONTROL_SPEC_BUILDERS: Mapping[str, ControlSpecBuilder] = {
    OLLAMA_SPEC.id: ollama_control_spec,
    OPEN_WEBUI_SPEC.id: open_webui_control_spec,
    COMFYUI_SPEC.id: comfyui_control_spec,
}

_OBSERVATION_ONLY: Mapping[str, tuple[ServiceSpec, RuntimeProcessSpec]] = {
    spec.id: (spec, process)
    for spec, process in zip(DEFAULT_SERVICE_SPECS, DEFAULT_PROCESS_SPECS, strict=True)
}


def observation_only_spec(
    service_id: str,
    *,
    host: str | None = None,
    port: int | None = None,
) -> ServiceControlSpec:
    """A service we can watch but not start: health and discovery, no launch."""
    try:
        service, process = _OBSERVATION_ONLY[service_id]
    except KeyError:
        known = ", ".join(sorted(_OBSERVATION_ONLY))
        raise KeyError(f"unknown service {service_id!r} (known: {known})") from None
    resolved_host = service.host if host is None else host
    resolved_port = service.port if port is None else port
    return ServiceControlSpec(
        _aligned(service, resolved_host, resolved_port),
        _aligned_process(process, resolved_port),
    )


def observation_only_specs(
    service_ids: Sequence[str] = (OPEN_WEBUI_SPEC.id, COMFYUI_SPEC.id),
) -> tuple[ServiceControlSpec, ...]:
    """Watch-only specs, by default for everything except Ollama."""
    return tuple(observation_only_spec(service_id) for service_id in service_ids)


def control_spec_for(
    service_id: str,
    executable: str | Path | None,
    *,
    layout=None,
    **options: Any,
) -> ServiceControlSpec:
    """Controllable when an executable was handed over, watch-only otherwise.

    This is the whole of the "may we launch it?" decision on the runtime side:
    provisioning already answered it by supplying, or withholding, a path.
    """
    if executable is None:
        return observation_only_spec(
            service_id,
            host=options.get("host"),
            port=options.get("port"),
        )
    try:
        builder = CONTROL_SPEC_BUILDERS[service_id]
    except KeyError:
        known = ", ".join(sorted(CONTROL_SPEC_BUILDERS))
        raise KeyError(f"no launch contract for {service_id!r} (known: {known})") from None
    return builder(executable, layout=layout, **options)


def default_control_specs(
    *,
    executables: Mapping[str, str | Path] | None = None,
    layout=None,
    options: Mapping[str, Mapping[str, Any]] | None = None,
    ollama_executable: str | Path | None = None,
    **ollama_options: Any,
) -> tuple[ServiceControlSpec, ...]:
    """Every known service, controllable wherever an executable was supplied."""
    supplied: dict[str, str | Path] = dict(executables or {})
    if ollama_executable is not None:
        supplied[OLLAMA_SPEC.id] = ollama_executable
    per_service: dict[str, dict[str, Any]] = {
        service_id: dict(values) for service_id, values in (options or {}).items()
    }
    if ollama_options:
        per_service.setdefault(OLLAMA_SPEC.id, {}).update(ollama_options)
    return tuple(
        control_spec_for(
            spec.id,
            supplied.get(spec.id),
            layout=layout,
            **per_service.get(spec.id, {}),
        )
        for spec in DEFAULT_SERVICE_SPECS
    )
