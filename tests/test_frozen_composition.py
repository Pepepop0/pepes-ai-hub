r"""The Hub composes from a frozen bundle exactly as it does from a checkout.

The first packaged build opened, drew the Dashboard and died: the Installation
section composed a plan executor, ``default_install_actions()`` defaulted its
data root to the *workspace* -- the executable's folder when frozen -- and
looked for ``manifests\workflows.yaml`` beside ``PepitosAIHub.exe``, where
PyInstaller never puts it. ``--smoke`` had read every resource through
``resource_root()`` and never called the composition the TUI runs.

These tests build the one-directory layout PyInstaller produces, with the real
shipped resources copied into ``_internal`` and nothing beside the executable,
and drive the same composition the running Hub drives. All but the first fail
on the previous implementation with "workflow manifest not found" under the
app folder -- the traceback the packaged build printed.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from local_ai_stack.app import default_executor_factory, project_root, workspace_root
from local_ai_stack.cli import smoke_report
from local_ai_stack.hub_application import create_hub_application
from local_ai_stack.installation import InstallLocation, resolve_installation
from local_ai_stack.integration import default_install_actions, provision_context
from local_ai_stack.layout import ENV_ROOT
from local_ai_stack.planning import build_install_plan
from local_ai_stack.resources import ENV_RESOURCE_ROOT, RESOURCE_DIRECTORIES, resource_root
from local_ai_stack.stack_application import StackApplication

CHECKOUT = Path(__file__).resolve().parents[1]
PROFILES = {"capella", "pepe", "test"}


@pytest.fixture
def frozen_build(monkeypatch, tmp_path):
    r"""``X:\dist\PepitosAIHub\PepitosAIHub.exe`` with its data in ``_internal``.

    The working directory is moved somewhere unrelated and the default install
    root pointed at a scratch folder, so nothing can be found by accident.
    """
    app = tmp_path / "dist" / "PepitosAIHub"
    bundle = app / "_internal"
    for name in RESOURCE_DIRECTORIES:
        shutil.copytree(CHECKOUT / name, bundle / name)
    exe = app / "PepitosAIHub.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.delenv(ENV_RESOURCE_ROOT, raising=False)
    monkeypatch.setenv(ENV_ROOT, str(tmp_path / "default-root"))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return app, bundle


def test_the_two_roots_are_distinct_when_frozen(frozen_build):
    app, bundle = frozen_build
    assert project_root() == resource_root() == bundle
    assert workspace_root() == app
    assert not (app / "manifests").exists(), "nothing but the exe lives in the app folder"


def test_install_actions_read_their_data_from_the_bundle(frozen_build):
    app, bundle = frozen_build
    installation = resolve_installation(InstallLocation.custom(app.parent / "root"))

    actions = default_install_actions(provision_context(installation))

    assert actions.repo_root == bundle
    assert set(actions.profiles) == PROFILES
    assert "flux2_klein_t2i" in actions.workflow_specs


def test_the_installation_plan_composes_the_way_the_tui_does(frozen_build):
    """The exact path that crashed: the plan panel's executor, then a resolve."""
    app, _ = frozen_build
    installation = resolve_installation(InstallLocation.custom(app.parent / "root"))

    executor = default_executor_factory(installation)
    actions = default_install_actions(provision_context(installation))
    for profile in actions.profiles.values():
        steps = executor.resolve(build_install_plan(profile, installation=installation))
        assert steps, profile.id


def test_the_stack_application_composes_without_a_checkout(frozen_build):
    app, bundle = frozen_build
    installation = resolve_installation(InstallLocation.custom(app.parent / "root"))

    application = StackApplication(installation)

    assert application.repo_root == bundle
    assert set(application.profiles) == PROFILES
    handlers = application.install_actions(application.profile("test"))
    assert handlers.repo_root == bundle


def test_the_hub_keeps_a_portable_stack_beside_the_executable(frozen_build):
    app, bundle = frozen_build
    hub = create_hub_application(project_root())
    try:
        portable = hub.resolve(InstallLocation.portable()).root
        default = hub.resolve(InstallLocation.default()).root
    finally:
        hub.close()
    assert portable == app / "stack"
    assert bundle not in portable.parents and bundle not in default.parents
    assert {profile.id for profile in hub.profiles} == PROFILES


def test_the_smoke_exercises_the_composition_the_tui_needs(frozen_build):
    app, bundle = frozen_build
    report = smoke_report()
    assert report["ok"], report["problems"]
    assert report["frozen"] is True
    composed = report["composition"]
    assert composed["resource_root"] == str(bundle)
    assert composed["portable_root"] == str(app / "stack")
    assert set(composed["plans"]) == PROFILES
    assert "flux2_klein_t2i" in composed["workflow_specs"]


def test_the_image_chat_filter_is_read_from_the_bundle(frozen_build):
    from local_ai_stack.open_webui_filters import filter_path, filter_source

    _, bundle = frozen_build
    assert filter_path() == bundle / "assets" / "open-webui" / "pepito_image_chat.py"
    assert "class Filter" in filter_source()
    report = smoke_report()
    assert report["ok"], report["problems"]
    assert report["image_filter"]["path"] == str(filter_path())


def test_the_smoke_names_a_missing_workflow_manifest(frozen_build):
    _, bundle = frozen_build
    (bundle / "manifests" / "workflows.yaml").unlink()
    report = smoke_report()
    assert not report["ok"]
    assert any(problem.startswith("workflows:") for problem in report["problems"])
    assert any(problem.startswith("composition:") for problem in report["problems"])
    assert not any("Traceback" in problem for problem in report["problems"])
