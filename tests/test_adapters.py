from __future__ import annotations

import sys
from pathlib import Path

import pytest

from local_ai_stack.adapters import (
    COMFYUI_STARTUP_TIMEOUT,
    CONTROL_SPEC_BUILDERS,
    OPEN_WEBUI_STARTUP_TIMEOUT,
    comfyui_control_spec,
    comfyui_launch_spec,
    control_spec_for,
    default_control_specs,
    observation_only_spec,
    observation_only_specs,
    ollama_control_spec,
    open_webui_control_spec,
    open_webui_launch_spec,
)
from local_ai_stack.layout import StackLayout
from local_ai_stack.services import DEFAULT_SERVICE_SPECS

EXECUTABLE = sys.executable  # a real file; never actually started here


@pytest.fixture
def layout(tmp_path) -> StackLayout:
    return StackLayout.from_root(tmp_path / "stack")


# --- Open WebUI ------------------------------------------------------------


def test_open_webui_is_bound_through_supported_cli_and_managed_environment(layout):
    spec = open_webui_launch_spec(EXECUTABLE, port=8081, data_dir=layout.open_webui_data)
    assert spec.argv == (EXECUTABLE, "serve", "--host", "127.0.0.1", "--port", "8081")
    assert spec.env["HOST"] == "127.0.0.1"
    assert spec.env["PORT"] == "8081"
    assert spec.env["DATA_DIR"] == str(layout.open_webui_data)
    assert spec.service_id == "open-webui"


def test_open_webui_accepts_a_different_invocation(layout):
    """The uvx form passes its own arguments before the subcommand."""
    spec = open_webui_launch_spec(
        EXECUTABLE, args=("--python", "3.11", "open-webui@latest", "serve")
    )
    assert spec.argv == (
        EXECUTABLE,
        "--python",
        "3.11",
        "open-webui@latest",
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        "8080",
    )


def test_open_webui_control_spec_wires_health_process_and_logs(layout):
    spec = open_webui_control_spec(EXECUTABLE, layout=layout)
    assert spec.controllable
    assert spec.service.id == spec.process.service_id == "open-webui"
    assert spec.service.endpoint == "http://127.0.0.1:8080/health"
    assert spec.launch.logs.stdout == layout.log_file("open-webui")
    assert spec.launch.env["DATA_DIR"] == str(layout.open_webui_data)
    assert spec.launch.cwd == layout.open_webui_data
    assert spec.launch.startup_timeout == OPEN_WEBUI_STARTUP_TIMEOUT


def test_open_webui_moves_health_and_discovery_together(layout):
    spec = open_webui_control_spec(EXECUTABLE, layout=layout, port=9090)
    assert spec.service.port == 9090
    assert spec.process.port == 9090
    assert spec.service.endpoint == "http://127.0.0.1:9090/health"
    assert spec.launch.env["PORT"] == "9090"


# --- ComfyUI ---------------------------------------------------------------


def test_comfyui_binds_its_address_with_the_documented_flags(layout):
    spec = comfyui_launch_spec(EXECUTABLE, script=layout.comfyui_app / "main.py", port=8189)
    assert spec.argv == (
        EXECUTABLE,
        str(layout.comfyui_app / "main.py"),
        "--listen",
        "127.0.0.1",
        "--port",
        "8189",
    )


def test_a_packaged_comfyui_needs_no_script(layout):
    spec = comfyui_launch_spec(EXECUTABLE)
    assert spec.argv == (EXECUTABLE, "--listen", "127.0.0.1", "--port", "8188")


def test_comfyui_arguments_can_be_replaced_entirely():
    spec = comfyui_launch_spec(EXECUTABLE, args=("--cpu",))
    assert spec.argv == (EXECUTABLE, "--cpu")


def test_comfyui_control_spec_runs_from_its_own_directory(layout):
    spec = comfyui_control_spec(EXECUTABLE, layout=layout)
    assert spec.controllable
    assert spec.launch.cwd == layout.comfyui_app
    assert spec.launch.logs.stdout == layout.log_file("comfyui")
    assert spec.service.endpoint == "http://127.0.0.1:8188/system_stats"
    assert spec.launch.startup_timeout == COMFYUI_STARTUP_TIMEOUT


