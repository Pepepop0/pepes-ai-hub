from __future__ import annotations

import copy
import io
import tarfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from local_ai_stack import catalog
from local_ai_stack.compatibility import evaluate_compatibility
from local_ai_stack.layout import StackLayout
from local_ai_stack.linux_bench import (
    BenchProfileError,
    bench_preflight,
    bench_profile,
    needs_civitai,
)
from local_ai_stack.model_resolver import profile_model_specs, unresolved_roles
from local_ai_stack.models import FileModelSource
from local_ai_stack.profiles import (
    ProfileError,
    declared_workflows,
    load_profile,
    validate_profile_data,
)
from local_ai_stack.system import GPUInfo, NvidiaFacts, SystemFacts
from local_ai_stack.workflows import load_workflow_manifest

ROOT = Path(__file__).resolve().parents[1]


def facts(**changes):
    base = SystemFacts(
        "Linux",
        "6.8",
        "3.11.16",
        True,
        True,
        (GPUInfo("NVIDIA GeForce RTX 5090", 32607),),
        machine="x86_64",
        nvidia=NvidiaFacts(
            smi_available=True, query_ok=True, cuda_version_supported_by_driver="13.0"
        ),
    )
    return replace(base, **changes)


def test_capella_supports_linux_and_windows_without_relaxing_vram():
    profile = load_profile(ROOT / "profiles/capella.yaml")
    assert evaluate_compatibility(facts(), profile).compatible
    assert evaluate_compatibility(facts(os_name="Windows"), profile).compatible
    assert not evaluate_compatibility(facts(os_name="Darwin"), profile).compatible
    assert not evaluate_compatibility(facts(gpus=(GPUInfo("RTX 4090", 24564),)), profile).compatible


@pytest.mark.parametrize("profile_id", ["pepe", "pepe-8gb"])
def test_pepe_profiles_support_linux_and_windows_without_relaxing_vram(profile_id):
    profile = load_profile(ROOT / f"profiles/{profile_id}.yaml")
    assert profile.hardware.os == ("windows", "linux")
    assert evaluate_compatibility(facts(), profile).compatible
    assert evaluate_compatibility(facts(os_name="Windows"), profile).compatible
    assert not evaluate_compatibility(facts(os_name="Darwin"), profile).compatible
    small = facts(gpus=(GPUInfo("NVIDIA GeForce GTX 1650", 4096),))
    assert not evaluate_compatibility(small, profile).compatible


@pytest.mark.parametrize("profile_id", ["capella", "pepe", "pepe-8gb"])
def test_linux_bench_profiles_have_a_complete_linux_plan(monkeypatch, profile_id):
    """Every artifact, model and workflow a bench profile needs exists for Linux x86-64."""
    monkeypatch.setattr(catalog.platform, "system", lambda: "Linux")
    monkeypatch.setattr(catalog.platform, "machine", lambda: "x86_64")
    profile = bench_profile(profile_id)
    assert "linux" in profile.hardware.os

    # Platform-specific binaries: the Linux variant, never a Windows one.
    ollama = catalog.artifact_for("ollama")
    assert ollama is not None and ollama.platform == "linux" and ollama.has_checksum
    assert catalog.repository_for("comfyui") is not None
    assert catalog.package_for("open-webui") is not None

    # Models: every declared role resolves to a pinned, checksummed source for this OS.
    assert not unresolved_roles(profile)
    specs = profile_model_specs(profile)
    assert specs
    for spec in specs:
        if isinstance(spec.source, FileModelSource):
            artifact = spec.source.artifact
            assert artifact.platform in {None, "linux"}, spec.id
            assert artifact.has_checksum, spec.id

    # Workflows: manifest entry, graph file and every model role the graph loads.
    manifest = load_workflow_manifest(ROOT / "manifests/workflows.yaml")
    roles = {requirement.role for requirement in profile.model_requirements}
    declared = [
        workflow
        for _key, _field, workflow in declared_workflows(profile)
        if workflow.manifest_id is not None
    ]
    assert declared
    for workflow in declared:
        spec = manifest[workflow.manifest_id]
        assert (ROOT / spec.file).is_file(), spec.file
        assert set(spec.required_roles) <= roles, spec.id


@pytest.mark.parametrize("profile_id", ["capella", "pepe", "pepe-8gb"])
def test_bench_preflight_accepts_every_linux_profile(monkeypatch, tmp_path, profile_id):
    monkeypatch.setattr(
        "local_ai_stack.linux_bench.psutil.virtual_memory",
        lambda: SimpleNamespace(total=64 * 1024**3),
    )
    report = bench_preflight(StackLayout.from_root(tmp_path / "data"), facts(), profile_id)
    assert report["ok"] is True and report["profile"] == profile_id


