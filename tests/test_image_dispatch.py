"""A real Open WebUI image request must become a ComfyUI job.

The gap this closes: 727482a was tested by calling the filter's hooks with a
tool call that had exactly the right shape. The composer's real turn has the
UI's own request metadata (``function_calling: native``, a dozen builtin tools
offered) and a small model that answers however it likes -- a native tool
call with invented argument names, a serialized call to a name that does not
exist, or nothing. None of those ever reached ``POST /prompt``.

Open WebUI itself cannot be imported here (it lives in the managed venv), so
:class:`Composer` is a deliberately literal model of the pinned 0.11.3 request
path, with the line references it mirrors::

    utils/middleware.py process_chat_payload
      2635  inlet filter functions run first
      2680  features.image_generation: forced generation only when
            metadata.params.function_calling == "legacy"
      1768  chat_image_generation_handler: task-model prompt, then
            image_generations() -> POST {COMFYUI_BASE_URL}/prompt
      2771  builtin tools (generate_image) offered only when not legacy
      3030  native: tools go to the model; the executor runs what it returns
    tools/builtin.py generate_image(prompt) -> image_generations(prompt)

The filter under test is the shipped source, executed the way Open WebUI
executes it. ComfyUI is an HTTP mock that records ``POST /prompt`` and the
workflow it received. The model's answers are the shapes captured from the
managed Pepe root. The assertions are about the job: it is posted, it carries
the configured Flux2 Klein workflow, and its prompt says what the user said.
"""

from __future__ import annotations

import asyncio
import json
import subprocess

import httpx
import pytest

from local_ai_stack.installation import detect_repo_root
from local_ai_stack.open_webui_filters import filter_source

WORKFLOW = json.loads(
    (detect_repo_root() / "workflows" / "flux2-klein-t2i-api.json").read_text(encoding="utf-8")
)
PROMPT_NODE = "76"  # manifests/workflows.yaml: prompt -> node 76, key "value"
USER_PROMPT = "Generate an image of a red apple on a plain white background."

# Assistant answers observed on the real root, with the Image feature on.
HERMES_NATIVE_WRONG_ARGS = {
    "tool_calls": [
        {
            "id": "call_gcqhrxnn",
            "type": "function",
            "function": {
                "name": "generate_image",
                "arguments": json.dumps(
                    {"background_color": "white", "apple_color": "red", "include_shadow": False}
                ),
            },
        }
    ],
    "content": "",
}
HERMES_SERIALIZED_UNKNOWN_TOOL = {
    "tool_calls": [],
    "content": json.dumps(
        {
            "arguments": {"prompt": "a single red apple on a plain white background"},
            "name": "create_image",
        }
    ),
}
HERMES_SILENCE = {"tool_calls": [], "content": ""}
HERMES_PROSE_WITH_LINK = {
    "tool_calls": [],
    "content": "Here is the image you requested:\n\n![Red Apple](https://www.example.com/red_apple.jpg)",
}


def load_filter(source: str):
    namespace: dict = {}
    exec(compile(source, "function_pepito_image_chat", "exec"), namespace)  # noqa: S102
    return namespace["Filter"]()


class ComfyUI:
    """The only thing that matters at the end: what reached /prompt."""

    def __init__(self) -> None:
        self.jobs: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/prompt":
            self.jobs.append(json.loads(request.content))
            return httpx.Response(200, json={"prompt_id": f"job-{len(self.jobs)}", "number": 1})
        if request.url.path == "/queue":
            return httpx.Response(200, json={"queue_running": [], "queue_pending": []})
        return httpx.Response(404)


