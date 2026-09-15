"""An image attached in the composer, plus an instruction, must become a ComfyUI edit.

Open WebUI cannot be imported here (it lives in the managed venv), so
:class:`EditTurn` is a literal model of the pinned 0.11.3 path, with the lines
it mirrors::

    utils/middleware.py process_chat_payload
      2431  saved chat: user message image files become image_url parts
      2635  inlet filter functions (the Hub's filter forces legacy on Image turns)
      2680  features.image_generation + legacy -> chat_image_generation_handler
      1673  get_images_from_messages: newest message first, first two sets
      1799  images present and images.edit.enable -> image_edits, else generation
      3108  request filter functions, after the image step, before the model
    routers/images.py image_edits (comfyui engine)
      load_url_image: a file id is read through the Files API (404 when gone)
      get_image_file_item: the upload is renamed to a fresh uuid
      comfyui_upload_image: POST /api/upload/image, type=input
      _apply_workflow_nodes: image list by position, prompt, model, seed
      comfyui_edit_image -> POST /prompt, /history, /view (type=output only)
      upload_image: the PNG becomes a new Open WebUI file on the message
    socket/main.py  'files' event -> persisted on the assistant message

The configuration is exactly what the Hub's sync writes (the real manifest and
the real embedded graph, marked). The filter is the shipped source. ComfyUI is
an HTTP mock that records what it received.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import random
import sys
import types
import uuid
from email.parser import BytesParser
from email.policy import default as email_policy

import httpx
import pytest
from test_image_dispatch import WORKFLOW as T2I_WORKFLOW
from test_image_dispatch import load_filter
from test_open_webui_image_edit import settings  # noqa: F401 - fixture

from local_ai_stack.open_webui_filters import filter_source
from local_ai_stack.open_webui_image_edit import desired_fields

PNG = b"\x89PNG\r\n\x1a\n"
RED_APPLE = PNG + b"red apple on white" * 64
EDIT_SUCCESS = (
    "<context>The requested image has been edited and created and is now being shown to "
    "the user. Let them know that it has been generated.</context>"
)
GENERATION_SUCCESS = (
    "<context>The requested image has been created by the system successfully and is now "
    "being shown to the user. Let the user know that the image they requested has been "
    "generated and is now shown in the chat.</context>"
)


class ComfyUI:
    """What reached ComfyUI, and what it gives back."""

    def __init__(self, *, execution_fails: bool = False, offline: bool = False) -> None:
        self.uploads: list[dict] = []
        self.jobs: list[dict] = []
        self.outputs: dict[str, bytes] = {}
        self.execution_fails = execution_fails
        self.offline = offline

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.offline:
            raise httpx.ConnectError("connection refused")
        path = request.url.path
        if request.method == "POST" and path == "/api/upload/image":
            fields = parse_multipart(request)
            image = fields["image"]
            self.uploads.append({**image, "type": fields["type"]["data"]})
            return httpx.Response(
                200, json={"name": image["filename"], "subfolder": "", "type": "input"}
            )
        if request.method == "POST" and path == "/prompt":
            body = json.loads(request.content)
            prompt_id = f"job-{len(self.jobs) + 1}"
            self.jobs.append(body["prompt"])
            if not self.execution_fails:
                self.outputs[prompt_id] = PNG + f"edited output {prompt_id}".encode() * 32
            return httpx.Response(200, json={"prompt_id": prompt_id, "number": len(self.jobs)})
        if request.method == "GET" and path.startswith("/history/"):
            prompt_id = path.rsplit("/", 1)[1]
            images = (
                [{"filename": f"Flux2-Klein_{prompt_id}.png", "subfolder": "", "type": "output"}]
                if prompt_id in self.outputs
                else []
            )
            return httpx.Response(200, json={prompt_id: {"outputs": {"9": {"images": images}}}})
        if request.method == "GET" and path == "/view":
            prompt_id = (
                request.url.params["filename"].removeprefix("Flux2-Klein_").removesuffix(".png")
            )
            return httpx.Response(
                200, content=self.outputs[prompt_id], headers={"content-type": "image/png"}
            )
        return httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(base_url="http://127.0.0.1:8188", transport=httpx.MockTransport(self))


def parse_multipart(request: httpx.Request) -> dict:
    header = f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode()
    message = BytesParser(policy=email_policy).parsebytes(header + request.content)
    fields = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        fields[name] = {
            "filename": part.get_filename(),
            "data": part.get_payload(decode=True),
            "content_type": part.get_content_type(),
        }
    fields["type"]["data"] = fields["type"]["data"].decode()
    return fields


class OpenWebUIFiles:
    """The Files API as the edit path uses it: bytes by id, new files for results."""

    def __init__(self) -> None:
        self.store: dict[str, tuple[bytes, str]] = {}

    def add(self, data: bytes, content_type: str = "image/png") -> str:
        file_id = str(uuid.uuid4())
        self.store[file_id] = (data, content_type)
        return file_id


class NotFound(Exception):
    pass


class EditTurn:
    """One Image-on (or off) composer turn in a saved chat, routed as 0.11.3 routes it."""

    def __init__(self, filter_, comfyui: ComfyUI, files: OpenWebUIFiles, config: dict) -> None:
        self.filter = filter_
        self.comfyui = comfyui
        self.files = files
        self.config = config
        self.history: list[dict] = []
        self.statuses: list[str] = []
        self.model_requests: list[list[dict]] = []
        self.shown: list[str] = []

    # -- routers/images.py ---------------------------------------------------------------

    def load_image(self, url: str) -> tuple[bytes, str]:
        file_id = url.split("/api/v1/files/")[-1].split("/content")[0]
        if file_id not in self.files.store:
            raise NotFound("404: file not found")
        return self.files.store[file_id]

    def image_edits(self, prompt: str, images: list[str], metadata: dict) -> list[dict]:
        uploaded = []
        with self.comfyui.client() as client:
            for url in images:
                data, content_type = self.load_image(url)
                name = f"{uuid.uuid4()}.png"  # get_image_file_item
                try:
                    response = client.post(
                        "/api/upload/image",
                        files={"image": (name, io.BytesIO(data), content_type)},
                        data={"type": "input"},
                    )
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    raise RuntimeError("Failed to upload images to ComfyUI.") from exc
                uploaded.append(response.json().get("name", name))
            workflow = json.loads(self.config["IMAGES_EDIT_COMFYUI_WORKFLOW"])
            for node in self.config["IMAGES_EDIT_COMFYUI_WORKFLOW_NODES"]:
                inputs = [workflow[node_id]["inputs"] for node_id in node["node_ids"]]
                if node["type"] == "image":
                    for index, target in enumerate(inputs):
                        if index < len(uploaded):
                            target[node["key"]] = uploaded[index]
                elif node["type"] == "prompt":
                    for target in inputs:
                        target[node["key"] or "text"] = prompt
                elif node["type"] == "model":
                    for target in inputs:
                        target[node["key"]] = self.config["IMAGE_EDIT_MODEL"]
                elif node["type"] == "seed":
                    for target in inputs:
                        target[node["key"]] = random.randint(0, 1125899906842624)
            prompt_id = client.post("/prompt", json={"prompt": workflow, "client_id": "x"}).json()[
                "prompt_id"
            ]
            history = client.get(f"/history/{prompt_id}").json()[prompt_id]
            results = []
            for output in history["outputs"].values():
                for image in output.get("images", []):
                    data = client.get("/view", params=image).content
                    file_id = self.files.add(data)
                    results.append(
                        {"type": "image", "id": file_id, "url": f"/api/v1/files/{file_id}/content"}
                    )
            return results

    def image_generations(self, prompt: str) -> list[dict]:
        workflow = json.loads(json.dumps(T2I_WORKFLOW))
        workflow["76"]["inputs"]["value"] = prompt
        with self.comfyui.client() as client:
            prompt_id = client.post("/prompt", json={"prompt": workflow}).json()["prompt_id"]
        file_id = self.files.add(self.comfyui.outputs.get(prompt_id, PNG))
        return [{"type": "image", "id": file_id, "url": f"/api/v1/files/{file_id}/content"}]

    # -- utils/middleware.py ----------------------------------------------------------------

    def images_in_history(self) -> list[str]:
        sets = []
        for message in reversed(self.history):
            found = [
                f["url"]
                for f in message.get("files") or []
                if f.get("type") == "image" or str(f.get("content_type", "")).startswith("image/")
            ]
            if found:
                sets.append(found)
        return [url for group in sets[:2] for url in group]

    def image_handler(self, prompt: str, metadata: dict, assistant: dict) -> str:
        images = self.images_in_history()
        editing = bool(images) and self.config["ENABLE_IMAGE_EDIT"]
        self.statuses.append("Creating image")
        try:
            files = (
                self.image_edits(prompt, images, metadata)
                if editing
                else self.image_generations(prompt)
            )
        except Exception as exc:  # noqa: BLE001 - the pinned handler catches everything
            self.statuses.append("An error occurred while generating an image")
            return (
                "<context>Image generation was attempted but failed. The system is currently unable "
                f"to generate the image. Tell the user that the following error occurred: {exc}</context>"
            )
        self.statuses.append("Image created")
        assistant["files"] = [*assistant.get("files", []), *files]  # the 'files' event, persisted
        return EDIT_SUCCESS if editing else GENERATION_SUCCESS

    def turn(
        self, text: str, *, attach: list[str] = (), image: bool = True, answer: str = "Done."
    ) -> dict:
        user = {
            "role": "user",
            "content": text,
            "files": [
                {"type": "file", "id": fid, "url": fid, "content_type": "image/png"}
                for fid in attach
            ],
        }
        assistant = {"role": "assistant", "content": "", "files": []}
        self.history += [user, assistant]
        metadata = {
            "session_id": "sid-1",
            "chat_id": "chat-1",
            "message_id": f"msg-{len(self.history)}",
            "features": {"image_generation": image, "memory": True},
            "params": {"function_calling": "native", "tool_approval_mode": "full"},
        }
        # 2431: image files of user messages become image_url parts for the model.
        messages = []
        for message in self.history[:-1]:
            images = [f["url"] for f in message.get("files") or [] if message["role"] == "user"]
            content = message["content"]
            if images:
                content = [{"type": "text", "text": content}] + [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{u}"}}
                    for u in images
                ]
            messages.append({"role": message["role"], "content": content})
        body = {"model": "hermes3:8b", "messages": messages}
        body = run(self.filter.inlet(body, metadata))
        if image and metadata["params"].get("function_calling") == "legacy":
            context = self.image_handler(text, metadata, assistant)
            body["messages"].insert(0, {"role": "system", "content": context})
        with persisted_files(assistant):
            body = run(self.filter.request(body, metadata, self.emit))
        self.model_requests.append(body["messages"])
        event = {"choices": [{"delta": {"content": answer}, "finish_reason": "stop"}]}
        event = run(self.filter.stream(event, metadata))
        shown = event["choices"][0]["delta"].get("content", "")
        outlet = run(
            self.filter.outlet({"messages": [{"role": "assistant", "content": shown}]}, metadata)
        )
        assistant["content"] = outlet["messages"][-1]["content"]
        self.shown.append(assistant["content"])
        return assistant

    async def emit(self, event: dict) -> None:
        if event.get("type") == "status":
            self.statuses.append(event["data"]["description"])


class persisted_files:
    """Stand in for open_webui.models.chats while the request hook looks up the message."""

    def __init__(self, assistant: dict) -> None:
        self.assistant = assistant
        self.saved: dict = {}

    def __enter__(self):
        assistant = self.assistant

        class Chats:
            @staticmethod
            async def get_message_metadata(chat_id, message_id, key):
                return assistant.get(key)

        chats = types.ModuleType("open_webui.models.chats")
        chats.Chats = Chats
        chat_id = types.ModuleType("open_webui.utils.chat_id")
        chat_id.is_saved_chat_id = lambda value: bool(value) and not value.startswith("local:")
        modules = {
            "open_webui": types.ModuleType("open_webui"),
            "open_webui.models": types.ModuleType("open_webui.models"),
            "open_webui.models.chats": chats,
            "open_webui.utils": types.ModuleType("open_webui.utils"),
            "open_webui.utils.chat_id": chat_id,
        }
        for name, module in modules.items():
            self.saved[name] = sys.modules.get(name)
            sys.modules[name] = module
        return self

    def __exit__(self, *exc):
        for name, module in self.saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def run(coroutine):
    return asyncio.run(coroutine)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def edit_config(settings):  # noqa: F811 - the shared fixture
    """The Open WebUI settings after the Hub's sync, as the image path reads them."""
    return {**desired_fields(settings)}


