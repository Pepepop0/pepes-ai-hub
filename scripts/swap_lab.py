#!/usr/bin/env python3
"""Swap lab: which method puts a character from one picture in the place of another's?

Each case is a scene (whose person is replaced), a character reference, the target's box
and its pose in words. Round 2 (after the 04/11 ratings of round 1):

* ``P_ref`` (preparation, once per case): Qwen-Image 2.1 redraws the reference as the
  character alone on plain gray. Round 1 carried the reference's props and background into
  the scene (the bottle in T2);
* ``S1_qwen_marked``: Qwen with the target outlined in red, the clean reference and the
  pose spelled out; Qwen kept the most detail in round 1 but copied the reference's pose;
* ``S4_prompt``: the original request and reference, without preparation or marking.

The final is the raw output over the scene through an adaptive soft mask: the target's box
joined with where the raw output really changed next to it, grown and feathered, never
over a ``keep_boxes`` area. Round 1's hard box cut the character at its edges. The scene
keeps its aspect: it is scaled, then cropped (not stretched) to a 32 px multiple.

``prepare`` copies the pictures into the results and draws the boxes (``preview.jpg``);
``run`` needs ComfyUI with the Capella models; ``review`` rebuilds ``avaliar-troca.html`` and
``overlay-review`` the balloon page ``avaliar-baloes.html`` (SPEC-205's two compositions).
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import re
import sys
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

# Shared ComfyUI client, metrics and reviewer, loaded by path in the bare ComfyUI venv.
_spec = importlib.util.spec_from_file_location(
    "_swap_lab_common", Path(__file__).with_name("lab_common.py")
)
lab = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = lab
_spec.loader.exec_module(lab)
LabError = lab.LabError

_swap_spec = importlib.util.spec_from_file_location(
    "_swap_lab_pepito_swap",
    Path(__file__).resolve().parents[1] / "assets" / "open-webui" / "pepito_swap.py",
)
swap = importlib.util.module_from_spec(_swap_spec)
sys.modules[_swap_spec.name] = swap
_swap_spec.loader.exec_module(swap)

SCHEMA = "swap-lab-1"
# Qwen builds its latent from the first image: the scene goes in at this bound (32-aligned).
# The chat's swap uses the same bounds (pepito_swap), so what the lab rates is what it runs.
SCENE_PIXELS = swap.SCENE_PIXELS
REFERENCE_PIXELS = swap.REFERENCE_PIXELS
# A raw pixel "changed" past this (0..255, max channel); the mask may grow this far past
# the box (share of the box's larger side), then is feathered by FEATHER of the short side.
CHANGED = 40
GROWTH = 0.25
FEATHER = 1 / 90
PAGE = "avaliar-troca.html"
OVERLAY_PAGE = "avaliar-baloes.html"
# The public Capella uses Qwen; Krea experiments live in the optional pack's private lab.
SWAP_BASE = "S1_qwen_marked"
QWEN_ONE = "qwen-image-2.1-edit-api.json"
QWEN_TWO = "qwen-image-2.1-edit-2-api.json"


@dataclass(frozen=True)
class Method:
    id: str
    label: str
    family: str
    note: str


CLEAN_REF = Method("P_ref", "Referência limpa", "qwen", "preparação: só a personagem, fundo cinza")
METHODS = (
    Method(
        "S1_qwen_marked",
        "Qwen com marcação",
        "qwen",
        "Qwen 2.1: alvo contornado, referência limpa, pose descrita",
    ),
    # Only for a case with ``free_prompt``: the user's own one-sentence request, the scene
    # and the original reference, as a chat edit would send it. No marking or pose text.
    Method("S4_prompt", "Só o pedido", "qwen", "Qwen 2.1: só o pedido do usuário, sem preparo"),
)


def applies(method: Method, case: dict) -> bool:
    if method.id == "S4_prompt":
        return bool(case.get("free_prompt"))
    return True


BY_ID = {method.id: method for method in METHODS}
# Run the public Qwen methods when no --methods is given.
DEFAULT_METHODS = ("S1_qwen_marked", "S4_prompt")


# --- geometry ------------------------------------------------------------------------


def fit_canvas(image: Image.Image, pixels: int = SCENE_PIXELS, step: int = 32) -> Image.Image:
    """Scaled to at most ``pixels`` and center-cropped to ``step`` multiples: no stretch."""
    factor = min(1.0, (pixels / (image.width * image.height)) ** 0.5)
    scaled = image.resize(
        (max(step, round(image.width * factor)), max(step, round(image.height * factor))),
        Image.Resampling.LANCZOS,
    )
    width, height = scaled.width // step * step, scaled.height // step * step
    left, top = (scaled.width - width) // 2, (scaled.height - height) // 2
    return scaled.crop((left, top, left + width, top + height))


def bounded(size: tuple[int, int], pixels: int, step: int) -> tuple[int, int]:
    width, height = size
    factor = min(1.0, (pixels / (width * height)) ** 0.5)
    return (
        max(step, int(width * factor) // step * step),
        max(step, int(height * factor) // step * step),
    )


def box_pixels(box: list[float], size: tuple[int, int]) -> tuple[int, int, int, int]:
    try:
        return swap.box_pixels(box, size)
    except ValueError as exc:
        raise LabError(str(exc)) from exc


def keep_mask(case: dict, size: tuple[int, int]) -> Image.Image:
    """White where the scene must stay exact inside the target's area (bubbles, text)."""
    kept = Image.new("L", size, 0)
    draw = ImageDraw.Draw(kept)
    for box in case.get("keep_boxes") or ():
        x0, y0, x1, y1 = box_pixels(box, size)
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=255)
    return kept


