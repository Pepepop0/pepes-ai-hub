"""A server-side replica of one Open WebUI 0.11.3 composer turn, with evidence.

The boundary this reproduces is the one the 727482a smoke missed: not a helper
call, but the request the browser composer actually sends, handled by the real
Open WebUI middleware and its server-side agent loop.

    composer envelope  (Chat.svelte sendMessageSocket, copied field by field)
      -> POST /api/chat/completions with a live Socket.IO session id
      -> Open WebUI process_chat_payload: features, builtin tools, native FC
      -> Ollama (through ollama_probe.py, which records the exact wire traffic)
      -> tool call parsed by Open WebUI -> real builtin generate_image/edit_image
      -> real image_generations()/image_edits() -> ComfyUI protocol (fake_comfyui.py)
      -> tool result reinjected -> model continuation -> message persisted

Nothing in Open WebUI is patched. The only stand-ins are below the tool
executor (the image engine) and, for the self-test only, the model.

Run everything with the Open WebUI venv's Python (it has aiohttp and
python-socketio). The stack is entirely scratch: its own ports, its own
DATA_DIR/STATIC_DIR/cwd, its own Ollama store. It never talks to a product
root.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Self

import aiohttp
import socketio

HERE = Path(__file__).resolve().parent

MODEL_ID = os.environ.get("HARNESS_MODEL", "hermes3:8b")
ADMIN = {"name": "Pepito Harness", "email": "harness@pepito.local", "password": "harness-pass-1"}

# The capability set the Hub writes (open_webui_models.CAPABILITIES) with
# builtin tools ON: the UI's default capabilities, verbatim.
CAPABILITIES = {
    "file_context": True,
    "vision": True,
    "file_upload": True,
    "web_search": True,
    "image_generation": True,
    "code_interpreter": True,
    "terminal": True,
    "citations": True,
    "status_updates": True,
    "memory": True,
    "builtin_tools": True,
}
# Every category BuiltinTools.svelte offers in 0.11.3, same keys as
# utils/tools.py get_builtin_tools.
BUILTIN_CATEGORIES = (
    "time",
    "user_input",
    "memory",
    "chats",
    "notes",
    "knowledge",
    "files",
    "channels",
    "notifications",
    "web_search",
    "image_generation",
    "code_interpreter",
    "tasks",
    "automations",
    "calendar",
    "subagents",
)
PUBLIC_READ = {"principal_type": "anyone", "principal_id": "*", "permission": "read"}


def variant_meta(variant: str) -> dict:
    """Model record meta for each configuration under test."""
    if variant == "hub-7972472":  # what master ships: builtin tools off entirely
        return {"capabilities": {**CAPABILITIES, "builtin_tools": False}}
    if variant == "all-builtins":  # the UI default before 7972472
        return {"capabilities": dict(CAPABILITIES)}
    if variant == "image-only":  # granular: only the image category, Image on by default
        return {
            "capabilities": dict(CAPABILITIES),
            "builtinTools": {c: False for c in BUILTIN_CATEGORIES if c != "image_generation"},
            "defaultFeatureIds": ["image_generation"],
        }
    raise ValueError(variant)


@dataclass
class Paths:
    scratch: Path

    @property
    def data(self) -> Path:
        return self.scratch / "owui-data"

    @property
    def static(self) -> Path:
        return self.scratch / "owui-static"

    @property
    def logs(self) -> Path:
        return self.scratch / "logs"

    @property
    def comfy_log(self) -> Path:
        return self.logs / "comfyui-calls.jsonl"

    @property
    def probe_log(self) -> Path:
        return self.logs / "ollama-calls.jsonl"

    @property
    def pids(self) -> Path:
        return self.scratch / "harness-pids.json"


@dataclass
class Ports:
    webui: int = 38080
    comfy: int = 38188
    probe: int = 31435
    ollama: int = 31434


# --- stack ------------------------------------------------------------------------


def webui_env(
    paths: Paths, ports: Ports, workflow: str, nodes: str, image_model: str
) -> dict[str, str]:
    """Open WebUI's environment: the Hub's generated open-webui.env, re-pointed at scratch.

    Same keys and values as the product writes (DEFAULT_MODELS, ENABLE_OLLAMA_API,
    ENABLE_IMAGE_GENERATION, IMAGE_GENERATION_ENGINE=comfyui, the Flux2 workflow and
    node map, IMAGE_GENERATION_MODEL), plus HOST/PORT/DATA_DIR as the launcher sets
    them. Two deliberate additions, both after the routing decision: title, tag and
    follow-up generation are off, so a CPU-bound benchmark does not spend three extra
    LLM calls per trial on text nobody measures; and STATIC_DIR is scratch, so this
    instance never writes into a shared venv.
    """
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(("OLLAMA_", "WEBUI_", "OPEN_WEBUI_")):
            env.pop(key)
    env.update(
        {
            "HOST": "127.0.0.1",
            "PORT": str(ports.webui),
            "DATA_DIR": str(paths.data),
            "STATIC_DIR": str(paths.static),
            "OLLAMA_BASE_URL": f"http://127.0.0.1:{ports.probe}",
            "ENABLE_OLLAMA_API": "true",
            "DEFAULT_MODELS": MODEL_ID,
            "ENABLE_IMAGE_GENERATION": "true",
            "IMAGE_GENERATION_ENGINE": "comfyui",
            "COMFYUI_BASE_URL": f"http://127.0.0.1:{ports.comfy}",
            "COMFYUI_WORKFLOW": workflow,
            "COMFYUI_WORKFLOW_NODES": nodes,
            "IMAGE_GENERATION_MODEL": image_model,
            "ENABLE_TITLE_GENERATION": "false",
            "ENABLE_TAGS_GENERATION": "false",
            "ENABLE_FOLLOW_UP_GENERATION": "false",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    return env


def spawn(argv: list[str], *, env: dict | None, cwd: Path, log: Path) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    handle = log.open("ab")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    process = subprocess.Popen(
        argv, env=env, cwd=cwd, stdout=handle, stderr=subprocess.STDOUT, creationflags=flags
    )
    return process.pid


def stack_up(
    paths: Paths,
    ports: Ports,
    *,
    mode: str,
    webui_exe: Path,
    workflow_file: Path,
    nodes_file: Path,
    image_model: str,
) -> dict:
    """Start fake ComfyUI, the Ollama probe (scripted or proxy) and Open WebUI."""
    for directory in (paths.data, paths.static, paths.logs):
        directory.mkdir(parents=True, exist_ok=True)
    python = sys.executable
    pids = {}
    pids["comfy"] = spawn(
        [
            python,
            str(HERE / "fake_comfyui.py"),
            "--port",
            str(ports.comfy),
            "--log",
            str(paths.comfy_log),
        ],
        env=None,
        cwd=paths.scratch,
        log=paths.logs / "fake_comfyui.out",
    )
    probe = [
        python,
        str(HERE / "ollama_probe.py"),
        "--port",
        str(ports.probe),
        "--log",
        str(paths.probe_log),
    ]
    if mode == "proxy":
        probe += ["--upstream", f"http://127.0.0.1:{ports.ollama}"]
    pids["probe"] = spawn(probe, env=None, cwd=paths.scratch, log=paths.logs / "ollama_probe.out")
    workflow = json.dumps(json.loads(workflow_file.read_text(encoding="utf-8")), sort_keys=True)
    nodes = json.dumps(json.loads(nodes_file.read_text(encoding="utf-8"))["nodes"])
    pids["webui"] = spawn(
        [str(webui_exe), "serve", "--host", "127.0.0.1", "--port", str(ports.webui)],
        env=webui_env(paths, ports, workflow, nodes, image_model),
        cwd=paths.data,
        log=paths.logs / "open-webui.out",
    )
    paths.pids.write_text(json.dumps(pids), encoding="utf-8")
    return pids


def stack_down(paths: Paths) -> None:
    if not paths.pids.exists():
        return
    for pid in json.loads(paths.pids.read_text(encoding="utf-8")).values():
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
    paths.pids.unlink()


# --- Open WebUI client ---------------------------------------------------------------


@dataclass
class TurnResult:
    content: str
    prompt: str
    variant: str
    features: dict
    chat_id: str | None
    message_id: str
    t_start: float
    t_end: float
    done: bool
    error: object
    output: list
    files: list
    message: dict
    probe_calls: list = field(default_factory=list)
    comfy_calls: list = field(default_factory=list)
    truncations: list = field(default_factory=list)


class OpenWebUI:
    def __init__(self, base_url: str, paths: Paths) -> None:
        self.base_url = base_url.rstrip("/")
        self.paths = paths
        self.http: aiohttp.ClientSession | None = None
        self.token: str | None = None
        self.user: dict = {}
        self.sio: socketio.AsyncClient | None = None
        self.config: dict = {}

    async def __aenter__(self) -> Self:
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600))
        return self

    async def __aexit__(self, *exc) -> None:
        if self.sio and self.sio.connected:
            await self.sio.disconnect()
        if self.http:
            await self.http.close()

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def request(self, method: str, path: str, **kwargs) -> tuple[int, object]:
        assert self.http is not None
        async with self.http.request(
            method, f"{self.base_url}{path}", headers=self._headers(), **kwargs
        ) as r:
            text = await r.text()
            try:
                return r.status, json.loads(text)
            except ValueError:
                return r.status, text

    async def wait_healthy(self, timeout: float = 240.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                status, _ = await self.request("GET", "/health")
                if status == 200:
                    return
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(2)
        raise TimeoutError("Open WebUI did not become healthy")

    async def login(self) -> None:
        status, body = await self.request(
            "POST",
            "/api/v1/auths/signin",
            json={"email": ADMIN["email"], "password": ADMIN["password"]},
        )
        if status != 200:
            status, body = await self.request("POST", "/api/v1/auths/signup", json=ADMIN)
        if status != 200:
            raise RuntimeError(f"auth failed: {status} {body}")
        self.token = body["token"]
        self.user = body
        _, self.config = await self.request("GET", "/api/config")

    async def connect_socket(self) -> str:
        self.sio = socketio.AsyncClient(reconnection=False)
        await self.sio.connect(
            self.base_url,
            socketio_path="/ws/socket.io",
            transports=["websocket"],
            auth={"token": self.token},
        )
        await self.sio.emit("user-join", {"auth": {"token": self.token}})
        await asyncio.sleep(0.3)
        return self.sio.get_sid("/")

    async def set_model_variant(self, variant: str) -> dict:
        record = {
            "id": MODEL_ID,
            "base_model_id": None,
            "name": MODEL_ID,
            "meta": {"description": f"harness variant {variant}", **variant_meta(variant)},
            "params": {},
            "access_grants": [dict(PUBLIC_READ)],
            "is_active": True,
        }
        status, _ = await self.request("GET", "/api/v1/models/model", params={"id": MODEL_ID})
        if status == 404:
            status, body = await self.request("POST", "/api/v1/models/create", json=record)
        else:
            status, body = await self.request(
                "POST", "/api/v1/models/model/update", params={"id": MODEL_ID}, json=record
            )
        if status != 200:
            raise RuntimeError(f"model record write failed: {status} {body}")
        return await self.model_item()

    async def model_item(self) -> dict:
        status, body = await self.request("GET", "/api/models")
        if status != 200:
            raise RuntimeError(f"/api/models failed: {status} {body}")
        for model in body.get("data", []):
            if model.get("id") == MODEL_ID:
                return model
        raise RuntimeError(f"{MODEL_ID} not listed by Open WebUI")

    async def set_image_edit(self, enabled: bool) -> None:
        status, config = await self.request("GET", "/api/v1/images/config")
        if status != 200:
            raise RuntimeError(f"images config read failed: {status}")
        config["ENABLE_IMAGE_EDIT"] = enabled
        config["IMAGE_EDIT_ENGINE"] = "comfyui"
        config["IMAGES_EDIT_COMFYUI_BASE_URL"] = config.get("COMFYUI_BASE_URL")
        config["IMAGES_EDIT_COMFYUI_WORKFLOW"] = (HERE / "edit_workflow_stub.json").read_text(
            encoding="utf-8"
        )
        config["IMAGES_EDIT_COMFYUI_WORKFLOW_NODES"] = [
            {"type": "prompt", "key": "text", "node_ids": ["6"]},
            {"type": "image", "key": "image", "node_ids": ["10"]},
        ]
        status, body = await self.request("POST", "/api/v1/images/config/update", json=config)
        if status != 200:
            raise RuntimeError(f"images config update failed: {status} {body}")

    async def upload_image(self, png: bytes, name: str) -> dict:
        form = aiohttp.FormData()
        form.add_field("file", png, filename=name, content_type="image/png")
        # MessageInput.svelte uploads images with process=false (no extraction).
        status, body = await self.request(
            "POST", "/api/v1/files/", params={"process": "false"}, data=form
        )
        if status != 200:
            raise RuntimeError(f"upload failed: {status} {body}")
        return body

    def composer_features(self, image_on: bool) -> dict:
        """Chat.svelte getFeatures() for an admin with the given Image toggle."""
        features = self.config.get("features", {})
        result = {
            "voice": False,
            "image_generation": bool(features.get("enable_image_generation")) and image_on,
            "code_interpreter": False,
            "web_search": False,
        }
        if features.get("enable_memories", False):
            result["memory"] = True
        return result

    @staticmethod
    def default_image_on(model_item: dict) -> bool:
        """What Chat.svelte sets imageGenerationEnabled to when a new chat opens."""
        meta = (model_item.get("info") or {}).get("meta") or {}
        return bool(
            meta.get("capabilities", {}).get("image_generation")
        ) and "image_generation" in (meta.get("defaultFeatureIds") or [])

    async def send_turn(
        self,
        model_item: dict,
        content: str,
        *,
        image_on: bool,
        variant: str,
        files: list | None = None,
        timeout: float = 900.0,
    ) -> TurnResult:
        assert self.sio is not None
        now = datetime.now().astimezone()
        user_message_id, response_id = str(uuid.uuid4()), str(uuid.uuid4())
        user_message = {
            "id": user_message_id,
            "parentId": None,
            "childrenIds": [],
            "role": "user",
            "content": content,
            "timestamp": int(time.time()),
            "models": [MODEL_ID],
        }
        if files:
            user_message["files"] = files
        features = self.composer_features(image_on)
        envelope = {
            "stream": True,
            "model": MODEL_ID,
            "params": {},
            "tool_servers": [],
            "features": features,
            "variables": {
                "{{USER_NAME}}": self.user.get("name"),
                "{{USER_EMAIL}}": self.user.get("email") or "Unknown",
                "{{USER_LOCATION}}": "Unknown",
                "{{CURRENT_DATETIME}}": now.strftime("%Y-%m-%d %H:%M:%S"),
                "{{CURRENT_DATE}}": now.strftime("%Y-%m-%d"),
                "{{CURRENT_TIME}}": now.strftime("%H:%M:%S"),
                "{{CURRENT_WEEKDAY}}": now.strftime("%A"),
                "{{CURRENT_TIMEZONE}}": "America/Sao_Paulo",
                "{{USER_LANGUAGE}}": "en-US",
            },
            "chat_variables": {},
            "model_item": model_item,
            "session_id": self.sio.get_sid("/"),
            "id": response_id,
            "parent_id": None,
            "user_message": user_message,
            "background_tasks": {
                "title_generation": True,
                "tags_generation": True,
                "follow_up_generation": True,
            },
        }
        t_start = time.time()
        status, body = await self.request("POST", "/api/chat/completions", json=envelope)
        if status != 200 or not isinstance(body, dict):
            return TurnResult(
                "",
                content,
                variant,
                features,
                None,
                response_id,
                t_start,
                time.time(),
                False,
                {"http": status, "body": body},
                [],
                [],
                {},
            )
        chat_id = body.get("chat_id")
        message: dict = {}
        deadline = time.time() + timeout
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            s, chat = await self.request("GET", f"/api/v1/chats/{chat_id}")
            if s != 200 or not isinstance(chat, dict):
                continue
            message = (((chat.get("chat") or {}).get("history") or {}).get("messages") or {}).get(
                response_id
            ) or {}
            # An upstream refusal (e.g. an image sent to a model without vision)
            # ends the turn with an error and never sets done.
            if message.get("done") or message.get("error"):
                break
        await asyncio.sleep(1.0)  # let the last Ollama/ComfyUI log lines land
        t_end = time.time()
        result = TurnResult(
            content=message.get("content") or "",
            prompt=content,
            variant=variant,
            features=features,
            chat_id=chat_id,
            message_id=response_id,
            t_start=t_start,
            t_end=t_end,
            done=bool(message.get("done")),
            error=message.get("error"),
            output=message.get("output") or [],
            files=message.get("files") or [],
            message=message,
        )
        result.probe_calls = read_jsonl_window(self.paths.probe_log, "t_start", t_start, t_end)
        result.comfy_calls = read_jsonl_window(self.paths.comfy_log, "t", t_start, t_end)
        return result


def read_jsonl_window(path: Path, key: str, t0: float, t1: float) -> list:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if t0 <= float(row.get(key, 0)) <= t1:
            rows.append(row)
    return rows


def truncations_between(ollama_log: Path, t0: float, t1: float) -> list[str]:
    """``truncating input prompt`` warnings Ollama wrote during a turn."""
    if not ollama_log.exists():
        return []
    hits = []
    for line in ollama_log.read_text(encoding="utf-8", errors="replace").splitlines():
        if "truncating input prompt" not in line or not line.startswith("time="):
            continue
        stamp = line[5:].split(" ", 1)[0]
        try:
            moment = datetime.fromisoformat(stamp).timestamp()
        except ValueError:
            continue
        if t0 <= moment <= t1:
            hits.append(line)
    return hits
