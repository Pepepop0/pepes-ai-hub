#!/usr/bin/env python3
"""Swap compositor: put a swap's raw output back into the scene without cut marks.

The swap lab's final (rounds 1-2) pasted the raw output through one feathered mask: the
target's box joined with what changed. Measured on rounds 1-2 (05/11/2026), the marks the
user reported come from three causes, each with a fix here:

1. **Color drift.** Krea darkens the whole frame by 5-26 levels per channel and Qwen by
   1-7; at the feather ring the step between raw and scene is almost all low-frequency.
   The rectangles around T1/T8's speech bubbles and Krea's "pasted on the screen" look in
   T7/T9 are that step. Fix: ``drift_field`` measures scene - raw on the background around
   the region and spreads it, smoothly, over the region before compositing.
2. **Other people repainted.** Krea erased the right man's arm in T3 and the box-joined
   mask carried that. Fix: ``protection`` keeps the scene's pixels of every other person
   who is in front of the replaced one where they meet (nearer in the scene's depth).
3. **Ghosts of the old person.** Where a hard box or a grown mask cut through a limb, the
   feather mixed both bodies (T7 shoulder, T4 hand). Fix: the region always contains the
   old person's whole silhouette and the new one's, plus changes attached to them (hats,
   held things), so the feather lies on background.

Final written per case and method: ``FINAL`` (the composite), beside the final rated last
round (``RATED``) when an earlier output is given (``--previous``): the reviewer shows that
one as the base and asks for the new one against it.

Silhouettes come from torchvision's Mask R-CNN (COCO person) and depth from MiDaS small,
CPU-friendly stand-ins for SAM 3.1 and Depth Anything 3, both native in ComfyUI v0.39.
Run it in a throwaway environment, never the Hub's .venv (docs/testing/swap-lab.md):

    python -m venv %TEMP%\\swap-venv
    %TEMP%\\swap-venv\\Scripts\\pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
    %TEMP%\\swap-venv\\Scripts\\pip install numpy opencv-python-headless pillow timm
    %TEMP%\\swap-venv\\Scripts\\python scripts\\swap_composite.py --results R --out O
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "assets" / "open-webui"))
from pepito_swap_compose import (  # one implementation, shared with the chat
    FINAL,
    box_mask,
    compose,
)

OTHER_SCORE = 0.8  # other people below this were curtains and a plant in T4 (0.56-0.71)
PAGE = "avaliar-composicao.html"
BASES = {"S1_qwen_marked": ("Q", "Qwen com marcação")}
RATED = "F2"  # the final rated last round: copied from --previous, the reviewer's base
FINALS = (
    ("F2", "F2 de 08/11", "a melhor da rodada anterior; a nova é avaliada contra ela"),
    ("G2", "G2 (alinhada)", "F2 com a saída alinhada à cena e ajustada nas marcas"),
)


# --- detectors (CPU-friendly stand-ins for SAM 3.1 and Depth Anything 3) ---------------


class Detectors:
    def __init__(self):
        import torch
        import torchvision

        torch.set_grad_enabled(False)
        self.torch = torch
        weights = torchvision.models.detection.MaskRCNN_ResNet50_FPN_V2_Weights.DEFAULT
        self.people = torchvision.models.detection.maskrcnn_resnet50_fpn_v2(weights=weights).eval()
        # MiDaS loads its backbone from a second hub repo, which torch would ask about on
        # stdin; both are recorded as trusted first, as `trust_repo=True` does for the first.
        trusted = Path(torch.hub.get_dir()) / "trusted_list"
        trusted.parent.mkdir(parents=True, exist_ok=True)
        known = trusted.read_text(encoding="utf-8").split() if trusted.is_file() else []
        missing = [
            r for r in ("intel-isl_MiDaS", "rwightman_gen-efficientnet-pytorch") if r not in known
        ]
        if missing:
            with trusted.open("a", encoding="utf-8") as handle:
                handle.write("".join(f"{repo}\n" for repo in missing))
        self.depth_model = torch.hub.load("intel-isl/MiDaS", "MiDaS_small", trust_repo=True).eval()
        transforms = torch.hub.load("intel-isl/MiDaS", "transforms", trust_repo=True)
        self.depth_transform = transforms.small_transform

    def depth(self, image: np.ndarray) -> np.ndarray:
        """Relative inverse depth (bigger = nearer) at the image's size."""
        out = self.depth_model(self.depth_transform(image.astype(np.uint8)))
        out = self.torch.nn.functional.interpolate(
            out.unsqueeze(1), size=image.shape[:2], mode="bicubic", align_corners=False
        )
        return out[0, 0].numpy()

    def person(self, image: np.ndarray, box, min_score=0.5, other_score=OTHER_SCORE):
        """(the person overlapping ``box`` most as a hard mask or None, every other person
        detected with at least ``other_score``)."""
        tensor = self.torch.from_numpy(image / 255.0).permute(2, 0, 1).float()
        found = self.people([tensor])[0]
        h, w = image.shape[:2]
        target = box_mask(box, (w, h))
        ranked = []
        for label, score, mask in zip(
            found["labels"], found["scores"], found["masks"], strict=True
        ):
            if int(label) != 1 or float(score) < min_score:
                continue
            hard = mask[0].numpy() > 0.5
            if not hard.any():
                continue
            inside = (hard & target).sum() / hard.sum()
            overlap = (hard & target).sum() / max(1, target.sum())
            ranked.append((inside * 0.5 + overlap if inside > 0.5 else 0.0, float(score), hard))
        ranked.sort(key=lambda item: item[0], reverse=True)
        if not ranked or ranked[0][0] == 0.0:
            return None, [hard for _, score, hard in ranked if score >= other_score]
        return ranked[0][2], [hard for _, score, hard in ranked[1:] if score >= other_score]


