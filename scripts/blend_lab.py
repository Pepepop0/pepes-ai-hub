#!/usr/bin/env python3
"""Blend lab: does a light Qwen repaint make a swapped character sit better in the scene?

Round 2 of the swap lab (05/11) showed Krea -> Qwen had the best color integration but,
redrawing the whole character at full strength, broke faces and proportions. Here the
repaint is masked img2img (the edit lab's E2: VAEEncode + SetLatentNoiseMask) at low
denoise on top of an existing swap, so shapes and the face stay:

* ``B1_edge`` (0.30): only a ring on the seam of the swap's own mask, where round 1 was
  "cut" and "pasted";
* ``B2_body`` (0.35): the whole swapped region (the character and the background around
  her inside that mask), lightly;
* ``B3_body`` (0.50): the same, stronger, to see where it starts to break.

Inputs are a finished swap lab results folder (``source.png`` and, per base method,
``<method>_final.png`` and ``<method>_mask.png``). Each case and base becomes one review
case: ``Q<n>`` on Qwen com marcação. The base swap is the "base" the
reviewer compares against; outside the blend mask the base is exact.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter

_spec = importlib.util.spec_from_file_location(
    "_blend_lab_swap_lab", Path(__file__).with_name("swap_lab.py")
)
swap = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = swap
_spec.loader.exec_module(swap)
lab, LabError = swap.lab, swap.LabError

BASES = {"S1_qwen_marked": ("Q", "Qwen com marcação")}
PAGE = "avaliar-blending.html"
PROMPT = (
    "Blend the person naturally into this photo or drawing: match the scene's lighting "
    "direction, color temperature, shadows, contrast and grain on her skin, hair and "
    "clothes, and clean up the edges where she meets the background. Keep her face, "
    "expression, hair, outfit, body shape, pose and proportions exactly the same. Do not "
    "add or remove anything."
)


@dataclass(frozen=True)
class Blend:
    id: str
    label: str
    region: str  # "edge" or "body"
    denoise: float
    note: str


BLENDS = (
    Blend("B1_edge", "Só a costura (0,30)", "edge", 0.30, "repinta só a faixa da emenda da troca"),
    Blend(
        "B2_body", "Região leve (0,35)", "body", 0.35, "luz e cor em toda a região trocada, leve"
    ),
    Blend("B3_body", "Região média (0,50)", "body", 0.50, "o mesmo, mais forte"),
)
BY_ID = {blend.id: blend for blend in BLENDS}


def blend_mask(person: Image.Image, region: str) -> Image.Image:
    """``body``: the swap's own mask, slightly grown. ``edge``: a feathered ring on its
    seam, about 2.5% of the short side wide. The swap mask is the target's box joined
    with what the model changed, not a person silhouette (no segmentation model yet)."""
    binary = person.convert("L").point(lambda v: 255 if v >= 128 else 0)
    width = max(4, round(min(binary.size) * 0.025))
    size = 2 * (width // 2) + 1
    if region == "body":
        grown = binary.filter(ImageFilter.MaxFilter(size))
        return grown.filter(ImageFilter.GaussianBlur(max(2, width // 3)))
    if region != "edge":
        raise LabError(f"Unknown blend region {region!r}.")
    outer = binary.filter(ImageFilter.MaxFilter(size))
    inner = binary.filter(ImageFilter.MinFilter(size))
    return ImageChops.subtract(outer, inner).filter(ImageFilter.GaussianBlur(max(2, width // 3)))


def build_graph(case_id: str, blend: Blend, names: dict, seed: int) -> dict:
    part = lab.engine.masked_part(
        {
            "workflow": json.dumps(lab.load_workflow(swap.QWEN_ONE)),
            "nodes": [
                {"type": "prompt", "key": "prompt", "node_ids": ["459:474"]},
                {"type": "image", "key": "image", "node_ids": ["470"]},
                {"type": "seed", "key": "seed", "node_ids": ["459:458"]},
            ],
            "model": "qwen_image_2_1_edit",
            "reference_count": 1,
            "family": "qwen21",
        },
        family="qwen21",
        denoise=blend.denoise,
    )
    graph = json.loads(part["workflow"])
    image_nodes = next(n["node_ids"] for n in part["nodes"] if n["type"] == "image")
    for node, name in zip(image_nodes, (names["image"], names["mask"]), strict=True):
        graph[node]["inputs"]["image"] = name
    lab._inputs(graph, "459:474", "TextEncodeQwenImage21")["prompt"] = PROMPT
    lab._inputs(graph, "459:458", "KSampler")["seed"] = seed
    graph[lab._save_node(graph)]["inputs"]["filename_prefix"] = f"blend_lab/{case_id}_{blend.id}"
    return graph


def stage(swap_results: Path, cases: list[dict], out: Path) -> list[dict]:
    """One review case per swap case and base method, with its blend masks written."""
    staged = []
    for number, case in enumerate(cases, start=1):
        source_dir = swap_results / case["id"]
        for method, (letter, label) in BASES.items():
            base_path = source_dir / f"{method}_final.png"
            person_path = source_dir / f"{method}_mask.png"
            if not base_path.is_file() or not person_path.is_file():
                continue
            review_id = f"{letter}{number}"
            case_dir = out / review_id
            case_dir.mkdir(parents=True, exist_ok=True)
            base = Image.open(base_path).convert("RGB")
            base.save(case_dir / "source.png")
            shutil.copyfile(source_dir / "source.png", case_dir / "scene.png")
            if (source_dir / "reference_clean.png").is_file():
                shutil.copyfile(
                    source_dir / "reference_clean.png", case_dir / "reference_clean.png"
                )
            person = Image.open(person_path).convert("L")
            masks = {}
            for region in ("edge", "body"):
                path = case_dir / f"blend_{region}.png"
                blend_mask(person, region).save(path)
                masks[region] = path
            staged.append(
                {
                    "id": review_id,
                    "case": case,
                    "method": method,
                    "ask": f"{case['id']} · troca {label}: blending",
                    "dir": case_dir,
                    "base": base,
                    "masks": masks,
                }
            )
    if not staged:
        raise LabError(f"No swap finals with masks under {swap_results}.")
    return staged


def write_review(out: Path, staged: list[dict], metrics: dict) -> Path:
    """The shared reviewer: the swap without blending (today) as the base, then the blends
    each case ran."""
    data = []
    for item in staged:
        records = (metrics.get(item["id"]) or {}).get("variants") or {}
        variants = [
            {
                "id": "B0_none",
                "label": "Sem blending (hoje)",
                "note": "a troca como o chat entrega",
                "status": "ok",
                "ok": True,
                "base": True,
                "paths": {"final": f"{item['id']}/source.png"},
                "meta": "",
                "error": "",
            }
        ]
        for blend in BLENDS:
            record = records.get(blend.id)
            if record is None:
                continue
            paths = {
                kind: f"{item['id']}/{blend.id}_{kind}.png"
                for kind in ("raw", "final")
                if (out / item["id"] / f"{blend.id}_{kind}.png").is_file()
            }
            ok = record.get("status") == "ok" and len(paths) == 2
            variants.append(
                {
                    "id": blend.id,
                    "label": blend.label,
                    "note": blend.note,
                    "status": record.get("status", "not run"),
                    "ok": ok,
                    "paths": paths if ok else {},
                    "prompt": record.get("prompt") or "",
                    "seconds": record.get("seconds") if ok else None,
                    "changed": record.get("changed_fraction_inside") if ok else None,
                    "outside": record.get("raw_mad_outside") if ok else None,
                    "error": record.get("error", ""),
                }
            )
        references = [{"src": f"{item['id']}/scene.png", "label": "Cena"}]
        if (item["dir"] / "reference_clean.png").is_file():
            references.append({"src": f"{item['id']}/reference_clean.png", "label": "Personagem"})
        data.append(
            {
                "id": item["id"],
                "ask": item["ask"],
                "instruction": "Base = a troca sem blending. Segure Espaço para comparar.",
                "source": f"{item['id']}/source.png",
                "references": references,
                "variants": variants,
            }
        )
    return lab.review_page(out, PAGE, data, lab="blending", title="Laboratório de blending")


def run(
    cases_path: Path,
    swap_results: Path,
    out: Path,
    blends: list[Blend],
    *,
    root: Path | None,
    dry_run: bool,
    comfyui_url: str,
    ollama_url: str,
) -> dict:
    cases = swap.load_cases(cases_path)
    out.mkdir(parents=True, exist_ok=True)
    staged = stage(swap_results, cases, out)
    metrics = {}
    for item in staged:
        previous = item["dir"] / "metrics.json"
        record = json.loads(previous.read_text(encoding="utf-8")) if previous.is_file() else {}
        record.update(case=item["case"]["id"], base=item["method"])
        record.setdefault("variants", {})
        metrics[item["id"]] = record
    comfy = lab.ComfyUI(comfyui_url)
    session = {
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "dry_run": dry_run,
        "blends": [b.id for b in blends],
        "jobs": len(staged) * len(blends),
    }
    if not dry_run:
        try:
            comfy.check()
        except LabError as error:
            raise LabError(
                f"ComfyUI is not answering ({error}). Start the services first."
            ) from None
        session["ollama_unloaded"] = lab.unload_ollama(ollama_url)
    for blend in blends:
        for item in staged:
            case_dir, review_id = item["dir"], item["id"]
            mask_path = item["masks"][blend.region]
            names = {
                "image": f"blend_lab_{review_id}_base.png",
                "mask": f"blend_lab_{review_id}_{blend.region}.png",
            }
            entry = {"status": "dry_run" if dry_run else "failed", "prompt": PROMPT}
            started = time.monotonic()
            try:
                graph = build_graph(review_id, blend, names, item["case"]["seed"])
                if not dry_run:
                    print(f"[{review_id}] {blend.id} ...", flush=True)
                    actual = {
                        "image": comfy.upload(case_dir / "source.png", names["image"]),
                        "mask": comfy.upload(mask_path, names["mask"]),
                    }
                    graph = build_graph(review_id, blend, actual, item["case"]["seed"])
                (case_dir / f"{blend.id}_graph.json").write_text(
                    json.dumps(graph, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
                )
                if not dry_run:
                    data = comfy.execute(graph)
                    entry["seconds"] = round(time.monotonic() - started, 1)
                    with Image.open(io.BytesIO(data)) as picture:
                        entry["raw_size"] = list(picture.size)
                        raw = lab.fit_to(picture, item["base"].size)
                    final, numbers = lab.change_metrics(
                        item["base"], raw, Image.open(mask_path).convert("L")
                    )
                    (case_dir / f"{blend.id}_raw.png").write_bytes(data)
                    final.save(case_dir / f"{blend.id}_final.png")
                    entry.update(numbers, status="ok")
                    print(f"[{review_id}] {blend.id} ok {entry['seconds']}s", flush=True)
            except Exception as error:  # noqa: BLE001 - one job never aborts the run
                entry["status"] = "failed"
                entry["error"] = f"{type(error).__name__}: {error}"
                tail = lab.comfyui_log_tail(root)
                if tail:
                    entry["comfyui_log_tail"] = tail
                print(f"[{review_id}] {blend.id} FAILED {entry['error']}", flush=True)
            lab.retain_attempt(metrics[review_id]["variants"], blend.id, entry)
            (case_dir / "metrics.json").write_text(
                json.dumps(metrics[review_id], indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
    if not dry_run:
        comfy.free()
    write_review(out, staged, metrics)
    session["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    (out / "run.json").write_text(json.dumps(session, indent=2) + "\n", encoding="utf-8")
    return metrics


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cases", type=Path, required=True, help="the swap lab cases.json")
    parser.add_argument(
        "--swap-results", type=Path, required=True, help="a swap lab results folder"
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--blends", action="append")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--comfyui", default="http://127.0.0.1:8188")
    parser.add_argument("--ollama", default="http://127.0.0.1:11434")
    args = parser.parse_args(argv)
    wanted = {n.strip() for v in args.blends or () for n in v.split(",") if n.strip()}
    unknown = sorted(wanted - set(BY_ID))
    try:
        if unknown:
            raise LabError(f"Unknown blends: {', '.join(unknown)}. Known: {', '.join(BY_ID)}.")
        blends = [b for b in BLENDS if not wanted or b.id in wanted]
        run(
            args.cases,
            args.swap_results,
            args.out,
            blends,
            root=args.root,
            dry_run=args.dry_run,
            comfyui_url=args.comfyui,
            ollama_url=args.ollama,
        )
        print(args.out / PAGE)
    except LabError as error:
        print(f"blend lab: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