@pytest.fixture
def world(edit_config):
    comfyui = ComfyUI()
    files = OpenWebUIFiles()
    turn = EditTurn(load_filter(filter_source()), comfyui, files, edit_config)
    return turn, comfyui, files


# --- D, E, F, G: the user's image in, the edited PNG out, the original untouched -----------


def test_d_the_users_image_bytes_reach_the_mapped_load_image_node(world):
    turn, comfyui, files = world
    original = files.add(RED_APPLE)
    turn.turn("Change the apple to green but keep the background white.", attach=[original])

    assert len(comfyui.uploads) == 1 and len(comfyui.jobs) == 1
    upload = comfyui.uploads[0]
    assert upload["data"] == RED_APPLE and upload["type"] == "input"
    assert upload["filename"].endswith(".png") and uuid.UUID(upload["filename"][:-4])
    job = comfyui.jobs[0]
    assert job["76"]["class_type"] == "LoadImage"
    assert job["76"]["inputs"]["image"] == upload["filename"]


def test_e_the_instruction_model_and_seed_land_on_their_nodes(world):
    turn, comfyui, files = world
    turn.turn(
        "Change the apple to green but keep the background white.", attach=[files.add(RED_APPLE)]
    )
    job = comfyui.jobs[0]
    assert (
        job["75:74"]["inputs"]["text"] == "Change the apple to green but keep the background white."
    )
    assert job["75:70"]["inputs"]["unet_name"] == "flux-2-klein-4b-fp8.safetensors"
    assert isinstance(job["75:73"]["inputs"]["noise_seed"], int)
    assert job["75:62"]["inputs"]["steps"] == 4 and job["75:63"]["inputs"]["cfg"] == 1


