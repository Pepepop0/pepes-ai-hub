"""What the newly pinned ComfyUI models change, and what they must not.

Pinning turns three declared models into installable ones. It must not turn
any of them into an *available* one: nothing is on disk, so resolution stays
fail-closed, and a dry run still downloads nothing.

Nothing here fetches a model. The shipped URLs point at multi-gigabyte weights
and are only ever read as strings.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from local_ai_stack.model_catalog import model_catalog
from local_ai_stack.model_installer import ModelInstaller, ModelOutcome
from local_ai_stack.model_resolver import (
    StackModelResolver,
    profile_model_specs,
    unresolved_roles,
)
from local_ai_stack.models import ManagedModelPaths, ModelId, ModelProvider, ModelState
from local_ai_stack.planning import PlanAction, build_install_plan
from local_ai_stack.profiles import load_profile

REPO = Path(__file__).resolve().parents[1]

COMFYUI_IDS = (
    "comfyui/flux2_klein_fp8",
    "comfyui/qwen_3_4b_encoder",
    "comfyui/flux2_vae",
)


def refusing_downloader():
    """Any real request is a test failure, not a slow test."""

    def explode(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError(f"a model download was attempted: {request.url}")

    from local_ai_stack.download import Downloader

    return Downloader(client_factory=lambda: httpx.Client(transport=httpx.MockTransport(explode)))


def installer_for(tmp_path: Path) -> ModelInstaller:
    return ModelInstaller(
        paths=ManagedModelPaths(tmp_path / "stack" / "models"),
        downloader=refusing_downloader(),
    )


def resolver_for(tmp_path: Path) -> StackModelResolver:
    return StackModelResolver(installer=installer_for(tmp_path), catalog=model_catalog())


@pytest.fixture
def pepe():
    return load_profile(REPO / "profiles" / "pepe.yaml")


# --- os specs deixaram de ser TBD ------------------------------------------


@pytest.mark.parametrize("model_id", COMFYUI_IDS)
def test_the_model_is_installable_now(model_id):
    catalog = model_catalog()
    spec = catalog.get(ModelId(model_id))
    assert spec is not None
    assert catalog.reason_for(ModelId(model_id)) is None
    assert spec.source.artifact.has_checksum


def test_the_catalog_loads_without_touching_the_network(monkeypatch):
    def explode(*args, **kwargs):  # pragma: no cover
        raise AssertionError("loading the catalog must not touch the network")

    monkeypatch.setattr(httpx.Client, "request", explode)
    monkeypatch.setattr(httpx.Client, "stream", explode)
    catalog = model_catalog()
    assert catalog.unpinned == {}
    # The 4B set pepe and pepe-8gb use, plus Capella's 9B set.
    assert len(catalog.for_provider(ModelProvider.COMFYUI)) == 6


def test_no_role_is_unresolvable_any_more(pepe):
    assert dict(unresolved_roles(pepe, model_catalog())) == {}


def test_every_profile_role_now_has_a_spec(pepe):
    specs = profile_model_specs(pepe, model_catalog())
    assert {str(spec.id) for spec in specs} >= set(COMFYUI_IDS)


# --- resolve continua fail-closed ------------------------------------------


@pytest.mark.parametrize("model_id", COMFYUI_IDS)
def test_a_pinned_model_with_no_file_on_disk_is_still_not_installed(tmp_path, model_id):
    """Installable is not installed. Nothing may leak a location."""
    resolved = resolver_for(tmp_path).resolve(ModelId(model_id))
    assert resolved.state is ModelState.NOT_INSTALLED
    assert resolved.location is None
    assert not resolved.available


def test_resolution_creates_nothing(tmp_path):
    resolver = resolver_for(tmp_path)
    for model_id in COMFYUI_IDS:
        resolver.resolve(ModelId(model_id))
    assert not (tmp_path / "stack").exists()


def test_the_profile_still_reports_the_image_roles_as_missing(tmp_path, pepe):
    missing = resolver_for(tmp_path).missing_for_profile(pepe)
    assert {"image.diffusion", "image.text_encoder", "image.vae"} <= set(missing)
    assert all(value.location is None for value in missing.values())


# --- plan oferece ACQUIRE_MODEL --------------------------------------------


def test_the_install_plan_offers_acquire_model_for_each(pepe):
    plan = build_install_plan(pepe)
    targets = {step.target for step in plan.steps if step.action is PlanAction.ACQUIRE_MODEL}
    assert set(COMFYUI_IDS) <= targets


def test_the_model_plan_proposes_a_verified_download(tmp_path):
    installer = installer_for(tmp_path)
    spec = model_catalog().require(ModelId("comfyui/flux2_klein_fp8"))
    plan = installer.plan(spec)

    assert not plan.is_noop
    assert [step.action for step in plan.steps] == ["download"]
    assert "SHA-256" in plan.steps[0].summary
    assert plan.steps[0].target == "flux-2-klein-4b-fp8.safetensors"


def test_the_plan_targets_the_managed_tree(tmp_path):
    installer = installer_for(tmp_path)
    spec = model_catalog().require(ModelId("comfyui/qwen_3_4b_encoder"))
    destination = installer.paths.destination_for(spec)
    assert destination.parent.name == "text_encoders"
    assert destination.parent.parent.name == "comfyui"
    assert destination.name == "qwen_3_4b.safetensors"


# --- dry-run nao baixa -----------------------------------------------------


@pytest.mark.parametrize("model_id", COMFYUI_IDS)
def test_dry_run_downloads_nothing_and_writes_nothing(tmp_path, model_id):
    installer = installer_for(tmp_path)
    spec = model_catalog().require(ModelId(model_id))

    result = installer.apply(spec, dry_run=True)

    assert result.outcome is ModelOutcome.DRY_RUN
    assert result.steps
    assert not (tmp_path / "stack").exists()


def test_a_dry_run_over_the_whole_profile_stays_inert(tmp_path, pepe):
    installer = installer_for(tmp_path)
    specs = profile_model_specs(pepe, model_catalog())
    results = installer.apply_all(specs, dry_run=True)

    assert results
    assert all(result.outcome is not ModelOutcome.ACQUIRED for result in results)
    assert not (tmp_path / "stack").exists()


# --- validacao continua rigorosa -------------------------------------------


def test_a_url_that_is_not_https_is_still_rejected():
    from local_ai_stack.model_catalog import ModelCatalogError, validate_model_manifest

    with pytest.raises(ModelCatalogError, match="https"):
        validate_model_manifest(
            {
                "schema_version": 1,
                "comfyui": {
                    "demo": {
                        "kind": "vae",
                        "placement": "vae",
                        "filename": "d.safetensors",
                        "url": "http://example.invalid/d.safetensors",
                        "sha256": "a" * 64,
                    }
                },
            }
        )


@pytest.mark.parametrize("sha", ["deadbeef", "z" * 64, "A" * 63])
def test_a_malformed_checksum_is_still_rejected(sha):
    from local_ai_stack.model_catalog import ModelCatalogError, validate_model_manifest

    with pytest.raises(ModelCatalogError):
        validate_model_manifest(
            {
                "schema_version": 1,
                "comfyui": {
                    "demo": {
                        "kind": "vae",
                        "placement": "vae",
                        "filename": "d.safetensors",
                        "url": "https://example.invalid/d.safetensors",
                        "sha256": sha,
                    }
                },
            }
        )


def test_a_file_whose_bytes_do_not_match_the_pin_is_invalid(tmp_path):
    """The pinned weights are never fetched; a stand-in proves the check runs."""
    installer = installer_for(tmp_path)
    spec = model_catalog().require(ModelId("comfyui/flux2_vae"))
    destination = installer.paths.destination_for(spec)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"not the real weights")

    status = installer.detect(spec)
    assert status.state is ModelState.INVALID
    assert (
        StackModelResolver(installer=installer, catalog=model_catalog()).resolve(spec.id).location
        is None
    )
