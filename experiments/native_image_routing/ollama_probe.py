"""An Ollama endpoint for Open WebUI that records exactly what crosses the wire.

Two modes, one server:

``--upstream URL``  (proxy)
    Forwards every request to a real Ollama and streams the answer back
    unchanged. For ``/api/chat`` it records, per call: the tool names Open
    WebUI offered, the full message list it sent, the options, the model's raw
    content and tool calls as Ollama parsed them, ``done_reason``, and Ollama's
    own ``prompt_eval_count``/``eval_count``. That is the ground truth for
    token overhead, truncation risk, raw-JSON leaks and empty answers, measured
    at the boundary Open WebUI actually talks to - no Open WebUI code patched.

no upstream  (scripted)
    Answers like a model would, deterministically, so the harness can be
    proven end to end without loading anything: a user turn containing
    ``SELFTEST IMAGE`` with tools offered gets a ``generate_image`` tool call;
    a turn that already has a tool result gets a short acknowledgement;
    anything else gets a plain reply.

Run with the Open WebUI venv's Python (aiohttp is available there):

    python ollama_probe.py --port 31435 --upstream http://127.0.0.1:31434 --log ollama.jsonl
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
from aiohttp import web

SCRIPTED_MODEL = "hermes3:8b"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class OllamaProbe:
    def __init__(self, log_path: Path, upstream: str | None) -> None:
        self.log_path = log_path
        self.upstream = upstream.rstrip("/") if upstream else None
        self.session: aiohttp.ClientSession | None = None

    def record(self, entry: dict) -> None:
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    async def on_startup(self, app: web.Application) -> None:
        if self.upstream:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None))

    async def on_cleanup(self, app: web.Application) -> None:
        if self.session:
            await self.session.close()

    # --- proxy -----------------------------------------------------------------

    async def forward(self, request: web.Request) -> web.StreamResponse:
        assert self.session is not None and self.upstream is not None
        body = await request.read()
        is_chat = request.path in ("/api/chat", "/api/generate")
        payload = json.loads(body) if (is_chat and body) else None
        started = time.time()
        headers = {
            k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")
        }
        async with self.session.request(
            request.method, f"{self.upstream}{request.path_qs}", data=body, headers=headers
        ) as upstream:
            response = web.StreamResponse(status=upstream.status)
            for key, value in upstream.headers.items():
                if key.lower() not in (
                    "content-length",
                    "transfer-encoding",
                    "content-encoding",
                    "connection",
                ):
                    response.headers[key] = value
            await response.prepare(request)
            content, tool_calls, final, raw_error = [], [], {}, None
            buffer = b""
            async for chunk in upstream.content.iter_any():
                await response.write(chunk)
                if not is_chat:
                    continue
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    self._accumulate(line, content, tool_calls, final)
            if is_chat and buffer.strip():
                self._accumulate(buffer, content, tool_calls, final)
            if is_chat and upstream.status >= 400:
                raw_error = final.get("error")
            await response.write_eof()
        if is_chat:
            self._log_chat(
                request.path,
                payload,
                started,
                upstream.status,
                content,
                tool_calls,
                final,
                raw_error,
            )
        return response

    @staticmethod
    def _accumulate(line: bytes, content: list, tool_calls: list, final: dict) -> None:
        line = line.strip()
        if not line:
            return
        try:
            item = json.loads(line)
        except ValueError:
            final.setdefault("unparsed", []).append(line[:500].decode("utf-8", "replace"))
            return
        message = item.get("message") or {}
        if message.get("content"):
            content.append(message["content"])
        if item.get("response"):
            content.append(item["response"])
        if message.get("tool_calls"):
            tool_calls.extend(message["tool_calls"])
        if item.get("done") or item.get("error"):
            final.update({k: v for k, v in item.items() if k != "message"})

    def _log_chat(self, path, payload, started, status, content, tool_calls, final, error) -> None:
        payload = payload or {}
        tools = payload.get("tools") or []
        self.record(
            {
                "t_start": started,
                "t_end": time.time(),
                "path": path,
                "status": status,
                "model": payload.get("model"),
                "stream": payload.get("stream"),
                "options": payload.get("options"),
                "tool_names": [((t or {}).get("function") or {}).get("name") for t in tools],
                "tools": tools,
                "messages": payload.get("messages"),
                "content": "".join(content),
                "tool_calls": tool_calls,
                "done_reason": final.get("done_reason"),
                "prompt_eval_count": final.get("prompt_eval_count"),
                "eval_count": final.get("eval_count"),
                "error": error or final.get("error"),
            }
        )

    # --- scripted ----------------------------------------------------------------

    async def scripted(self, request: web.Request) -> web.StreamResponse:
        path = request.path
        if path == "/api/version":
            return web.json_response({"version": "0.33.2"})
        if path == "/api/tags":
            return web.json_response(
                {
                    "models": [
                        {"name": SCRIPTED_MODEL, "model": SCRIPTED_MODEL, "size": 1, "details": {}}
                    ]
                }
            )
        if path == "/api/ps":
            return web.json_response({"models": []})
        if path == "/api/show":
            return web.json_response(
                {"capabilities": ["completion", "tools"], "details": {}, "model_info": {}}
            )
        if path not in ("/api/chat", "/api/generate"):
            return web.json_response({})
        payload = await request.json()
        started = time.time()
        messages = payload.get("messages") or []
        tools = payload.get("tools") or []
        last_user = next((m for m in reversed(messages) if m.get("role") == "user"), {})
        text = (
            last_user.get("content")
            if isinstance(last_user.get("content"), str)
            else json.dumps(last_user.get("content"))
        )
        has_tool_result = any(m.get("role") == "tool" for m in messages)
        tool_calls: list = []
        if has_tool_result:
            content = "Selftest image acknowledged."
        elif tools and "SELFTEST IMAGE" in (text or ""):
            content = ""
            tool_calls = [
                {
                    "function": {
                        "name": "generate_image",
                        "arguments": {"prompt": "a selftest apple"},
                    }
                }
            ]
        else:
            content = "Selftest chat reply."
        response = web.StreamResponse(status=200, headers={"Content-Type": "application/x-ndjson"})
        await response.prepare(request)
        first = {
            "model": payload.get("model"),
            "created_at": now_iso(),
            "done": False,
            "message": {"role": "assistant", "content": content},
        }
        if tool_calls:
            first["message"]["tool_calls"] = tool_calls
        await response.write((json.dumps(first) + "\n").encode())
        final = {
            "model": payload.get("model"),
            "created_at": now_iso(),
            "done": True,
            "done_reason": "stop",
            "message": {"role": "assistant", "content": ""},
            "prompt_eval_count": 0,
            "eval_count": 1,
        }
        await response.write((json.dumps(final) + "\n").encode())
        await response.write_eof()
        self._log_chat(path, payload, started, 200, [content], tool_calls, final, None)
        return response

    def app(self) -> web.Application:
        app = web.Application(client_max_size=256 * 1024 * 1024)
        handler = self.forward if self.upstream else self.scripted
        app.router.add_route("*", "/{tail:.*}", handler)
        app.on_startup.append(self.on_startup)
        app.on_cleanup.append(self.on_cleanup)
        return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=31435)
    parser.add_argument("--upstream", default=None)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
    args.log.parent.mkdir(parents=True, exist_ok=True)
    web.run_app(
        OllamaProbe(args.log, args.upstream).app(), host=args.host, port=args.port, print=None
    )


if __name__ == "__main__":
    main()
