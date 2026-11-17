#!/usr/bin/env python3
"""Pod lab: SAM 3.1 person masks and Depth Anything 3 depth with the real weights.

Runs the chat's own analysis graphs (workflows/sam3-people-api.json and
workflows/da3-depth-api.json) through a running ComfyUI on every picture of a swap lab
``results`` folder: each case's ``source.png`` (masks + depth) and each ``*_raw.png``
(masks). Records seconds and the VRAM ComfyUI held per stage, and writes the masks and
depth where ``scripts/swap_analysis_compose.py`` (run on the PC) composes them with G2.
Standard library only: it runs in ComfyUI's environment.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def call(url: str, data: bytes | None = None, headers: dict | None = None):
    request = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(request, timeout=600) as response:
        return response.read()


def upload(base: str, path: Path) -> str:
    boundary = uuid.uuid4().hex
    name = f"swaplab_{uuid.uuid4().hex}.png"
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\n'
            "Content-Type: image/png\r\n\r\n"
        ).encode()
        + path.read_bytes()
        + (
            f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="type"\r\n\r\ninput'
            f"\r\n--{boundary}--\r\n"
        ).encode()
    )
    result = json.loads(
        call(
            base + "/upload/image",
            body,
            {"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
    )
    return result["name"]


def vram(base: str) -> int:
    """Bytes of VRAM in use on the first device, by ComfyUI's own report."""
    device = json.loads(call(base + "/system_stats"))["devices"][0]
    return int(device["vram_total"] - device["vram_free"])


def run(base: str, graph: dict, save: str) -> tuple[list[bytes], float, int]:
    started = time.monotonic()
    queued = json.loads(
        call(
            base + "/prompt",
            json.dumps({"prompt": graph, "client_id": uuid.uuid4().hex}).encode(),
            {"Content-Type": "application/json"},
        )
    )
    prompt_id = queued["prompt_id"]
    peak = 0
    while True:
        history = json.loads(call(f"{base}/history/{prompt_id}"))
        peak = max(peak, vram(base))
        item = history.get(prompt_id)
        if item and item.get("outputs") is not None and item.get("status", {}).get("completed"):
            break
        if item and item.get("status", {}).get("status_str") == "error":
            raise RuntimeError(json.dumps(item["status"])[:800])
        time.sleep(0.25)
    seconds = time.monotonic() - started
    images = []
    for image in item["outputs"].get(save, {}).get("images", []):
        images.append(call(f"{base}/view?{urllib.parse.urlencode(image)}"))
    return images, round(seconds, 2), peak


def graph(name: str, image: str) -> tuple[dict, str]:
    loaded = json.loads((REPO / "workflows" / name).read_text(encoding="utf-8"))
    loaded["1"]["inputs"]["image"] = image
    return loaded, next(k for k, v in loaded.items() if v["class_type"] == "SaveImage")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--comfyui", default="http://127.0.0.1:8188")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    report: dict = {}
    for case_dir in sorted(p for p in args.results.iterdir() if (p / "source.png").is_file()):
        target = args.out / case_dir.name
        target.mkdir(exist_ok=True)
        facts: dict = {}
        stages = [("scene", case_dir / "source.png", "sam3-people-api.json")]
        stages += [
            (raw.stem.removesuffix("_raw"), raw, "sam3-people-api.json")
            for raw in sorted(case_dir.glob("*_raw.png"))
        ]
        for label, path, workflow in stages:
            name = upload(args.comfyui, path)
            sam, save = graph(workflow, name)
            masks, seconds, peak = run(args.comfyui, sam, save)
            for index, data in enumerate(masks):
                (target / f"{label}-mask-{index}.png").write_bytes(data)
            facts[f"{label}_people"] = len(masks)
            facts[f"{label}_sam_s"] = seconds
            facts[f"{label}_sam_vram_gb"] = round(peak / 2**30, 2)
            print(case_dir.name, label, "people", len(masks), seconds, "s", flush=True)
        name = upload(args.comfyui, case_dir / "source.png")
        depth, save = graph("da3-depth-api.json", name)
        images, seconds, peak = run(args.comfyui, depth, save)
        (target / "depth.png").write_bytes(images[0])
        facts["depth_s"] = seconds
        facts["depth_vram_gb"] = round(peak / 2**30, 2)
        report[case_dir.name] = facts
        print(case_dir.name, "depth", seconds, "s", flush=True)
    (args.out / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
