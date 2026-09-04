"""The IC5 side of the boundary: ModelId in, ResolvedModel out.

This is the whole surface IC6 consumes. A workflow or service configuration
asks for a model by its canonical id and receives a value that either carries a
runtime locator or refuses to. It never sees the catalog, a URL, a checksum, a
placement rule or the downloader, and it never reconstructs a path.

Fail-closed by construction. A locator exists only in the AVAILABLE state, and
AVAILABLE is only reached when the installer has verified the file's checksum
or found the reference in Ollama's store. Everything else — unknown to the
catalog, declared but unpinned, missing, corrupted, re-pinned — resolves to a
state with no location at all, so a partial install cannot become a workflow
input by accident.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .model_catalog import ModelCatalog, model_catalog
from .model_installer import ModelInstaller, ModelStatus
from .models import (
    FileModelLocation,
    FileModelSource,
    ModelId,
    ModelKind,
    ModelLocation,
    ModelSpec,
    ModelState,
    OllamaModelLocation,
    OllamaModelSource,
    ResolvedModel,
)
from .profiles import Profile

UNKNOWN_KIND = ModelKind.AUXILIARY


@dataclass
class StackModelResolver:
    """Resolves catalog models against one installation.

    Implements the :class:`~local_ai_stack.models.ModelResolver` protocol, which
    is the only thing IC6 is allowed to depend on.
    """

    installer: ModelInstaller
    catalog: ModelCatalog | None = None

    def _catalog(self) -> ModelCatalog:
        return self.catalog if self.catalog is not None else model_catalog()

    # --- protocolo ---------------------------------------------------------

    def resolve(self, model_id: ModelId) -> ResolvedModel:
        """The single entry point IC6 uses. Never raises for a missing model."""
        catalog = self._catalog()
        spec = catalog.get(model_id)
        if spec is None:
            reason = catalog.reason_for(model_id)
            detail = (
                f"{model_id} is declared but not installable: {reason}"
                if reason is not None
                else f"{model_id} is not in the model catalog"
            )
            return ResolvedModel(
                id=model_id,
                kind=UNKNOWN_KIND,
                state=ModelState.NOT_INSTALLED,
                detail=detail,
            )
        return self.resolve_spec(spec)

    def resolve_spec(self, spec: ModelSpec) -> ResolvedModel:
        status = self.installer.detect(spec)
        if not status.available:
            return ResolvedModel(
                id=spec.id,
                kind=spec.kind,
                state=status.state,
                revision=status.revision,
                detail=status.detail,
            )
        return ResolvedModel(
            id=spec.id,
            kind=spec.kind,
            state=ModelState.AVAILABLE,
            location=self._location(spec, status),
            revision=status.revision,
            detail=status.detail,
        )

    @staticmethod
    def _location(spec: ModelSpec, status: ModelStatus) -> ModelLocation:
        """Build the locator the provider actually uses. No path is invented."""
        if isinstance(spec.source, FileModelSource):
            if status.path is None:  # pragma: no cover - available implies a path
                raise AssertionError("an available file model must carry its path")
            return FileModelLocation(
                path=status.path,
                runtime_name=spec.source.artifact.filename,
                placement=spec.source.placement,
            )
        assert isinstance(spec.source, OllamaModelSource)
        return OllamaModelLocation(reference=spec.source.reference)

    # --- conveniencias -----------------------------------------------------

    def resolve_many(self, model_ids: Iterable[ModelId]) -> dict[ModelId, ResolvedModel]:
        return {model_id: self.resolve(model_id) for model_id in model_ids}

    def resolve_profile(self, profile: Profile) -> dict[str, ResolvedModel]:
        """Every active role in a profile, resolved.

        Roles come from the profile itself, so no profile name is known here.
        A role left as TBD produces no requirement upstream and therefore does
        not appear: an undecided model is absent, never a fake entry.
        """
        return {
            requirement.role: self.resolve(requirement.model_id)
            for requirement in profile.model_requirements
        }

    def missing_for_profile(self, profile: Profile) -> dict[str, ResolvedModel]:
        """The roles that cannot be handed to IC6 yet, with their reasons."""
        return {
            role: resolved
            for role, resolved in self.resolve_profile(profile).items()
            if not resolved.available
        }


def profile_model_specs(
    profile: Profile, catalog: ModelCatalog | None = None
) -> tuple[ModelSpec, ...]:
    """The installable specs a profile asks for, in role order.

    Declared-but-unpinned roles are skipped rather than faked, so acquisition
    never invents a source it does not have.
    """
    resolved_catalog = catalog if catalog is not None else model_catalog()
    specs: list[ModelSpec] = []
    seen: set[ModelId] = set()
    for requirement in profile.model_requirements:
        spec = resolved_catalog.get(requirement.model_id)
        if spec is not None and spec.id not in seen:
            seen.add(spec.id)
            specs.append(spec)
    return tuple(specs)


def unresolved_roles(profile: Profile, catalog: ModelCatalog | None = None) -> Mapping[str, str]:
    """Roles whose model the catalog cannot install, and why."""
    resolved_catalog = catalog if catalog is not None else model_catalog()
    reasons: dict[str, str] = {}
    for requirement in profile.model_requirements:
        if resolved_catalog.get(requirement.model_id) is not None:
            continue
        reason = resolved_catalog.reason_for(requirement.model_id)
        reasons[requirement.role] = reason or "not in the model catalog"
    return reasons


__all__ = [
    "StackModelResolver",
    "profile_model_specs",
    "unresolved_roles",
]
