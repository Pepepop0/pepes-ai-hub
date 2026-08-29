"""Tests for the installed configuration.

Nothing here reads or writes a configuration file: persistence is deliberately
not automatic yet.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from local_ai_stack.config import (
    CONFIG_FILENAME,
    CONFIG_SCHEMA_VERSION,
    ConfigError,
    GeneralConfig,
    InstalledConfig,
    UiConfig,
    config_path,
    dumps,
    loads,
)
from local_ai_stack.installation import (
    InstallLocation,
    InstallLocationMode,
    resolve_installation,
)
from local_ai_stack.layout import StackLayout
from local_ai_stack.profiles import load_profile
from local_ai_stack.stacks import STANDARD_STACK

REPO = Path(__file__).resolve().parents[1]

SAMPLE = """
schema_version: 1

profile:
  id: pepe

stack:
  id: standard

installation:
  mode: portable
  root: null

general:
  ui:
    right_rail_visible: true
    right_rail_expanded: true
"""


# --- forma canonica --------------------------------------------------------


def test_sample_document_loads():
    config = loads(SAMPLE)
    assert config.schema_version == 1
    assert config.profile_id == "pepe"
    assert config.stack_id == "standard"
    assert config.installation.mode is InstallLocationMode.PORTABLE
    assert config.installation.root is None
    assert config.general.ui.right_rail_visible is True
    assert config.general.ui.right_rail_expanded is True


def test_serialized_shape_keeps_the_four_concepts_separate():
    data = InstalledConfig(profile_id="pepe").to_dict()
    assert set(data) == {"schema_version", "profile", "stack", "installation", "general"}
    assert data["profile"] == {"id": "pepe"}
    assert data["stack"] == {"id": "standard"}
    assert data["installation"]["mode"] == "default"


def test_round_trip_through_yaml(tmp_path):
    config = InstalledConfig(
        profile_id="capella",
        installation=InstallLocation.custom(tmp_path / "IA's" / "Pepito's"),
        general=GeneralConfig(ui=UiConfig(theme="dark", pet="capybara")),
    )
    assert loads(dumps(config)) == config


def test_dump_preserves_document_order():
    keys = list(yaml.safe_load(dumps(InstalledConfig(profile_id="pepe"))))
    assert keys == ["schema_version", "profile", "stack", "installation", "general"]


def test_defaults_are_default_mode_and_standard_stack():
    config = InstalledConfig(profile_id="test")
    assert config.stack_id == "standard"
    assert config.installation.mode is InstallLocationMode.DEFAULT
    assert config.general.ui.theme is None
    assert config.general.ui.pet is None


# --- validacao -------------------------------------------------------------


def test_unsupported_schema_version_is_rejected():
    with pytest.raises(ConfigError) as exc:
        loads(SAMPLE.replace("schema_version: 1", "schema_version: 2"))
    assert str(CONFIG_SCHEMA_VERSION) in str(exc.value)


def test_non_integer_schema_version_is_rejected():
    with pytest.raises(ConfigError):
        loads(SAMPLE.replace("schema_version: 1", 'schema_version: "1"'))


def test_missing_profile_is_rejected():
    with pytest.raises(ConfigError):
        loads("schema_version: 1\ninstallation:\n  mode: default\n")


def test_missing_installation_is_rejected():
    with pytest.raises(ConfigError):
        loads("schema_version: 1\nprofile:\n  id: pepe\n")


def test_unknown_top_level_field_is_rejected():
    with pytest.raises(ConfigError) as exc:
        loads(SAMPLE + "\nmystery: 1\n")
    assert "mystery" in str(exc.value)


def test_unknown_ui_field_is_rejected():
    with pytest.raises(ConfigError):
        loads(SAMPLE + "    left_rail_visible: true\n")


def test_invalid_profile_id_is_rejected():
    with pytest.raises(ConfigError):
        loads(SAMPLE.replace("id: pepe", "id: 'Pepe!'"))


def test_relative_custom_root_is_rejected_as_config_error():
    document = SAMPLE.replace("mode: portable\n  root: null", "mode: custom\n  root: ../../windows")
    with pytest.raises(ConfigError):
        loads(document)


def test_malformed_yaml_is_rejected():
    with pytest.raises(ConfigError):
        loads("schema_version: [1\n")


def test_non_boolean_rail_flag_is_rejected():
    with pytest.raises(ConfigError):
        loads(SAMPLE.replace("right_rail_visible: true", "right_rail_visible: yes please"))


# --- general config --------------------------------------------------------


def test_ui_defaults_show_the_right_rail():
    ui = UiConfig()
    assert ui.right_rail_visible is True
    assert ui.right_rail_expanded is True


def test_theme_and_pet_are_unset_by_default_so_a_profile_can_supply_them():
    """None means 'not set here', which leaves room for a profile default."""
    config = loads(SAMPLE)
    assert config.general.ui.theme is None
    assert config.general.ui.pet is None
    assert config.to_dict()["general"]["ui"]["theme"] is None


def test_explicit_theme_and_pet_survive_serialization():
    config = InstalledConfig(
        profile_id="pepe", general=GeneralConfig(ui=UiConfig(theme="dark", pet="capybara"))
    )
    assert loads(dumps(config)).general.ui.theme == "dark"
    assert loads(dumps(config)).general.ui.pet == "capybara"


def test_general_config_is_independent_of_the_profile():
    general = GeneralConfig(ui=UiConfig(theme="light"))
    pepe = InstalledConfig(profile_id="pepe", general=general)
    capella = InstalledConfig(profile_id="capella", general=general)
    assert pepe.general == capella.general


def test_missing_general_section_falls_back_to_defaults():
    config = loads("schema_version: 1\nprofile:\n  id: pepe\ninstallation:\n  mode: default\n")
    assert config.general == GeneralConfig()


# --- separacao profile / stack / installation / general --------------------


def test_profile_and_installation_are_orthogonal(tmp_path):
    """Pepe + portable, Pepe + custom and Capella + default are all valid."""
    combinations = [
        ("pepe", InstallLocation.portable()),
        ("pepe", InstallLocation.custom(tmp_path / "IA's")),
        ("capella", InstallLocation.default()),
        ("test", InstallLocation.portable()),
    ]
    for profile_id, location in combinations:
        config = InstalledConfig(profile_id=profile_id, installation=location)
        assert loads(dumps(config)) == config


def test_changing_installation_keeps_the_profile(tmp_path):
    config = InstalledConfig(profile_id="pepe")
    moved = config.with_installation(InstallLocation.custom(tmp_path / "AI"))
    assert moved.profile_id == "pepe"
    assert moved.installation.mode is InstallLocationMode.CUSTOM
    assert config.installation.mode is InstallLocationMode.DEFAULT  # original intocada


def test_changing_general_keeps_profile_stack_and_installation():
    config = InstalledConfig(profile_id="pepe", installation=InstallLocation.portable())
    themed = config.with_general(GeneralConfig(ui=UiConfig(theme="dark")))
    assert themed.profile_id == config.profile_id
    assert themed.stack_id == config.stack_id
    assert themed.installation == config.installation


def test_installed_config_is_not_a_profile_definition():
    """The installed config references a profile; it does not restate it."""
    profile = load_profile(REPO / "profiles" / "pepe.yaml")
    data = InstalledConfig(profile_id=profile.id).to_dict()
    assert data["profile"] == {"id": profile.id}
    assert "hardware" not in data
    assert "models" not in data
    assert "services" not in data


def test_config_references_the_stack_by_id_only():
    data = InstalledConfig(profile_id="pepe", stack_id=STANDARD_STACK.id).to_dict()
    assert data["stack"] == {"id": "standard"}
    assert "components" not in data["stack"]


def test_config_is_immutable():
    with pytest.raises(AttributeError):
        InstalledConfig(profile_id="pepe").profile_id = "capella"


# --- persistencia ----------------------------------------------------------


def test_config_path_lives_in_the_state_directory(tmp_path):
    layout = StackLayout.from_root(tmp_path / "stack")
    assert config_path(layout) == layout.state / CONFIG_FILENAME


def test_config_path_follows_the_resolved_installation(tmp_path):
    resolved = resolve_installation(InstallLocation.custom(tmp_path / "AI"))
    assert config_path(resolved.layout) == resolved.root / "state" / CONFIG_FILENAME


def test_nothing_is_written_to_disk(tmp_path):
    resolved = resolve_installation(InstallLocation.custom(tmp_path / "AI"))
    config = InstalledConfig(profile_id="pepe", installation=resolved.location)
    dumps(config)
    config_path(resolved.layout)
    assert not resolved.root.exists()
    assert list(tmp_path.iterdir()) == []


def test_portable_config_stores_no_machine_specific_path():
    config = loads(SAMPLE)
    assert config.installation.is_relocatable
    assert "root: null" in dumps(config).replace("root: ", "root: ")
    assert config.to_dict()["installation"]["root"] is None
