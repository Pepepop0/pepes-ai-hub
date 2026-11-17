"""Source-level credential guidance, independent of installation and model detection."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit

from .models import FileModelSource, ModelSpec
from .profiles import Profile

# Public Hugging Face files remain available without an account. Civitai's
# authenticated downloads need a token; neither rule claims a saved token is valid.
_PROVIDERS = {
    "huggingface.co": ("hf", "Hugging Face", False),
    "civitai.com": ("civitai", "Civitai", True),
}


def download_auth_providers(profile: Profile, specs: Mapping[str, ModelSpec]) -> list[dict]:
    """Describe only approved HTTPS sources selected by the profile's real roles.

    Never inspect model files, follow redirects, contact providers, or expose URLs.
    A warning does not decide whether a file already exists in the local cache.
    """
    providers: dict[str, dict] = {}
    for requirement in profile.model_requirements:
        spec = specs.get(str(requirement.model_id))
        if spec is None or not isinstance(spec.source, FileModelSource):
            continue
        try:
            source = urlsplit(spec.source.artifact.url)
            approved = _PROVIDERS.get(source.hostname or "")
            if (
                source.scheme != "https"
                or not approved
                or source.username is not None
                or source.password is not None
                or source.port not in {None, 443}
            ):
                continue
        except ValueError:
            continue
        provider, name, required = approved
        row = providers.setdefault(
            provider,
            {"provider": provider, "name": name, "required": required, "model_ids": []},
        )
        model_id = str(spec.id)
        if model_id not in row["model_ids"]:
            row["model_ids"].append(model_id)
    return list(providers.values())