def test_f_the_edited_png_is_rehosted_and_attached_to_the_reply(world):
    turn, comfyui, files = world
    reply = turn.turn("Make it green.", attach=[files.add(RED_APPLE)], answer="Here it is!")
    assert len(reply["files"]) == 1
    rehosted, _ = files.store[reply["files"][0]["id"]]
    assert rehosted == comfyui.outputs["job-1"]
    assert turn.statuses == ["Creating image", "Image created"]
    assert reply["content"] == "Here it is!"


def test_g_the_original_is_never_altered_and_a_second_edit_edits_the_first_result(world):
    turn, comfyui, files = world
    original = files.add(RED_APPLE)
    before = sha(files.store[original][0])
    first = turn.turn("Change the apple to green.", attach=[original])
    second = turn.turn("Change the background to black while keeping the apple green.")

    assert sha(files.store[original][0]) == before
    assert len(comfyui.jobs) == 2, "a second instruction is a second job"
    first_png = files.store[first["files"][0]["id"]][0]
    # Newest image first: the second job's input is the first edit's output.
    second_job = comfyui.jobs[1]
    input_name = second_job["76"]["inputs"]["image"]
    assert next(u["data"] for u in comfyui.uploads if u["filename"] == input_name) == first_png
    assert second_job["75:74"]["inputs"]["text"].startswith("Change the background to black")
    assert second["files"][0]["id"] != first["files"][0]["id"] != original


