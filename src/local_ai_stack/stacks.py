"""Which programs make up the stack.

A stack says what gets installed. A profile says how the AI behaves and what
hardware it needs. An install location says where it all lives. Keeping them
apart means a stack can gain a component without touching any profile, and a
profile can move between install locations without changing what a stack is.

This version ships one stack, Standard. Choosing between stacks is out of
scope, so there is no selection logic here, only the definition and a lookup
that currently has a single answer.

Component ids come from :mod:`components`, so a stack component, a profile
service, a plan step target and a runtime service id are all the same string.
The directory names on disk belong to StackLayout and are reached by attribute,
derived from the canonical id, which keeps the layout free of any stack
knowledge and keeps the two spellings from drifting apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .components import ComponentId
from .layout import StackLayout

STANDARD_STACK_ID = "standard"


class StackError(ValueError):
    """Unknown stack or component."""


@dataclass(frozen=True)
class StackComponent:
    """One program in a stack, bound to its directories by layout attribute."""

    id: str
    name: str
    app_dir_attr: str
    data_dir_attr: str | None = None

    def app_dir(self, layout: StackLayout) -> Path:
        return getattr(layout, self.app_dir_attr)

    def data_dir(self, layout: StackLayout) -> Path | None:
        if self.data_dir_attr is None:
            return None
        return getattr(layout, self.data_dir_attr)


@dataclass(frozen=True)
class StackDefinition:
    id: str
    name: str
    description: str
    components: tuple[StackComponent, ...]

    @property
    def component_ids(self) -> tuple[str, ...]:
        return tuple(component.id for component in self.components)

    def component(self, component_id: str) -> StackComponent:
        for component in self.components:
            if component.id == component_id:
                return component
        known = ", ".join(self.component_ids)
        raise StackError(f"unknown component {component_id!r} in stack {self.id!r}; known: {known}")

    def directories(self, layout: StackLayout) -> tuple[Path, ...]:
        """Every directory this stack occupies under a given layout."""
        paths: list[Path] = []
        for component in self.components:
            paths.append(component.app_dir(layout))
            data_dir = component.data_dir(layout)
            if data_dir is not None:
                paths.append(data_dir)
        return tuple(paths)


STANDARD_STACK = StackDefinition(
    id=STANDARD_STACK_ID,
    name="Standard",
    description="Ollama for chat, Open WebUI for the interface, ComfyUI for images.",
    components=(
        StackComponent(
            id=ComponentId.OLLAMA,
            name="Ollama",
            app_dir_attr=f"{ComponentId.OLLAMA.attr}_app",
        ),
        StackComponent(
            id=ComponentId.OPEN_WEBUI,
            name="Open WebUI",
            app_dir_attr=f"{ComponentId.OPEN_WEBUI.attr}_app",
            data_dir_attr=f"{ComponentId.OPEN_WEBUI.attr}_data",
        ),
        StackComponent(
            id=ComponentId.COMFYUI,
            name="ComfyUI",
            app_dir_attr=f"{ComponentId.COMFYUI.attr}_app",
            data_dir_attr=f"{ComponentId.COMFYUI.attr}_data",
        ),
    ),
)

_STACKS: dict[str, StackDefinition] = {STANDARD_STACK.id: STANDARD_STACK}

DEFAULT_STACK_ID = STANDARD_STACK_ID


def available_stacks() -> tuple[StackDefinition, ...]:
    return tuple(_STACKS.values())


def get_stack(stack_id: str = DEFAULT_STACK_ID) -> StackDefinition:
    try:
        return _STACKS[stack_id]
    except KeyError as exc:
        known = ", ".join(sorted(_STACKS))
        raise StackError(f"unknown stack {stack_id!r}; known stacks: {known}") from exc
