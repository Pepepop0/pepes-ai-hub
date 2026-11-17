#!/usr/bin/env python3
"""Compose a swap lab's raw outputs with the pod's SAM 3.1 masks and DA3 depth (G2).

Input: the swap lab ``results`` folder, its ``cases.json`` and the ``swap_analysis_lab.py``
output. For every case and method it writes ``<id>_sam.png`` (the chat's composite) and,
when ``--compare`` holds a ``swap_composite.py`` output (Mask R-CNN/MiDaS detectors), a
``<id>_sheet.jpg`` with scene | raw | lab G2 | SAM G2, plus ``report.json``.
Run it in the compositor environment (numpy, OpenCV, Pillow).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "assets" / "open-webui"))
from pepito_swap_compose import compose_swap

LETTERS = {"S1_qwen_marked": "Q"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--analysis", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--compare", type=Path)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    cases = json.loads(args.cases.read_text(encoding="utf-8"))["cases"]
    report = {}
    for number, case in enumerate(cases, start=1):
        folder, found = args.results / case["id"], args.analysis / case["id"]
        if not (found / "depth.png").is_file():
            continue
        scene_masks = [p.read_bytes() for p in sorted(found.glob("scene-mask-*.png"))]
        for method, letter in LETTERS.items():
            raw_path = folder / f"{method}_raw.png"
            if not raw_path.is_file():
                continue
            raw_masks = [p.read_bytes() for p in sorted(found.glob(f"{method}-mask-*.png"))]
            key = f"{letter}{number}"
            try:
                final, facts = compose_swap(
                    (folder / "source.png").read_bytes(),
                    raw_path.read_bytes(),
                    scene_masks,
                    raw_masks,
                    (found / "depth.png").read_bytes(),
                    case["box"],
                    case.get("keep_boxes") or [],
                )
            except ValueError as error:
                report[key] = {"error": str(error)}
                print(key, "ERRO", error, flush=True)
                continue
            (args.out / f"{key}_sam.png").write_bytes(final)
            report[key] = facts
            print(
                key, "ok", {k: facts[k] for k in ("new_found", "others", "aligned_px")}, flush=True
            )
            if args.compare and (args.compare / key / "G2_final.png").is_file():
                tiles = [
                    ("cena", folder / "source.png"),
                    ("bruto", raw_path),
                    ("G2 lab (Mask R-CNN)", args.compare / key / "G2_final.png"),
                    ("G2 SAM 3.1", args.out / f"{key}_sam.png"),
                ]
                images = []
                for label, path in tiles:
                    image = Image.open(path).convert("RGB")
                    image.thumbnail((520, 520))
                    ImageDraw.Draw(image).text((6, 4), f"{key} {label}", fill=(255, 255, 0))
                    images.append(image)
                sheet = Image.new(
                    "RGB", (sum(i.width for i in images) + 15, max(i.height for i in images))
                )
                x = 0
                for image in images:
                    sheet.paste(image, (x, 0))
                    x += image.width + 5
                sheet.save(args.out / f"{key}_sheet.jpg", quality=88)
    (args.out / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
