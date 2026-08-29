from __future__ import annotations

import socket
from dataclasses import dataclass


@dataclass(frozen=True)
class PortHealth:
    name: str
    host: str
    port: int
    reachable: bool


DEFAULT_SERVICES = (
    ("Ollama", "127.0.0.1", 11434),
    ("Open WebUI", "127.0.0.1", 8080),
    ("ComfyUI", "127.0.0.1", 8188),
)


def check_port(name: str, host: str, port: int, timeout: float = 0.35) -> PortHealth:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            reachable = True
    except OSError:
        reachable = False
    return PortHealth(name, host, port, reachable)


def check_default_services() -> list[PortHealth]:
    return [check_port(*s) for s in DEFAULT_SERVICES]
