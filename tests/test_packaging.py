"""The Windows build: resources, state paths, and the interpreter a frozen Hub uses.

Nothing here runs PyInstaller. A frozen process is simulated by setting the
two attributes PyInstaller sets (``sys.frozen`` and ``sys._MEIPASS``), which
is exactly what every branch under test keys on. The managed Python runtime
is exercised with a controlled archive placed where the downloader looks, so
no byte leaves the machine. The one test that runs the real executable skips
itself when no build is present.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from test_open_webui import FakeRunner, context_for, pinned

from local_ai_stack import PRODUCT_NAME, __version__, display_version, product_title
from local_ai_stack.artifacts import Artifact
from local_ai_stack.catalog import artifact_for, manifest_dir
from local_ai_stack.cli import main, smoke_report
from local_ai_stack.comfyui import provisioner_from_pin
from local_ai_stack.installation import (
    InstallLocation,
    detect_repo_root,
    resolve_install_root,
    resolve_installation,
)
from local_ai_stack.integration import default_provisioners
from local_ai_stack.markers import read_marker
from local_ai_stack.provisioning import FailureReason, ProvisionContext, ProvisionOutcome
from local_ai_stack.pyenv import (
    create_environment,
    find_interpreter,
    running_interpreter_is_usable,
)
from local_ai_stack.python_runtime import (
    ARTIFACT_ID,
    COMPONENT_ID,
    ManagedPythonRuntime,
    managed_python_runtime,
    resolve_interpreter,
)
from local_ai_stack.resources import (
    ENV_RESOURCE_ROOT,
    RESOURCE_DIRECTORIES,
    bundle_directory,
    checkout_root,
    is_frozen,
    missing_resources,
    resource_root,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BUILT_EXE = PROJECT_ROOT / "dist" / "PepitosAIHub" / "PepitosAIHub.exe"


@pytest.fixture
def frozen(monkeypatch, tmp_path):
    """Make this process look like a PyInstaller build rooted at ``tmp_path``.

    ``app/PepitosAIHub.exe`` is the executable and ``app/_internal`` the bundle,
    which is the one-directory layout the spec produces.
    """
    app = tmp_path / "app"
    bundle = app / "_internal"
    bundle.mkdir(parents=True)
    exe = app / "PepitosAIHub.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.delenv(ENV_RESOURCE_ROOT, raising=False)
    return app, bundle


# --- resources ------------------------------------------------------------------------


def test_a_checkout_finds_its_resources_beside_src():
    assert not is_frozen()
    assert resource_root() == checkout_root() == PROJECT_ROOT
    assert missing_resources() == ()
    assert manifest_dir() == PROJECT_ROOT / "manifests"


def test_a_frozen_build_reads_resources_from_its_bundle(frozen):
    _, bundle = frozen
    assert is_frozen()
    assert bundle_directory() == bundle
    assert resource_root() == bundle
    # Nothing was bundled in this fake, and that is reported, not hidden.
    assert missing_resources() == RESOURCE_DIRECTORIES
    assert manifest_dir() is None


def test_the_resource_root_can_be_overridden_for_a_build(frozen, monkeypatch):
    monkeypatch.setenv(ENV_RESOURCE_ROOT, str(PROJECT_ROOT))
    assert resource_root() == PROJECT_ROOT
    assert missing_resources() == ()


def test_resource_lookup_never_depends_on_the_working_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert resource_root() == PROJECT_ROOT
    assert artifact_for("ollama") is not None


# --- state paths -----------------------------------------------------------------------


def test_default_state_lives_under_local_appdata_not_the_bundle(frozen):
    _, bundle = frozen
    resolved = resolve_installation(InstallLocation.default())
    assert bundle not in resolved.layout.state.parents
    assert resolved.layout.state.name == "state"
    assert "PepitosAIHub" in str(resolved.layout.state)


def test_portable_means_beside_the_executable_never_inside_internal(frozen):
    app, bundle = frozen
    assert detect_repo_root() == app
    portable = resolve_install_root(InstallLocation.portable())
    assert portable == app / "stack"
    assert bundle not in portable.parents and portable != bundle


def test_custom_roots_are_untouched_by_freezing(frozen, tmp_path):
    custom = tmp_path / "elsewhere"
    assert resolve_install_root(InstallLocation.custom(custom)) == custom


# --- the interpreter a frozen Hub may use ---------------------------------------------


class NoHostPython(FakeRunner):
    def which(self, name, extra_env=None):
        return None


def test_a_frozen_process_never_offers_itself_as_an_interpreter(frozen):
    assert not running_interpreter_is_usable()
    assert find_interpreter(NoHostPython()) is None
    # Even a host Python on PATH is preferred over the executable.
    found = find_interpreter(FakeRunner())
    assert found is None or found[0] != sys.executable


def test_a_checkout_still_prefers_its_own_interpreter():
    assert running_interpreter_is_usable()
    assert find_interpreter(NoHostPython()) == (sys.executable,)


def test_the_pinned_python_artifact_is_complete():
    artifact = artifact_for(COMPONENT_ID)
    assert artifact is not None
    assert artifact.id == ARTIFACT_ID
    assert artifact.version.startswith("3.11.")
    assert artifact.sha256 and len(artifact.sha256) == 64
    assert artifact.size_bytes > 0
    assert artifact.filename.endswith("install_only.tar.gz")


def test_only_a_frozen_process_selects_the_managed_runtime(frozen, monkeypatch):
    artifact = artifact_for(COMPONENT_ID)
    assert isinstance(managed_python_runtime(artifact), ManagedPythonRuntime)
    monkeypatch.setattr(sys, "frozen", False)
    assert managed_python_runtime(artifact) is None
    assert managed_python_runtime(None) is None


# --- provisioning the managed runtime from a controlled archive ------------------------


def controlled_archive(directory: Path, *, version: str = "3.11.16") -> Artifact:
    """A tiny ``install_only``-shaped tarball with a stub python.exe, no network."""
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as bundle:

        def add(name: str, data: bytes) -> None:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))

        add("python/python.exe", b"stub interpreter")
        add("python/Lib/venv/__init__.py", b"")
        add("python/Lib/ensurepip/__init__.py", b"")
    blob = payload.getvalue()
    filename = f"cpython-{version}-controlled-install_only.tar.gz"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_bytes(blob)
    return Artifact(
        id=ARTIFACT_ID,
        url="https://example.invalid/never-fetched.tar.gz",
        filename=filename,
        sha256=hashlib.sha256(blob).hexdigest(),
        size_bytes=len(blob),
        version=version,
        component=COMPONENT_ID,
    )


def runtime_in(tmp_path: Path, **kwargs) -> tuple[ManagedPythonRuntime, ProvisionContext]:
    context = context_for(tmp_path, NoHostPython())
    artifact = controlled_archive(context.layout.downloads, **kwargs)
    return ManagedPythonRuntime(artifact), context


def test_the_runtime_is_provisioned_into_the_install_root_with_a_marker(tmp_path):
    runtime, context = runtime_in(tmp_path)
    seen = []
    context.events = seen.append
    assert not runtime.is_ready(context.layout)

    resolution = runtime.ensure(context, "open-webui")

    python = context.layout.runtime / "python" / "python.exe"
    assert resolution.interpreter == (str(python),)
    assert python.read_bytes() == b"stub interpreter"
    assert (context.layout.runtime / "python" / "Lib" / "venv").is_dir()
    marker = read_marker(context.layout.runtime / "python", component=COMPONENT_ID)
    assert marker is not None
    assert marker.source_id == runtime.artifact.id
    assert marker.version == "3.11.16"
    assert marker.checksum == runtime.artifact.sha256
    assert runtime.is_ready(context.layout)
    assert not (context.layout.cache / f"{COMPONENT_ID}-{ARTIFACT_ID}").exists()
    assert any(e.component_id == "open-webui" and "Python 3.11.16" in e.message for e in seen)


def test_a_ready_runtime_is_reused_without_touching_the_archive(tmp_path):
    runtime, context = runtime_in(tmp_path)
    runtime.ensure(context, "comfyui")
    archive = context.layout.downloads / runtime.artifact.filename
    archive.unlink()
    again = runtime.ensure(context, "comfyui")
    assert again.interpreter == (str(runtime.interpreter(context.layout)),)


def test_a_tampered_archive_is_refused_as_a_checksum_failure(tmp_path):
    runtime, context = runtime_in(tmp_path)
    archive = context.layout.downloads / runtime.artifact.filename
    archive.write_bytes(b"not the pinned bytes")
    resolution = runtime.ensure(context, "comfyui")
    # A stale local file is redownloaded, which this test has no network for:
    # either way the interpreter is not handed over.
    assert resolution.interpreter is None
    assert resolution.reason in {FailureReason.CHECKSUM, FailureReason.NETWORK}
    assert not runtime.is_ready(context.layout)


def test_resolution_refuses_a_runtime_outside_the_component_range(tmp_path):
    runtime, context = runtime_in(tmp_path)
    resolution = resolve_interpreter(
        context, "comfyui", minimum=(3, 12), below=(3, 13), runtime=runtime
    )
    assert resolution.interpreter is None
    assert resolution.reason is FailureReason.PRECONDITION
    assert "3.11.16" in resolution.detail


def test_a_dry_run_plans_the_managed_interpreter_without_provisioning_it(tmp_path):
    runtime, context = runtime_in(tmp_path)
    resolution = resolve_interpreter(
        context, "comfyui", minimum=(3, 11), below=(3, 13), runtime=runtime, dry_run=True
    )
    assert resolution.interpreter == (str(runtime.interpreter(context.layout)),)
    assert not runtime.is_ready(context.layout)


def test_without_a_runtime_the_host_is_searched_as_before(tmp_path):
    context = context_for(tmp_path, NoHostPython())
    resolution = resolve_interpreter(
        context, "comfyui", minimum=(3, 99), below=(4, 0), runtime=None
    )
    assert resolution.interpreter is None
    assert resolution.reason is FailureReason.PRECONDITION


def test_the_venv_command_targets_the_managed_python_not_the_executable(frozen, tmp_path):
    runtime, context = runtime_in(tmp_path)
    resolution = runtime.ensure(context, "open-webui")
    environment_root = context.layout.apps / "open-webui" / ".venv"
    from local_ai_stack.pyenv import PythonEnvironment

    create_environment(context.runner, PythonEnvironment(environment_root), resolution.interpreter)
    venv_calls = [call for call in context.runner.calls if "venv" in call]
    assert len(venv_calls) == 1
    argv = venv_calls[0]
    assert argv[0] == str(runtime.interpreter(context.layout))
    assert argv[0] != sys.executable
    assert "PepitosAIHub" not in argv[0]
    assert argv[1:3] == ("-m", "venv")


def test_open_webui_installs_from_the_managed_runtime_end_to_end(frozen, tmp_path):
    runtime, context = runtime_in(tmp_path)
    result = pinned(python_runtime=runtime).apply(context)
    assert result.outcome is ProvisionOutcome.INSTALLED, result.failure
    venv_calls = [call for call in context.runner.calls if "venv" in call]
    assert venv_calls and venv_calls[0][0] == str(runtime.interpreter(context.layout))
    assert all(sys.executable not in call for call in context.runner.calls)


def test_comfyui_receives_the_same_runtime_through_its_factory(tmp_path):
    runtime, _ = runtime_in(tmp_path)
    provisioner = provisioner_from_pin(None, python_runtime=runtime)
    assert provisioner.python_runtime is runtime


def test_default_provisioners_share_one_managed_runtime_only_when_frozen(frozen):
    provisioners = default_provisioners()
    comfy = provisioners["comfyui"].python_runtime
    webui = provisioners["open-webui"].python_runtime
    assert isinstance(comfy, ManagedPythonRuntime)
    assert comfy is webui
    assert comfy.artifact.id == ARTIFACT_ID


def test_default_provisioners_use_the_host_python_on_a_checkout():
    provisioners = default_provisioners()
    assert provisioners["comfyui"].python_runtime is None
    assert provisioners["open-webui"].python_runtime is None


# --- version and entrypoint ----------------------------------------------------------------


def test_one_version_string_feeds_the_footer_and_the_metadata():
    assert __version__ == "0.1.0"
    assert display_version() == "v0.1"
    assert product_title() == f"{PRODUCT_NAME}  v0.1"
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'version = "{__version__}"' in pyproject


def test_the_launcher_and_the_console_script_run_the_same_main():
    launcher = (PROJECT_ROOT / "packaging" / "launch.py").read_text(encoding="utf-8")
    assert "from local_ai_stack.cli import main" in launcher
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'local-ai-stack = "local_ai_stack.cli:main"' in pyproject


def test_the_spec_bundles_every_resource_directory_and_keeps_the_console():
    spec = (PROJECT_ROOT / "packaging" / "pepitos_ai_hub.spec").read_text(encoding="utf-8")
    assert "RESOURCE_DIRECTORIES" in spec
    assert "console=True" in spec
    assert "COLLECT(" in spec, "one-directory build"
    assert "onefile" not in spec.lower().replace("one-file", "")


def test_the_smoke_report_from_a_checkout_is_clean():
    report = smoke_report()
    assert report["ok"], report["problems"]
    assert report["frozen"] is False
    assert report["profiles"] == ["capella", "pepe", "pepe-8gb", "test"]
    assert "python-windows-amd64" in report["manifests"]["artifacts"]
    assert report["workflows"]["embedded"] == ["flux2_klein_edit", "flux2_klein_t2i"]
    assert report["python_runtime"]["managed_runtime_selected"] is False
    assert report["folder_picker"] in {"WindowsFolderPicker", "NullFolderPicker"}


def test_the_smoke_report_flags_a_frozen_build_with_no_bundled_resources(frozen):
    report = smoke_report()
    assert not report["ok"]
    assert any(problem.startswith("resources:") for problem in report["problems"])
    assert report["python_runtime"]["managed_runtime_selected"] is True
    assert report["python_runtime"]["running_interpreter_usable"] is False


def test_smoke_flag_exits_nonzero_when_problems_exist(frozen, capsys):
    code = main(["--smoke"])
    assert code == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_version_flag_reports_the_product_and_version(capsys):
    with pytest.raises(SystemExit) as stop:
        main(["--version"])
    assert stop.value.code == 0
    assert capsys.readouterr().out.strip() == f"{PRODUCT_NAME} {__version__}"


# --- the real build, when one exists --------------------------------------------------------


@pytest.mark.skipif(not BUILT_EXE.exists(), reason="no dist/PepitosAIHub build present")
def test_the_built_executable_passes_its_own_smoke_check(tmp_path):
    completed = subprocess.run(
        [str(BUILT_EXE), "--smoke"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,  # never the checkout: resources must come from the bundle
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert "Traceback" not in completed.stderr
    report = json.loads(completed.stdout)
    assert report["ok"], report["problems"]
    assert report["frozen"] is True
    assert report["resource_root"].endswith("_internal")
    assert report["python_runtime"]["managed_runtime_selected"] is True
    assert report["python_runtime"]["running_interpreter_usable"] is False
    assert report["folder_picker"] == "WindowsFolderPicker"
    assert not report["state_path"].startswith(report["bundle_directory"])
    assert not report["portable_root"].startswith(report["bundle_directory"])