def box_mask(case: dict, size: tuple[int, int]) -> Image.Image:
    mask = Image.new("L", size, 0)
    left, top, right, bottom = box_pixels(case["box"], size)
    ImageDraw.Draw(mask).rectangle((left, top, right - 1, bottom - 1), fill=255)
    return ImageChops.subtract(mask, keep_mask(case, size))


def adaptive_mask(case: dict, scene: Image.Image, raw: Image.Image) -> Image.Image:
    """The box joined with the changed regions that touch it, within GROWTH of it, grown
    and feathered; zero on ``keep_boxes``. Small changes elsewhere (a model redrawing the
    whole frame slightly) stay out."""
    size = scene.size
    raw = lab.fit_to(raw, size)
    diff = ImageChops.difference(scene.convert("RGB"), raw)
    red, green, blue = diff.split()
    peak = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    scale = 8
    small = (max(1, size[0] // scale), max(1, size[1] // scale))
    changed = peak.resize(small, Image.Resampling.BOX).point(lambda v: 255 if v > CHANGED else 0)
    changed = changed.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))
    left, top, right, bottom = box_pixels(case["box"], size)
    reach = int(max(right - left, bottom - top) * GROWTH)
    limit = (
        max(0, (left - reach) // scale),
        max(0, (top - reach) // scale),
        min(small[0], (right + reach) // scale + 1),
        min(small[1], (bottom + reach) // scale + 1),
    )
    inside = (
        left // scale,
        top // scale,
        max(left // scale + 1, right // scale),
        max(top // scale + 1, bottom // scale),
    )
    pixels = changed.load()
    grown = Image.new("L", small, 0)
    out = grown.load()
    queue = deque(
        (x, y)
        for x in range(inside[0], min(inside[2], small[0]))
        for y in range(inside[1], min(inside[3], small[1]))
        if pixels[x, y]
    )
    for x, y in queue:
        out[x, y] = 255
    while queue:
        x, y = queue.popleft()
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if (
                limit[0] <= nx < limit[2]
                and limit[1] <= ny < limit[3]
                and pixels[nx, ny]
                and not out[nx, ny]
            ):
                out[nx, ny] = 255
                queue.append((nx, ny))
    mask = ImageChops.lighter(
        grown.resize(size, Image.Resampling.NEAREST), box_mask({**case, "keep_boxes": []}, size)
    )
    radius = max(3, round(min(size) * FEATHER))
    mask = mask.filter(ImageFilter.MaxFilter(2 * (radius // 2) + 1))
    mask = mask.filter(ImageFilter.GaussianBlur(radius))
    return ImageChops.subtract(mask, keep_mask(case, size))


# --- prompts and graphs ----------------------------------------------------------------


def prompt(case: dict, method: Method) -> str:
    pose = case["pose"]
    if method.id == "S4_prompt":
        return case["free_prompt"]
    if method.id == CLEAN_REF.id:
        return swap.reference_prompt()
    return swap.swap_prompt(
        "marked",
        case["target"],
        case["character_desc"],
        pose,
        case["keep"],
        case["style"],
        case.get("character_style", ""),
    )


def build_graph(case: dict, method: Method, names: dict, canvas: tuple[int, int]) -> dict:
    prefix = f"swap_lab/{case['id']}_{method.id}"
    if method.id == CLEAN_REF.id:
        graph = lab.load_workflow(QWEN_ONE)
        lab._inputs(graph, "470", "LoadImage")["image"] = names["character"]
    else:
        graph = lab.load_workflow(QWEN_TWO)
        lab._inputs(graph, "470", "LoadImage")["image"] = names["scene"]
        lab._inputs(graph, "471", "LoadImage")["image"] = names["character"]
    lab._inputs(graph, "459:474", "TextEncodeQwenImage21")["prompt"] = prompt(case, method)
    lab._inputs(graph, "459:458", "KSampler")["seed"] = case["seed"]
    graph[lab._save_node(graph)]["inputs"]["filename_prefix"] = prefix
    return graph


# --- cases and staging -----------------------------------------------------------------


def _load(path: Path) -> Image.Image:
    picture = lab._open_rgb(path)
    if picture is None:
        raise LabError(f"Missing picture {path}.")
    return picture


def stage(case: dict, base: Path, case_dir: Path) -> dict:
    """Bounded inputs for one case, written under ``case_dir``; returns their paths."""
    case_dir.mkdir(parents=True, exist_ok=True)
    scene = fit_canvas(_load(base / case["scene"]))
    character = _load(base / case["character"])
    character = lab.fit_to(character, bounded(character.size, REFERENCE_PIXELS, 16))
    mask = box_mask(case, scene.size)
    paths = {
        "scene": case_dir / "source.png",
        "character": case_dir / "reference.png",
        "marked": case_dir / "scene_marked.png",
        "mask": case_dir / "mask.png",
        "clean": case_dir / "reference_clean.png",
    }
    scene.save(paths["scene"])
    character.save(paths["character"])
    swap.marked_scene(scene, case["box"]).save(paths["marked"])
    mask.save(paths["mask"])
    return {"paths": paths, "canvas": scene.size, "scene": scene, "mask": mask}


def preview(staged: dict, case: dict) -> Image.Image:
    """The reference beside the scene, darkened where the scene is kept exactly, for
    checking the target's area by eye."""
    scene = Image.open(staged["paths"]["marked"]).convert("RGB")
    kept = Image.blend(scene, Image.new("RGB", scene.size), 0.6)
    scene = Image.composite(scene, kept, staged["mask"])
    reference = Image.open(staged["paths"]["character"]).convert("RGB")
    height = 720
    scene = scene.resize((round(scene.width * height / scene.height), height))
    reference = reference.resize((round(reference.width * height / reference.height), height))
    sheet = Image.new("RGB", (scene.width + reference.width + 16, height), (24, 24, 24))
    sheet.paste(reference, (0, 0))
    sheet.paste(scene, (reference.width + 16, 0))
    return sheet


REQUIRED = {"id", "ask", "style", "scene", "character", "box", "target", "character_desc"}
REQUIRED |= {"keep", "pose", "seed"}


def load_cases(path: Path) -> list[dict]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema") != SCHEMA:
        raise LabError(f"{path} is not a {SCHEMA} cases file.")
    cases = document["cases"]
    for case in cases:
        missing = REQUIRED - set(case)
        if missing or case["style"] not in swap.STYLE_WORDS:
            raise LabError(f"Case {case.get('id')} is incomplete: {sorted(missing) or 'style'}.")
    return cases


# --- review ----------------------------------------------------------------------------


def write_review(out: Path, cases: list[dict], metrics: dict) -> Path:
    """The shared reviewer with the methods each case ran, rated against ``SWAP_BASE``."""
    data = []
    for case in cases:
        records = (metrics.get(case["id"]) or {}).get("variants") or {}
        variants = []
        for method in METHODS:
            record = records.get(method.id)
            if not applies(method, case) or record is None:
                continue
            paths = {
                kind: f"{case['id']}/{method.id}_{kind}.png"
                for kind in ("raw", "final")
                if (out / case["id"] / f"{method.id}_{kind}.png").is_file()
            }
            ok = record.get("status") == "ok" and len(paths) == 2
            variants.append(
                {
                    "id": method.id,
                    "label": method.label,
                    "note": method.note,
                    "status": record.get("status", "not run"),
                    "ok": ok,
                    "base": ok and method.id == SWAP_BASE,
                    "paths": paths if ok else {},
                    "prompt": record.get("prompt") or "",
                    "seconds": record.get("seconds") if ok else None,
                    "changed": record.get("changed_fraction_inside") if ok else None,
                    "outside": record.get("raw_mad_outside") if ok else None,
                    "error": record.get("error", ""),
                }
            )
        data.append(
            {
                "id": case["id"],
                "ask": case["ask"],
                "instruction": f"Pose: {case['pose']}.",
                "source": f"{case['id']}/source.png",
                "references": references(out, case["id"]),
                "variants": variants,
            }
        )
    return lab.review_page(out, PAGE, data, lab="troca", title="Laboratório de troca")


def references(out: Path, case_id: str) -> list[dict]:
    found = []
    for name, text in (("reference.png", "Referência"), ("reference_clean.png", "Limpa")):
        if (out / case_id / name).is_file():
            found.append({"src": f"{case_id}/{name}", "label": text})
    return found


# --- run -------------------------------------------------------------------------------


def prepare(cases_path: Path, out: Path) -> list[Path]:
    previews = []
    for case in load_cases(cases_path):
        staged = stage(case, cases_path.parent, out / case["id"])
        path = out / case["id"] / "preview.jpg"
        preview(staged, case).save(path, quality=88)
        previews.append(path)
    return previews


def good_cases(path: Path | None, cases: list[dict]) -> set[str]:
    """Case ids the user called good in a reviewer export (``_case.good``) or a
    ``{"good": [...]}`` file. A composite review id counts for its case: ``Q<n>``/``K<n>``
    is case number ``n`` with the Qwen/Krea method, and a case is good when its own id is,
    or both of its methods are."""
    if path is None:
        return set()
    text = path.read_text(encoding="utf-8")
    fence = re.search(r"```json\s*(.*?)```", text, re.DOTALL)
    data = json.loads(fence.group(1) if fence else text)
    if isinstance(data, dict) and isinstance(data.get("good"), list):
        marked = {str(item) for item in data["good"]}
    else:
        marked = {
            key
            for key, value in (data or {}).items()
            if isinstance(value, dict) and (value.get("_case") or {}).get("good") is True
        }
    found = set()
    for number, case in enumerate(cases, start=1):
        if case["id"] in marked or {f"Q{number}", f"K{number}"} <= marked:
            found.add(case["id"])
    return found


def run(
    cases_path: Path,
    out: Path,
    methods: list[Method],
    *,
    root: Path | None,
    dry_run: bool,
    comfyui_url: str,
    ollama_url: str,
    only: set[str] | None = None,
    skip: set[str] = frozenset(),
) -> dict:
    """Run ``methods`` on the cases (all, or the ids in ``only``, never those in ``skip``);
    the review lists every case that has results, so a run of new cases keeps the earlier
    ones beside them."""
    every = load_cases(cases_path)
    unknown = sorted((only or set()) - {case["id"] for case in every})
    if unknown:
        raise LabError(f"Unknown cases: {', '.join(unknown)}.")
    cases = [case for case in every if (not only or case["id"] in only) and case["id"] not in skip]
    if skip:
        print(f"Already good, skipped: {', '.join(sorted(skip))}", flush=True)
    staged = {case["id"]: stage(case, cases_path.parent, out / case["id"]) for case in cases}
    metrics = {}
    for case in every:
        previous = out / case["id"] / "metrics.json"
        record = json.loads(previous.read_text(encoding="utf-8")) if previous.is_file() else {}
        if case["id"] in staged:
            record.update(case=case["id"], box=case["box"], seed=case["seed"], pose=case["pose"])
        record.setdefault("variants", {})
        metrics[case["id"]] = record
    comfy = lab.ComfyUI(comfyui_url)
    order = [CLEAN_REF] + [m for m in methods if m.id in ("S1_qwen_marked", "S4_prompt")]
    phases = [("qwen", order)]
    session = {
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "dry_run": dry_run,
        "methods": [m.id for phase in phases for m in phase[1]],
        "jobs": sum(
            1 for phase in phases for method in phase[1] for case in cases if applies(method, case)
        ),
        "frees": [],
    }
    if not dry_run:
        try:
            comfy.check()
        except LabError as error:
            raise LabError(
                f"ComfyUI is not answering ({error}). Start the services first."
            ) from None
        session["ollama_unloaded"] = lab.unload_ollama(ollama_url)

    def upload(case_id: str, key: str, path: Path) -> str:
        return comfy.upload(path, f"swap_lab_{case_id}_{key}.png")

    def save(case_id: str) -> None:
        (out / case_id / "metrics.json").write_text(
            json.dumps(metrics[case_id], indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    loaded = None
    for family, batch in phases:
        if loaded is not None and family != loaded and not dry_run:
            comfy.free()
            session["frees"].append(f"{loaded} -> {family}")
        loaded = family
        for method in batch:
            for case in cases:
                if not applies(method, case):
                    continue
                case_id, case_dir = case["id"], out / case["id"]
                paths = staged[case_id]["paths"]
                inputs = {
                    CLEAN_REF.id: {"character": paths["character"]},
                    "S1_qwen_marked": {"scene": paths["marked"], "character": paths["clean"]},
                    "S4_prompt": {"scene": paths["scene"], "character": paths["character"]},
                }[method.id]
                names = {key: f"swap_lab_{case_id}_{method.id}_{key}.png" for key in inputs}
                item = {
                    "status": "dry_run" if dry_run else "failed",
                    "prompt": prompt(case, method),
                }
                started = time.monotonic()
                try:
                    graph = build_graph(case, method, names, staged[case_id]["canvas"])
                    if not dry_run:
                        missing = [str(path.name) for path in inputs.values() if not path.is_file()]
                        if missing:
                            raise LabError(f"needs {', '.join(missing)} (an earlier step failed)")
                        print(f"[{case_id}] {method.id} ...", flush=True)
                        actual = {
                            key: upload(case_id, f"{method.id}_{key}", path)
                            for key, path in inputs.items()
                        }
                        graph = build_graph(case, method, actual, staged[case_id]["canvas"])
                    (case_dir / f"{method.id}_graph.json").write_text(
                        json.dumps(graph, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
                    )
                    if not dry_run:
                        data = comfy.execute(graph)
                        item["seconds"] = round(time.monotonic() - started, 1)
                        if method.id == CLEAN_REF.id:
                            paths["clean"].write_bytes(data)
                        else:
                            with Image.open(io.BytesIO(data)) as picture:
                                item["raw_size"] = list(picture.size)
                                raw = lab.fit_to(picture, staged[case_id]["canvas"])
                            mask = adaptive_mask(case, staged[case_id]["scene"], raw)
                            mask.save(case_dir / f"{method.id}_mask.png")
                            final, numbers = lab.change_metrics(staged[case_id]["scene"], raw, mask)
                            (case_dir / f"{method.id}_raw.png").write_bytes(data)
                            final.save(case_dir / f"{method.id}_final.png")
                            item.update(numbers)
                        item["status"] = "ok"
                        print(f"[{case_id}] {method.id} ok {item['seconds']}s", flush=True)
                except Exception as error:  # noqa: BLE001 - one method never aborts the run
                    item["status"] = "failed"
                    item["error"] = f"{type(error).__name__}: {error}"
                    tail = lab.comfyui_log_tail(root)
                    if tail:
                        item["comfyui_log_tail"] = tail
                    print(f"[{case_id}] {method.id} FAILED {item['error']}", flush=True)
                lab.retain_attempt(metrics[case_id]["variants"], method.id, item)
                save(case_id)
    if loaded is not None and not dry_run:
        comfy.free()
    write_review(out, [case for case in every if metrics[case["id"]]["variants"]], metrics)
    session["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    (out / "run.json").write_text(json.dumps(session, indent=2) + "\n", encoding="utf-8")
    return metrics


# --- SPEC-205: balões e texto detectados pelo SAM, com e sem ------------------------------

OVERLAY_PREFIX = "PepitoSwap/overlay"  # the SAM graph's speech-bubble and text outputs
OVERLAY_SHARE_LEVEL = (
    15  # a pixel "changed" past this (max channel), as the compositor's KEPT_LEVEL
)


def overlay_graph(image_name: str) -> dict:
    """``sam3-people-api.json`` reduced to the speech-bubble and text branches, on one image."""
    path = Path(__file__).resolve().parents[1] / "workflows" / "sam3-people-api.json"
    graph = json.loads(path.read_text(encoding="utf-8"))
    graph["1"]["inputs"]["image"] = image_name

    def feeding(roots) -> set:
        seen, pending = set(roots), list(roots)
        while pending:
            for value in graph[pending.pop()]["inputs"].values():
                if isinstance(value, list) and value[0] not in seen:
                    seen.add(value[0])
                    pending.append(value[0])
        return seen

    saves = {n: g for n, g in graph.items() if g["class_type"] == "SaveImage"}
    overlay = [
        n for n, g in saves.items() if g["inputs"]["filename_prefix"].startswith(OVERLAY_PREFIX)
    ]
    people = [n for n in saves if n not in overlay]
    dropped = feeding(people) - feeding(overlay)
    return {n: g for n, g in graph.items() if n not in dropped}


def execute_outputs(
    comfy, graph: dict, timeout: float = lab.JOB_TIMEOUT
) -> list[tuple[str, bytes]]:
    """Queue ``graph`` and return (``SaveImage`` filename prefix, PNG) for every saved image."""
    queued = lab._json(f"{comfy.url}/prompt", {"prompt": graph, "client_id": comfy.client_id})
    prompt_id = (queued or {}).get("prompt_id")
    if not prompt_id:
        raise LabError(f"ComfyUI did not queue the prompt: {queued}")
    deadline = time.monotonic() + timeout
    while True:
        entry = (lab._json(f"{comfy.url}/history/{prompt_id}") or {}).get(prompt_id)
        if entry:
            if (entry.get("status") or {}).get("status_str") == "error":
                raise LabError(f"ComfyUI execution error: {entry.get('status')}")
            break
        if time.monotonic() > deadline:
            raise LabError(f"Timed out after {timeout:.0f}s.")
        time.sleep(1.0)
    found = []
    for node, output in (entry.get("outputs") or {}).items():
        prefix = graph.get(node, {}).get("inputs", {}).get("filename_prefix", "")
        for image in output.get("images") or []:
            query = urllib.parse.urlencode(
                {
                    "filename": image["filename"],
                    "subfolder": image.get("subfolder", ""),
                    "type": image.get("type", "output"),
                }
            )
            found.append((prefix, lab._request(f"{comfy.url}/view?{query}", timeout=120)))
    return found


def detected_mask(data: bytes) -> bool:
    """Whether a saved SAM mask PNG holds any detection (above the compositor's threshold)."""
    with Image.open(io.BytesIO(data)) as image:
        return image.convert("L").getextrema()[1] > 127


def overlay_detect(
    cases_path: Path, out: Path, *, comfyui_url: str, only: set[str] | None = None
) -> dict:
    """The variant's GPU half: SAM 3.1 ``speech bubble`` and ``text`` on each case's scene.
    Writes ``overlay-<bubble|text>-<n>.png`` and ``overlay_detect.json`` (seconds, counts)
    beside the case's ``source.png``. Run ``swap_analysis_lab.py`` for the people and depth.
    The graph saves one union mask per kind, blank when SAM found nothing; a blank one is
    not written and counts 0, so a scene without bubbles has no ``overlay-*.png``."""
    comfy = lab.ComfyUI(comfyui_url)
    comfy.check()
    report = {}
    for case in load_cases(cases_path):
        folder = out / case["id"]
        if (only and case["id"] not in only) or not (folder / "source.png").is_file():
            continue
        name = comfy.upload(folder / "source.png", f"swap_lab_{case['id']}_overlay_source.png")
        started = time.monotonic()
        found = execute_outputs(comfy, overlay_graph(name))
        seconds = round(time.monotonic() - started, 2)
        for stale in folder.glob("overlay-*.png"):
            stale.unlink()
        counts: dict[str, int] = {}
        for prefix, data in found:
            kind = prefix.removeprefix(OVERLAY_PREFIX).lstrip("_") or "overlay"
            counts.setdefault(kind, 0)
            if not detected_mask(data):
                continue
            (folder / f"overlay-{kind}-{counts[kind]}.png").write_bytes(data)
            counts[kind] += 1
        report[case["id"]] = {"sam_overlay_s": seconds, "masks": counts}
        (folder / "overlay_detect.json").write_text(
            json.dumps(report[case["id"]], indent=2) + "\n", encoding="utf-8"
        )
        print(f"[{case['id']}] overlay {counts} {seconds}s", flush=True)
    comfy.free()
    return report


def _compose_module():
    path = Path(__file__).resolve().parents[1] / "assets" / "open-webui" / "pepito_swap_compose.py"
    module_spec = importlib.util.spec_from_file_location("_swap_lab_pepito_swap_compose", path)
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module  # its dataclasses look themselves up
    module_spec.loader.exec_module(module)
    return module


def overlay_compose(
    cases_path: Path, out: Path, analysis: Path, only: set[str] | None = None
) -> dict:
    """The variant's PC half (numpy and OpenCV needed): every case and method with a raw
    output composed twice by the chat's compositor, ``<method>_g2_shape.png`` (overlay by
    shape, today) and ``<method>_g2_detected.png`` (the SAM detection), plus
    ``overlay_compare.json``: the compositor's facts and, per detected piece, the share of its
    pixels each final changed against the scene (words rewritten show as a nonzero share)."""
    import cv2
    import numpy as np

    compose = _compose_module()
    report = {}
    for case in load_cases(cases_path):
        folder, found = out / case["id"], analysis / case["id"]
        masks = sorted(folder.glob("overlay-*.png"))
        if (only and case["id"] not in only) or not masks or not (found / "depth.png").is_file():
            continue
        overlay_masks = [path.read_bytes() for path in masks]
        scene_masks = [path.read_bytes() for path in sorted(found.glob("scene-mask-*.png"))]
        scene_png = (folder / "source.png").read_bytes()
        scene = np.asarray(Image.open(io.BytesIO(scene_png)).convert("RGB"), dtype=np.float32)
        detected = np.zeros(scene.shape[:2], bool)
        for data in overlay_masks:
            detected |= compose._decode_mask(data, (scene.shape[1], scene.shape[0]))
        count, parts = cv2.connectedComponents(compose.dilate(detected, 2).astype(np.uint8))
        for raw_path in sorted(folder.glob("*_raw.png")):
            method = raw_path.stem.removesuffix("_raw")
            raw_masks = [path.read_bytes() for path in sorted(found.glob(f"{method}-mask-*.png"))]
            record = {}
            for variant, extra in (("shape", ()), ("detected", (overlay_masks,))):
                try:
                    final, facts = compose.compose_swap(
                        scene_png,
                        raw_path.read_bytes(),
                        scene_masks,
                        raw_masks,
                        (found / "depth.png").read_bytes(),
                        case["box"],
                        case.get("keep_boxes") or [],
                        *extra,
                    )
                except ValueError as error:
                    record[variant] = {"error": str(error)}
                    continue
                (folder / f"{method}_g2_{variant}.png").write_bytes(final)
                picture = np.asarray(Image.open(io.BytesIO(final)).convert("RGB"), dtype=np.float32)
                changed = np.abs(picture - scene).max(axis=2) >= OVERLAY_SHARE_LEVEL
                shares = [
                    round(float(changed[parts == index].mean()), 3) for index in range(1, count)
                ]
                record[variant] = {"facts": facts, "piece_changed_share": shares}
            report[f"{case['id']}/{method}"] = record
            print(f"[{case['id']}] {method} composed both ways", flush=True)
    (out / "overlay_compare.json").write_text(
        json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    if report:
        print(overlay_review(cases_path, out), flush=True)
    return report


# The two compositions of SPEC-205, today's (by shape) as the base.
OVERLAY_VARIANTS = (
    ("shape", "Por forma (hoje)", "balões e letras guardados pela forma, como o chat faz hoje"),
    ("detected", "Detectados (SAM)", "balões e letras detectados pelo SAM 3.1"),
)


def overlay_review(cases_path: Path, out: Path) -> Path:
    """``avaliar-baloes.html``: one case per scene and swap method with both compositions,
    the one by shape (today) as the base. Reads ``overlay_compare.json`` and the
    ``<method>_g2_<kind>.png`` that ``overlay-compose`` wrote; no numpy needed."""
    path = out / "overlay_compare.json"
    compare = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    data = []
    for case in load_cases(cases_path):
        case_id = case["id"]
        metrics_path = out / case_id / "metrics.json"
        records = (
            json.loads(metrics_path.read_text(encoding="utf-8")).get("variants") or {}
            if metrics_path.is_file()
            else {}
        )
        for method in METHODS:
            composed = compare.get(f"{case_id}/{method.id}")
            if composed is None:
                continue
            raw = f"{case_id}/{method.id}_raw.png"
            variants = []
            for kind, label, note in OVERLAY_VARIANTS:
                result = composed.get(kind)
                if result is None:
                    continue
                name = f"{case_id}/{method.id}_g2_{kind}.png"
                ok = "error" not in result and (out / name).is_file()
                shares = result.get("piece_changed_share") or []
                meta = "nenhuma peça detectada para medir"
                if shares:
                    meta = (
                        f"balões e letras alterados: média {round(sum(shares) / len(shares) * 100)}%"
                        f" · máximo {round(max(shares) * 100)}%"
                    )
                paths = {"final": name}
                if (out / raw).is_file():
                    paths["raw"] = raw
                variants.append(
                    {
                        "id": kind,
                        "label": label,
                        "note": note,
                        "status": "ok"
                        if ok
                        else ("failed" if "error" in result else "missing outputs"),
                        "ok": ok,
                        "base": ok and kind == "shape",
                        "paths": paths if ok else {},
                        "prompt": (records.get(method.id) or {}).get("prompt") or "",
                        "meta": meta,
                        "error": result.get("error", ""),
                    }
                )
            if variants:
                data.append(
                    {
                        "id": f"{case_id}_{method.id.split('_')[0]}",
                        "ask": f"{case['ask']} · {method.label}: balões",
                        "instruction": "À esquerda, a composição de hoje (balões pela forma). "
                        "Veja se os balões e as letras ficaram intactos na outra.",
                        "source": f"{case_id}/source.png",
                        "references": references(out, case_id),
                        "variants": variants,
                    }
                )
    if not data:
        raise LabError(f"No overlay compositions in {out}: run overlay-compose first.")
    return lab.review_page(out, OVERLAY_PAGE, data, lab="baloes", title="Balões na troca")


def _methods(values: list[str] | None) -> list[Method]:
    if not values:
        return [BY_ID[name] for name in DEFAULT_METHODS]
    wanted = {name.strip() for value in values for name in value.split(",") if name.strip()}
    unknown = sorted(wanted - set(BY_ID))
    if unknown:
        raise LabError(f"Unknown methods: {', '.join(unknown)}. Known: {', '.join(BY_ID)}.")
    return [method for method in METHODS if method.id in wanted]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "run", "review", "overlay-detect", "overlay-compose", "overlay-review"):
        command = commands.add_parser(name)
        command.add_argument("--cases", type=Path, required=True, help="swap-lab-1 cases.json")
        command.add_argument("--out", type=Path, required=True)
        if name == "run":
            command.add_argument("--methods", action="append")
            command.add_argument("--only", help="comma-separated case ids to run")
            command.add_argument("--root", type=Path)
            command.add_argument("--dry-run", action="store_true")
            command.add_argument("--comfyui", default="http://127.0.0.1:8188")
            command.add_argument("--ollama", default="http://127.0.0.1:11434")
            command.add_argument(
                "--skip-good", type=Path, help="reviewer export or {'good': [...]} to skip"
            )
        if name == "overlay-detect":
            command.add_argument("--only", help="comma-separated case ids")
            command.add_argument("--comfyui", default="http://127.0.0.1:8188")
        if name == "overlay-compose":
            command.add_argument("--only", help="comma-separated case ids")
            command.add_argument(
                "--analysis", type=Path, required=True, help="swap_analysis_lab.py output"
            )
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            for path in prepare(args.cases, args.out):
                print(path)
        elif args.command == "run":
            run(
                args.cases,
                args.out,
                _methods(args.methods),
                root=args.root,
                dry_run=args.dry_run,
                comfyui_url=args.comfyui,
                only={c.strip() for c in (args.only or "").split(",") if c.strip()} or None,
                ollama_url=args.ollama,
                skip=good_cases(args.skip_good, load_cases(args.cases)),
            )
            print(args.out / PAGE)
        elif args.command == "overlay-detect":
            overlay_detect(
                args.cases,
                args.out,
                comfyui_url=args.comfyui,
                only={c.strip() for c in (args.only or "").split(",") if c.strip()} or None,
            )
        elif args.command == "overlay-compose":
            overlay_compose(
                args.cases,
                args.out,
                args.analysis,
                {c.strip() for c in (args.only or "").split(",") if c.strip()} or None,
            )
        elif args.command == "overlay-review":
            print(overlay_review(args.cases, args.out))
        else:
            cases = load_cases(args.cases)
            metrics = {}
            for case in cases:
                path = args.out / case["id"] / "metrics.json"
                if path.is_file():
                    metrics[case["id"]] = json.loads(path.read_text(encoding="utf-8"))
            print(write_review(args.out, cases, metrics))
    except LabError as error:
        print(f"swap lab: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