class Composer:
    """One composer turn, routed as Open WebUI 0.11.3 routes it."""

    def __init__(self, filter_, comfyui: ComfyUI, answer: dict) -> None:
        self.filter = filter_
        self.comfyui = comfyui
        self.answer = answer
        self.shown: list[str] = []
        self.persisted: str | None = None
        self.files: list[dict] = []

    # -- Open WebUI's own image pipeline --------------------------------------

    async def image_generations(self, prompt: str) -> list[dict]:
        workflow = json.loads(json.dumps(WORKFLOW))
        workflow[PROMPT_NODE]["inputs"]["value"] = prompt
        with httpx.Client(
            base_url="http://127.0.0.1:8188", transport=httpx.MockTransport(self.comfyui)
        ) as client:
            response = client.post("/prompt", json={"prompt": workflow, "client_id": "open-webui"})
            response.raise_for_status()
        image = {"url": "/api/v1/files/generated/content", "content_type": "image/png"}
        self.files.append(image)
        return [image]

    async def chat_image_generation_handler(self, user_message: str) -> None:
        # image_generation.prompt.enable: the task model may rewrite the
        # prompt; on any failure the user's message is used verbatim (1890-1916).
        await self.image_generations(user_message)

    async def builtin_generate_image(self, prompt: str) -> str:
        images = await self.image_generations(prompt)
        return json.dumps({"status": "success", "images": images})

    # -- the turn ------------------------------------------------------------

    async def turn(self, user_message: str, *, image: bool = True, session: bool = True) -> None:
        metadata = {
            "session_id": "sid-1" if session else None,
            "chat_id": "chat-1",
            "message_id": "msg-1",
            "features": {"image_generation": image, "memory": True},
            "params": {"function_calling": "native", "tool_approval_mode": "full"},
            "tools": {},
        }
        body = {
            "model": "hermes3:8b",
            "stream": True,
            "messages": [{"role": "user", "content": user_message}],
        }
        # 2635: inlet filters see the metadata before any routing decision.
        if hasattr(self.filter, "inlet"):
            body = await self.filter.inlet(body, metadata)
        legacy = metadata["params"].get("function_calling") == "legacy"
        # 2680-2690: forced generation is Open WebUI's, and only in legacy mode.
        if image and legacy:
            await self.chat_image_generation_handler(user_message)
        # 2771 / 3030: native mode offers the builtin tool to the model.
        if image and session and not legacy:
            metadata["tools"]["generate_image"] = {
                "type": "builtin",
                "callable": self.builtin_generate_image,
                "spec": {"parameters": {"properties": {"prompt": {}}}},
            }
            body["tools"] = [{"type": "function", "function": {"name": "generate_image"}}]
        if hasattr(self.filter, "request"):
            body = await self.filter.request(body, metadata)
        # The model answers; its content streams through the filter's stream hook.
        await self.deliver(metadata, self.answer)

    async def deliver(self, metadata: dict, answer: dict) -> None:
        delta = {"content": answer["content"]}
        if answer["tool_calls"]:
            delta["tool_calls"] = [dict(call, index=0) for call in answer["tool_calls"]]
        event = {
            "choices": [
                {"delta": delta, "finish_reason": "tool_calls" if answer["tool_calls"] else "stop"}
            ]
        }
        if hasattr(self.filter, "stream"):
            event = await self.filter.stream(event, metadata)
        choice = event["choices"][0]
        text = choice["delta"].get("content") or ""
        calls = choice["delta"].get("tool_calls") or []
        tool = metadata["tools"].get("generate_image")
        for call in calls:
            name = call["function"]["name"]
            arguments = json.loads(call["function"]["arguments"] or "{}")
            if name == "generate_image" and tool is not None:
                # utils/middleware.py: arguments are matched to the spec's parameters.
                accepted = {
                    key: value
                    for key, value in arguments.items()
                    if key in tool["spec"]["parameters"]["properties"]
                }
                await tool["callable"](**accepted)
            # an unknown tool name is not executed
        if text:
            self.shown.append(text)
        body = {
            "id": "msg-1",
            "messages": [{"id": "msg-1", "role": "assistant", "content": "".join(self.shown)}],
        }
        if hasattr(self.filter, "outlet"):
            body = await self.filter.outlet(body, metadata)
        self.persisted = body["messages"][-1]["content"]


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture
def shipped_filter():
    return load_filter(filter_source())


# --- the regression: the real request shape must produce a real job -------------


