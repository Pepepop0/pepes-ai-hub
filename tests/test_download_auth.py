"""Credential warnings follow real sources, without contacting providers or model files."""

from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from test_installation_switching import World, hub

from local_ai_stack import civitai_auth, huggingface_auth
from local_ai_stack.artifacts import Artifact
from local_ai_stack.desktop.api import DesktopController, DesktopError
from local_ai_stack.download_auth import download_auth_providers
from local_ai_stack.model_catalog import model_catalog
from local_ai_stack.models import (
    FileModelSource,
    ModelId,
    ModelKind,
    ModelPlacement,
    ModelRequirement,
    ModelSpec,
)
from local_ai_stack.profiles import validate_profile_data

ROOT = Path(__file__).resolve().parents[1]


def capella():
    path = ROOT / "profiles" / "capella.yaml"
    return validate_profile_data(yaml.safe_load(path.read_text(encoding="utf-8")), path)


def source_profile(url):
    model_id = ModelId("comfyui/source-boundary")
    spec = ModelSpec(
        model_id,
        ModelKind.DIFFUSION,
        FileModelSource(
            Artifact("source-boundary", url, "weights.bin"), ModelPlacement.CHECKPOINTS
        ),
    )
    profile = replace(
        capella(),
        model_requirements=(
            ModelRequirement("image.primary", model_id),
            ModelRequirement("image.alternate", model_id),
        ),
    )
    return profile, {str(model_id): spec}


@pytest.mark.parametrize(
    "url,provider",
    [
        ("https://civitai.com/api/download/models/123", "civitai"),
        ("https://civitai.com:443/api/download/models/123", "civitai"),
        ("https://huggingface.co/org/repo/resolve/main/model.bin", "hf"),
        ("http://civitai.com/api/download/models/123", None),
        ("https://civitai.com.attacker.invalid/model", None),
        ("https://subdomain.civitai.com/model", None),
        ("https://huggingface.co.attacker.invalid/model", None),
        ("https://huggingface.co@attacker.invalid/model", None),
        ("https://user@civitai.com/model", None),
        ("https://civitai.com:8443/model", None),
        ("https://civitai.com:invalid/model", None),
        ("https://example.invalid/civitai.com/model", None),
    ],
)
def test_only_exact_approved_https_sources_create_provider_guidance(url, provider):
    profile, specs = source_profile(url)
    rows = download_auth_providers(profile, specs)
    if provider is None:
        assert rows == []
    else:
        assert len(rows) == 1
        assert rows[0]["provider"] == provider
        assert rows[0]["required"] is (provider == "civitai")
        assert rows[0]["model_ids"] == ["comfyui/source-boundary"]
        assert "url" not in rows[0]


def test_public_capella_sources_offer_optional_public_hugging_face_only():
    catalog = {str(key): spec for key, spec in model_catalog().specs.items()}
    profile = capella()
    providers = {row["provider"]: row for row in download_auth_providers(profile, catalog)}
    assert set(providers) == {"hf"}
    assert providers["hf"]["required"] is False
    # Every declared role/variant is included, not a hand-maintained list of model ids.
    expected = {
        str(requirement.model_id)
        for requirement in profile.model_requirements
        if str(requirement.model_id) in catalog
        and isinstance(catalog[str(requirement.model_id)].source, FileModelSource)
        and catalog[str(requirement.model_id)].source.artifact.url.startswith(
            "https://huggingface.co/"
        )
    }
    assert set(providers["hf"]["model_ids"]) == expected
    assert expected


def test_a_profile_without_file_models_ignores_unselected_authenticated_catalog_sources():
    catalog = {str(key): spec for key, spec in model_catalog().specs.items()}
    chat = next(spec for spec in catalog.values() if not isinstance(spec.source, FileModelSource))
    profile = replace(capella(), model_requirements=(ModelRequirement("chat.primary", chat.id),))
    assert download_auth_providers(profile, catalog) == []


@pytest.fixture
def desktop(tmp_path):
    app = hub(tmp_path, World())
    app.profiles = (capella(),)
    controller = DesktopController(app, folder_picker=object())
    yield controller, app
    controller.close()


def forbidden_civitai():
    pytest.fail("a public profile must not read unrelated provider credentials")


@pytest.mark.parametrize("configured", [False, True])
def test_metadata_works_before_installation_while_busy_without_network_or_detection(
    desktop, monkeypatch, configured
):
    controller, app = desktop
    assert app.active_root() is None
    monkeypatch.setattr(controller, "operation_busy", lambda: True)
    # A truthy opaque local credential is not an account validation.
    monkeypatch.setattr(
        huggingface_auth, "load_token", lambda: (object() if configured else None, "local")
    )
    monkeypatch.setattr(civitai_auth, "load_token", forbidden_civitai)

    def forbidden(*_args, **_kwargs):
        pytest.fail("credential metadata must not perform auth checks or model detection")

    monkeypatch.setattr(huggingface_auth, "check_status", forbidden)
    monkeypatch.setattr(civitai_auth, "check_status", forbidden)
    monkeypatch.setattr(app, "verify_installation", forbidden)
    monkeypatch.setattr(app, "backend_factory", forbidden)
    result = controller.command("download_auth", {"profile_id": "capella"})
    providers = {row["provider"]: row for row in result["providers"]}
    assert providers["hf"]["configured"] is configured
    assert set(providers) == {"hf"}
    assert app.active_root() is None
    expected_fields = {"provider", "name", "required", "configured", "model_ids"}
    assert all(set(row) == expected_fields for row in providers.values())


@pytest.mark.parametrize(
    "provider,url,auth,error",
    [
        (
            "hf",
            "https://huggingface.co/org/repo/resolve/main/model.bin",
            huggingface_auth,
            huggingface_auth.HuggingFaceAuthError,
        ),
        (
            "civitai",
            "https://civitai.com/api/download/models/123",
            civitai_auth,
            civitai_auth.CivitaiAuthError,
        ),
    ],
)
def test_unreadable_local_credential_still_offers_configuration(
    desktop, monkeypatch, provider, url, auth, error
):
    controller, app = desktop
    profile, specs = source_profile(url)
    app.profiles = (profile,)
    monkeypatch.setattr(app, "catalog_specs", lambda: specs)

    def unreadable():
        raise error("stored credential unreadable")

    monkeypatch.setattr(auth, "load_token", unreadable)
    result = controller.command("download_auth", {"profile_id": profile.id})
    row = next(row for row in result["providers"] if row["provider"] == provider)
    assert row["required"] is (provider == "civitai")
    assert row["configured"] is False


def test_unknown_profile_does_not_echo_renderer_input(desktop):
    controller, _app = desktop
    with pytest.raises(DesktopError) as caught:
        controller.command("download_auth", {"profile_id": "not-a-profile"})
    assert caught.value.code == "unknown_profile"
    assert "not-a-profile" not in str(caught.value)
