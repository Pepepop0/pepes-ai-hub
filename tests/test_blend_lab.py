"""The blend lab: masks, graphs and staging from a swap lab results folder, no ComfyUI."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "blend_lab.py"
spec = importlib.util.spec_from_file_location("blend_lab_under_test", SCRIPT)
blend = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = blend
spec.loader.exec_module(blend)


def person(size=(400, 600)):
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rectangle((100, 100, 300, 500), fill=255)
    return mask


def test_the_edge_ring_follows_the_seam_and_leaves_the_middle_alone():
    ring = blend.blend_mask(person(), "edge")
    assert ring.getpixel((100, 300)) > 200, "on the seam"
    assert ring.getpixel((200, 300)) == 0, "the character's middle is not repainted"
    assert ring.getpixel((20, 20)) == 0


def test_the_body_region_covers_the_whole_swap_slightly_grown():
    body = blend.blend_mask(person(), "body")
    assert body.getpixel((200, 300)) == 255 and body.getpixel((97, 300)) > 0
    assert body.getpixel((20, 20)) == 0


@pytest.mark.parametrize("item", blend.BLENDS, ids=lambda b: b.id)
def test_each_blend_is_a_masked_low_denoise_qwen_pass_on_the_base(item):
    graph = blend.build_graph("Q3", item, {"image": "base.png", "mask": "mask.png"}, 9)
    sampler = graph["459:458"]["inputs"]
    assert sampler["denoise"] == item.denoise and sampler["seed"] == 9
    latent = graph[sampler["latent_image"][0]]
    assert latent["class_type"] == "SetLatentNoiseMask"
    encoded = graph[latent["inputs"]["samples"][0]]
    assert encoded["class_type"] == "VAEEncode" and encoded["inputs"]["pixels"] == ["470", 0]
    assert graph["470"]["inputs"]["image"] == "base.png"
    assert graph[latent["inputs"]["mask"][0]]["inputs"]["image"] == "mask.png"


def test_a_dry_run_makes_one_review_case_per_case_and_base(tmp_path):
    swap_results, out = tmp_path / "swap", tmp_path / "blend"
    (swap_results / "T1").mkdir(parents=True)
    scene = Image.new("RGB", (400, 600), (30, 30, 30))
    scene.save(swap_results / "T1" / "source.png")
    for method in blend.BASES:
        scene.save(swap_results / "T1" / f"{method}_final.png")
        person().save(swap_results / "T1" / f"{method}_mask.png")
    cases = {
        "schema": blend.swap.SCHEMA,
        "cases": [
            {
                "id": "T1",
                "ask": "x",
                "style": "art",
                "scene": "s.png",
                "character": "c.png",
                "box": [100, 100, 500, 900],
                "target": "t",
                "character_desc": "d",
                "keep": "k",
                "pose": "p",
                "seed": 3,
            }
        ],
    }
    (tmp_path / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
    blend.run(
        tmp_path / "cases.json",
        swap_results,
        out,
        list(blend.BLENDS),
        root=None,
        dry_run=True,
        comfyui_url="http://127.0.0.1:1",
        ollama_url="http://127.0.0.1:1",
    )
    page = (out / blend.PAGE).read_text(encoding="utf-8")
    data = json.loads(page.split("const DATA = ", 1)[1].split(";\nconst KEY", 1)[0])
    assert [case["id"] for case in data] == ["Q1"]
    for case in data:
        base, *blends = case["variants"]
        assert base["base"] and base["ok"] and base["paths"]["final"] == f"{case['id']}/source.png"
        assert [v["id"] for v in blends] == [b.id for b in blend.BLENDS]
        assert not any(v["ok"] for v in blends)
        assert all(v["prompt"] == blend.PROMPT for v in blends)
    key = json.loads(page.split("const KEY = ", 1)[1].split(";\n", 1)[0])
    assert key.startswith("pepito-review:blending:")
