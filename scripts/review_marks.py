#!/usr/bin/env python3
"""Draw the marks painted in a lab reviewer onto the pictures they were painted on.

The reviewers (scripts/lab_review.html) let the user paint failures (red) and good
parts (green) on a variant; the export's JSON keeps each stroke in image coordinates. This
writes, for every marked variant, ``<case>_<variant>_marcas.png`` (the picture with the
marks) and ``<case>_<variant>_falha.png`` (the failure mask alone), and prints where each
failure lies, so a fix can be checked against the exact area that was wrong.

    python scripts/review_marks.py --review avaliacao.md --page-dir DIR --out OUT

``--page-dir`` is the folder of the reviewer page that was rated (its DATA names each
variant's picture relative to it).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw

COLORS = {"f": (240, 64, 64), "g": (64, 200, 110)}


def ratings(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    fence = re.search(r"```json\s*(.*?)```", text, re.DOTALL)
    data = json.loads(fence.group(1) if fence else text)
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: no ratings object")
    return data


def page_data(page_dir: Path) -> list[dict]:
    pages = sorted(page_dir.glob("avaliar*.html"))
    if not pages:
        raise SystemExit(f"{page_dir}: no avaliar*.html")
    text = pages[0].read_text(encoding="utf-8")
    return json.loads(text.split("const DATA = ", 1)[1].split(";\nconst KEY", 1)[0])


def draw(size: tuple[int, int], strokes: list[dict], kinds: set[str]) -> Image.Image:
    """An RGBA layer with the strokes of ``kinds``, as the reviewer drew them."""
    w, h = size
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    pen = ImageDraw.Draw(layer)
    for stroke in strokes:
        kind = stroke.get("k")
        points = stroke.get("p") or []
        if kind not in kinds or not points:
            continue
        radius = max(1, round(float(stroke.get("r", 0.01)) * w))
        color = (*COLORS[kind], 255)
        xy = [(x * w, y * h) for x, y in points]
        if len(xy) > 1:
            pen.line(xy, fill=color, width=2 * radius, joint="curve")
        for x, y in xy:
            pen.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)
    return layer


def where(mask: Image.Image) -> str:
    """The failure's bounding box as fractions of the picture, e.g. 'x 62-78%, y 18-31%'."""
    box = mask.getbbox()
    if not box:
        return "nenhuma"
    w, h = mask.size
    return f"x {box[0] * 100 // w}-{box[2] * 100 // w}%, y {box[1] * 100 // h}-{box[3] * 100 // h}%"


def run(review: Path, page_dir: Path, out: Path) -> list[str]:
    marks = ratings(review)
    cases = {case["id"]: case for case in page_data(page_dir)}
    out.mkdir(parents=True, exist_ok=True)
    lines = []
    for case_id, variants in marks.items():
        for variant_id, value in (variants or {}).items():
            strokes = (value or {}).get("m") or []
            if not strokes:
                continue
            variant = next(
                (v for v in cases.get(case_id, {}).get("variants", []) if v["id"] == variant_id),
                None,
            )
            path = (variant or {}).get("paths", {}).get("final")
            if not path or not (page_dir / path).is_file():
                lines.append(f"{case_id} {variant_id}: imagem não encontrada")
                continue
            picture = Image.open(page_dir / path).convert("RGBA")
            both = draw(picture.size, strokes, {"f", "g"})
            alpha = both.getchannel("A").point(lambda v: v * 0.5)
            both.putalpha(alpha)
            Image.alpha_composite(picture, both).convert("RGB").save(
                out / f"{case_id}_{variant_id}_marcas.png"
            )
            failure = draw(picture.size, strokes, {"f"}).getchannel("A")
            failure.save(out / f"{case_id}_{variant_id}_falha.png")
            kinds = [s.get("k") for s in strokes]
            lines.append(
                f"{case_id} {variant_id}: {kinds.count('f')} falha(s), {kinds.count('g')} "
                f"acerto(s); falhas em {where(failure)}"
            )
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--review", type=Path, required=True, help="the exported avaliacao-*.md")
    parser.add_argument("--page-dir", type=Path, required=True, help="the rated reviewer's folder")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    for line in run(args.review, args.page_dir, args.out):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
