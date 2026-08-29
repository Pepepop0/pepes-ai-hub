from __future__ import annotations

from .processes import RuntimeProcessSpec, RuntimeProcessState, RuntimeProcessStatus
from .runtime import ServiceRuntime, StackRuntime, stack_runtime
from .services import StackHealth, aggregate_states
from .system import detect_system


def _as_runtime(stack: StackHealth | StackRuntime) -> StackRuntime:
    """Accept the health-only view older callers still pass in."""
    if isinstance(stack, StackRuntime):
        return stack
    services = tuple(
        ServiceRuntime(
            health,
            RuntimeProcessStatus(
                RuntimeProcessSpec(health.spec.id, health.spec.name, port=health.spec.port),
                RuntimeProcessState.UNKNOWN,
                detail="process state not inspected",
            ),
            health.state,
        )
        for health in stack.services
    )
    return StackRuntime(services, aggregate_states(s.state for s in services))


def _process_line(process: RuntimeProcessStatus) -> str:
    line = f"  Runtime: {process.state.value}"
    if process.pids:
        pids = ", ".join(f"pid {pid}" for pid in process.pids)
        line = f"{line} ({pids}, {process.origin.value})"
    elif process.detail:
        line = f"{line} — {process.detail}"
    return line


def doctor_text(stack: StackHealth | StackRuntime | None = None) -> str:
    f = detect_system()
    lines = [
        "Local AI Stack — Doctor",
        "",
        f"OS: {f.os_name} {f.os_release}",
        f"Python: {f.python_version}",
        f"Git: {'OK' if f.git_available else 'MISSING'}",
        f"nvidia-smi: {'OK' if f.nvidia_smi_available else 'MISSING'}",
    ]
    if f.gpus:
        lines += [f"GPU {i}: {g.name} — {g.vram_mb / 1024:.1f} GB" for i, g in enumerate(f.gpus)]
    else:
        lines.append("GPU: no NVIDIA GPU detected by nvidia-smi")
    runtime = stack_runtime() if stack is None else _as_runtime(stack)
    lines += ["", "Services:"]
    for service in runtime.services:
        health = service.health
        detail = f" — {health.detail}" if health.detail else ""
        lines += [
            "",
            f"{service.name} ({service.spec.address}) [{service.state.value}]",
            _process_line(service.process),
            f"  Health:  {health.state.value}{detail}",
        ]
    lines += ["", f"Stack: {runtime.state.value}"]
    return "\n".join(lines)