# --- run and review --------------------------------------------------------------------


def rgb(path: Path, size=None) -> np.ndarray:
    image = Image.open(path).convert("RGB")
    if size and image.size != size:
        image = image.resize(size, Image.Resampling.LANCZOS)
    return np.asarray(image, dtype=np.float32)


def save(array: np.ndarray, path: Path) -> None:
    Image.fromarray(np.clip(array, 0, 255).astype(np.uint8)).save(path)


def _review_page():
    """The shared laboratory reviewer, loaded by path."""
    path = Path(__file__).with_name("lab_common.py")
    module_spec = importlib.util.spec_from_file_location("_swap_composite_lab_common", path)
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    return module.review_page


def write_review(out: Path, items: list[dict]) -> Path:
    """The finals each item has; ``RATED`` (from --previous) is the base when present."""
    data = []
    for item in items:
        variants = []
        for final_id, label, note in FINALS:
            path = f"{item['id']}/{final_id}_final.png"
            if not (out / path).is_file():
                continue
            variants.append(
                {
                    "id": final_id,
                    "label": label,
                    "base": final_id == RATED,
                    "note": note,
                    "status": "ok",
                    "ok": True,
                    "paths": {"raw": f"{item['id']}/raw.png", "final": path},
                    "meta": "",
                    "error": "",
                }
            )
        data.append(
            {
                "id": item["id"],
                "ask": item["ask"],
                "instruction": (
                    f"À esquerda, a versão base ({RATED}); O mostra a cena original. "
                    "Avalie a nova contra a base."
                    if any(v["base"] for v in variants)
                    else "À esquerda, a cena original. Compare as composições com ←/→."
                ),
                "source": f"{item['id']}/source.png",
                "references": [{"src": f"{item['id']}/reference.png", "label": "Personagem"}],
                "variants": variants,
            }
        )
    return _review_page()(out, PAGE, data, lab="composicao", title="Laboratório de composição")


