"""An image attached in the composer, plus an instruction, must become a ComfyUI edit.

Open WebUI cannot be imported here (it lives in the managed venv), so
:class:`EditTurn` is a literal model of the pinned 0.11.3 path, with the lines
it mirrors::

    utils/middleware.py process_chat_payload
      2431  saved chat: user message image files become image_url parts
      2635  inlet filter functions (the Hub's filter forces legacy on Image turns,
            decides edit-or-new from the user's words, and turns the feature off
            for the turn when a new picture is asked in a chat that holds one)
      2680  features.image_generation + legacy -> chat_image_generation_handler
      1785  the handler reads the SAVED chat's history, not the request
      1673  get_images_from_messages: newest message first, first two sets
      1799  images present and images.edit.enable -> image_edits, else generation
      3108  request filter functions, after the image step, before the model
            (the Hub's filter generates the taken-over turn here, through
            routers/images.py image_generations, from the current message only)
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
import base64
import hashlib
import io
import json
import random
import sys
import types
import uuid
from email.parser import BytesParser
from email.policy import default as email_policy
from pathlib import Path

import httpx
import pytest
from test_image_dispatch import WORKFLOW as T2I_WORKFLOW
from test_image_dispatch import load_filter
from test_open_webui_image_edit import settings  # noqa: F401
from test_open_webui_image_filter import with_plan

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

    def __init__(
        self, *, execution_fails: bool = False, offline: bool = False, fail_edits: bool = False
    ) -> None:
        self.uploads: list[dict] = []
        self.jobs: list[dict] = []
        self.outputs: dict[str, bytes] = {}
        self.execution_fails = execution_fails
        self.offline = offline
        self.fail_edits = fail_edits

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
        self.chat_id = "chat-1"
        # The user's valves of every turn, under what a turn passes itself.
        self.valves: dict = {}
        self.generation_enabled = True  # Config image_generation.enable
        self.user_role = "admin"
        # 2497 process_messages_with_output removes `files` from the request.
        self.request_files_stripped = True
        self.inline_image_data = False
        self.image_generations_calls = 0
        self.model_requests: list[list[dict]] = []
        self.shown: list[str] = []
        # The web search: the setting, what the engine answers (a dict, or an exception to
        # raise), the queries it was asked, and the files the inlet left on the request.
        self.web_search_enabled = False
        self.web_results: object = None
        self.web_queries: list[list[str]] = []
        self.inlet_files: list | None = None
        # The settings each generation ran with (the stack overlay shows in them).
        self.generation_configs: list[dict] = []
        self.generation_forms: list[dict] = []
        self.embeds: list[str] = []

    # -- routers/images.py ---------------------------------------------------------------

    def load_image(self, url: str) -> tuple[bytes, str]:
        if url.startswith("data:"):  # routers/images.py: a data URI is decoded in place
            header, _, payload = url.partition(",")
            return base64.b64decode(payload), header[5:].split(";")[0]
        file_id = url.split("/api/v1/files/")[-1].split("/content")[0]
        if file_id not in self.files.store:
            raise NotFound("404: file not found")
        return self.files.store[file_id]

    def image_edits(
        self, prompt: str, images: list[str], metadata: dict, config: dict | None = None
    ) -> list[dict]:
        config = self.config if config is None else config
        if self.comfyui.fail_edits:
            raise RuntimeError("Image refinement failed")
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
            workflow = json.loads(config["IMAGES_EDIT_COMFYUI_WORKFLOW"])
            for node in config["IMAGES_EDIT_COMFYUI_WORKFLOW_NODES"]:
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
                        target[node["key"]] = config["IMAGE_EDIT_MODEL"]
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

    def image_generations(self, prompt: str, config: dict | None = None) -> list[dict]:
        """routers/images.py image_generations: the effective settings' workflow, with the
        prompt written into the nodes mapped for it, submitted to ComfyUI."""
        self.image_generations_calls += 1
        config = self.config if config is None else config
        self.generation_configs.append(config)
        text = config.get("COMFYUI_WORKFLOW")
        workflow = json.loads(text) if isinstance(text, str) and text else None
        if workflow is None:
            workflow = json.loads(json.dumps(T2I_WORKFLOW))
            workflow["76"]["inputs"]["value"] = prompt
        else:
            for node in config.get("COMFYUI_WORKFLOW_NODES") or []:
                if node["type"] == "prompt":
                    for node_id in node["node_ids"]:
                        workflow[node_id]["inputs"][node.get("key") or "text"] = prompt
                elif node["type"] in ("width", "height"):
                    width, height = map(int, config["IMAGE_SIZE"].split("x"))
                    for node_id in node["node_ids"]:
                        workflow[node_id]["inputs"][node["key"]] = (
                            width if node["type"] == "width" else height
                        )
                elif node["type"] == "seed":
                    for node_id in node["node_ids"]:
                        workflow[node_id]["inputs"][node["key"]] = random.randint(
                            0, 1125899906842624
                        )
        with self.comfyui.client() as client:
            prompt_id = client.post("/prompt", json={"prompt": workflow}).json()["prompt_id"]
        if self.comfyui.execution_fails:
            raise RuntimeError("ComfyUI returned no image")
        file_id = self.files.add(self.comfyui.outputs.get(prompt_id, PNG))
        return [{"type": "image", "id": file_id, "url": f"/api/v1/files/{file_id}/content"}]

    # -- what the filter asks Open WebUI for ---------------------------------------------

    def saved_chat(self):
        """Chats.get_chat_by_id_and_user_id: the persisted history with its parent chain."""
        messages = {}
        previous = None
        for message in self.history:
            messages[message["id"]] = {**message, "parentId": previous}
            previous = message["id"]
        return types.SimpleNamespace(
            chat={"history": {"messages": messages, "currentId": previous}}
        )

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

    def request_images(self, messages: list) -> list[str]:
        """1782: a temporary or channel chat's images come from the request's messages."""
        sets = []
        for message in reversed(messages):
            found = [
                f["url"]
                for f in message.get("files") or []
                if f.get("type") == "image" or str(f.get("content_type", "")).startswith("image/")
            ]
            if found:
                sets.append(found)
        return [url for group in sets[:2] for url in group]

    def image_handler(
        self, prompt: str, metadata: dict, assistant: dict, request_messages: list
    ) -> str | None:
        saved = not metadata["chat_id"].startswith(("temporary:", "local:", "channel:"))
        images = self.images_in_history() if saved else self.request_images(request_messages)
        editing = bool(images) and self.config["ENABLE_IMAGE_EDIT"]
        if not editing and not self.generation_enabled:
            return None  # 1807: the handler leaves the turn alone
        self.statuses.append("Creating image")
        try:
            files = (
                self.image_edits(prompt, images, metadata)
                if editing
                else self.image_generations(prompt)
            )
        except Exception as exc:  # noqa: BLE001
            self.statuses.append("An error occurred while generating an image")
            return (
                "<context>Image generation was attempted but failed. The system is currently unable "
                f"to generate the image. Tell the user that the following error occurred: {exc}</context>"
            )
        self.statuses.append("Image created")
        assistant["files"] = [*assistant.get("files", []), *files]  # the 'files' event, persisted
        return EDIT_SUCCESS if editing else GENERATION_SUCCESS

    def turn(
        self,
        text: str,
        *,
        attach: list[str] = (),
        image: bool = True,
        answer: str = "Done.",
        filter_ids: tuple = (),
        valves=None,
    ) -> dict:
        user = {
            "id": f"msg-{len(self.history) + 1}",
            "role": "user",
            "content": text,
            "files": [
                {"type": "file", "id": fid, "url": fid, "content_type": "image/png"}
                for fid in attach
            ],
        }
        if valves is None:
            valves = dict(self.valves) or None
        elif isinstance(valves, dict):
            valves = {**self.valves, **valves}
        assistant = {"id": f"msg-{len(self.history) + 2}", "role": "assistant", "content": ""}
        assistant["files"] = []
        self.history += [user, assistant]
        features = {"image_generation": image, "memory": True}
        metadata = {
            "session_id": "sid-1",
            "chat_id": self.chat_id,
            "message_id": assistant["id"],
            "features": features,  # main.py: the same dict as the body's
            "params": {"function_calling": "native", "tool_approval_mode": "full"},
            "filter_ids": list(filter_ids),  # the toggle filters the user switched on
        }
        # Saved chat messages, and routed temporary-chat requests, carry
        # user-uploaded pictures as image_url data parts. The legacy handler
        # still reads its `files` metadata only.
        saved = not self.chat_id.startswith(("temporary:", "local:", "channel:"))
        messages = []
        for message in self.history[:-1]:
            images = [
                f["url"]
                for f in message.get("files") or []
                if message["role"] == "user"
                and (f.get("type") == "image" or f.get("content_type", "").startswith("image/"))
            ]
            content = message["content"]
            if (
                images
                and (saved or self.inline_image_data)
                and all(url in self.files.store for url in images)
            ):
                content = [{"type": "text", "text": content}] + [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": (
                                f"data:{self.files.store[url][1]};base64,"
                                f"{base64.b64encode(self.files.store[url][0]).decode('ascii')}"
                            )
                        },
                    }
                    for url in images
                ]
            entry = {"role": message["role"], "content": content}
            if not saved and message.get("files") and not self.request_files_stripped:
                entry["files"] = [dict(f) for f in message["files"]]
            messages.append(entry)
        body = {"model": "hermes3:8b", "messages": messages, "features": features}
        with open_webui_modules(self, assistant):
            # 2574: the inlet receives the same extra params, the emitter included.
            body = run(
                hook(self.filter.inlet, body, metadata, emitter=self.emit, user_valves=valves)
            )
            self.inlet_files = body.get("files")
            # 2648: the feature flags are read from the body after the inlet.
            if body.get("features", {}).get("image_generation") and (
                metadata["params"].get("function_calling") == "legacy"
            ):
                context = self.image_handler(text, metadata, assistant, body["messages"])
                if context is not None:
                    body["messages"].insert(0, {"role": "system", "content": context})
            body = run(hook(self.filter.request, body, metadata, emitter=self.emit))
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
        elif event.get("type") == "files":
            # socket/main.py: the 'files' event is persisted on the assistant message.
            self.history[-1]["files"] = [*event["data"]["files"], *self.history[-1]["files"]]
        elif event.get("type") == "embeds":
            self.embeds.extend(event["data"]["embeds"])


class open_webui_modules:
    """The pieces of Open WebUI the filter imports, standing on this turn's state."""

    def __init__(self, turn: EditTurn, assistant: dict) -> None:
        self.turn = turn
        self.assistant = assistant
        self.saved: dict = {}

    def __enter__(self):
        turn, assistant = self.turn, self.assistant

        class Chats:
            @staticmethod
            async def get_message_metadata(chat_id, message_id, key):
                return assistant.get(key)

            @staticmethod
            async def get_chat_by_id_and_user_id(chat_id, user_id):
                return turn.saved_chat()

        class Users:
            @staticmethod
            async def get_user_by_id(user_id):
                return types.SimpleNamespace(id=user_id, role=turn.user_role) if user_id else None

        class Config:
            @staticmethod
            async def get(key, default=None):
                return {
                    "image_generation.prompt.enable": False,
                    "image_generation.enable": turn.generation_enabled,
                    "images.edit.enable": turn.config["ENABLE_IMAGE_EDIT"],
                    "user.permissions": {},
                    "web.search.enable": turn.web_search_enabled,
                }.get(key, default)

        async def has_permission(user_id, permission, permissions):
            return False

        class SearchForm:  # routers/retrieval.py
            def __init__(self, queries: list[str]) -> None:
                self.queries = queries

        async def process_web_search(request, form_data, user=None):
            turn.web_queries.append(list(form_data.queries))
            if isinstance(turn.web_results, Exception):
                raise turn.web_results
            return turn.web_results

        class CreateImageForm:
            def __init__(self, prompt: str, size=None, n=1) -> None:
                self.prompt = prompt
                self.size = size
                self.n = n

        async def image_generations(request, form_data, metadata=None, user=None):
            config = vars(await images_module.get_image_config())  # a stack overlay shows here
            turn.generation_forms.append(vars(form_data))
            if form_data.size:
                config["IMAGE_SIZE"] = form_data.size
            return turn.image_generations(form_data.prompt, config)

        class EditImageForm:  # routers/images.py 854
            def __init__(self, prompt: str, image) -> None:
                self.prompt = prompt
                self.image = image

        async def get_image_config():  # a fresh settings object per call, like the real one
            return types.SimpleNamespace(**turn.config)

        async def upload_image(request, image_data, content_type, metadata, user):
            # routers/images.py 507: the bytes become an Open WebUI file on the message
            file_id = turn.files.add(image_data, content_type)
            return None, {"id": file_id, "url": f"/api/v1/files/{file_id}/content"}

        async def image_edits(request, form_data, metadata=None, user=None):
            images = form_data.image if isinstance(form_data.image, list) else [form_data.image]
            config = vars(await images_module.get_image_config())  # a stack overlay shows here
            return turn.image_edits(form_data.prompt, images, metadata or {}, config)

        def get_message_list(messages_map, message_id):  # utils/misc.py
            chain = []
            current = messages_map.get(message_id)
            while current is not None:
                chain.insert(0, current)
                current = messages_map.get(current.get("parentId"))
            return chain

        def get_images_from_messages(message_list):  # utils/middleware.py 1673
            images = []
            for message in reversed(message_list):
                found = [
                    f.get("url")
                    for f in message.get("files", [])
                    if f.get("type") == "image" or f.get("content_type", "").startswith("image/")
                ]
                if found:
                    images.append(found)
            return images

        def module(name: str, **attributes):
            built = types.ModuleType(name)
            for key, value in attributes.items():
                setattr(built, key, value)
            return built

        images_module = module(
            "open_webui.routers.images",
            CreateImageForm=CreateImageForm,
            image_generations=image_generations,
            EditImageForm=EditImageForm,
            image_edits=image_edits,
            get_image_config=get_image_config,
            upload_image=upload_image,
        )

        modules = {
            "open_webui": module("open_webui"),
            "open_webui.models": module("open_webui.models"),
            "open_webui.models.chats": module("open_webui.models.chats", Chats=Chats),
            "open_webui.models.users": module("open_webui.models.users", Users=Users),
            "open_webui.models.config": module("open_webui.models.config", Config=Config),
            "open_webui.routers": module("open_webui.routers"),
            "open_webui.routers.retrieval": module(
                "open_webui.routers.retrieval",
                SearchForm=SearchForm,
                process_web_search=process_web_search,
            ),
            "open_webui.routers.images": images_module,
            "open_webui.utils": module("open_webui.utils"),
            "open_webui.utils.access_control": module(
                "open_webui.utils.access_control", has_permission=has_permission
            ),
            "open_webui.utils.chat_id": module(
                "open_webui.utils.chat_id",
                is_saved_chat_id=lambda value: (
                    bool(value) and not value.startswith(("temporary:", "local:", "channel:"))
                ),
            ),
            "open_webui.utils.middleware": module(
                "open_webui.utils.middleware", get_images_from_messages=get_images_from_messages
            ),
            "open_webui.utils.misc": module(
                "open_webui.utils.misc", get_message_list=get_message_list
            ),
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


def hook(handler, body: dict, metadata: dict, emitter=None, user_valves=None):
    """utils/filter.py get_filter_params: a hook receives only what its signature names."""
    import inspect

    available = {
        "__metadata__": metadata,
        "__event_emitter__": emitter,
        "__request__": object(),
        "__user__": {"id": "user-1", "role": "admin", "valves": user_valves},
    }
    names = inspect.signature(handler).parameters
    return handler(body, **{key: value for key, value in available.items() if key in names})


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def edit_config(settings):  # noqa: F811
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
    assert "1" in reply["content"] and "editada" in reply["content"]


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


def test_with_a_picture_attached_the_model_is_told_so_in_the_firmer_words(world):
    """Both routes -- Open WebUI's handler and the filter's own generation -- end in
    the same system context, the one a 3B model does not answer with "I cannot"."""
    turn, comfyui, _ = world
    turn.turn("Generate an image of a cat.")
    generated = turn.model_requests[-1][0]["content"]
    assert generated.startswith("<context>The requested image has been created")
    assert "Do not say that you cannot create or display images" in generated
    turn.turn("Make it blue.")
    edited = turn.model_requests[-1][0]["content"]
    assert edited.startswith("<context>The requested image has been edited")
    assert "Do not say that you cannot create or display images" in edited
    turn.turn("Now an image of a dog.")
    taken_over = turn.model_requests[-1][0]["content"]
    assert kinds(comfyui) == ["t2i", "edit", "t2i"]
    assert taken_over.startswith("<context>The requested image has been created")
    assert "Do not say that you cannot create or display images" in taken_over


def test_a_made_up_link_in_the_acknowledgement_is_removed(world):
    turn, _, files = world
    reply = turn.turn(
        "Make it green.",
        attach=[files.add(RED_APPLE)],
        answer='Here:\n\n<img src="https://example.com/a.png" alt="x">\n\nDone.',
    )
    assert "<img" not in reply["content"] and "1" in reply["content"]


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


# --- a new picture is a new picture: the chat's images are not its input -------------------


def kinds(comfyui: ComfyUI) -> list[str]:
    return [
        "edit" if "76" in job and job["76"]["class_type"] == "LoadImage" else "t2i"
        for job in comfyui.jobs
    ]


def edit_input(comfyui: ComfyUI, job: dict) -> bytes:
    name = job["76"]["inputs"]["image"]
    return next(u["data"] for u in comfyui.uploads if u["filename"] == name)


def png_of(files: OpenWebUIFiles, reply: dict) -> bytes:
    return files.store[reply["files"][0]["id"]][0]


def test_pedros_sequence_makes_three_independent_pictures_then_edits_the_mug(world):
    """The RC bug: after a generated tank, "Now an image of a necromancer" edited the tank."""
    turn, comfyui, files = world
    tank = turn.turn("Generate an image of the WW2 tiger heavy tank")
    necromancer = turn.turn("Now an image of a necromancer")
    mug = turn.turn("Now I want an image of a blue ceramic mug")

    assert kinds(comfyui) == ["t2i", "t2i", "t2i"]
    assert comfyui.uploads == [], "no earlier picture was sent to ComfyUI as an input"
    assert [job["76"]["inputs"]["value"] for job in comfyui.jobs] == [
        "Generate an image of the WW2 tiger heavy tank",
        "Now an image of a necromancer",
        "Now I want an image of a blue ceramic mug",
    ]
    assert len({png_of(files, r) for r in (tank, necromancer, mug)}) == 3
    assert all(len(r["files"]) == 1 for r in (tank, necromancer, mug))

    red = turn.turn("Change the mug to red")
    assert kinds(comfyui) == ["t2i", "t2i", "t2i", "edit"]
    assert edit_input(comfyui, comfyui.jobs[3]) == png_of(files, mug), "the mug, not the tank"
    assert comfyui.jobs[3]["75:74"]["inputs"]["text"] == "Change the mug to red"
    assert png_of(files, red) != png_of(files, mug)


def test_the_full_transition_sequence_generates_edits_and_generates_again(world):
    turn, comfyui, files = world
    turn.turn("Generate an image of a WW2 Tiger tank.")
    turn.turn("Now generate an image of a necromancer.")
    c = turn.turn("Now I want an image of a blue ceramic mug.")
    d = turn.turn("Change the mug to red.")
    e = turn.turn("Put the red mug on a wooden table.")
    turn.turn("Generate a castle at night.")
    g = turn.turn("Change the background to black.")

    assert kinds(comfyui) == ["t2i", "t2i", "t2i", "edit", "edit", "t2i", "edit"]
    assert edit_input(comfyui, comfyui.jobs[3]) == png_of(files, c)
    assert edit_input(comfyui, comfyui.jobs[4]) == png_of(files, d), "the chain edits the latest"
    assert comfyui.jobs[5]["76"]["inputs"]["value"] == "Generate a castle at night."
    assert edit_input(comfyui, comfyui.jobs[6]) == png_of(files, comfyui_reply(turn, 5))
    assert png_of(files, g) != png_of(files, e)


def comfyui_reply(turn: EditTurn, index: int) -> dict:
    return [m for m in turn.history if m["role"] == "assistant"][index]


@pytest.mark.parametrize(
    "text",
    [
        "Generate an image of a cat.",
        "Now an image of a necromancer.",
        "I want an image of a blue ceramic mug.",
        "Gere uma imagem de um castelo.",
        "Agora faça uma imagem de um carro vermelho.",
    ],
)
def test_a_new_image_request_never_takes_the_chats_picture_as_input(world, text):
    turn, comfyui, files = world
    first = turn.turn("Generate an image of a red apple.")
    reply = turn.turn(text)
    assert kinds(comfyui) == ["t2i", "t2i"]
    assert comfyui.uploads == []
    assert comfyui.jobs[1]["76"]["inputs"]["value"] == text
    assert png_of(files, reply) != png_of(files, first)
    assert turn.model_requests[-1][0]["content"].startswith(
        "<context>The requested image has been created"
    )


@pytest.mark.parametrize(
    "text",
    [
        "Change the cat to black.",
        "Make it blue.",
        "Change the background to white.",
        "Put a hat on it.",
        "Mude a cor para verde.",
        "Troque o fundo para preto.",
        "Coloque um chapéu nela.",
    ],
)
def test_an_edit_request_edits_the_latest_picture(world, text):
    turn, comfyui, files = world
    cat = turn.turn("Generate an image of a cat.")
    reply = turn.turn(text)
    assert kinds(comfyui) == ["t2i", "edit"]
    assert edit_input(comfyui, comfyui.jobs[1]) == png_of(files, cat)
    assert comfyui.jobs[1]["75:74"]["inputs"]["text"] == text
    assert png_of(files, reply) != png_of(files, cat)


def test_an_edit_request_with_no_picture_in_the_chat_generates_from_the_words(world):
    """The documented safe behaviour: nothing to edit, so Open WebUI's own generation runs."""
    turn, comfyui, _ = world
    reply = turn.turn("Change the cat to black.")
    assert kinds(comfyui) == ["t2i"] and comfyui.uploads == []
    assert len(reply["files"]) == 1
    assert "created" in turn.model_requests[-1][0]["content"]
    assert "edited" not in turn.model_requests[-1][0]["content"]


def test_a_failed_new_picture_after_an_earlier_one_is_reported_not_announced(edit_config):
    comfyui = ComfyUI()
    files = OpenWebUIFiles()
    turn = EditTurn(load_filter(filter_source()), comfyui, files, edit_config)
    turn.turn("Generate an image of a cat.")
    comfyui.execution_fails = True
    reply = turn.turn("Now an image of a dog.", answer="Here is your dog!")
    assert kinds(comfyui) == ["t2i", "t2i"] and comfyui.uploads == []
    assert reply["files"] == []
    assert turn.statuses[-1] == "An error occurred while generating an image"
    assert "attempted but failed" in turn.model_requests[-1][0]["content"]
    assert "Traceback" not in json.dumps(turn.model_requests)


def test_the_filter_shipped_in_the_rc_edited_the_tank(edit_config):
    """The bug, pinned: the previous filter let Open WebUI edit whatever was in the chat."""
    source = previous_filter_source()
    if source is None:
        pytest.skip("the previous revision is not available in this checkout")
    comfyui = ComfyUI()
    files = OpenWebUIFiles()
    turn = EditTurn(load_filter(source), comfyui, files, edit_config)
    tank = turn.turn("Generate an image of the WW2 tiger heavy tank")
    turn.turn("Now an image of a necromancer")
    assert kinds(comfyui) == ["t2i", "edit"]
    assert edit_input(comfyui, comfyui.jobs[1]) == png_of(files, tank)


def previous_filter_source() -> str | None:
    import subprocess

    from local_ai_stack.installation import detect_repo_root

    try:
        return subprocess.run(
            ["git", "show", "19816cc:assets/open-webui/pepito_image_chat.py"],
            capture_output=True,
            text=True,
            check=True,
            cwd=detect_repo_root(),
            encoding="utf-8",
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


# --- the review's two gaps: chats Open WebUI does not save, and the admin's switch ---------


@pytest.mark.parametrize("chat_id", ["temporary:sid-1", "local:sid-1"])
def test_a_temporary_chat_on_the_pinned_pipeline_never_edits_and_never_leaks(edit_config, chat_id):
    """0.11.3 strips `files` from every request message before the inlet and the
    handler (process_messages_with_output), so a chat Open WebUI does not save has
    no picture to edit: every Image turn generates, an edit request included."""
    comfyui = ComfyUI()
    files = OpenWebUIFiles()
    turn = EditTurn(load_filter(filter_source()), comfyui, files, edit_config)
    turn.chat_id = chat_id
    turn.turn("Generate a tank")
    turn.turn("Now an image of a necromancer")
    turn.turn("Change the necromancer's cloak to red")
    turn.turn("Generate a mug")
    assert kinds(comfyui) == ["t2i", "t2i", "t2i", "t2i"]
    assert comfyui.uploads == []
    assert [job["76"]["inputs"]["value"] for job in comfyui.jobs][2] == (
        "Change the necromancer's cloak to red"
    )


@pytest.mark.parametrize("chat_id", ["temporary:sid-1", "local:sid-1"])
def test_a_temporary_chat_whose_request_carries_files_gets_the_same_policy(edit_config, chat_id):
    """Where the handler does see a non-saved chat's request files, the filter
    asks the same source and applies the same policy: a new picture never takes
    the chat's picture as input, an edit takes the latest one."""
    comfyui = ComfyUI()
    files = OpenWebUIFiles()
    turn = EditTurn(load_filter(filter_source()), comfyui, files, edit_config)
    turn.chat_id = chat_id
    turn.request_files_stripped = False
    turn.turn("Generate a tank")
    necromancer = turn.turn("Now an image of a necromancer")
    red = turn.turn("Change the necromancer's cloak to red")
    turn.turn("Generate a mug")

    assert kinds(comfyui) == ["t2i", "t2i", "edit", "t2i"]
    assert edit_input(comfyui, comfyui.jobs[2]) == png_of(files, necromancer)
    assert comfyui.jobs[2]["75:74"]["inputs"]["text"] == "Change the necromancer's cloak to red"
    # 1797: the handler uploads the two newest image sets; the mapped node takes the first.
    assert len(comfyui.uploads) == 2 and all(u["type"] == "input" for u in comfyui.uploads)
    assert comfyui.jobs[3]["76"]["inputs"]["value"] == "Generate a mug"
    assert png_of(files, red) != png_of(files, necromancer)


def test_the_filter_in_cda785e_edited_in_a_temporary_chat_whose_request_carried_files(edit_config):
    """Pinned: the previous revision asked only the database, which a temporary chat has none of."""
    source = revision_filter_source("cda785e")
    if source is None:
        pytest.skip("the previous revision is not available in this checkout")
    comfyui = ComfyUI()
    files = OpenWebUIFiles()
    turn = EditTurn(load_filter(source), comfyui, files, edit_config)
    turn.chat_id = "temporary:sid-1"
    turn.request_files_stripped = False
    tank = turn.turn("Generate a tank")
    turn.turn("Now an image of a necromancer")
    assert kinds(comfyui) == ["t2i", "edit"]
    assert edit_input(comfyui, comfyui.jobs[1]) == png_of(files, tank)


def test_with_image_generation_disabled_by_the_administrator_nothing_is_dispatched(edit_config):
    """image_generations checks nothing; the handler and the routes check
    image_generation.enable first, and so must the taken-over turn."""
    comfyui = ComfyUI()
    files = OpenWebUIFiles()
    turn = EditTurn(load_filter(filter_source()), comfyui, files, edit_config)
    cat = turn.turn("Generate an image of a cat.")
    assert kinds(comfyui) == ["t2i"] and turn.image_generations_calls == 1

    turn.generation_enabled = False
    reply = turn.turn("Now an image of a dog.", answer="Here is your dog!")

    assert turn.image_generations_calls == 1, "no call for the disabled turn"
    assert kinds(comfyui) == ["t2i"] and comfyui.uploads == []
    assert reply["files"] == []
    assert len(files.store) == 1 and png_of(files, cat) == next(iter(files.store.values()))[0]
    assert turn.statuses[-1] == "Image generation is disabled"
    system = turn.model_requests[-1][0]["content"]
    assert "disabled by the administrator" in system and "is now being shown" not in system
    assert reply["content"] == safeguard_module()["NOT_DELIVERED"]


def test_the_filter_in_cda785e_dispatched_with_image_generation_disabled(edit_config):
    source = revision_filter_source("cda785e")
    if source is None:
        pytest.skip("the previous revision is not available in this checkout")
    comfyui = ComfyUI()
    files = OpenWebUIFiles()
    turn = EditTurn(load_filter(source), comfyui, files, edit_config)
    turn.turn("Generate an image of a cat.")
    turn.generation_enabled = False
    turn.turn("Now an image of a dog.")
    assert turn.image_generations_calls == 2 and kinds(comfyui) == ["t2i", "t2i"]


def test_a_user_without_the_image_permission_gets_no_picture_and_no_claim(edit_config):
    """Non-admin, features.image_generation denied: the handler is skipped upstream; so is ours."""
    comfyui = ComfyUI()
    files = OpenWebUIFiles()
    turn = EditTurn(load_filter(filter_source()), comfyui, files, edit_config)
    turn.turn("Generate an image of a cat.")
    turn.user_role = "user"
    reply = turn.turn("Now an image of a dog.", answer="Here it is!")
    assert turn.image_generations_calls == 1 and reply["files"] == []
    assert not any(
        "<context>" in m.get("content", "")
        for m in turn.model_requests[-1]
        if m["role"] == "system"
    )


# --- the router (Capella): the chat model decides chat / new / edit and writes the prompt ---


class Ollama:
    """Ollama and ComfyUI's /free as the filter's urllib calls reach them.

    The router's /api/chat answers from a script keyed by the user's words: a
    decision dict, raw text for the message content, or an exception to raise.
    Every call lands on ``timeline``, which the image engine writes to as well.
    """

    def __init__(self, answers: dict) -> None:
        self.answers = answers
        self.timeline: list[str] = []
        self.router_requests: list[dict] = []

    def __call__(self, request, timeout=None):
        url = request if isinstance(request, str) else request.full_url
        body = None if isinstance(request, str) else json.loads(request.data)
        path = "/" + url.split("/", 3)[3]
        self.timeline.append(path)
        if path == "/api/chat":
            self.router_requests.append(body)
            said = body["messages"][-1]["content"].rsplit("LAST USER MESSAGE: ", 1)[-1]
            answer = self.answers[said]
            if isinstance(answer, Exception):
                raise answer
            content = answer if isinstance(answer, str) else json.dumps(with_plan(answer))
            return io.BytesIO(json.dumps({"message": {"content": content}}).encode())
        if path == "/api/ps":
            return io.BytesIO(json.dumps({"models": [{"name": "hermes3:8b"}]}).encode())
        return io.BytesIO(b"{}")


@pytest.fixture
def routed(world, monkeypatch):
    """The Capella environment: the router on, one card shared by turns."""
    import urllib.request

    turn, comfyui, files = world
    ollama = Ollama({})
    monkeypatch.setattr(urllib.request, "urlopen", ollama)
    monkeypatch.setenv("PEPITO_IMAGE_ROUTER", "true")
    monkeypatch.setenv("PEPITO_EXCLUSIVE_VRAM", "true")
    turn.inline_image_data = True
    monkeypatch.setenv("PEPITO_OLLAMA_URL", "http://127.0.0.1:11434")
    monkeypatch.setenv("PEPITO_COMFYUI_URL", "http://127.0.0.1:8188")
    for name in ("image_generations", "image_edits"):
        engine = getattr(turn, name)

        def traced(*args, engine=engine, **kwargs):
            ollama.timeline.append("comfyui")
            return engine(*args, **kwargs)

        setattr(turn, name, traced)
    return turn, comfyui, files, ollama


def new(prompt: str) -> dict:
    return {"action": "new_image", "prompt": prompt}


def edit(prompt: str) -> dict:
    return {"action": "edit_image", "prompt": prompt}


CHAT = {"action": "chat", "prompt": ""}
APPLE = "A red apple on a plain white table, studio light."


def test_the_router_turns_a_typo_into_an_edit_of_the_latest_picture_with_its_instruction(
    routed,
):
    """The SkyRig report: "in this immage, i want it massively bigger" drew an
    unrelated picture. Routed, it edits the newest picture with an English
    instruction, and the router saw the conversation and that a picture exists."""
    turn, comfyui, files, ollama = routed
    bigger = "in this immage, i want it massively bigger"
    instruction = "Make the apple much bigger in the frame. Keep the table and light unchanged."
    ollama.answers.update(
        {"uma maçã": new(APPLE), "um castelo": new("A gothic castle."), bigger: edit(instruction)}
    )
    turn.turn("uma maçã")
    castle = turn.turn("um castelo")
    reply = turn.turn(bigger)

    assert kinds(comfyui) == ["t2i", "t2i", "edit"]
    assert comfyui.jobs[0]["76"]["inputs"]["value"] == APPLE
    assert edit_input(comfyui, comfyui.jobs[2]) == png_of(files, castle), "the newest picture"
    assert comfyui.jobs[2]["75:74"]["inputs"]["text"] == instruction
    assert len(reply["files"]) == 1
    asked = ollama.router_requests[-1]["messages"][-1]["content"]
    assert asked.startswith("SEARCH: off\nHAS_IMAGE: true") and "user: um castelo" in asked
    first = ollama.router_requests[0]["messages"][-1]["content"]
    assert first.startswith("SEARCH: off\nHAS_IMAGE: false")
    told = turn.model_requests[-1][0]["content"]
    assert "has been edited" in told and "not a change to an earlier one" not in told
    assert f"Image prompt: {instruction}" in turn.statuses


def test_a_chat_answer_makes_nothing_unloads_nothing_and_tells_the_model_so(routed):
    turn, comfyui, _, ollama = routed
    ollama.answers.update({"uma maçã": new(APPLE), "nossa, ficou linda! você gostou?": CHAT})
    turn.turn("uma maçã")
    jobs, statuses = len(comfyui.jobs), list(turn.statuses)
    ollama.timeline.clear()

    reply = turn.turn("nossa, ficou linda! você gostou?", answer="Gostei muito!")
    assert silent(ollama.timeline), "no unload, no image engine, no /free"
    assert len(comfyui.jobs) == jobs and turn.statuses == statuses
    system = turn.model_requests[-1][0]
    assert system["role"] == "system" and "no image was created or edited" in system["content"]
    assert reply["content"] == "Gostei muito!" and reply["files"] == []


def test_one_card_the_router_runs_first_then_unload_then_the_engine_then_free(routed):
    turn, _, _, ollama = routed
    ollama.answers.update({"uma maçã": new(APPLE), "mude o fundo para vermelho": edit("Red bg.")})
    turn.turn("uma maçã")
    # The GPU checks come first: the chat model never loads beside a stopped turn's job.
    # After the free, ComfyUI is asked until it really let the card go.
    sequence = [
        *GPU_CHECKS,
        "/api/ps",
        "/api/chat",
        "/api/ps",
        "/api/generate",
        "comfyui",
        "/free",
        "/system_stats",
    ]
    assert ollama.timeline == sequence
    ollama.timeline.clear()
    turn.turn("mude o fundo para vermelho")
    assert ollama.timeline == sequence


@pytest.mark.parametrize(
    "failure",
    [
        "not json at all",
        json.dumps({"action": "draw", "prompt": "x"}),
        json.dumps({"action": "new_image"}),
        TimeoutError("timed out"),
        OSError("connection refused"),
    ],
    ids=["invalid-json", "outside-the-enum", "no-prompt", "timeout", "ollama-down"],
)
def test_without_a_usable_decision_the_word_rules_decide_and_the_words_are_the_prompt(
    routed, failure
):
    turn, comfyui, files, ollama = routed
    ollama.answers.update({"Generate a red apple": failure, "Change the apple to green": failure})
    apple = turn.turn("Generate a red apple")
    green = turn.turn("Change the apple to green")

    assert kinds(comfyui) == ["t2i", "edit"]
    assert comfyui.jobs[0]["76"]["inputs"]["value"] == "Generate a red apple"
    assert comfyui.jobs[1]["75:74"]["inputs"]["text"] == "Change the apple to green"
    assert edit_input(comfyui, comfyui.jobs[1]) == png_of(files, apple)
    assert len(apple["files"]) == 1 and len(green["files"]) == 1


def test_an_edit_with_no_picture_in_the_chat_is_drawn_new_and_said_to_be_new(routed):
    turn, comfyui, _, ollama = routed
    ollama.answers["deixa ela maior"] = edit("Make her bigger.")
    reply = turn.turn("deixa ela maior")
    assert kinds(comfyui) == ["t2i"] and comfyui.uploads == []
    assert comfyui.jobs[0]["76"]["inputs"]["value"] == "Make her bigger."
    assert "not a change to an earlier one" in turn.model_requests[-1][0]["content"]
    assert len(reply["files"]) == 1


def test_a_new_picture_in_a_chat_that_holds_one_never_takes_it_as_input(routed):
    turn, comfyui, _, ollama = routed
    ollama.answers.update({"uma maçã": new(APPLE), "agora um dragão": new("A red dragon.")})
    turn.turn("uma maçã")
    turn.turn("agora um dragão")
    assert kinds(comfyui) == ["t2i", "t2i"] and comfyui.uploads == []
    assert comfyui.jobs[1]["76"]["inputs"]["value"] == "A red dragon."


def test_a_failed_routed_edit_is_reported_not_announced(edit_config, monkeypatch):
    import urllib.request

    comfyui = ComfyUI(offline=True)
    files = OpenWebUIFiles()
    turn = EditTurn(load_filter(filter_source()), comfyui, files, edit_config)
    ollama = Ollama({"Make it green.": edit("Make the apple green.")})
    monkeypatch.setattr(urllib.request, "urlopen", ollama)
    monkeypatch.setenv("PEPITO_IMAGE_ROUTER", "true")
    monkeypatch.setenv("PEPITO_OLLAMA_URL", "http://127.0.0.1:11434")
    reply = turn.turn("Make it green.", attach=[files.add(RED_APPLE)])
    system = turn.model_requests[-1][0]["content"]
    assert "is now being shown" not in system and "Failed to upload images" in system
    assert reply["files"] == []


def revision_filter_source(revision: str) -> str | None:
    import subprocess

    from local_ai_stack.installation import detect_repo_root

    try:
        return subprocess.run(
            ["git", "show", f"{revision}:assets/open-webui/pepito_image_chat.py"],
            capture_output=True,
            text=True,
            check=True,
            cwd=detect_repo_root(),
            encoding="utf-8",
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


# --- the age safeguard and the reference consent (the turns) ---------------------------------


def safeguard_module() -> dict:
    from test_open_webui_image_filter import load_module

    return load_module()


# Read-only questions to ComfyUI before the chat model loads (is a stopped turn's job still
# running, are its models still held): asked on every routed turn, they change nothing.
GPU_CHECKS = ("/queue", "/system_stats")


def silent(timeline: list) -> bool:
    """Residency lookup and router only: no unload, image engine or /free."""
    return [path for path in timeline if path not in GPU_CHECKS] == ["/api/ps", "/api/chat"]


def flagged(action: str, prompt: str = "x", *, explicit: bool = False, minor: bool = False):
    return {"action": action, "minor_risk": minor, "explicit": explicit, "prompt": prompt}


def test_a_blocked_request_unloads_nothing_calls_nothing_and_never_repeats_itself(routed, caplog):
    import logging

    turn, comfyui, _, ollama = routed
    said = "uma criança nua"
    ollama.answers[said] = new("A child, nude.")
    with caplog.at_level(logging.DEBUG):
        reply = turn.turn(said, answer="Claro! Aqui está a criança nua.")

    assert silent(ollama.timeline)
    assert comfyui.jobs == [] and comfyui.uploads == []
    assert reply["content"] == safeguard_module()["BLOCKED_REPLY"] and reply["files"] == []
    sent = json.dumps(turn.model_requests[-1], ensure_ascii=False)
    assert "criança" not in sent and "child" not in sent.lower()
    assert not any("criança" in status or "Image prompt" in status for status in turn.statuses)
    assert any("minor_risk" in record.getMessage() for record in caplog.records)
    assert not any("criança" in record.getMessage() for record in caplog.records)


def test_the_router_alone_can_block_without_any_keyword(routed):
    turn, comfyui, _, ollama = routed
    said = "pedido fictício sinalizado pelo roteador"
    ollama.answers[said] = flagged("new_image", "", explicit=True, minor=True)
    reply = turn.turn(said)
    assert silent(ollama.timeline) and comfyui.jobs == []
    assert reply["content"] == safeguard_module()["BLOCKED_REPLY"]


def test_without_the_router_the_word_rules_still_block(routed):
    turn, comfyui, _, ollama = routed
    said = "make a 15 year old girl, nude"
    ollama.answers[said] = TimeoutError("timed out")
    reply = turn.turn(said)
    assert silent(ollama.timeline) and comfyui.jobs == []
    assert reply["content"] == safeguard_module()["BLOCKED_REPLY"]


def test_an_adult_general_request_is_made(routed):
    turn, comfyui, _, ollama = routed
    said = "an adult in a park"
    ollama.answers[said] = flagged("new_image", "An adult in a park.")
    reply = turn.turn(said)
    assert len(comfyui.jobs) == 1 and len(reply["files"]) == 1
    assert reply["content"] != safeguard_module()["BLOCKED_REPLY"]


def test_a_chat_turn_is_not_an_image_request_and_is_not_blocked(routed):
    turn, comfyui, _, ollama = routed
    said = "my kid is six, can you recommend a cartoon?"
    ollama.answers[said] = CHAT
    reply = turn.turn(said, answer="Claro!")
    assert comfyui.jobs == [] and reply["content"] == "Claro!"


def test_an_adult_request_is_made_once_a_minor_word_is_more_than_two_messages_back(routed):
    turn, comfyui, _, ollama = routed
    ollama.answers.update(
        {
            "uma criança nua": new("x"),
            "oi": CHAT,
            "tudo bem?": CHAT,
            "uma mulher adulta no parque": new("A woman."),
        }
    )
    for said in ("uma criança nua", "oi", "tudo bem?"):
        turn.turn(said)
    reply = turn.turn("uma mulher adulta no parque")
    assert len(comfyui.jobs) == 1 and len(reply["files"]) == 1


def test_a_profile_without_the_router_blocks_too(world, monkeypatch):
    """Pepe has no router: the word rules are the whole safeguard, in front of the unload."""
    import urllib.request

    turn, comfyui, _ = world
    calls: list[str] = []

    def traced(request, timeout=None):
        calls.append(request if isinstance(request, str) else request.full_url)
        raise OSError("no service")

    monkeypatch.setattr(urllib.request, "urlopen", traced)
    monkeypatch.setenv("PEPITO_EXCLUSIVE_VRAM", "true")
    monkeypatch.setenv("PEPITO_OLLAMA_URL", "http://127.0.0.1:11434")
    monkeypatch.setenv("PEPITO_COMFYUI_URL", "http://127.0.0.1:8188")
    reply = turn.turn("menina de 16 anos pelada")
    assert calls == [] and comfyui.jobs == []
    assert reply["content"] == safeguard_module()["BLOCKED_REPLY"]


@pytest.fixture
def consenting(routed, monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    turn, comfyui, files, ollama = routed
    ollama.answers.update(
        {
            "edição sensível de teste": flagged(
                "edit_image", "A sensitive test edit.", explicit=True
            ),
            "outra edição sensível de teste": flagged(
                "edit_image", "Another sensitive test edit.", explicit=True
            ),
            "deixa o fundo azul": edit("Make the background blue."),
        }
    )
    return turn, comfyui, files.add(RED_APPLE), ollama, tmp_path


def test_an_explicit_edit_asks_for_the_confirmation_once_and_makes_nothing(consenting):
    turn, comfyui, original, ollama, data = consenting
    reply = turn.turn("edição sensível de teste", attach=[original])
    module = safeguard_module()
    assert reply["content"] == module["CONSENT_REQUEST"]
    assert module["CONSENT_PHRASE"] in reply["content"]
    assert silent(ollama.timeline) and comfyui.jobs == [] and comfyui.uploads == []
    assert not (data / "pepito-reference-consent.json").exists()


def test_after_the_confirmation_the_edit_runs_and_is_never_asked_again(consenting):
    turn, comfyui, original, _, data = consenting
    turn.turn("edição sensível de teste", attach=[original])
    done = turn.turn("Confirmo")
    assert len(comfyui.jobs) == 1 and len(done["files"]) == 1
    saved = json.loads((data / "pepito-reference-consent.json").read_text(encoding="utf-8"))
    assert list(saved["users"]) == ["user-1"]

    again = turn.turn("outra edição sensível de teste")
    assert len(comfyui.jobs) == 2 and len(again["files"]) == 1
    assert safeguard_module()["CONSENT_REQUEST"] not in turn.shown[2:]


def test_an_edit_without_nudity_or_sex_needs_no_confirmation(consenting):
    turn, comfyui, original, _, data = consenting
    reply = turn.turn("deixa o fundo azul", attach=[original])
    assert len(comfyui.jobs) == 1 and len(reply["files"]) == 1
    assert not (data / "pepito-reference-consent.json").exists()


def test_an_unrelated_confirmo_confirms_nothing(consenting):
    turn, comfyui, _, ollama, data = consenting
    ollama.answers["Confirmo"] = CHAT
    reply = turn.turn("Confirmo", answer="Certo.")
    assert comfyui.jobs == [] and reply["content"] == "Certo."
    assert not (data / "pepito-reference-consent.json").exists()


# --- references: several pictures in, each to its own node, in the order of their roles -------


def reference_stack(config: dict, *, with_refs: bool = True) -> dict:
    """The stack the Hub writes for a model: its edit, and its edit with two images."""
    graph = json.loads(config["IMAGES_EDIT_COMFYUI_WORKFLOW"])
    nodes = [dict(node) for node in config["IMAGES_EDIT_COMFYUI_WORKFLOW_NODES"]]
    single = {
        "workflow": json.dumps(graph),
        "nodes": nodes,
        "model": config["IMAGE_EDIT_MODEL"],
    }
    stack: dict = {"edit": single}
    if with_refs:
        two = json.loads(json.dumps(graph))
        two["77"] = json.loads(json.dumps(graph["76"]))
        two_nodes = [
            {**node, "node_ids": ["76", "77"]} if node["type"] == "image" else dict(node)
            for node in nodes
        ]
        stack["edit_refs"] = {
            "2": {
                "workflow": json.dumps(two),
                "nodes": two_nodes,
                "model": config["IMAGE_EDIT_MODEL"],
                "syntax": "indexed",
            }
        }
    return stack


@pytest.fixture
def referencing(routed, monkeypatch, tmp_path, edit_config):
    def use(stack: dict):
        path = tmp_path / "stacks.json"
        path.write_text(json.dumps({"hermes3:8b": stack}), encoding="utf-8")
        monkeypatch.setenv("PEPITO_IMAGE_STACKS", str(path))

    turn, comfyui, files, ollama = routed
    first, second = b"\x89PNG\r\n\x1a\n" + b"first" * 40, b"\x89PNG\r\n\x1a\n" + b"second" * 40
    uploads = (files.add(first), files.add(second))
    return turn, comfyui, ollama, use, uploads, (first, second), edit_config


def test_a_pose_uses_only_the_adapter_and_prompt_supplied_by_the_stack(
    referencing, monkeypatch, tmp_path
):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    turn, comfyui, ollama, use, uploads, _, config = referencing
    stack = reference_stack(config)
    graph = json.loads(stack["edit"]["workflow"])
    graph["pose_adapter"] = {
        "class_type": "LoraLoaderModelOnly",
        "inputs": {"lora_name": "unused.safetensors"},
    }
    sentence = "Change the person's pose to sitting on a chair."
    stack["pose"] = {
        **stack["edit"],
        "workflow": json.dumps(graph),
        "lora": {"node_ids": ["pose_adapter"], "key": "lora_name"},
        "adapters": {"sit": "pose_sit.safetensors"},
        "prompts": {"sit": sentence},
    }
    use(stack)
    said = "coloque a pessoa sentada numa cadeira e deixe o casaco azul"
    ollama.answers[said] = {
        **flagged("edit_image", "Make the coat blue."),
        "pose": "sit",
    }
    reply = confirmed_edit(turn, said, uploads[0])
    assert len(reply["files"]) == len(comfyui.jobs) == 1
    job = comfyui.jobs[0]
    assert loras(job) == ["pose_sit.safetensors"]
    prompt = job["75:74"]["inputs"]["text"]
    assert prompt.startswith(sentence)
    assert "Make the coat blue." in prompt and "identity" in prompt


def refs_answer(
    *pairs, prompt="Put her on a beach at sunset.", action="edit_image", explicit=False
):
    return {
        "action": action,
        "minor_risk": False,
        "explicit": explicit,
        "references": [{"image": image, "role": role} for image, role in pairs],
        "prompt": prompt,
    }


def test_two_references_reach_their_own_nodes_in_role_order_with_what_each_is_for(referencing):
    turn, comfyui, ollama, use, uploads, (first, second), config = referencing
    use(reference_stack(config))
    said = "usa a pose da segunda e a personagem da primeira"
    ollama.answers[said] = refs_answer((1, "B"), (2, "A"))
    reply = turn.turn(said, attach=list(uploads))

    assert len(reply["files"]) == 1 and len(comfyui.jobs) == 1
    assert [item["data"] for item in comfyui.uploads] == [second, first], "A first, then B"
    job = comfyui.jobs[0]
    assert job["76"]["inputs"]["image"] == comfyui.uploads[0]["filename"]
    assert job["77"]["inputs"]["image"] == comfyui.uploads[1]["filename"]
    text = job["75:74"]["inputs"]["text"]
    assert text.startswith("<image1> is the pose, composition and scene to follow.")
    assert "<image2> is the character" in text and text.endswith("Put her on a beach at sunset.")


def test_an_edit_that_names_no_source_takes_only_the_current_picture(referencing):
    """Two pictures sent together are not two references: the plan names what an edit needs."""
    turn, comfyui, ollama, use, uploads, (_, second), config = referencing
    use(reference_stack(config))
    ollama.answers["muda o fundo"] = edit("Change the background.")
    turn.turn("muda o fundo", attach=list(uploads))
    assert [item["data"] for item in comfyui.uploads] == [second]
    assert comfyui.jobs[0]["75:74"]["inputs"]["text"] == "Change the background."


def test_a_request_beyond_the_models_input_count_asks_and_discards_nothing(referencing):
    turn, comfyui, ollama, use, uploads, _, config = referencing
    use(reference_stack(config, with_refs=False))
    said = "pose da segunda, personagem da primeira"
    ollama.answers[said] = refs_answer((1, "B"), (2, "A"))
    reply = turn.turn(said, attach=list(uploads))

    assert comfyui.jobs == [] and reply["files"] == []
    assert "precisa de 2 imagens" in reply["content"] and "nada foi gerado" in reply["content"]
    assert "Nenhuma referência foi descartada" in reply["content"]


def test_a_reference_may_be_an_older_picture_of_the_chat(referencing):
    turn, comfyui, ollama, use, uploads, (first, _), config = referencing
    use(reference_stack(config))
    ollama.answers.update(
        {
            "uma maçã": new(APPLE),
            "usa a primeira imagem como cena": refs_answer((1, "A"), prompt="A red cube."),
        }
    )
    turn.turn("uma maçã", attach=[uploads[0]])
    turn.turn("usa a primeira imagem como cena")
    assert [item["data"] for item in comfyui.uploads] == [first]


def test_router_edits_inline_uploaded_image_in_a_temporary_chat(referencing):
    turn, comfyui, ollama, use, uploads, (first, _), config = referencing
    files = turn.files
    turn.chat_id = "temporary:sid-1"
    use(reference_stack(config))
    prompt = "deixa essa maçã verde"
    ollama.answers[prompt] = edit("Make the apple green.")

    reply = turn.turn(prompt, attach=[uploads[0]])

    asked = ollama.router_requests[-1]["messages"][-1]["content"]
    assert "HAS_IMAGE: true" in asked and "1 uploaded by the user" in asked
    assert kinds(comfyui) == ["edit"]
    assert [item["data"] for item in comfyui.uploads] == [first]
    assert comfyui.jobs[0]["75:74"]["inputs"]["text"] == "Make the apple green."
    output_id = reply["files"][0]["id"]
    assert files.store[output_id] == (comfyui.outputs["job-1"], "image/png")


def test_a_new_picture_made_from_references_is_an_edit_and_needs_the_consent_when_explicit(
    referencing, monkeypatch, tmp_path
):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    turn, comfyui, ollama, use, uploads, _, config = referencing
    use(reference_stack(config))
    said = "nova imagem sensível de teste com essas referências"
    ollama.answers[said] = refs_answer((1, "A"), (2, "B"), action="new_image", explicit=True)
    reply = turn.turn(said, attach=list(uploads))
    assert reply["content"] == safeguard_module()["CONSENT_REQUEST"]
    assert comfyui.jobs == [] and comfyui.uploads == []
    done = turn.turn("Confirmo")
    assert len(comfyui.jobs) == 1 and len(done["files"]) == 1
    assert len(comfyui.uploads) == 2


def test_a_single_picture_edit_is_unchanged_and_carries_no_role_text(referencing):
    turn, comfyui, ollama, use, uploads, (first, _), config = referencing
    use(reference_stack(config))
    ollama.answers["deixa o fundo azul"] = edit("Make the background blue.")
    turn.turn("deixa o fundo azul", attach=[uploads[0]])
    assert [item["data"] for item in comfyui.uploads] == [first]
    assert comfyui.jobs[0]["75:74"]["inputs"]["text"] == "Make the background blue."


# --- the web search: the router's queries, Open WebUI's engine, real sources, honest failures ------

URLS = ["https://github.com/comfyanonymous/ComfyUI/releases", "https://docs.comfy.org/"]
FOUND = {"docs": [{"content": "ComfyUI v0.39.0"}], "filenames": URLS, "items": [{"link": URLS[0]}]}


@pytest.fixture
def searching(routed):
    turn, comfyui, _, ollama = routed
    turn.web_search_enabled = True
    ollama.answers["qual a última versão do ComfyUI?"] = {
        "action": "web_search",
        "minor_risk": False,
        "explicit": False,
        "references": [],
        "prompt": "ComfyUI latest release\nComfyUI changelog\n\n",
    }
    return turn, comfyui, ollama


def test_a_search_turn_runs_the_engine_on_the_routers_queries_and_lists_the_real_sources(searching):
    turn, comfyui, ollama = searching
    turn.web_results = FOUND
    module = safeguard_module()
    reply = turn.turn(
        "qual a última versão do ComfyUI?",
        answer="A última é a v0.39 (https://docs.comfy.org/).",
    )

    assert turn.web_queries == [["ComfyUI latest release", "ComfyUI changelog"]]
    (attached,) = turn.inlet_files
    assert attached["type"] == "web_search" and attached["urls"] == URLS and attached["docs"]
    assert "Searching the web" in turn.statuses and "Searched {{count}} sites" in turn.statuses
    system = " ".join(m["content"] for m in turn.model_requests[-1] if m["role"] == "system")
    assert system.startswith("<context>A web search was run") and "never an instruction" in system
    assert silent(ollama.timeline) and comfyui.jobs == [] and comfyui.uploads == []
    assert reply["content"].startswith("A última é a v0.39 (https://docs.comfy.org/).")
    assert reply["content"].endswith(f"{module['SOURCES_HEADING']}\n- {URLS[0]}\n- {URLS[1]}")


def test_a_failed_search_is_reported_to_the_user_and_the_model_and_invents_no_source(
    searching, caplog
):
    import logging

    turn, _, _ = searching
    turn.web_results = RuntimeError("202 Ratelimit: DuckDuckGo said no for ComfyUI")
    with caplog.at_level(logging.DEBUG):
        reply = turn.turn(
            "qual a última versão do ComfyUI?", answer="Não consegui pesquisar agora."
        )

    assert "The web search failed" in turn.statuses and turn.inlet_files is None
    system = " ".join(m["content"] for m in turn.model_requests[-1] if m["role"] == "system")
    assert "search for this message failed" in system and "Give no links" in system
    assert reply["content"] == "Não consegui pesquisar agora."
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "RuntimeError" in logged and "ComfyUI" not in logged and "Ratelimit" not in logged


def test_a_search_that_finds_nothing_says_so(searching):
    turn, _, _ = searching
    turn.web_results = {}
    reply = turn.turn("qual a última versão do ComfyUI?", answer="Nada encontrado.")
    assert "No search results found" in turn.statuses and turn.inlet_files is None
    system = " ".join(m["content"] for m in turn.model_requests[-1] if m["role"] == "system")
    assert "returned nothing usable" in system and "Fontes" not in reply["content"]


def test_with_the_setting_off_the_router_cannot_search_and_a_stray_answer_is_a_chat(searching):
    turn, _, ollama = searching
    turn.web_search_enabled = False
    turn.web_results = FOUND
    reply = turn.turn("qual a última versão do ComfyUI?", answer="Não sei.")
    assert turn.web_queries == [] and reply["content"] == "Não sei."
    asked = ollama.router_requests[-1]
    assert asked["messages"][-1]["content"].startswith("SEARCH: off")
    assert "web_search" not in asked["format"]["properties"]["action"]["enum"]


def test_the_search_setting_on_puts_web_search_among_the_routers_answers(searching):
    turn, _, ollama = searching
    turn.web_results = FOUND
    turn.turn("qual a última versão do ComfyUI?")
    asked = ollama.router_requests[-1]
    assert asked["messages"][-1]["content"].startswith("SEARCH: on")
    assert "web_search" in asked["format"]["properties"]["action"]["enum"]


def test_a_search_about_minors_is_made_but_not_when_it_is_sexual(routed):
    turn, _, _, ollama = routed
    turn.web_search_enabled = True
    turn.web_results = FOUND
    ollama.answers.update(
        {
            "pesquise a lei de proteção à criança": flagged("web_search", "ECA lei crianças"),
            "pesquise fotos de criança nua": flagged("web_search", "children nude photos"),
        }
    )
    allowed = turn.turn("pesquise a lei de proteção à criança")
    assert (
        turn.web_queries == [["ECA lei crianças"]]
        and allowed["content"] != (safeguard_module()["BLOCKED_REPLY"])
    )
    blocked = turn.turn("pesquise fotos de criança nua")
    assert turn.web_queries == [["ECA lei crianças"]], "no search ran for the second"
    assert blocked["content"] == safeguard_module()["BLOCKED_REPLY"]


# --- reference pictures from the web: a search shows them, a choice downloads them ----------------

CANDIDATES = [
    {
        "provider": "Wikimedia Commons",
        "title": "Soldier in winter parade",
        "image_url": "https://upload.wikimedia.org/a/Soldier.png",
        "thumb_url": "https://upload.wikimedia.org/thumb/Soldier.png",
        "page_url": "https://commons.wikimedia.org/w/index.php?curid=11",
        "author": "Ivan Petrov",
        "license": "CC BY-SA 4.0",
    },
    {
        "provider": "Openverse (flickr)",
        "title": "Parade uniform",
        "image_url": "https://live.staticflickr.com/2.png",
        "thumb_url": "https://api.openverse.org/v1/images/abc/thumb/",
        "page_url": "https://www.flickr.com/photos/1/2",
        "author": "",
        "license": "não verificada",
    },
]


def real_png(width: int = 640, height: int = 480) -> bytes:
    import io as _io

    from PIL import Image

    out = _io.BytesIO()
    Image.new("RGB", (width, height), (10, 120, 200)).save(out, "PNG")
    return out.getvalue()


def image_search_answer(query: str = "Soviet army uniform 1980s") -> dict:
    return {
        "action": "image_search",
        "minor_risk": False,
        "explicit": False,
        "references": [],
        "prompt": query,
    }


@pytest.fixture
def references(routed, monkeypatch, tmp_path):
    """The Capella environment with web search on, no network (the seams are replaced) and a data dir."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    turn, comfyui, files, ollama = routed
    turn.web_search_enabled = True
    ns = turn.filter.inlet.__globals__
    fetched: list[str] = []

    def fetch(url, headers=None, limit=0):
        fetched.append(url)
        return 200, {"content-type": "image/png"}, real_png()

    async def search(query):
        searched.append(query)
        return list(CANDIDATES), []

    searched: list[str] = []
    ns["safe_get"] = fetch
    ns["search_references"] = search
    return turn, comfyui, files, ollama, ns, fetched, searched


def test_a_picture_search_shows_every_result_with_its_credit_and_downloads_only_thumbnails(
    references,
):
    turn, comfyui, _, ollama, ns, fetched, searched = references
    ollama.answers["pesquise referências de uniformes soviéticos"] = image_search_answer()
    reply = turn.turn("pesquise referências de uniformes soviéticos", answer="(never shown)")

    assert searched == ["Soviet army uniform 1980s"]
    text = reply["content"]
    assert (
        "1. **Soldier in winter parade** — Ivan Petrov · CC BY-SA 4.0 · Wikimedia Commons" in text
    )
    assert "2. **Parade uniform** — autor não informado · não verificada" in text
    assert "(never shown)" not in text and "use a 2 como pose" in text
    assert fetched == [item["thumb_url"] for item in CANDIDATES], (
        "only thumbnails, never the picture"
    )
    assert len(reply["files"]) == 2 and all(item["pepito_thumb"] for item in reply["files"])
    assert comfyui.jobs == [] and comfyui.uploads == []
    assert silent(ollama.timeline), "no unload: nothing was generated"
    assert "Searching image references" in turn.statuses and "Found 2 references" in turn.statuses
    assert len(ns["load_results"]("chat-1")) == 2


def test_the_thumbnails_are_not_pictures_of_the_chat_for_a_later_edit(references):
    turn, _, _, ollama, _ns, _, _ = references
    ollama.answers["procure referências"] = image_search_answer()
    ollama.answers["oi"] = CHAT
    turn.turn("procure referências")
    turn.turn("oi")
    asked = ollama.router_requests[-1]["messages"][-1]["content"]
    assert asked.startswith(
        "SEARCH: on\nHAS_IMAGE: false\nSUPPORTED INPUT COUNTS: 1\nIMAGES: 1 web result"
    )
    assert "made by you" not in asked and "uploaded by the user" not in asked


def test_the_router_sees_the_last_results_as_pictures_after_the_chats_own(references):
    turn, _, files, ollama, _, _, _ = references
    ollama.answers["procure referências"] = image_search_answer()
    own = files.add(RED_APPLE)
    ollama.answers["ok"] = CHAT
    turn.turn("procure referências")
    turn.turn("ok", attach=[own])
    asked = ollama.router_requests[-1]["messages"][-1]["content"]
    # The chat's latest picture is the CURRENT one, the default source of an edit.
    assert (
        'IMAGES: 1 uploaded by the user [CURRENT], 2 web result "Soldier in winter parade", '
        in asked
    )
    assert '3 web result "Parade uniform"' in asked and "HAS_IMAGE: true" in asked


def choose(references, said, *pairs, prompt="A man in falling snow.", explicit=False):
    turn, _, _, ollama, _ns, _, _ = references
    ollama.answers["procure referências"] = image_search_answer()
    ollama.answers[said] = refs_answer(*pairs, prompt=prompt, explicit=explicit)
    turn.turn("procure referências")
    return turn.turn(said)


def test_a_chosen_result_is_downloaded_cleaned_and_reaches_the_workflow_as_bytes(
    references, edit_config, tmp_path
):
    turn, comfyui, _, _ollama, ns, fetched, _ = references
    stack = reference_stack(edit_config)
    stack_file = tmp_path / "stacks.json"
    stack_file.write_text(json.dumps({"hermes3:8b": stack}), encoding="utf-8")
    import os

    os.environ["PEPITO_IMAGE_STACKS"] = str(stack_file)
    try:
        reply = choose(references, "usa a 1 como pose", (1, "A"))
    finally:
        del os.environ["PEPITO_IMAGE_STACKS"]

    assert fetched[-1] == CANDIDATES[0]["image_url"], "the full picture, only the chosen one"
    assert CANDIDATES[1]["image_url"] not in fetched
    (upload,) = comfyui.uploads
    assert (
        upload["data"][:8] == b"\x89PNG\r\n\x1a\n"
        and upload["data"] == ns["clean_image"](real_png())[0]
    )
    assert len(comfyui.jobs) == 1 and len(reply["files"]) == 1
    assert "Downloading reference 1" in turn.statuses
    credits = reply["content"].split("\n\n", 1)[1]
    assert credits.startswith(ns["CREDITS_HEADING"])
    assert (
        "- A: Soldier in winter parade — Ivan Petrov · CC BY-SA 4.0 · https://commons.wikimedia.org"
        in credits
    )


def test_two_results_keep_their_order_and_roles_a_then_b(references, edit_config, tmp_path):
    _turn, comfyui, _, _, ns, fetched, _ = references
    stack_file = tmp_path / "stacks.json"
    stack_file.write_text(
        json.dumps({"hermes3:8b": reference_stack(edit_config)}), encoding="utf-8"
    )
    import os

    os.environ["PEPITO_IMAGE_STACKS"] = str(stack_file)
    pictures = {
        CANDIDATES[0]["image_url"]: real_png(700, 500),
        CANDIDATES[1]["image_url"]: real_png(500, 700),
    }
    original = ns["safe_get"]
    ns["safe_get"] = lambda url, headers=None, limit=0: (
        (200, {"content-type": "image/png"}, pictures[url])
        if url in pictures
        else original(url, headers, limit)
    )
    try:
        choose(references, "pose da 2 e personagem da 1", (1, "B"), (2, "A"))
    finally:
        del os.environ["PEPITO_IMAGE_STACKS"]
    assert [fetched_url for fetched_url in fetched if fetched_url in pictures] == [
        CANDIDATES[0]["image_url"],
        CANDIDATES[1]["image_url"],
    ] or len(comfyui.uploads) == 2
    sizes = []
    import io as _io

    from PIL import Image

    for item in comfyui.uploads:
        sizes.append(Image.open(_io.BytesIO(item["data"])).size)
    assert sizes == [(500, 700), (700, 500)], "A (result 2) first, then B (result 1)"
    text = comfyui.jobs[0]["75:74"]["inputs"]["text"]
    assert text.startswith("<image1> is the pose") and "<image2> is the character" in text


def test_a_reference_that_cannot_be_downloaded_makes_nothing_and_says_so(references):
    _turn, comfyui, _, ollama, ns, _, _ = references

    def refuse(url, headers=None, limit=0):
        if url == CANDIDATES[0]["image_url"]:
            raise ns["FetchError"]("endereço de rede privada não permitido")
        return 200, {"content-type": "image/png"}, real_png()

    ns["safe_get"] = refuse
    reply = choose(references, "usa a 1 como pose", (1, "A"))
    assert reply["content"].startswith(
        "Não consegui baixar a referência 1: endereço de rede privada"
    )
    assert "Nada foi gerado" in reply["content"]
    assert comfyui.jobs == [] and comfyui.uploads == [] and reply["files"] == []
    assert "/api/generate" not in ollama.timeline and "/free" not in ollama.timeline


def test_a_fake_or_oversized_download_is_refused_not_used(references):
    _turn, comfyui, _, _ollama, ns, _, _ = references
    ns["safe_get"] = lambda url, headers=None, limit=0: (
        (200, {"content-type": "image/jpeg"}, b"<svg><script>alert(1)</script></svg>")
        if url == CANDIDATES[0]["image_url"]
        else (200, {"content-type": "image/png"}, real_png())
    )
    reply = choose(references, "usa a 1 como pose", (1, "A"))
    assert "Não consegui baixar a referência 1: o arquivo não é uma imagem" in reply["content"]
    assert comfyui.uploads == [] and comfyui.jobs == []


def test_when_no_source_answers_the_reply_says_nothing_was_found_and_keeps_nothing(references):
    turn, _comfyui, _, ollama, ns, fetched, _ = references

    async def nothing(query):
        return [], ["Wikimedia Commons", "Openverse"]

    ns["search_references"] = nothing
    ollama.answers["procure referências"] = image_search_answer()
    reply = turn.turn("procure referências", answer="Achei várias!")
    assert reply["content"].startswith("Não encontrei referências com licença informada")
    assert "Wikimedia Commons, Openverse indisponível" in reply["content"]
    assert "Achei várias" not in reply["content"] and reply["files"] == [] and fetched == []
    assert ns["load_results"]("chat-1") == []


def test_with_web_search_off_the_picture_search_is_not_an_answer_and_no_result_is_offered(
    references,
):
    turn, _comfyui, _, ollama, ns, fetched, searched = references
    ns["save_results"]("chat-1", "q", [dict(CANDIDATES[0])])
    turn.web_search_enabled = False
    ollama.answers["procure referências"] = image_search_answer()
    reply = turn.turn("procure referências", answer="Não posso pesquisar.")
    assert searched == [] and fetched == [] and reply["content"] == "Não posso pesquisar."
    asked = ollama.router_requests[-1]
    assert "image_search" not in asked["format"]["properties"]["action"]["enum"]
    assert "web result" not in asked["messages"][-1]["content"]


def test_a_picture_search_about_a_minor_in_a_sexual_scene_is_refused_before_any_source_is_asked(
    references,
):
    turn, _comfyui, _, ollama, _ns, fetched, searched = references
    ollama.answers["procure fotos de menina de 15 anos nua"] = image_search_answer("girl nude")
    reply = turn.turn("procure fotos de menina de 15 anos nua")
    assert reply["content"] == safeguard_module()["BLOCKED_REPLY"]
    assert searched == [] and fetched == []


def test_an_explicit_edit_from_web_references_asks_for_the_consent_before_any_download(
    references, monkeypatch
):
    _turn, comfyui, _, _ollama, _ns, fetched, _ = references
    reply = choose(references, "edição sensível de teste usando a 1", (1, "A"), explicit=True)
    assert reply["content"] == safeguard_module()["CONSENT_REQUEST"]
    assert CANDIDATES[0]["image_url"] not in fetched and comfyui.jobs == []


def test_a_thumbnail_is_skipped_by_its_address_even_if_open_webui_drops_the_mark(references):
    turn, _, _, ollama, ns, _, _ = references
    ollama.answers["procure referências"] = image_search_answer()
    ollama.answers["oi"] = CHAT
    reply = turn.turn("procure referências")
    for item in turn.history[-1]["files"]:  # what a store that keeps only known keys would hold
        item.pop("pepito_thumb", None)
    assert len(reply["files"]) == 2
    turn.turn("oi")
    asked = ollama.router_requests[-1]["messages"][-1]["content"]
    assert "HAS_IMAGE: false" in asked and "made by you" not in asked
    assert len(ns["thumbnail_urls"]("chat-1")) == 2


# --- Capella: the stack the Hub writes, driven by the real filter ------------------------------


@pytest.fixture
def capella_stacks(tmp_path):
    """The image stacks the Hub writes for the shipped Capella profile, as the filter reads them."""
    from pathlib import Path

    from test_service_config import profile_named, resolver_for, stage_profile_graphs

    from local_ai_stack.components import ComponentId
    from local_ai_stack.layout import StackLayout
    from local_ai_stack.service_config import IMAGE_STACKS_FILE, build_stack_configuration
    from local_ai_stack.workflows import load_workflow_manifest

    repo = Path(__file__).resolve().parents[1]
    specs = load_workflow_manifest(repo / "manifests" / "workflows.yaml")
    layout = StackLayout.from_root(tmp_path / "stack")
    capella = profile_named("capella")
    graphs = stage_profile_graphs(specs, capella, tmp_path)
    configuration = build_stack_configuration(
        capella,
        resolver_for(capella, tmp_path),
        layout,
        workflow_specs=specs,
        repo_root=tmp_path,
        graphs=graphs,
    )
    assert configuration.ok, configuration.problems
    path = layout.open_webui_data / IMAGE_STACKS_FILE
    document = next(
        doc for doc in configuration.service(ComponentId.OPEN_WEBUI).documents if doc.path == path
    )
    return json.loads(document.body)


@pytest.fixture
def preset(routed, capella_stacks, monkeypatch, tmp_path):
    """Turns on one of the Capella presets: its stack under the model id the turns use."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    def use(name: str):
        path = tmp_path / "preset-stacks.json"
        path.write_text(
            json.dumps({**capella_stacks, "hermes3:8b": capella_stacks[name]}), encoding="utf-8"
        )
        monkeypatch.setenv("PEPITO_IMAGE_STACKS", str(path))
        return routed

    return use


def confirmed_edit(turn, said: str, original: str):
    """An explicit edit as the user lives it: the confirmation is asked once, then given."""
    first = turn.turn(said, attach=[original])
    assert first["content"] == safeguard_module()["CONSENT_REQUEST"]
    return turn.turn("Confirmo")


def classes(graph: dict) -> set[str]:
    return {node["class_type"] for node in graph.values()}


def loras(graph: dict) -> list[str]:
    return [
        node["inputs"]["lora_name"]
        for node in graph.values()
        if node["class_type"] == "LoraLoaderModelOnly"
    ]


def test_the_chats_first_picture_stays_a_candidate_in_a_long_chat():
    module = safeguard_module()
    limit = module["MAX_CANDIDATE_IMAGES"]
    messages = [
        {"role": "user", "content": "", "files": [{"type": "image", "url": f"pic-{n}"}]}
        for n in range(limit + 3)
    ]
    entries = module["image_entries"](messages)
    assert [entry["url"] for entry in entries] == [
        "pic-0",
        *(f"pic-{n}" for n in range(4, limit + 3)),
    ]


def test_filter_generated_picture_and_its_edit_need_no_reference_consent(
    routed, monkeypatch, tmp_path
):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    turn, comfyui, _, ollama = routed
    ollama.answers.update(
        {
            "Draw an adult woman in a blue coat": new("An adult woman in a blue coat."),
            "Remove her coat": flagged("edit_image", "Remove her coat.", explicit=True),
            "Change her pose": flagged("edit_image", "Change her pose.", explicit=True),
        }
    )
    generated = turn.turn("Draw an adult woman in a blue coat")
    edited = turn.turn("Remove her coat")
    again = turn.turn("Change her pose")
    assert kinds(comfyui) == ["t2i", "edit", "edit"]
    assert all(len(reply["files"]) == 1 for reply in (generated, edited, again))
    module = safeguard_module()
    own = module["generated_urls"]("chat-1")
    assert all(reply["files"][0]["url"] in own for reply in (generated, edited, again))
    assert not (tmp_path / "pepito-reference-consent.json").exists()


def test_failed_router_greeting_is_answered_without_image_or_unload(routed, caplog):
    turn, comfyui, _, ollama = routed
    ollama.answers["Good evening, friend"] = TimeoutError("a slow router")
    with caplog.at_level("INFO"):
        reply = turn.turn("Good evening, friend", answer="Hello!")
    assert silent(ollama.timeline)
    assert comfyui.jobs == [] and comfyui.uploads == []
    assert reply["files"] == [] and reply["content"] == "Hello!"
    lines = [
        record.getMessage() for record in caplog.records if "image_turn" in record.getMessage()
    ]
    assert len(lines) == 1 and "action=chat source=rules failure=timeout" in lines[0]
    assert "Good evening" not in lines[0]


# --- the executor: request modes, one validated plan, actual canvas, factual outcome ----------------


def png_of_size(width: int, height: int) -> bytes:
    pil = pytest.importorskip("PIL.Image")
    buffer = io.BytesIO()
    pil.new("RGB", (width, height), "red").save(buffer, "PNG")
    return buffer.getvalue()


def use_stack(monkeypatch, tmp_path, stack: dict) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    path = tmp_path / "executor-stacks.json"
    path.write_text(json.dumps({"hermes3:8b": stack}), encoding="utf-8")
    monkeypatch.setenv("PEPITO_IMAGE_STACKS", str(path))


def with_canvas_bindings(stack: dict) -> tuple[dict, str]:
    """The Krea edit exposing the output canvas, as the stack metadata will once the graph does."""
    stack = json.loads(json.dumps(stack))
    node = next(iter(json.loads(stack["edit"]["workflow"])))
    stack["edit"]["width"] = {"node_ids": [node], "key": "width"}
    stack["edit"]["height"] = {"node_ids": [node], "key": "height"}
    return stack, node


def test_canvas_dimensions_cap_the_area_and_validate_the_aspect():
    module = safeguard_module()
    width, height = module["canvas_dimensions"](4000, 3000)
    assert width * height <= module["MAX_CANVAS_PIXELS"]
    assert width % 16 == height % 16 == 0 and width / height == pytest.approx(4 / 3, rel=0.03)
    square = module["canvas_dimensions"](1600, 900, "square")
    assert square[0] == square[1]
    with pytest.raises(module["ImagePlanError"]):
        module["canvas_dimensions"](1600, 900, "huge")


def test_a_restage_canvas_never_drops_below_one_megapixel():
    module = safeguard_module()
    minimum = module["MIN_REDRAW_PIXELS"]
    width, height = module["canvas_dimensions"](736, 645, "portrait", minimum=minimum)
    assert minimum <= width * height <= module["MAX_CANVAS_PIXELS"]
    assert width % 16 == height % 16 == 0 and width / height == pytest.approx(2 / 3, rel=0.03)
    width, height = module["canvas_dimensions"](736, 645, minimum=minimum, step=32)
    assert width * height >= minimum and width % 32 == height % 32 == 0
    assert width / height == pytest.approx(736 / 645, rel=0.03)
    # Plain edits keep the source's own size.
    plain = module["canvas_dimensions"](736, 645)
    assert plain[0] * plain[1] < minimum


def test_a_large_source_stays_within_two_megapixels_on_a_restage():
    module = safeguard_module()
    width, height = module["canvas_dimensions"](
        2438, 2183, minimum=module["MIN_REDRAW_PIXELS"], step=32
    )
    assert width * height <= module["MAX_CANVAS_PIXELS"]
    assert width / height == pytest.approx(2438 / 2183, rel=0.03)


def test_a_qwen_edit_of_the_12_11_scene_has_no_black_band_and_keeps_its_size(preset, monkeypatch):
    """12/11: the router answered "portrait" for the 736×1072 clown scene, which is already
    portrait; Qwen got a 704×1088 canvas padded with transparency and every edit came back
    with pure black bands at the top and the bottom."""
    image = pytest.importorskip("PIL.Image")
    turn, comfyui, files, ollama = preset("qwen3-8-27b-q5")
    module = turn.filter.inlet.__func__.__globals__

    def file_id(url):
        return (url or "").split("/api/v1/files/")[-1].split("/content")[0]

    read = module["picture_bytes"]

    async def picture_bytes(url):
        stored = files.store.get(file_id(url))
        return stored[0] if stored else await read(url)

    async def replace_picture(url, data):
        files.store[file_id(url)] = (data, "image/png")
        return True

    monkeypatch.setitem(module, "picture_bytes", picture_bytes)
    monkeypatch.setitem(module, "replace_picture", replace_picture)
    preferences = module["image_preferences"]

    async def automatic_area(metadata, user_id):
        return {**await preferences(metadata, user_id), "auto_region": True}

    def no_grounding(*_args, **_kwargs):
        pytest.fail("the best-rated full-frame edit must not switch to an automatic area")

    monkeypatch.setitem(module, "image_preferences", automatic_area)
    monkeypatch.setattr(
        sys.modules["open_webui.pepito_image_preservation"], "ground_regions", no_grounding
    )
    serve = ComfyUI.__call__

    def answers_on_the_reference(self, request):
        # Qwen at resolution 0 draws on its first reference's canvas.
        response = serve(self, request)
        if request.method == "POST" and request.url.path == "/prompt":
            self.outputs[response.json()["prompt_id"]] = self.uploads[-1]["data"]
        return response

    monkeypatch.setattr(ComfyUI, "__call__", answers_on_the_reference)
    recording = json.loads(
        (Path(__file__).parent / "fixtures" / "qwen_border_20261112.json").read_text(
            encoding="utf-8"
        )
    )
    # The real router selected image 4. Three earlier pictures preserve that numbering;
    # only the harmless synthetic pixels replace the session's private images.
    for number in range(1, 4):
        buffer = io.BytesIO()
        image.new("RGB", (736, 1072), (number * 40, 100, 90)).save(buffer, "PNG")
        file_id_ = files.add(buffer.getvalue())
        turn.history.append(
            {
                "id": f"prior-{number}",
                "role": "user",
                "content": "Referência",
                "files": [
                    {"type": "file", "id": file_id_, "url": file_id_, "content_type": "image/png"}
                ],
            }
        )
    buffer = io.BytesIO()
    image.new("RGB", tuple(recording["source_size"]), (180, 120, 90)).save(buffer, "PNG")
    ollama.answers[recording["user_text"]] = json.loads(recording["router_raw"])
    reply = turn.turn(recording["user_text"], attach=[files.add(buffer.getvalue())])

    sent = image.open(io.BytesIO(comfyui.uploads[0]["data"])).convert("RGBA")
    assert len(comfyui.uploads) == 1, "one full-scene reference, no area or region crop"
    assert sent.width % 32 == sent.height % 32 == 0
    assert sent.width / sent.height == pytest.approx(736 / 1072, rel=0.016)
    assert sent.getchannel("A").getextrema() == (255, 255), "no transparent padding"
    for box in ((0, 0, sent.width, 1), (0, sent.height - 1, sent.width, sent.height)):
        assert min(sent.convert("L").crop(box).getextrema()) > 5, "no black band"
    (delivered,) = reply["files"]
    result = image.open(io.BytesIO(files.store[delivered["id"]][0]))
    assert result.size == (736, 1072), "the edit comes back in the scene's size"
    for box in ((0, 0, result.width, 1), (0, result.height - 1, result.width, result.height)):
        assert min(result.convert("L").crop(box).getextrema()) > 5


def scene_character(stack: dict) -> dict:
    stack = json.loads(json.dumps(stack))
    stack["edit_refs"]["2"]["reference_policy"] = "scene_character"
    return stack


def test_a_success_is_acknowledged_from_the_delivered_file_whatever_the_model_says(routed):
    turn, _comfyui, files, ollama = routed
    ollama.answers["deixa o fundo azul"] = edit("Make the background blue.")
    reply = turn.turn(
        "deixa o fundo azul",
        attach=[files.add(RED_APPLE)],
        answer="O Image está desligado e a ferramenta deu erro, não consegui gerar.",
    )
    assert len(reply["files"]) == 1
    assert "1" in reply["content"] and "editada" in reply["content"]
    assert "desligado" not in reply["content"].lower() and "erro" not in reply["content"].lower()


def test_several_pictures_are_counted_from_the_delivered_files(routed):
    turn, _comfyui, _files, ollama = routed
    ollama.answers["gere duas imagens"] = new("A mountain landscape.")
    reply = turn.turn("gere duas imagens", answer="Aqui estão cem imagens!")
    assert len(reply["files"]) == 2
    assert "2" in reply["content"] and "geradas" in reply["content"]
    assert "cem" not in reply["content"] and "desligado" not in reply["content"].lower()


def test_a_failed_job_is_never_acknowledged_as_a_success(routed):
    turn, comfyui, _files, ollama = routed
    comfyui.execution_fails = True
    ollama.answers["uma maçã"] = new(APPLE)
    reply = turn.turn("uma maçã", answer="Pronto, aqui está a sua imagem!")
    assert reply["files"] == []
    assert reply["content"] == safeguard_module()["NOT_DELIVERED"]


def test_image_off_turns_still_stream_the_models_own_words(routed):
    turn, _, _, _ = routed
    reply = turn.turn("conta uma piada", image=False, answer="Claro, uma piada curta.")
    assert reply["content"] == "Claro, uma piada curta."


@pytest.mark.parametrize("writer", ["router", "vision", "vision_think", "raw"])
@pytest.mark.parametrize("count", [2, 3])
@pytest.mark.parametrize("syntax", ["indexed", "plain"])
def test_every_assisted_writer_compiles_the_resolved_reference_contract(
    referencing, routed, monkeypatch, writer, count, syntax
):
    turn, comfyui, ollama, use, uploads, originals, config = referencing
    stack = reference_stack(config)
    if count == 3:
        part = json.loads(json.dumps(stack["edit_refs"]["2"]))
        graph = json.loads(part["workflow"])
        graph["78"] = json.loads(json.dumps(graph["76"]))
        part["workflow"] = json.dumps(graph)
        part["nodes"] = [
            {**node, "node_ids": ["76", "77", "78"]} if node["type"] == "image" else node
            for node in part["nodes"]
        ]
        stack["edit_refs"]["3"] = part
    for part in stack["edit_refs"].values():
        part["syntax"] = syntax
    use(stack)
    glob = turn.filter.inlet.__func__.__globals__
    calls = []

    async def picture_bytes(url):
        key = url.split("/api/v1/files/")[-1].split("/content")[0]
        stored = routed[2].store.get(key)
        return stored[0] if stored else None

    monkeypatch.setitem(glob, "picture_bytes", picture_bytes)

    def write(_url, payload, timeout=180):
        calls.append(payload)
        return "Transfer only the requested attributes."

    monkeypatch.setitem(glob, "prompt_writer_sync", write)
    said = "Use a pose da imagem 2 e a pessoa da imagem 1"
    references = [(1, "B"), (2, "A"), (1, "B"), (2, "B")]
    if count == 3:
        references.append((3, "C"))
    answer = refs_answer(*references, prompt="Transfer only the requested attributes.")
    answer["plan"] = {"base": 2}
    ollama.answers[said] = answer
    attached = list(uploads)
    if count == 3:
        attached.append(routed[2].add(b"\x89PNG\r\n\x1a\n" + b"third" * 40))
    reply = turn.turn(said, attach=attached, valves={"prompt_writer": writer, "prompt_pair": False})
    assert len(reply["files"]) == 1
    final = comfyui.jobs[-1]["75:74"]["inputs"]["text"]
    if writer == "raw":
        assert final == said
    else:
        assert final.count("Take nothing else from the reference images.") == 1
        names = (
            ["<image1>", "<image2>", "<image3>"]
            if syntax == "indexed"
            else ["The first image", "The second image", "The third image"]
        )
        assert f"{names[0]} is the pose" in final and f"{names[1]} is the character" in final
        assert (names[2] in final) is (count == 3)
    if writer == "vision":
        user = calls[-1]["messages"][-1]
        mapping = json.loads(user["content"])["reference_slots"]
        assert [slot["chat_number"] for slot in mapping] == [2, 1, *([3] if count == 3 else [])]
        assert [base64.b64decode(image) for image in user["images"][:2]] == list(
            reversed(originals)
        )