def test_a_hostile_file_name_never_reaches_comfyui(world):
    turn, comfyui, files = world
    turn.turn("Make it green.", attach=[files.add(RED_APPLE)])
    assert all("/" not in u["filename"] and ".." not in u["filename"] for u in comfyui.uploads)


def test_the_text_model_is_never_sent_the_pixels_on_an_image_turn(world):
    """Ollama refuses image parts for a model without vision; the edit must not end in raw JSON."""
    turn, _, files = world
    turn.turn("Make it green.", attach=[files.add(RED_APPLE)])
    parts = [
        part
        for message in turn.model_requests[-1]
        if isinstance(message["content"], list)
        for part in message["content"]
    ]
    assert parts == []
    user = next(m for m in turn.model_requests[-1] if m["role"] == "user")
    assert user["content"] == "Make it green."
    assert turn.history[0]["files"], "the files the image path reads are kept"


# --- failure paths: controlled, never a picture that is not there -------------------------


def test_an_execution_error_in_comfyui_is_reported_not_announced_as_an_image(edit_config):
    comfyui = ComfyUI(execution_fails=True)
    files = OpenWebUIFiles()
    turn = EditTurn(load_filter(filter_source()), comfyui, files, edit_config)
    reply = turn.turn(
        "Make it green.", attach=[files.add(b"not an image")], answer="Here is the edited image!"
    )
    assert reply["files"] == []
    assert reply["content"].startswith("The image could not be created")
    assert turn.statuses[-1] == "The image could not be created"
    system = turn.model_requests[-1][0]["content"]
    assert "is now being shown" not in system and "could not be created" in system


