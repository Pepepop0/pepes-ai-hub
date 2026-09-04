"""Where each service listens, derived once from the profile.

An address that is written down twice is an address that will disagree with
itself. Ports live in the profile, service identity and health paths live in
:mod:`local_ai_stack.services`, and this module is the single place the two are
combined into the URLs everything else quotes: launch specs, generated
configuration, and one service being told how to reach another.

Nothing here contacts a service. It answers "what address would this be?", not
"is it up?".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from .components import ComponentId
from .profiles import Profile
from .services import DEFAULT_SERVICE_SPECS, ServiceSpec

DEFAULT_HOST = "127.0.0.1"


class EndpointError(KeyError):
    """A service was asked about that this stack does not describe."""


def aligned_service_spec(spec: ServiceSpec, *, host: str = DEFAULT_HOST, port: int) -> ServiceSpec:
    """The same service, moved to where the caller says it listens."""
    if port != spec.port:
        spec = replace(spec, port=port)
    if host != spec.host:
        spec = replace(spec, host=host)
    return spec


def base_url(spec: ServiceSpec) -> str:
    """The service root, without any health path attached to it."""
    return f"{spec.scheme}://{spec.host}:{spec.port}"


@dataclass(frozen=True)
class StackEndpoints:
    """Every service in one stack, at the addresses the profile chose."""

    services: Mapping[str, ServiceSpec]

    @classmethod
    def from_profile(
        cls,
        profile: Profile,
        *,
        specs: Sequence[ServiceSpec] = DEFAULT_SERVICE_SPECS,
        host: str = DEFAULT_HOST,
    ) -> StackEndpoints:
        """Read the ports the profile declares; keep every other detail.

        A service the profile does not mention keeps its declared default, so
        adding a service to a profile is what moves it, and nothing else.
        """
        resolved: dict[str, ServiceSpec] = {}
        for spec in specs:
            configured = profile.services.get(spec.id)
            port = configured.port if configured is not None else spec.port
            resolved[spec.id] = aligned_service_spec(spec, host=host, port=port)
        return cls(resolved)

    def spec(self, service_id: str) -> ServiceSpec:
        try:
            return self.services[service_id]
        except KeyError:
            known = ", ".join(sorted(self.services))
            raise EndpointError(f"unknown service {service_id!r} (known: {known})") from None

    def base_url(self, service_id: str) -> str:
        return base_url(self.spec(service_id))

    def port(self, service_id: str) -> int:
        return self.spec(service_id).port

    def enabled(self, profile: Profile, service_id: str) -> bool:
        configured = profile.services.get(service_id)
        return bool(configured and configured.enabled)

    @property
    def ollama(self) -> str:
        return self.base_url(ComponentId.OLLAMA)

    @property
    def open_webui(self) -> str:
        return self.base_url(ComponentId.OPEN_WEBUI)

    @property
    def comfyui(self) -> str:
        return self.base_url(ComponentId.COMFYUI)