def load_good(path: Path | None) -> set[str]:
    """Review ids the user marked "já está boa": from a reviewer export (its JSON keeps
    ``{"<id>": {"_case": {"good": true}}}``) or a ``{"good": [...]}`` file."""
    if path is None:
        return set()
    text = path.read_text(encoding="utf-8")
    fence = re.search(r"```json\s*(.*?)```", text, re.DOTALL)
    data = json.loads(fence.group(1) if fence else text)
    if isinstance(data, dict) and isinstance(data.get("good"), list):
        return {str(item) for item in data["good"]}
    return {
        key
        for key, value in (data or {}).items()
        if isinstance(value, dict) and (value.get("_case") or {}).get("good") is True
    }


def run(
    results: Path,
    cases_path: Path,
    out: Path,
    methods: list[str],
    detectors=None,
    previous: Path | None = None,
    good: set[str] = frozenset(),
) -> dict:
    """``FINAL`` for every case and method, beside the final rated last round (``RATED``)
    copied from ``previous`` (an earlier composite output). Review ids in ``good`` are
    composed and reported (against ``RATED``, to catch a regression) but left out of the
    reviewer: the user already called them good."""
    cases = json.loads(cases_path.read_text(encoding="utf-8"))["cases"]
    detectors = detectors or Detectors()
    out.mkdir(parents=True, exist_ok=True)
    items, report = [], {}
    for number, case in enumerate(cases, start=1):
        case_dir = results / case["id"]
        if not (case_dir / "source.png").is_file():
            continue
        scene = rgb(case_dir / "source.png")
        h, w = scene.shape[:2]
        old, others = detectors.person(scene, case["box"])
        near = detectors.depth(scene)
        for method in methods:
            raw_path = case_dir / f"{method}_raw.png"
            if not raw_path.is_file() or method not in BASES:
                continue
            letter, label = BASES[method]
            review_id = f"{letter}{number}"
            target = out / review_id
            target.mkdir(parents=True, exist_ok=True)
            raw = rgb(raw_path, (w, h))
            new, _ = detectors.person(raw, case["box"])
            result = compose(scene, raw, old, new, others, near, case.get("keep_boxes") or ())
            save(scene, target / "source.png")
            save(raw, target / "raw.png")
            # The final rated last round stays beside the new ones, byte for byte.
            rated = target / f"{RATED}_final.png"
            earlier = (previous / review_id / f"{RATED}_final.png") if previous else None
            if earlier and earlier.is_file() and earlier != rated:
                shutil.copyfile(earlier, rated)
            for name, image in result.finals.items():
                save(image, target / f"{name}_final.png")
            save(result.alpha * 255, target / "D_mask.png")
            reference = case_dir / "reference_clean.png"
            if not reference.is_file():
                reference = case_dir / "reference.png"
            shutil.copyfile(reference, target / "reference.png")
            report[review_id] = {
                "case": case["id"],
                "method": method,
                "old_found": old is not None,
                "new_found": new is not None,
                **result.info,
            }
            if rated.is_file():
                change = np.abs(result.finals[FINAL] - rgb(rated, (w, h)))
                report[review_id][f"{FINAL}_vs_{RATED}"] = {
                    "mean": round(float(change.mean()), 2),
                    "moved_share": round(float((change.max(axis=2) > 20).mean()), 4),
                }
            if review_id in good:
                report[review_id]["good"] = True
            else:
                items.append({"id": review_id, "ask": f"{case['id']} · {label}: composição"})
            print(
                review_id,
                case["id"],
                method,
                report[review_id].get(f"{FINAL}_vs_{RATED}"),
                flush=True,
            )
    (out / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    write_review(out, items)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--results", type=Path, required=True, help="a swap lab results folder")
    parser.add_argument("--cases", type=Path, help="its cases.json (default: beside results)")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--methods", default=",".join(BASES))
    parser.add_argument("--previous", type=Path, help=f"an earlier composite output ({RATED})")
    parser.add_argument(
        "--good", type=Path, help="reviewer export or {'good': [...]}: ids left out of the page"
    )
    args = parser.parse_args(argv)
    cases = args.cases or args.results.parent / "cases.json"
    methods = [m for m in args.methods.split(",") if m]
    good = load_good(args.good)
    run(args.results, cases, args.out, methods, previous=args.previous, good=good)
    print(args.out / PAGE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