# --- the shared rule -------------------------------------------------------


def test_every_known_service_has_a_launch_contract():
    assert set(CONTROL_SPEC_BUILDERS) == {spec.id for spec in DEFAULT_SERVICE_SPECS}


def test_no_executable_means_watch_only():
    for spec in DEFAULT_SERVICE_SPECS:
        watched = control_spec_for(spec.id, None)
        assert not watched.controllable
        assert watched.launch is None
        assert watched.service.id == spec.id


def test_an_executable_makes_a_service_controllable(layout):
    for spec in DEFAULT_SERVICE_SPECS:
        controlled = control_spec_for(spec.id, EXECUTABLE, layout=layout)
        assert controlled.controllable
        assert controlled.launch.argv[0] == EXECUTABLE


def test_an_unknown_service_is_a_clear_error():
    with pytest.raises(KeyError, match="unknown service"):
        observation_only_spec("stable-diffusion")
    with pytest.raises(KeyError, match="no launch contract"):
        control_spec_for("stable-diffusion", EXECUTABLE)


def test_observation_only_specs_default_to_the_two_web_apps():
    assert [spec.service_id for spec in observation_only_specs()] == ["open-webui", "comfyui"]
    assert not any(spec.controllable for spec in observation_only_specs())


def test_default_control_specs_cover_every_service(layout):
    specs = {spec.service_id: spec for spec in default_control_specs()}
    assert list(specs) == [spec.id for spec in DEFAULT_SERVICE_SPECS]
    assert not any(spec.controllable for spec in specs.values())


def test_default_control_specs_take_an_executable_per_service(layout):
    specs = {
        spec.service_id: spec
        for spec in default_control_specs(
            executables={"open-webui": EXECUTABLE, "comfyui": EXECUTABLE},
            layout=layout,
            options={"comfyui": {"port": 8199}},
        )
    }
    assert specs["ollama"].controllable is False
    assert specs["open-webui"].controllable is True
    assert specs["comfyui"].launch.argv[-1] == "8199"


def test_the_ollama_keyword_form_still_works(layout):
    specs = {
        spec.service_id: spec
        for spec in default_control_specs(
            ollama_executable=EXECUTABLE, layout=layout, models_dir=layout.models
        )
    }
    assert specs["ollama"].controllable
    assert specs["ollama"].launch.env["OLLAMA_MODELS"] == str(layout.models)
    assert not specs["comfyui"].controllable


def test_no_adapter_uses_a_shell_or_an_unstructured_command(layout):
    for build in (ollama_control_spec, open_webui_control_spec, comfyui_control_spec):
        launch = build(EXECUTABLE, layout=layout).launch
        assert isinstance(launch.argv, tuple)
        assert all(isinstance(part, str) for part in launch.argv)
        assert launch.argv[0] == EXECUTABLE
        assert not any(part.strip() == "&&" or ";" in part for part in launch.argv[1:])


def test_adapters_never_invent_a_path(layout):
    """Without a layout and without explicit paths, nothing is guessed."""
    assert open_webui_control_spec(EXECUTABLE).launch.env.get("DATA_DIR") is None
    assert comfyui_control_spec(EXECUTABLE).launch.cwd is None
    assert ollama_control_spec(EXECUTABLE).launch.env.get("OLLAMA_MODELS") is None
    assert open_webui_control_spec(EXECUTABLE).launch.logs is None


def test_force_kill_is_off_by_default_everywhere(layout):
    for build in (ollama_control_spec, open_webui_control_spec, comfyui_control_spec):
        assert build(EXECUTABLE, layout=layout).launch.allow_force_kill is False


def test_paths_survive_as_strings(tmp_path):
    executable = tmp_path / "some dir" / "app.exe"
    spec = open_webui_launch_spec(executable)
    assert spec.argv[0] == str(executable)
    assert Path(spec.argv[0]).name == "app.exe"
