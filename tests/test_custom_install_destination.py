"""Custom parent inputs resolve once, while installed roots remain exact on disk."""

from dataclasses import replace

import pytest
from test_app import reference_facts
from test_installation_switching import PROJECT_ROOT, RecordingBackend, World, hub

from local_ai_stack.desktop.api import DesktopController, DesktopError
from local_ai_stack.hub_application import create_hub_application
from local_ai_stack.installation import InstallLocation, InstallLocationMode, resolve_installation
from local_ai_stack.installations import read_state
from local_ai_stack.layout import ENV_ROOT
from local_ai_stack.stack_application import StackDependencies
from local_ai_stack.system import GPUInfo
from local_ai_stack.tui.application import InstallSelection


def capella_facts():
    return replace(reference_facts(), gpus=(GPUInfo("NVIDIA GeForce RTX 5090", 32607),))


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    monkeypatch.setattr(RecordingBackend, "facts", lambda self: capella_facts())
    world = World()
    app = hub(tmp_path, world)
    controller = DesktopController(app, folder_picker=object())
    yield controller, app, world
    controller.close()


@pytest.mark.parametrize("flag", [None, True, False])
def test_desktop_installs_and_persists_final_root_but_remembers_parent(desktop, tmp_path, flag):
    controller, app, world = desktop
    parent = tmp_path / "Desktop"
    args = {"profile_id": "capella", "mode": "custom", "custom_root": str(parent)}
    if flag is not None:
        args["create_subfolder"] = flag
    target = parent if flag is False else parent / "Capella"

    controller.command("install", args)
    app.wait(10)

    assert world.commands_on(target) == ["first_run"]
    assert app.active_root() == target
    assert app.backend.installation.layout.root == target
    assert app.config.installation == InstallLocation.custom(target)
    persisted = read_state(app.selection_path)
    assert persisted.active_root == target
    assert persisted.entry_for(target) is not None
    assert persisted.general.recent_custom_roots == (str(parent),)
    assert (target / "state").is_dir()
    if flag is not False:
        assert not (parent / "state").exists()

    # The recent entry is another parent input, not the resolved child fed back in.
    args["custom_root"] = app.recent_custom_roots()[0]
    controller.command("install", args)
    app.wait(10)
    assert app.active_root() == target
    assert not (target / "Capella").exists()
    assert app.recent_custom_roots() == (str(parent),)


@pytest.mark.parametrize("flag", [True, False])
def test_estimate_and_real_dry_run_plan_use_same_destination_without_creating_it(
    tmp_path, monkeypatch, flag
):
    from local_ai_stack import hub_application

    def resolve(location):
        return resolve_installation(
            location, repo_root=tmp_path / "repo", env={ENV_ROOT: str(tmp_path / "default")}
        )

    app = create_hub_application(
        PROJECT_ROOT,
        resolve=resolve,
        dependencies=StackDependencies(detect_facts=reference_facts),
    )
    controller = DesktopController(app, folder_picker=object())
    checked = []
    real_free_space = hub_application.free_space

    def observe_free_space(root):
        checked.append(root)
        return real_free_space(root)

    monkeypatch.setattr(hub_application, "free_space", observe_free_space)
    parent = tmp_path / "not-created" / "Desktop"
    target = parent / "Capella" if flag else parent
    args = {
        "profile_id": "capella",
        "mode": "custom",
        "custom_root": str(parent),
        "create_subfolder": flag,
    }
    try:
        estimate = controller.command("estimate", args)
        review = controller.command("plan", args)
        assert checked == [target]
        assert estimate["known_bytes"] > 0
        assert review["dry_run"] is True
        assert review["plan"]["install_root"] == str(target)
        assert any(step["target"] == str(target) for step in review["plan"]["steps"])
        assert not parent.parent.exists()
        assert not app.selection_path.exists()
        assert app.active_root() is None
    finally:
        controller.close()


def test_conflict_preflight_and_resolution_stay_bound_to_child(desktop, tmp_path):
    controller, app, world = desktop
    parent = tmp_path / "Desktop"
    child = parent / "Capella"
    world.conflicting.add(child)
    with pytest.raises(DesktopError) as error:
        controller.command(
            "install", {"profile_id": "capella", "mode": "custom", "custom_root": str(parent)}
        )
    assert error.value.code == "conflict_required"
    assert not parent.exists()
    assert not app.recent_custom_roots()
    controller.command(
        "resolve_conflicts",
        {"preflight_id": error.value.details["preflight_id"], "policy": "abort"},
    )
    assert world.backends[child].commands == ["resolve:abort"]
    assert app.active_root() is None
    assert not parent.exists()


@pytest.mark.parametrize("command", ["estimate", "plan", "install"])
@pytest.mark.parametrize("value", [None, "true", "false", 0, 1, [], {}])
def test_create_subfolder_requires_json_boolean(desktop, tmp_path, command, value):
    controller, app, world = desktop
    parent = tmp_path / "Desktop"
    with pytest.raises(DesktopError) as error:
        controller.command(
            command,
            {
                "profile_id": "capella",
                "mode": "custom",
                "custom_root": str(parent),
                "create_subfolder": value,
            },
        )
    assert error.value.code == "invalid_request"
    assert not parent.exists()
    assert world.calls == []
    assert app.active_root() is None


def test_domain_custom_selection_remains_exact_without_opt_in(tmp_path):
    selection = InstallSelection("capella", InstallLocationMode.CUSTOM, str(tmp_path))
    assert selection.location("Capella") == InstallLocation.custom(tmp_path)
    assert not (tmp_path / "Capella").exists()


@pytest.mark.parametrize("mode", [InstallLocationMode.DEFAULT, InstallLocationMode.PORTABLE])
def test_subfolder_option_does_not_change_non_custom_modes(mode):
    assert InstallSelection("capella", mode, create_subfolder=True).location("Capella") == (
        InstallLocation(mode)
    )


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".",
        "..",
        "../outside",
        "nested/Capella",
        "nested\\Capella",
        "C:\\outside",
        ".. ",
        "Capella.",
    ],
)
def test_profile_child_cannot_escape_or_split_the_selected_parent(tmp_path, name):
    selection = InstallSelection("capella", InstallLocationMode.CUSTOM, str(tmp_path), True)
    with pytest.raises(ValueError):
        selection.location(name)
