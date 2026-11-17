"""SPEC-110: the chat model is loaded into Ollama once "Iniciar" is done, and kept there.

Composition over the controlled stack with a fake Ollama preload: the request a start
sends, that it runs behind the start (never in front of it), that its outcome reaches
``hub-events.log`` and that a failure never fails the start; plus the documented
request body itself and the Ollama keep-alive handed to the process.
"""

from __future__ import annotations

import json
import threading

import httpx
import pytest
import yaml
from first_run_support import controlled_stack

from local_ai_stack.adapters import ollama_control_spec
from local_ai_stack.installation import detect_repo_root
from local_ai_stack.logs import HUB_EVENTS_LOG
from local_ai_stack.ollama_models import OllamaClientError, preload_ollama_model
from local_ai_stack.operation_contracts import OperationState
from local_ai_stack.profiles import ProfileError, validate_profile_data

PEPE = "pepe"
CHAT = "    primary: ollama/hermes3_8b\n"


def profile_yaml(*, enabled: bool = True, keep_alive: str | None = '"60m"') -> str:
    text = (detect_repo_root() / "profiles" / f"{PEPE}.yaml").read_text(encoding="utf-8")
    assert CHAT in text
    text = text.replace(CHAT, CHAT + "    params: {num_ctx: 16384}\n")
    extra = ""
    if enabled:
        extra += "  preload_chat_model: true\n"
    if keep_alive is not None:
        extra += f"  chat_keep_alive: {keep_alive}\n"
    return text.replace("runtime:\n", "runtime:\n" + extra)


class Preload:
    """The Ollama preload as a script: records each request, may wait or fail."""

    def __init__(self, *, fail: bool = False, gate: threading.Event | None = None) -> None:
        self.calls: list[tuple] = []
        self.fail = fail
        self.gate = gate

    def __call__(self, endpoint, model, **options):
        self.calls.append((endpoint, model, options))
        if self.gate is not None:
            assert self.gate.wait(10)
        if self.fail:
            raise OllamaClientError("ollama is not answering")


def stack(preload: Preload, **yaml_options):
    built = controlled_stack(
        profile_yaml=profile_yaml(**yaml_options), profile_id=PEPE, vram_mib=12227
    )
    assert built.app.first_run("op-1", PEPE).state is OperationState.SUCCEEDED
    assert built.app.stop_stack("op-2", PEPE).state is OperationState.SUCCEEDED
    built.app.deps.ollama_preload = preload
    return built


def settle(built) -> None:
    for thread in built.app._preload_threads:
        thread.join(10)
        assert not thread.is_alive()


def events(built) -> str:
    path = built.app.layout.log_file("ollama").parent / HUB_EVENTS_LOG
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_start_sends_the_chat_model_with_the_profile_context_and_keep_alive():
    preload = Preload()
    built = stack(preload)
    try:
        assert built.app.start_stack("op-3", PEPE).state is OperationState.SUCCEEDED
        settle(built)
        ((endpoint, model, options),) = preload.calls
        assert endpoint == "http://127.0.0.1:11434"
        assert model == "hermes3:8b"
        assert options == {"num_ctx": 16384, "keep_alive": "60m"}
        assert "chat preload elapsed=" in events(built)
    finally:
        built.release()


def test_start_does_not_wait_for_the_preload():
    gate = threading.Event()
    preload = Preload(gate=gate)
    built = stack(preload)
    try:
        result = built.app.start_stack("op-3", PEPE)
        assert result.state is OperationState.SUCCEEDED
        assert built.app._preload_threads[-1].is_alive(), (
            "the load is still running behind the start"
        )
        assert "chat preload" not in events(built)
        gate.set()
        settle(built)
        assert "chat preload elapsed=" in events(built)
    finally:
        gate.set()
        built.release()


