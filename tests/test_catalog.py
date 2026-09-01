"""The pinned catalog. Reads manifests; never contacts the network."""

from __future__ import annotations

from pathlib import Path

import pytest

from local_ai_stack.artifacts import Artifact
from local_ai_stack.catalog import (
    ARTIFACT_MANIFEST,
    CatalogError,
    PinnedRepository,
    artifact_catalog,
    artifact_for,
    manifest_dir,
    repository_catalog,
    repository_for,
)
from local_ai_stack.components import ComponentId

REPO = Path(__file__).resolve().parents[1]

OLLAMA_SHA = "2439cbea65310b1aadf7d8fc41d7faf5d033f920d42e00a476c58bf9bff6950e"


# --- manifest existe e valida ----------------------------------------------


def test_manifest_ships_with_the_repository():
    directory = manifest_dir()
    assert directory is not None
    assert (directory / ARTIFACT_MANIFEST).is_file()
    assert directory == REPO / "manifests"


def test_artifact_catalog_loads():
    catalog = artifact_catalog()
    assert catalog
    assert all(isinstance(a, Artifact) for a in catalog.values())


# --- pin do Ollama ---------------------------------------------------------


def test_ollama_is_pinned_to_an_exact_version():
    artifact = artifact_for(ComponentId.OLLAMA)
    assert artifact is not None
    assert artifact.version == "0.33.2"
    assert artifact.component == "ollama"


def test_ollama_pin_carries_a_verifiable_checksum():
    artifact = artifact_for(ComponentId.OLLAMA)
    assert artifact is not None
    assert artifact.has_checksum
    assert artifact.sha256 == OLLAMA_SHA
    assert len(artifact.sha256) == 64


def test_ollama_url_is_the_official_release_asset():
    artifact = artifact_for(ComponentId.OLLAMA)
    assert artifact is not None
    assert artifact.url == (
        "https://github.com/ollama/ollama/releases/download/v0.33.2/ollama-windows-amd64.zip"
    )
    assert artifact.url.startswith("https://github.com/ollama/ollama/releases/download/")


def test_the_pinned_version_appears_in_the_url():
    """The URL and the recorded version cannot drift apart unnoticed."""
    artifact = artifact_for(ComponentId.OLLAMA)
    assert artifact is not None
    assert f"/v{artifact.version}/" in artifact.url


def test_nothing_resolves_latest():
    for artifact in artifact_catalog().values():
        assert "latest" not in artifact.url.lower()
        assert artifact.version is not None


def test_portable_archive_not_the_installer():
    """The installer writes PATH and the registry, so it is never pinned."""
    artifact = artifact_for(ComponentId.OLLAMA)
    assert artifact is not None
    assert artifact.filename.endswith(".zip")
    assert "setup" not in artifact.filename.lower()


def test_every_pinned_artifact_has_a_checksum_and_a_component():
    for artifact in artifact_catalog().values():
        assert artifact.has_checksum, artifact.id
        assert artifact.component is not None, artifact.id


def test_unknown_component_has_no_artifact():
    assert artifact_for("stable-diffusion") is None


def test_lookup_accepts_the_legacy_spelling():
    assert artifact_for("open_webui") == artifact_for("open-webui")


# --- repositorios ----------------------------------------------------------


def test_repository_catalog_loads_and_is_canonical():
    catalog = repository_catalog()
    assert catalog
    assert all(isinstance(entry, PinnedRepository) for entry in catalog.values())
    assert "open_webui" not in catalog


def test_unpinned_repository_is_reported_as_absent():
    """A TBD revision is a placeholder, not a pin."""
    for component, entry in repository_catalog().items():
        if not entry.is_pinned:
            assert repository_for(component) is None


# --- validacao -------------------------------------------------------------


def test_malformed_repository_manifest_is_rejected(tmp_path, monkeypatch):
    from local_ai_stack import catalog as catalog_module

    manifest = tmp_path / "manifests"
    manifest.mkdir()
    (manifest / "repositories.yaml").write_text(
        "schema_version: 1\nrepositories:\n  comfyui:\n    url: ftp://x/y.git\n    revision: v1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(catalog_module, "manifest_dir", lambda: manifest)
    catalog_module.clear_cache()
    try:
        with pytest.raises(CatalogError, match="https"):
            catalog_module.repository_catalog()
    finally:
        catalog_module.clear_cache()


def test_missing_manifest_directory_yields_an_empty_catalog(monkeypatch):
    from local_ai_stack import catalog as catalog_module

    monkeypatch.setattr(catalog_module, "manifest_dir", lambda: None)
    catalog_module.clear_cache()
    try:
        assert catalog_module.artifact_catalog() == {}
        assert catalog_module.artifact_for("ollama") is None
    finally:
        catalog_module.clear_cache()


def test_reading_the_catalog_opens_no_socket(monkeypatch):
    import httpx

    def explode(*args, **kwargs):  # pragma: no cover - deve nunca rodar
        raise AssertionError("the catalog must not touch the network")

    monkeypatch.setattr(httpx.Client, "request", explode)
    monkeypatch.setattr(httpx.Client, "stream", explode)
    assert artifact_for(ComponentId.OLLAMA) is not None
