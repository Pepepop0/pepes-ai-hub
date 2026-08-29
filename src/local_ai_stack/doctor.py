from __future__ import annotations

from .services import StackHealth, check_stack
from .system import detect_system


def doctor_text(stack: StackHealth | None = None) -> str:
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
    health = check_stack() if stack is None else stack
    lines += ["", "Services:"]
    for s in health.services:
        detail = f" — {s.detail}" if s.detail else ""
        lines.append(f"- {s.name:12} {s.spec.address:20} [{s.state.value}]{detail}")
    lines += ["", f"Stack: {health.state.value}"]
    return "\n".join(lines)