def test_bench_defaults_to_capella_and_refuses_unknown_profiles(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "local_ai_stack.linux_bench.psutil.virtual_memory",
        lambda: SimpleNamespace(total=64 * 1024**3),
    )
    assert (
        bench_preflight(StackLayout.from_root(tmp_path / "data"), facts())["profile"] == "capella"
    )
    with pytest.raises(BenchProfileError, match="nope"):
        bench_profile("nope")


def test_only_profiles_with_civitai_models_need_the_token():
    assert not needs_civitai(bench_profile("pepe"))
    assert not needs_civitai(bench_profile("pepe-8gb"))


@pytest.mark.parametrize("value", [[], ["linux", "linux"], ["windows", "darwin"], [None]])
def test_invalid_platform_sets_are_rejected(value):
    raw = copy.deepcopy(load_profile(ROOT / "profiles/capella.yaml").raw)
    raw["hardware"]["os"] = value
    with pytest.raises(ProfileError):
        validate_profile_data(raw)


@pytest.mark.parametrize(
    "host,filename",
    [
        ("Linux", "ollama-linux-amd64.tar.zst"),
        ("Windows", "ollama-windows-amd64.zip"),
    ],
)
def test_platform_lookup_never_substitutes_a_foreign_binary(monkeypatch, host, filename):
    monkeypatch.setattr(catalog.platform, "system", lambda: host)
    monkeypatch.setattr(catalog.platform, "machine", lambda: "x86_64")
    artifact = catalog.artifact_for("ollama")
    assert artifact.filename == filename and artifact.platform == host.lower()
    assert artifact.has_checksum
    monkeypatch.setattr(catalog.platform, "machine", lambda: "aarch64")
    assert catalog.artifact_for("ollama") is None


@pytest.mark.parametrize(
    "cuda,accepted", [("12.8", False), (None, False), ("13.0", True), ("13.4", True)]
)
def test_bench_refuses_driver_below_the_pinned_torch_cuda(monkeypatch, tmp_path, cuda, accepted):
    monkeypatch.setattr(
        "local_ai_stack.linux_bench.psutil.virtual_memory",
        lambda: SimpleNamespace(total=64 * 1024**3),
    )
    report = bench_preflight(
        StackLayout.from_root(tmp_path / "capella"),
        facts(nvidia=NvidiaFacts(cuda_version_supported_by_driver=cuda)),
    )
    assert report["ok"] is accepted
    assert not (tmp_path / "capella").exists()


def test_bench_rejects_missing_gpu_before_any_install_or_token_lookup(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "local_ai_stack.linux_bench.psutil.virtual_memory",
        lambda: SimpleNamespace(total=64 * 1024**3),
    )
    report = bench_preflight(
        StackLayout.from_root(tmp_path / "capella"), facts(gpus=(), nvidia_smi_available=False)
    )
    assert not report["ok"]
    assert not (tmp_path / "capella").exists()


def _archive(path, entries):
    zstandard = pytest.importorskip("zstandard")
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for entry, payload in entries:
            archive.addfile(entry, io.BytesIO(payload) if payload is not None else None)
    path.write_bytes(zstandard.ZstdCompressor().compress(stream.getvalue()))


def test_linux_archive_preserves_binary_mode_and_safe_library_links(tmp_path):
    from local_ai_stack.ollama import _extract_archive

    executable = tarfile.TarInfo("bin/ollama")
    executable.mode = 0o755
    executable.size = 4
    library = tarfile.TarInfo("lib/ollama/library.so.1")
    library.size = 3
    link = tarfile.TarInfo("lib/ollama/library.so")
    link.type = tarfile.SYMTYPE
    link.linkname = "library.so.1"
    archive = tmp_path / "ollama.tar.zst"
    _archive(archive, [(executable, b"test"), (library, b"lib"), (link, None)])
    destination = tmp_path / "unpacked"
    try:
        _extract_archive(archive, destination)
    except OSError as exc:
        if __import__("os").name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink permission unavailable; executed on Linux")
        raise
    assert (destination / "bin/ollama").read_bytes() == b"test"
    assert (destination / "lib/ollama/library.so").is_symlink()
    assert (destination / "lib/ollama/library.so").read_bytes() == b"lib"
    if __import__("os").name != "nt":
        assert (destination / "bin/ollama").stat().st_mode & 0o111


@pytest.mark.parametrize(
    "member_name,link_target",
    [("../escape", None), ("/escape", None), ("lib/link", "../../escape")],
)
def test_linux_archive_cannot_escape_owned_staging(tmp_path, member_name, link_target):
    from local_ai_stack.ollama import _extract_archive

    member = tarfile.TarInfo(member_name)
    if link_target:
        member.type = tarfile.SYMTYPE
        member.linkname = link_target
    else:
        member.size = 1
    archive = tmp_path / "bad.tar.zst"
    _archive(archive, [(member, None if link_target else b"x")])
    with pytest.raises((ValueError, tarfile.TarError)):
        _extract_archive(archive, tmp_path / "staging")
    assert not (tmp_path / "escape").exists()