def test_a_failed_preload_is_logged_and_does_not_fail_the_start():
    preload = Preload(fail=True)
    built = stack(preload)
    try:
        result = built.app.start_stack("op-3", PEPE)
        assert result.state is OperationState.SUCCEEDED
        assert not [d for d in result.diagnostics if "preload" in d.message]
        settle(built)
        assert "chat preload failed" in events(built)
        assert "ollama is not answering" in events(built)
    finally:
        built.release()


def test_a_profile_without_the_keys_changes_nothing():
    preload = Preload()
    built = stack(preload, enabled=False, keep_alive=None)
    try:
        assert built.app.start_stack("op-3", PEPE).state is OperationState.SUCCEEDED
        settle(built)
        assert preload.calls == []
        assert "chat preload" not in events(built)
    finally:
        built.release()


def test_the_ollama_process_receives_the_keep_alive():
    spec = ollama_control_spec("ollama.exe", keep_alive="60m")
    assert spec.launch.env["OLLAMA_KEEP_ALIVE"] == "60m"
    assert "OLLAMA_KEEP_ALIVE" not in ollama_control_spec("ollama.exe").launch.env


def test_the_hub_hands_the_profile_keep_alive_to_the_managed_ollama():
    built = stack(Preload())
    try:
        profile = built.app.profile(PEPE)
        assert profile.chat_keep_alive == "60m"
        controller = built.app._controller(profile)
        launch = controller.specs["ollama"].launch
        assert launch.env["OLLAMA_KEEP_ALIVE"] == "60m"
    finally:
        built.release()


def test_the_preload_warms_the_model_with_a_one_token_answer():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"done": True})

    preload_ollama_model(
        "http://127.0.0.1:11434/",
        "hermes3:8b",
        num_ctx=16384,
        keep_alive="60m",
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert seen == [
        (
            "POST",
            "/api/generate",
            {
                "model": "hermes3:8b",
                "prompt": "Oi",
                "stream": False,
                "keep_alive": "60m",
                "options": {"num_predict": 1, "num_ctx": 16384},
            },
        )
    ]


def test_the_preload_without_profile_values_still_generates_one_token():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"done": True, "response": "O"})

    preload_ollama_model(
        "http://127.0.0.1:11434",
        "hermes3:8b",
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert seen == [
        {"model": "hermes3:8b", "prompt": "Oi", "stream": False, "options": {"num_predict": 1}}
    ]


def test_a_refusing_ollama_raises_a_client_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "model not found"})

    with pytest.raises(OllamaClientError):
        preload_ollama_model(
            "http://127.0.0.1:11434",
            "missing:1b",
            client_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler)),
        )


@pytest.mark.parametrize("value", ["60m", "-1", "3600", "1h30m", "0"])
def test_the_profile_accepts_ollama_durations(value):
    data = yaml.safe_load(profile_yaml(keep_alive=json.dumps(value)))
    assert validate_profile_data(data).chat_keep_alive == value


@pytest.mark.parametrize("value", ["soon", "60 minutes", "", "m60", 60, True])
def test_the_profile_refuses_other_keep_alive_values(value):
    data = yaml.safe_load(profile_yaml())
    data["runtime"]["chat_keep_alive"] = value
    with pytest.raises(ProfileError, match="chat_keep_alive"):
        validate_profile_data(data)


def test_the_preload_flag_must_be_a_boolean():
    data = yaml.safe_load(profile_yaml())
    data["runtime"]["preload_chat_model"] = "yes"
    with pytest.raises(ProfileError, match="preload_chat_model"):
        validate_profile_data(data)


def test_capella_preloads_and_keeps_the_chat_model_for_an_hour():
    profile = validate_profile_data(
        yaml.safe_load((detect_repo_root() / "profiles" / "capella.yaml").read_text("utf-8"))
    )
    assert profile.runtime["preload_chat_model"] is True
    assert profile.chat_keep_alive == "60m"
    assert profile.chat_params["num_ctx"] == 16384
