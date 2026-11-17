"""A ComfyUI stand-in that speaks exactly what Open WebUI 0.11.3's client uses.

``open_webui/utils/images/comfyui.py`` opens ``WS /ws?clientId=``, sends
``POST /prompt``, waits for ``{"type": "executing", "data": {"node": null}}``
for that prompt id, reads ``GET /history/{id}`` and downloads each SaveImage
output with ``GET /view``. Editing additionally uploads inputs with
``POST /api/upload/image``. This server implements those endpoints and nothing
else heavy: no model, no GPU, answers in milliseconds.

Every ``POST /prompt`` gets a fresh server-side nonce that appears in no prompt
the model ever sees. It is written to the JSONL log together with the prompt
text Open WebUI put into the workflow, and it is embedded in the returned PNG
(a ``tEXt`` chunk and the filename). A nonce found in the log proves Open
WebUI's real ``generate_image``/``edit_image`` executor ran to the engine
boundary; the same nonce inside the file Open WebUI stored proves the result
travelled back into the chat.

Run with the Open WebUI venv's Python (aiohttp is a dependency of Open WebUI):

    python fake_comfyui.py --port 38188 --log calls.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import struct
import time
import uuid
import zlib
from pathlib import Path

from aiohttp import WSMsgType, web

PROMPT_NODE_TYPES = ("prompt",)


def probe_png(nonce: str) -> bytes:
    """A valid 8x8 grey PNG whose tEXt chunk carries the nonce."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", 8, 8, 8, 0, 0, 0, 0)
    pixels = b"".join(b"\x00" + b"\x80" * 8 for _ in range(8))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"tEXt", b"pepito_nonce\x00" + nonce.encode())
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


class FakeComfyUI:
    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self.sockets: dict[str, set[web.WebSocketResponse]] = {}
        self.pending: dict[str, list[str]] = {}
        self.outputs: dict[str, dict] = {}

    def record(self, entry: dict) -> None:
        entry = {"t": time.time(), **entry}
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # --- websocket ---------------------------------------------------------

    async def ws(self, request: web.Request) -> web.WebSocketResponse:
        client_id = request.query.get("clientId", "")
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        self.sockets.setdefault(client_id, set()).add(socket)
        await socket.send_json(
            {"type": "status", "data": {"status": {"exec_info": {"queue_remaining": 0}}}}
        )
        for prompt_id in self.pending.pop(client_id, []):
            await self._finish(socket, prompt_id)
        try:
            async for message in socket:
                if message.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break
        finally:
            self.sockets.get(client_id, set()).discard(socket)
        return socket

    async def _finish(self, socket: web.WebSocketResponse, prompt_id: str) -> None:
        await socket.send_json({"type": "executing", "data": {"node": "9", "prompt_id": prompt_id}})
        await socket.send_json(
            {"type": "executing", "data": {"node": None, "prompt_id": prompt_id}}
        )

    # --- http ----------------------------------------------------------------

    async def prompt(self, request: web.Request) -> web.Response:
        body = await request.json()
        workflow = body.get("prompt") or {}
        client_id = body.get("client_id", "")
        prompt_id = str(uuid.uuid4())
        nonce = secrets.token_hex(8)
        save_nodes = [
            node_id
            for node_id, node in workflow.items()
            if isinstance(node, dict) and node.get("class_type") in ("SaveImage", "PreviewImage")
        ]
        texts = {
            node_id: node.get("inputs", {}).get("value") or node.get("inputs", {}).get("text")
            for node_id, node in workflow.items()
            if isinstance(node, dict)
            and isinstance(
                node.get("inputs", {}).get("value") or node.get("inputs", {}).get("text"), str
            )
        }
        loads = {
            node_id: node.get("inputs", {}).get("image")
            for node_id, node in workflow.items()
            if isinstance(node, dict) and node.get("class_type") == "LoadImage"
        }
        filename = f"pepito_probe_{nonce}.png"
        self.outputs[prompt_id] = {
            (save_nodes[0] if save_nodes else "9"): {
                "images": [{"filename": filename, "subfolder": "", "type": "output"}]
            }
        }
        self.record(
            {
                "event": "prompt",
                "prompt_id": prompt_id,
                "client_id": client_id,
                "nonce": nonce,
                "texts": texts,
                "load_images": loads,
                "save_nodes": save_nodes,
            }
        )
        sockets = self.sockets.get(client_id)
        if sockets:
            for socket in list(sockets):
                asyncio.get_running_loop().call_later(
                    0.05, lambda s=socket: asyncio.ensure_future(self._finish(s, prompt_id))
                )
        else:
            self.pending.setdefault(client_id, []).append(prompt_id)
        return web.json_response({"prompt_id": prompt_id, "number": 0, "node_errors": {}})

    async def history(self, request: web.Request) -> web.Response:
        prompt_id = request.match_info["prompt_id"]
        outputs = self.outputs.get(prompt_id, {})
        return web.json_response({prompt_id: {"outputs": outputs, "status": {"completed": True}}})

    async def view(self, request: web.Request) -> web.Response:
        filename = request.query.get("filename", "")
        nonce = filename.removeprefix("pepito_probe_").removesuffix(".png")
        self.record({"event": "view", "filename": filename, "nonce": nonce})
        return web.Response(body=probe_png(nonce), content_type="image/png")

    async def upload(self, request: web.Request) -> web.Response:
        reader = await request.multipart()
        name = "upload.png"
        size = 0
        async for part in reader:
            if part.name == "image":
                name = part.filename or name
                size = len(await part.read())
        stored = f"in_{secrets.token_hex(4)}_{name}"
        self.record({"event": "upload", "filename": name, "stored": stored, "bytes": size})
        return web.json_response({"name": stored, "subfolder": "", "type": "input"})

    async def permissive(self, request: web.Request) -> web.Response:
        return web.json_response({})

    def app(self) -> web.Application:
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_get("/ws", self.ws)
        app.router.add_post("/prompt", self.prompt)
        app.router.add_get("/history/{prompt_id}", self.history)
        app.router.add_get("/view", self.view)
        app.router.add_post("/api/upload/image", self.upload)
        app.router.add_post("/upload/image", self.upload)
        app.router.add_get("/{tail:.*}", self.permissive)
        return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=38188)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
    args.log.parent.mkdir(parents=True, exist_ok=True)
    web.run_app(FakeComfyUI(args.log).app(), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
