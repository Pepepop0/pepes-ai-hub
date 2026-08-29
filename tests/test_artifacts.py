"""Tests for the artifact manifest contract. Nothing is downloaded here."""

from __future__ import annotations

import pytest

from local_ai_stack.artifacts import (
    ARTIFACT_SCHEMA_VERSION,
    Artifact,
    ArtifactError,
    load_artifact_manifest,
    validate_artifact_manifest,
)

SHA = "a" * 64

MANIFEST = f"""
schema_version: 1
artifacts:
  - id: ollama-windows
    url: https://example.invalid/ollama-windows-amd64.zip
    filename: ollama-windows-amd64.zip
    sha256: {SHA}
    size_bytes: 1234
    version: "0.5.0"
  - id: helper-tool
    url: https://example.invalid/helper.zip
    filename: helper.zip
"""


def artifact(**overrides):
    data = {
        "id": "ollama-windows",
        "url": "https://example.invalid/o.zip",
        "filename": "o.zip",
    }
    data.update(overrides)
    return Artifact.from_dict(data)


# --- forma minima ----------------------------------------------------------


def test_minimal_artifact_needs_only_id_url_and_filename():
    a = artifact()
    assert a.id == "ollama-windows"
    assert a.filename == "o.zip"
    assert a.sha256 is None
    assert a.has_checksum is False
    assert a.size_bytes is None
    assert a.version is None


def test_full_artifact_round_trips():
    a = artifact(sha256=SHA, size_bytes=10, version="0.5.0")
    assert Artifact.from_dict(a.to_dict()) == a
    assert a.has_checksum is True


def test_destination_is_chosen_by_the_caller(tmp_path):
    assert artifact().destination_in(tmp_path) == tmp_path / "o.zip"


def test_artifact_is_immutable():
    with pytest.raises(AttributeError):
        artifact().url = "https://elsewhere.invalid/x.zip"


# --- validacao -------------------------------------------------------------


@pytest.mark.parametrize("missing", ["id", "url", "filename"])
def test_required_fields(missing):
    data = {"id": "x", "url": "https://e.invalid/a.zip", "filename": "a.zip"}
    del data[missing]
    with pytest.raises(ArtifactError) as exc:
        Artifact.from_dict(data)
    assert missing in str(exc.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.invalid/a.zip",
        "ftp://example.invalid/a.zip",
        "file:///c:/a.zip",
        "example.invalid/a.zip",
    ],
)
def test_only_https_urls_are_accepted(url):
    with pytest.raises(ArtifactError) as exc:
        artifact(url=url)
    assert "url" in str(exc.value)


@pytest.mark.parametrize("filename", ["../escape.zip", "sub/dir.zip", "sub\\dir.zip", ".", ".."])
def test_filename_must_be_a_bare_name(filename):
    with pytest.raises(ArtifactError):
        artifact(filename=filename)


@pytest.mark.parametrize("sha", ["abc", "z" * 64, "a" * 63, "a" * 65, 12345])
def test_sha256_must_be_64_hex_characters(sha):
    with pytest.raises(ArtifactError):
        artifact(sha256=sha)


def test_sha256_is_normalized_to_lowercase():
    assert artifact(sha256="AB" * 32).sha256 == "ab" * 32


@pytest.mark.parametrize("bad_id", ["Ollama", "1tool", "with space", "with/slash", ""])
def test_id_must_be_a_slug(bad_id):
    with pytest.raises(ArtifactError):
        artifact(id=bad_id)


@pytest.mark.parametrize("size", [-1, "big", True])
def test_size_must_be_a_non_negative_integer(size):
    with pytest.raises(ArtifactError):
        artifact(size_bytes=size)


def test_unknown_fields_are_rejected():
    with pytest.raises(ArtifactError) as exc:
        artifact(mirror="https://elsewhere.invalid")
    assert "mirror" in str(exc.value)


# --- manifest --------------------------------------------------------------


def test_manifest_loads_into_artifacts_by_id(tmp_path):
    path = tmp_path / "m.yaml"
    path.write_text(MANIFEST, encoding="utf-8")
    artifacts = load_artifact_manifest(path)
    assert set(artifacts) == {"ollama-windows", "helper-tool"}
    assert artifacts["ollama-windows"].sha256 == SHA
    assert artifacts["helper-tool"].sha256 is None


def test_manifest_rejects_a_wrong_schema_version():
    with pytest.raises(ArtifactError) as exc:
        validate_artifact_manifest({"schema_version": 99, "artifacts": []})
    assert str(ARTIFACT_SCHEMA_VERSION) in str(exc.value)


def test_manifest_rejects_duplicate_ids():
    entry = {"id": "dup", "url": "https://e.invalid/a.zip", "filename": "a.zip"}
    with pytest.raises(ArtifactError) as exc:
        validate_artifact_manifest({"schema_version": 1, "artifacts": [entry, dict(entry)]})
    assert "duplicate" in str(exc.value)


def test_manifest_rejects_an_empty_artifact_list():
    with pytest.raises(ArtifactError):
        validate_artifact_manifest({"schema_version": 1, "artifacts": []})


def test_manifest_reports_the_offending_index():
    entries = [
        {"id": "good", "url": "https://e.invalid/a.zip", "filename": "a.zip"},
        {"id": "bad", "url": "nope", "filename": "b.zip"},
    ]
    with pytest.raises(ArtifactError) as exc:
        validate_artifact_manifest({"schema_version": 1, "artifacts": entries}, "m.yaml")
    assert "[1]" in str(exc.value)


def test_missing_manifest_file_is_a_clear_error(tmp_path):
    with pytest.raises(ArtifactError):
        load_artifact_manifest(tmp_path / "absent.yaml")


def test_loading_a_manifest_downloads_nothing(tmp_path, monkeypatch):
    import httpx

    def explode(*args, **kwargs):  # pragma: no cover - deve nunca rodar
        raise AssertionError("loading a manifest must not touch the network")

    monkeypatch.setattr(httpx.Client, "request", explode)
    monkeypatch.setattr(httpx.Client, "stream", explode)
    path = tmp_path / "m.yaml"
    path.write_text(MANIFEST, encoding="utf-8")
    assert load_artifact_manifest(path)
    assert list(tmp_path.iterdir()) == [path]
