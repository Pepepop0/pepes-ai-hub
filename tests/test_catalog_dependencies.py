"""Validation of the dependency pin recorded in the repository manifest.

The point of this schema is that a floating version cannot get in by accident,
so most of these tests are about what the manifest refuses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from local_ai_stack import catalog as catalog_module
from local_ai_stack.catalog import CatalogError, PinnedDependencies, repository_for

BASE = """
schema_version: 1
repositories:
  comfyui:
    url: https://github.com/comfyanonymous/ComfyUI.git
    revision: v0.34.0
{extra}
"""


def write_manifest(tmp_path: Path, extra: str) -> Path:
    directory = tmp_path / "manifests"
    directory.mkdir(exist_ok=True)
    (directory / "repositories.yaml").write_text(BASE.format(extra=extra), encoding="utf-8")
    return directory


def load(tmp_path: Path, extra: str, monkeypatch):
    directory = write_manifest(tmp_path, extra)
    monkeypatch.setattr(catalog_module, "manifest_dir", lambda: directory)
    catalog_module.clear_cache()
    return catalog_module.repository_catalog()


@pytest.fixture(autouse=True)
def _clear_cache():
    catalog_module.clear_cache()
    yield
    catalog_module.clear_cache()


# --- o pin real ------------------------------------------------------------


def test_the_shipped_pin_parses_into_dependencies():
    pin = repository_for("comfyui")
    assert pin is not None
    assert isinstance(pin.dependencies, PinnedDependencies)
    assert not pin.dependencies.is_empty


def test_the_shipped_pin_declares_what_to_verify():
    dependencies = repository_for("comfyui").dependencies
    assert dependencies.verify_distribution == "torch"
    assert dependencies.verify_version == "2.11.0"


def test_a_repository_without_dependencies_is_still_valid(tmp_path, monkeypatch):
    catalog = load(tmp_path, "    commit: " + "a" * 40, monkeypatch)
    assert catalog["comfyui"].dependencies is None


# --- o que o schema recusa -------------------------------------------------


@pytest.mark.parametrize(
    "requirement",
    ["torch", "torch>=2.11.0", "torch~=2.11", "torch<3"],
)
def test_a_floating_requirement_is_rejected(tmp_path, monkeypatch, requirement):
    extra = f"    dependencies:\n      requirements:\n        - {requirement}\n"
    with pytest.raises(CatalogError, match="exact version"):
        load(tmp_path, extra, monkeypatch)


def test_an_exact_requirement_is_accepted(tmp_path, monkeypatch):
    extra = "    dependencies:\n      requirements:\n        - torch==2.11.0+cu130\n"
    catalog = load(tmp_path, extra, monkeypatch)
    assert catalog["comfyui"].dependencies.requirements == ("torch==2.11.0+cu130",)


@pytest.mark.parametrize("key", ["index_url", "extra_index_url"])
def test_a_non_https_index_is_rejected(tmp_path, monkeypatch, key):
    extra = (
        "    dependencies:\n"
        "      requirements:\n        - torch==2.11.0\n"
        f"      {key}: http://insecure.invalid/whl\n"
    )
    with pytest.raises(CatalogError, match="https"):
        load(tmp_path, extra, monkeypatch)


def test_unknown_dependency_fields_are_rejected(tmp_path, monkeypatch):
    extra = "    dependencies:\n      mystery: 1\n"
    with pytest.raises(CatalogError, match="mystery"):
        load(tmp_path, extra, monkeypatch)


def test_requirements_must_be_a_list(tmp_path, monkeypatch):
    extra = "    dependencies:\n      requirements: torch==2.11.0\n"
    with pytest.raises(CatalogError, match="must be a list"):
        load(tmp_path, extra, monkeypatch)


def test_use_repository_requirements_must_be_a_boolean(tmp_path, monkeypatch):
    extra = "    dependencies:\n      use_repository_requirements: yes please\n"
    with pytest.raises(CatalogError, match="boolean"):
        load(tmp_path, extra, monkeypatch)


# --- faixa de interpretador ------------------------------------------------


def test_the_python_range_is_read_from_the_manifest(tmp_path, monkeypatch):
    extra = '    python:\n      minimum: "3.12"\n      below: "3.14"\n'
    catalog = load(tmp_path, extra, monkeypatch)
    assert catalog["comfyui"].python_minimum == (3, 12)
    assert catalog["comfyui"].python_below == (3, 14)


def test_an_inverted_python_range_is_rejected(tmp_path, monkeypatch):
    extra = '    python:\n      minimum: "3.13"\n      below: "3.11"\n'
    with pytest.raises(CatalogError, match="above minimum"):
        load(tmp_path, extra, monkeypatch)


def test_a_malformed_python_version_is_rejected(tmp_path, monkeypatch):
    extra = '    python:\n      minimum: "three.eleven"\n'
    with pytest.raises(CatalogError, match="numeric"):
        load(tmp_path, extra, monkeypatch)


def test_the_default_range_applies_when_unstated(tmp_path, monkeypatch):
    catalog = load(tmp_path, "    commit: " + "b" * 40, monkeypatch)
    assert catalog["comfyui"].python_minimum == (3, 11)
    assert catalog["comfyui"].python_below == (3, 13)