@pytest.mark.parametrize(
    "answer",
    [
        HERMES_NATIVE_WRONG_ARGS,
        HERMES_SERIALIZED_UNKNOWN_TOOL,
        HERMES_SILENCE,
        HERMES_PROSE_WITH_LINK,
    ],
    ids=["native-call-wrong-args", "serialized-unknown-tool", "silence", "prose-with-link"],
)
def test_a_composer_image_turn_posts_the_workflow_to_comfyui_whatever_the_model_says(
    shipped_filter, answer
):
    comfyui = ComfyUI()
    composer = Composer(shipped_filter, comfyui, answer)

    run(composer.turn(USER_PROMPT))

    assert len(comfyui.jobs) == 1, "exactly one POST /prompt"
    job = comfyui.jobs[0]["prompt"]
    assert job["75:70"]["inputs"]["unet_name"] == "flux-2-klein-4b-fp8.safetensors"
    assert job["75:71"]["inputs"]["clip_name"] == "qwen_3_4b.safetensors"
    prompt = job[PROMPT_NODE]["inputs"]["value"].lower()
    assert "red apple" in prompt and "white background" in prompt
    assert "hedgehog" not in prompt, "the workflow's example prompt must not leak"
    assert composer.files, "the image was attached to the message"


def test_the_acknowledgement_shown_and_persisted_carries_no_made_up_link(shipped_filter):
    composer = Composer(shipped_filter, ComfyUI(), HERMES_PROSE_WITH_LINK)
    run(composer.turn(USER_PROMPT))
    assert composer.shown == ["Here is the image you requested:"]
    assert composer.persisted == "Here is the image you requested:"


def test_silence_becomes_a_plain_statement_not_an_empty_message(shipped_filter):
    composer = Composer(shipped_filter, ComfyUI(), HERMES_SILENCE)
    run(composer.turn(USER_PROMPT))
    assert composer.shown == ["Image generated."] and composer.persisted == "Image generated."


def test_a_second_request_is_a_second_job(shipped_filter):
    comfyui = ComfyUI()
    run(Composer(shipped_filter, comfyui, HERMES_SILENCE).turn(USER_PROMPT))
    run(
        Composer(shipped_filter, comfyui, HERMES_SILENCE).turn(
            "Generate an image of a single green apple on a plain black background."
        )
    )
    assert len(comfyui.jobs) == 2
    prompts = [job["prompt"][PROMPT_NODE]["inputs"]["value"].lower() for job in comfyui.jobs]
    assert "red apple" in prompts[0] and "green apple" in prompts[1]


def test_normal_chat_posts_nothing_and_passes_the_text_through(shipped_filter):
    comfyui = ComfyUI()
    answer = {"tool_calls": [], "content": "Hello! How can I assist you today?"}
    composer = Composer(shipped_filter, comfyui, answer)
    run(composer.turn("hi", image=False))
    assert comfyui.jobs == []
    assert composer.shown == ["Hello! How can I assist you today?"]


def test_an_api_call_without_a_session_keeps_open_webui_native_behaviour(shipped_filter):
    comfyui = ComfyUI()
    composer = Composer(shipped_filter, comfyui, HERMES_SILENCE)
    run(composer.turn(USER_PROMPT, session=False))
    # Not a UI turn: the filter does not route it, and without a session
    # Open WebUI offers no builtin tool either -- exactly as before.
    assert comfyui.jobs == []


# --- the gap itself: 727482a's filter on the same turns -------------------------


def previous_filter_source() -> str | None:
    try:
        return subprocess.run(
            ["git", "show", "727482a:src/local_ai_stack/open_webui_image_filter.py"],
            capture_output=True,
            text=True,
            check=True,
            cwd=detect_repo_root(),
            encoding="utf-8",
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


@pytest.mark.parametrize(
    "answer",
    [HERMES_NATIVE_WRONG_ARGS, HERMES_SERIALIZED_UNKNOWN_TOOL, HERMES_SILENCE],
    ids=["native-call-wrong-args", "serialized-unknown-tool", "silence"],
)
def test_the_previous_filter_never_reached_comfyui_on_these_turns(answer):
    """Documents the gap: with 727482a the same turns posted nothing."""
    source = previous_filter_source()
    if source is None:
        pytest.skip("the previous revision is not available in this checkout")
    comfyui = ComfyUI()
    run(Composer(load_filter(source), comfyui, answer).turn(USER_PROMPT))
    assert comfyui.jobs == []
