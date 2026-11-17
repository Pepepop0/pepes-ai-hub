"""Command line for the native image routing harness.

    python run.py up --mode scripted|proxy     start fake ComfyUI, Ollama probe, Open WebUI
    python run.py down                         stop them
    python run.py selftest                     prove the plumbing with the scripted model
    python run.py bench --variant image-only --suites generate,chat,ambiguous --repeats 1
    python run.py tokens                       real prompt size per variant (needs proxy mode)

Paths default to a scratch directory given by NATIVE_IMAGE_SCRATCH. Use the
Open WebUI venv's Python. Nothing here connects to a product root.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from fake_comfyui import probe_png
from harness import (
    MODEL_ID,
    OpenWebUI,
    Paths,
    Ports,
    stack_down,
    stack_up,
    truncations_between,
)

DEFAULT_WEBUI_EXE = (
    Path(os.environ.get("PEPITO_E2E_ROOT", Path.home() / "Documents" / "pepito-e2e"))
    / "apps"
    / "open-webui"
    / ".venv"
    / "Scripts"
    / "open-webui.exe"
)
IMAGE_MODEL = "flux-2-klein-4b-fp8.safetensors"


def paths_from_env() -> Paths:
    scratch = os.environ.get("NATIVE_IMAGE_SCRATCH")
    if not scratch:
        raise SystemExit("set NATIVE_IMAGE_SCRATCH to a scratch directory")
    return Paths(Path(scratch))


def summarize(result) -> dict:
    """One line of evidence per turn, derived only from what crossed the wire."""
    main_calls = [c for c in result.probe_calls if c.get("path") == "/api/chat"]
    offered = sorted({name for c in main_calls for name in (c.get("tool_names") or [])})
    tool_calls = [tc for c in main_calls for tc in (c.get("tool_calls") or [])]
    prompts = [c for c in result.comfy_calls if c.get("event") == "prompt"]
    return {
        "variant": result.variant,
        "prompt": result.prompt,
        "features.image_generation": result.features.get("image_generation"),
        "done": result.done,
        "error": result.error,
        "llm_calls": len(main_calls),
        "tools_offered": offered,
        "tool_count_offered": max((len(c.get("tool_names") or []) for c in main_calls), default=0),
        "model_tool_calls": [
            ((tc.get("function") or {}).get("name"), (tc.get("function") or {}).get("arguments"))
            for tc in tool_calls
        ],
        "engine_prompts": [(p.get("nonce"), p.get("texts", {}).get("76")) for p in prompts],
        "prompt_eval_counts": [c.get("prompt_eval_count") for c in main_calls],
        "content": result.content[:300],
        "content_full": result.content,
        "output_types": [o.get("type") for o in result.output],
        "files": [f.get("url") or f.get("id") for f in result.files],
        "raw_model_content": [c.get("content") for c in main_calls],
        "done_reasons": [c.get("done_reason") for c in main_calls],
        "probe_errors": [c.get("error") for c in main_calls if c.get("error")],
    }


async def yield_to_root(
    root: Path | None, ports: Ports, *, idle_seconds: float = 180.0, min_free_vram_mib: int = 6500
) -> None:
    """Never compete with someone using a product root on the same machine.

    Before each trial: if any log under ``root/logs`` changed within
    ``idle_seconds``, or the GPU has less free memory than a Hermes load needs,
    unload this harness's model and wait. Yields within one trial of the other
    workload starting, and resumes only after it has been quiet for a while.
    """
    import subprocess
    import urllib.request

    if root is None:
        return

    def ollama(path: str, body: dict | None = None) -> dict:
        request = urllib.request.Request(
            f"http://127.0.0.1:{ports.ollama}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"},
        )
        try:
            return json.loads(urllib.request.urlopen(request, timeout=30).read() or b"{}")
        except (OSError, ValueError):
            return {}

    unloaded = False
    while True:
        logs = list((root / "logs").glob("*.log"))
        quiet_for = time.time() - max((p.stat().st_mtime for p in logs), default=0.0)
        try:
            probe = await asyncio.to_thread(
                subprocess.run,
                ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            free = int(probe.stdout.strip().splitlines()[0])
        except (ValueError, IndexError, OSError, subprocess.TimeoutExpired):
            free = None
        # Our own loaded model is VRAM we would give back, not VRAM somebody else needs.
        mine = sum(m.get("size_vram", 0) for m in ollama("/api/ps").get("models", [])) // (
            1024 * 1024
        )
        available = None if free is None else free + mine
        if quiet_for >= idle_seconds and (available is None or available >= min_free_vram_mib):
            return
        if not unloaded:
            ollama("/api/generate", {"model": MODEL_ID, "keep_alive": 0})
            unloaded = True
        print(
            f"  yielding: root quiet for {quiet_for:.0f}s, available VRAM {available} MiB",
            flush=True,
        )
        await asyncio.sleep(30)


async def bench(
    paths: Paths,
    ports: Ports,
    *,
    variant: str,
    suites: list[str],
    repeats: int,
    out: Path,
    image_edit: bool,
    image_on_override: bool | None,
    limit: int | None,
    ollama_log: Path | None,
    yield_root: Path | None = None,
) -> None:
    from metrics import classify

    from prompts import SUITES

    done_keys = set()
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            done_keys.add((row["repeat"], row["suite"], row["index"]))
    async with OpenWebUI(f"http://127.0.0.1:{ports.webui}", paths) as webui:
        await webui.wait_healthy()
        await webui.login()
        await webui.connect_socket()
        await webui.set_image_edit(image_edit)
        item = await webui.set_model_variant(variant)
        image_on = webui.default_image_on(item) if image_on_override is None else image_on_override
        png = probe_png("attached-" + base64.b16encode(os.urandom(4)).decode().lower())
        for repeat in range(repeats):
            for suite in suites:
                prompts = SUITES[suite][:limit] if limit else SUITES[suite]
                for index, text in enumerate(prompts):
                    if (repeat, suite, index) in done_keys:
                        continue
                    await yield_to_root(yield_root, ports)
                    files = None
                    if suite.startswith("attached"):
                        uploaded = await webui.upload_image(png, "apple.png")
                        files = [
                            {
                                "type": "file",
                                "file": uploaded,
                                "id": uploaded["id"],
                                "url": uploaded["id"],
                                "name": "apple.png",
                                "collection_name": "",
                                "status": "uploaded",
                                "size": len(png),
                                "error": "",
                                "itemId": uploaded["id"],
                                "content_type": "image/png",
                            }
                        ]
                    result = await webui.send_turn(
                        item, text, image_on=image_on, variant=variant, files=files
                    )
                    summary = summarize(result)
                    summary["truncations"] = (
                        truncations_between(ollama_log, result.t_start, result.t_end)
                        if ollama_log
                        else []
                    )
                    row = {
                        "repeat": repeat,
                        "suite": suite,
                        "index": index,
                        "seconds": round(result.t_end - result.t_start, 1),
                        "summary": summary,
                        "classification": classify(summary, suite=suite),
                    }
                    with out.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                    c = row["classification"]
                    print(
                        f"[{variant} r{repeat} {suite} #{index}] {row['seconds']}s tokens={c['prompt_tokens']} "
                        f"img={c['image_calls']} eng={c['engine_runs']} other={c['other_tool_calls']} "
                        f"empty={c['empty']} err={c['error']} trunc={c['truncated']} :: {text[:40]!r} -> "
                        f"{summary['content'][:60]!r}",
                        flush=True,
                    )


async def selftest(paths: Paths, ports: Ports) -> None:
    async with OpenWebUI(f"http://127.0.0.1:{ports.webui}", paths) as webui:
        await webui.wait_healthy()
        await webui.login()
        await webui.connect_socket()
        report = []
        for variant in ("hub-7972472", "all-builtins", "image-only"):
            item = await webui.set_model_variant(variant)
            image_default = webui.default_image_on(item)
            for text, image_on in (("SELFTEST IMAGE please", True), ("hello there", image_default)):
                result = await webui.send_turn(
                    item, text, image_on=image_on, variant=variant, timeout=120
                )
                report.append(summarize(result))
                print(json.dumps(report[-1], ensure_ascii=False))
        (paths.logs / "selftest.json").write_text(
            json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser("up")
    up.add_argument("--mode", choices=("scripted", "proxy"), required=True)
    up.add_argument("--webui-exe", type=Path, default=DEFAULT_WEBUI_EXE)
    sub.add_parser("down")
    sub.add_parser("selftest")
    bench_parser = sub.add_parser("bench")
    bench_parser.add_argument(
        "--variant", required=True, choices=("hub-7972472", "all-builtins", "image-only")
    )
    bench_parser.add_argument("--suites", default="generate,chat,ambiguous")
    bench_parser.add_argument("--repeats", type=int, default=1)
    bench_parser.add_argument("--limit", type=int, default=None)
    bench_parser.add_argument("--out", type=Path, required=True)
    bench_parser.add_argument("--image-edit", action="store_true")
    bench_parser.add_argument("--image-on", choices=("default", "on", "off"), default="default")
    bench_parser.add_argument("--ollama-log", type=Path, default=None)
    bench_parser.add_argument(
        "--yield-root",
        type=Path,
        default=None,
        help="a product root to never compete with (pause while its logs are active)",
    )
    report = sub.add_parser("report")
    report.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args()

    paths, ports = paths_from_env(), Ports()
    if args.command == "up":
        pids = stack_up(
            paths,
            ports,
            mode=args.mode,
            webui_exe=args.webui_exe,
            workflow_file=HERE / "fixtures" / "workflow_t2i.json",
            nodes_file=HERE / "fixtures" / "workflow_t2i_nodes.json",
            image_model=IMAGE_MODEL,
        )
        print(json.dumps(pids))
    elif args.command == "down":
        stack_down(paths)
    elif args.command == "selftest":
        asyncio.run(selftest(paths, ports))
    elif args.command == "bench":
        override = {"default": None, "on": True, "off": False}[args.image_on]
        asyncio.run(
            bench(
                paths,
                ports,
                variant=args.variant,
                suites=args.suites.split(","),
                repeats=args.repeats,
                out=args.out,
                image_edit=args.image_edit,
                image_on_override=override,
                limit=args.limit,
                ollama_log=args.ollama_log,
                yield_root=args.yield_root,
            )
        )
    elif args.command == "report":
        from metrics import aggregate, load

        rows = [row for path in args.files for row in load(path)]
        print(json.dumps(aggregate(rows), indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
