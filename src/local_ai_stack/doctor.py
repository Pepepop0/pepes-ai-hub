from .health import check_default_services
from .system import detect_system


def doctor_text() -> str:
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
    lines += ["", "Services:"]
    for h in check_default_services():
        lines.append(f"- {h.name:12} {h.host}:{h.port} [{'UP' if h.reachable else 'DOWN'}]")
    return "\n".join(lines)
