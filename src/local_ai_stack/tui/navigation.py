from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Section(StrEnum):
    DASHBOARD = "dashboard"
    INSTALLATION = "installation"
    SETTINGS = "settings"


@dataclass(frozen=True)
class NavigationItem:
    id: str
    label: str


SECTION_LABELS: dict[Section, str] = {
    Section.DASHBOARD: "Dashboard",
    Section.INSTALLATION: "Instalação",
    Section.SETTINGS: "Configurações",
}

SECTION_ITEMS: dict[Section, tuple[NavigationItem, ...]] = {
    Section.DASHBOARD: (
        NavigationItem("status", "Status"),
        NavigationItem("logs", "Logs"),
        NavigationItem("ollama", "Ollama"),
        NavigationItem("open-webui", "Open WebUI"),
        NavigationItem("comfyui", "ComfyUI"),
    ),
    Section.INSTALLATION: (
        NavigationItem("profile", "Perfil"),
        NavigationItem("stack", "Stack"),
        NavigationItem("location", "Local"),
        NavigationItem("plan", "Plano"),
    ),
    Section.SETTINGS: (
        NavigationItem("general", "Geral"),
        NavigationItem("interface", "Interface"),
        NavigationItem("runtime", "Runtime"),
        NavigationItem("advanced", "Avançado"),
    ),
}

DEFAULT_ITEM: dict[Section, str] = {
    Section.DASHBOARD: "status",
    Section.INSTALLATION: "profile",
    Section.SETTINGS: "general",
}


def route_id(section: Section, item_id: str) -> str:
    return f"view-{section.value}-{item_id}"


def sidebar_button_id(section: Section, item_id: str) -> str:
    return f"side-{section.value}-{item_id}"


SIDEBAR_ROUTES: dict[str, tuple[Section, str]] = {
    sidebar_button_id(section, item.id): (section, item.id)
    for section, items in SECTION_ITEMS.items()
    for item in items
}