def test_a_deleted_attachment_is_a_controlled_error_with_no_job(world):
    turn, comfyui, files = world
    gone = files.add(RED_APPLE)
    del files.store[gone]
    reply = turn.turn("Make it green.", attach=[gone], answer="Sorry, that failed.")
    assert comfyui.jobs == [] and reply["files"] == []
    assert turn.statuses[-1] == "An error occurred while generating an image"
    assert reply["content"] == "Sorry, that failed."
    assert "Traceback" not in json.dumps(turn.model_requests)


def test_comfyui_offline_is_a_controlled_error(edit_config):
    comfyui = ComfyUI(offline=True)
    files = OpenWebUIFiles()
    turn = EditTurn(load_filter(filter_source()), comfyui, files, edit_config)
    reply = turn.turn("Make it green.", attach=[files.add(RED_APPLE)], answer="It did not work.")
    assert reply["files"] == [] and turn.statuses[-1].startswith("An error occurred")
    assert "Failed to upload images to ComfyUI." in turn.model_requests[-1][0]["content"]


def test_a_made_up_link_in_the_acknowledgement_is_removed(world):
    turn, _, files = world
    reply = turn.turn(
        "Make it green.",
        attach=[files.add(RED_APPLE)],
        answer='Here:\n\n<img src="https://example.com/a.png" alt="x">\n\nDone.',
    )
    assert reply["content"] == "Here:\n\nDone."


# --- K, L: generation and plain chat are what they were -------------------------------------


def test_k_an_image_turn_without_any_image_still_generates_from_text(world):
    turn, comfyui, _ = world
    reply = turn.turn("Generate an image of a red apple on a plain white background.")
    assert comfyui.uploads == []
    assert len(comfyui.jobs) == 1 and "LoadImage" not in {
        n["class_type"] for n in comfyui.jobs[0].values()
    }
    assert comfyui.jobs[0]["76"]["inputs"]["value"].startswith("Generate an image of a red apple")
    assert len(reply["files"]) == 1


def test_l_plain_chat_posts_nothing_and_is_left_exactly_as_it_was(world):
    turn, comfyui, _ = world
    reply = turn.turn("hi", image=False, answer="Hello! How can I help? ![x](https://a.b/c.png)")
    assert comfyui.uploads == [] and comfyui.jobs == []
    assert reply["content"] == "Hello! How can I help? ![x](https://a.b/c.png)"
    assert turn.statuses == []


def test_l_with_image_off_an_attached_image_is_not_edited_and_not_stripped(world):
    turn, comfyui, files = world
    turn.turn("What is this?", attach=[files.add(RED_APPLE)], image=False)
    assert comfyui.uploads == [] and comfyui.jobs == []
    user = turn.model_requests[-1][0]
    assert isinstance(user["content"], list) and user["content"][1]["type"] == "image_url"
