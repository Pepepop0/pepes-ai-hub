"""The swap lab: staging, adaptive mask, graphs and the reviewer, without ComfyUI."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import comfy_sam_fake
import pytest
from PIL import Image, ImageDraw

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "swap_lab.py"
spec = importlib.util.spec_from_file_location("swap_lab_under_test", SCRIPT)
swap = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = swap
spec.loader.exec_module(swap)


def case(**overrides):
    return {
        "id": "T9",
        "ask": "teste",
        "style": "photo",
        "scene": "inputs/scene.png",
        "character": "inputs/character.png",
        "box": [100, 100, 500, 900],
        "keep_boxes": [[150, 150, 250, 250]],
        "target": "the man on the left",
        "character_desc": "the woman in the black jacket",
        "pose": "seated, facing the camera",
        "keep": "the table",
        "seed": 7,
        "free_prompt": "Put the woman from <image2> into <image1>.",
        **overrides,
    }


@pytest.fixture
def lab_dir(tmp_path):
    (tmp_path / "inputs").mkdir()
    Image.new("RGB", (2000, 1500), (40, 80, 120)).save(tmp_path / "inputs" / "scene.png")
    Image.new("RGB", (900, 1600), (200, 10, 10)).save(tmp_path / "inputs" / "character.png")
    document = {"schema": swap.SCHEMA, "cases": [case()]}
    (tmp_path / "cases.json").write_text(json.dumps(document), encoding="utf-8")
    return tmp_path


def test_the_scene_is_cropped_to_the_canvas_never_stretched():
    scene = Image.new("RGB", (736, 979))
    canvas = swap.fit_canvas(scene)
    assert canvas.size == (736, 960), "same scale, 19 rows cropped"
    assert canvas.width % 32 == 0 and canvas.height % 32 == 0


def test_staging_bounds_inputs_and_keeps_the_scene_outside_the_target(lab_dir):
    staged = swap.stage(case(), lab_dir, lab_dir / "out" / "T9")
    width, height = staged["canvas"]
    assert width % 32 == 0 and height % 32 == 0 and width * height <= swap.SCENE_PIXELS
    reference = Image.open(staged["paths"]["character"])
    assert reference.width * reference.height <= swap.REFERENCE_PIXELS
    mask = staged["mask"]
    inside = (int(0.3 * width), int(0.5 * height))
    kept = (int(0.2 * width), int(0.2 * height))  # inside the box but a keep box
    outside = (int(0.8 * width), int(0.5 * height))
    assert (mask.getpixel(inside), mask.getpixel(kept), mask.getpixel(outside)) == (255, 0, 0)


def test_the_adaptive_mask_follows_a_character_drawn_past_the_box_and_nothing_else():
    size = (800, 600)
    scene = Image.new("RGB", size, (60, 60, 60))
    raw = scene.copy()
    draw = ImageDraw.Draw(raw)
    draw.rectangle((100, 100, 300, 500), fill=(220, 30, 30))  # the character, past the box
    draw.rectangle((700, 50, 760, 110), fill=(220, 30, 30))  # an unrelated far change
    mask = swap.adaptive_mask(case(box=[125, 166, 250, 666], keep_boxes=[]), scene, raw)
    assert mask.getpixel((250, 430)) == 255, "the arm past the box is kept"
    assert mask.getpixel((730, 80)) == 0, "a far change stays out"
    assert mask.getpixel((500, 300)) == 0
    edge = [mask.getpixel((x, 300)) for x in range(300, 330)]
    assert any(0 < value < 255 for value in edge), "feathered, not a hard cut"


def test_keep_boxes_stay_exact_in_the_adaptive_mask():
    size = (800, 600)
    scene = Image.new("RGB", size, (60, 60, 60))
    raw = Image.new("RGB", size, (200, 200, 200))
    mask = swap.adaptive_mask(
        case(box=[100, 100, 900, 900], keep_boxes=[[200, 200, 400, 400]]), scene, raw
    )
    assert mask.getpixel((240, 180)) == 0 and mask.getpixel((600, 400)) == 255


def test_the_red_outline_lies_outside_the_kept_box(lab_dir):
    staged = swap.stage(case(), lab_dir, lab_dir / "out" / "T9")
    marked = Image.open(staged["paths"]["marked"]).convert("RGB")
    left, top, _right, bottom = swap.box_pixels(case()["box"], staged["canvas"])
    assert marked.getpixel((left - 2, (top + bottom) // 2)) == swap.swap.OUTLINE
    assert marked.getpixel((left, (top + bottom) // 2)) != swap.swap.OUTLINE


@pytest.mark.parametrize("method", (swap.CLEAN_REF, *swap.METHODS), ids=lambda m: m.id)
def test_each_graph_reads_its_inputs_in_order_and_names_the_pose(method):
    names = {"scene": "scene.png", "character": "character.png"}
    graph = swap.build_graph(case(character_style="art"), method, names, (1024, 1536))
    if method is swap.CLEAN_REF:
        assert graph["470"]["inputs"]["image"] == "character.png" and "471" not in graph
        text = graph["459:474"]["inputs"]["prompt"]
    else:
        assert graph["470"]["inputs"]["image"] == "scene.png"
        assert graph["471"]["inputs"]["image"] == "character.png"
        text = graph["459:474"]["inputs"]["prompt"]
    if method.id == "S1_qwen_marked":
        assert "seated, facing the camera" in text and "the woman in the black jacket" in text
    assert ("outlined in red" in text) is (method.id == "S1_qwen_marked")


def test_a_dry_run_plans_the_clean_reference_first_and_reviews_nothing_rated(lab_dir):
    out = lab_dir / "results"
    metrics = swap.run(
        lab_dir / "cases.json",
        out,
        list(swap.METHODS),
        root=None,
        dry_run=True,
        comfyui_url="http://127.0.0.1:1",
        ollama_url="http://127.0.0.1:1",
    )
    plan = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert plan["methods"] == [
        "P_ref",
        "S1_qwen_marked",
        "S4_prompt",
    ]
    assert plan["jobs"] == 3
    assert {v["status"] for v in metrics["T9"]["variants"].values()} == {"dry_run"}
    data = page_data(out / swap.PAGE)
    expected = [m.id for m in swap.METHODS if swap.applies(m, case())]
    assert [m["id"] for m in data[0]["variants"]] == expected
    assert metrics["T9"]["variants"]["S4_prompt"]["prompt"] == case()["free_prompt"]
    assert data[0]["references"][0]["src"] == "T9/reference.png"
    assert not any(v["ok"] for v in data[0]["variants"])
    assert page_key(out / swap.PAGE).startswith("pepito-review:troca:")


def page_data(path: Path) -> list[dict]:
    page = path.read_text(encoding="utf-8")
    return json.loads(page.split("const DATA = ", 1)[1].split(";\nconst KEY", 1)[0])


def page_key(path: Path) -> str:
    page = path.read_text(encoding="utf-8")
    return json.loads(page.split("const KEY = ", 1)[1].split(";\n", 1)[0])


def _picture(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 96), (90, 90, 90)).save(path)


def test_the_swap_review_rates_against_public_qwen_and_shows_each_prompt(lab_dir):
    out = lab_dir / "results"
    records = {}
    for method in ("S1_qwen_marked", "S4_prompt"):
        for kind in ("raw", "final"):
            _picture(out / "T9" / f"{method}_{kind}.png")
        records[method] = {"status": "ok", "prompt": swap.prompt(case(), swap.BY_ID[method])}
    data = page_data(swap.write_review(out, [case()], {"T9": {"variants": records}}))
    variants = data[0]["variants"]
    assert [v["id"] for v in variants] == ["S1_qwen_marked", "S4_prompt"]
    assert [v["id"] for v in variants if v["base"]] == [swap.SWAP_BASE] == ["S1_qwen_marked"]
    assert variants[1]["prompt"] == case()["free_prompt"]


def test_the_balloon_page_rates_detection_against_todays_shape_per_scene_and_method(lab_dir):
    second = case(id="T2")  # nothing detected there: overlay-compose skipped it
    document = {"schema": swap.SCHEMA, "cases": [case(), second]}
    (lab_dir / "cases.json").write_text(json.dumps(document), encoding="utf-8")
    out = lab_dir / "results"
    with pytest.raises(swap.LabError, match="overlay-compose"):
        swap.overlay_review(lab_dir / "cases.json", out)
    _picture(out / "T9" / "source.png")
    for method in ("S1_qwen_marked", "S4_prompt"):
        _picture(out / "T9" / f"{method}_raw.png")
        for kind in ("shape", "detected"):
            _picture(out / "T9" / f"{method}_g2_{kind}.png")
    (out / "T9" / "S4_prompt_g2_detected.png").unlink()
    compare = {
        "T9/S1_qwen_marked": {
            "shape": {"facts": {}, "piece_changed_share": [0.0, 0.5]},
            "detected": {"facts": {}, "piece_changed_share": [0.0, 0.1]},
        },
        "T9/S4_prompt": {
            "shape": {"facts": {}, "piece_changed_share": []},
            "detected": {"error": "Não encontrei a pessoa."},
        },
    }
    (out / "overlay_compare.json").write_text(json.dumps(compare), encoding="utf-8")
    marked = swap.prompt(case(), swap.BY_ID["S1_qwen_marked"])
    metrics = {"variants": {"S1_qwen_marked": {"status": "ok", "prompt": marked}}}
    (out / "T9" / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")

    assert (
        swap.main(["overlay-review", "--cases", str(lab_dir / "cases.json"), "--out", str(out)])
        == 0
    )
    path = out / swap.OVERLAY_PAGE
    data = page_data(path)
    assert [item["id"] for item in data] == ["T9_S1", "T9_S4"]
    qwen = {v["id"]: v for v in data[0]["variants"]}
    assert qwen["shape"]["base"] and qwen["detected"]["ok"] and not qwen["detected"]["base"]
    assert qwen["detected"]["paths"] == {
        "final": "T9/S1_qwen_marked_g2_detected.png",
        "raw": "T9/S1_qwen_marked_raw.png",
    }
    assert "máximo 50%" in qwen["shape"]["meta"] and "máximo 10%" in qwen["detected"]["meta"]
    assert qwen["detected"]["prompt"] == marked
    unprepared = {v["id"]: v for v in data[1]["variants"]}
    assert unprepared["shape"]["base"] and not unprepared["detected"]["ok"]
    assert unprepared["detected"]["error"] == "Não encontrei a pessoa."
    assert page_key(path).startswith("pepito-review:baloes:")
    assert page_key(path) != page_key(swap.write_review(out, [case()], {}))


def test_only_runs_the_named_cases_and_the_prompt_method_only_where_it_has_a_prompt(lab_dir):
    second = {k: v for k, v in case(id="T8").items() if k != "free_prompt"}
    document = {"schema": swap.SCHEMA, "cases": [case(), second]}
    (lab_dir / "cases.json").write_text(json.dumps(document), encoding="utf-8")
    out = lab_dir / "results"
    common = {
        "root": None,
        "dry_run": True,
        "comfyui_url": "http://x:1",
        "ollama_url": "http://x:1",
    }
    swap.run(lab_dir / "cases.json", out, list(swap.METHODS), only={"T8"}, **common)
    plan = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert plan["methods"] == ["P_ref", "S1_qwen_marked", "S4_prompt"]
    assert plan["jobs"] == 2
    metrics = json.loads((out / "T8" / "metrics.json").read_text(encoding="utf-8"))
    assert set(metrics["variants"]) == {"P_ref", "S1_qwen_marked"}
    assert not (out / "T9").exists(), "a case outside --only is not touched"
    with pytest.raises(swap.LabError, match="T7"):
        swap.run(lab_dir / "cases.json", out, list(swap.METHODS), only={"T7"}, **common)


def test_other_mediums_are_converted_by_public_qwen(lab_dir):
    drawn = case(style="photo", character_style="art")
    text = swap.prompt(drawn, swap.BY_ID["S1_qwen_marked"])
    assert "as a real person" in text, "a drawn character is photographed into a photo"
    assert "as a real person" not in swap.prompt(case(), swap.BY_ID["S1_qwen_marked"])
    real = case(style="art", character_style="photo")
    document = {"schema": swap.SCHEMA, "cases": [real]}
    (lab_dir / "cases.json").write_text(json.dumps(document), encoding="utf-8")
    out = lab_dir / "results"
    common = {
        "root": None,
        "dry_run": True,
        "comfyui_url": "http://x:1",
        "ollama_url": "http://x:1",
    }
    swap.run(lab_dir / "cases.json", out, swap._methods(None), **common)
    plan = json.loads((out / "run.json").read_text(encoding="utf-8"))
    assert plan["methods"] == ["P_ref", "S1_qwen_marked", "S4_prompt"]
    assert plan["jobs"] == 3


def test_cases_the_user_called_good_are_skipped(lab_dir, tmp_path):
    cases = [case(id=f"T{n}") for n in (1, 2, 3)]
    export = {
        "Q2": {"_case": {"good": True}, "D2": {"r": "S"}},
        "K2": {"_case": {"good": True}},
        "Q3": {"_case": {"good": True}},  # only one method of T3: T3 still runs
        "T1": {"_case": {"good": False}},
    }
    review = tmp_path / "avaliacao.md"
    review.write_text("| t |\n\n```json\n" + json.dumps(export) + "\n```\n", encoding="utf-8")
    assert swap.good_cases(review, cases) == {"T2"}
    listed = tmp_path / "bons.json"
    listed.write_text(json.dumps({"good": ["T3"]}), encoding="utf-8")
    assert swap.good_cases(listed, cases) == {"T3"}
    document = {"schema": swap.SCHEMA, "cases": cases}
    (lab_dir / "cases.json").write_text(json.dumps(document), encoding="utf-8")
    common = {
        "root": None,
        "dry_run": True,
        "comfyui_url": "http://x:1",
        "ollama_url": "http://x:1",
    }
    swap.run(lab_dir / "cases.json", lab_dir / "out", swap._methods(None), skip={"T2"}, **common)
    assert not (lab_dir / "out" / "T2").exists() and (lab_dir / "out" / "T1").exists()


def test_an_incomplete_case_is_refused(lab_dir):
    bad = {"schema": swap.SCHEMA, "cases": [{**case(), "style": "anime"}]}
    (lab_dir / "cases.json").write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(swap.LabError):
        swap.load_cases(lab_dir / "cases.json")
    no_pose = {"schema": swap.SCHEMA, "cases": [{k: v for k, v in case().items() if k != "pose"}]}
    (lab_dir / "cases.json").write_text(json.dumps(no_pose), encoding="utf-8")
    with pytest.raises(swap.LabError, match="pose"):
        swap.load_cases(lab_dir / "cases.json")


def test_overlay_detect_survives_scenes_without_bubbles_or_text(lab_dir, monkeypatch):
    found = {"T9": {"speech bubble": 2, "text": 1}, "T2": {}}  # T2: the lab's failing scenes
    document = {"schema": swap.SCHEMA, "cases": [case(), case(id="T2")]}
    (lab_dir / "cases.json").write_text(json.dumps(document), encoding="utf-8")
    out = lab_dir / "results"
    for case_id in found:
        (out / case_id).mkdir(parents=True)
        Image.new("RGB", (320, 256)).save(out / case_id / "source.png")
    (out / "T2" / "overlay-text-0.png").write_bytes(b"from an earlier run")
    queued = {}

    def fake_json(url, body=None, timeout=60):
        if url.endswith("/prompt"):
            graph = body["prompt"]
            image = graph["1"]["inputs"]["image"]
            queued[str(len(queued))] = (graph, found[image.split("_")[2]])
            return {"prompt_id": str(len(queued) - 1)}
        if "/history/" in url:
            prompt_id = url.rsplit("/", 1)[-1]
            return {prompt_id: comfy_sam_fake.history_item(prompt_id, *queued[prompt_id])}
        return {}

    def fake_request(url, data=None, headers=None, timeout=60):
        if url.endswith("/upload/image"):
            name = data.split(b'filename="', 1)[1].split(b'"', 1)[0].decode()
            return json.dumps({"name": name, "subfolder": ""}).encode()
        return comfy_sam_fake.mask_png(url.split("filename=", 1)[1].split("&", 1)[0])

    monkeypatch.setattr(swap.lab, "_json", fake_json)
    monkeypatch.setattr(swap.lab, "_request", fake_request)
    report = swap.overlay_detect(lab_dir / "cases.json", out, comfyui_url="http://x:1")
    assert report["T9"]["masks"] == {"bubble": 1, "text": 1}, "one union mask per kind"
    assert report["T2"]["masks"] == {"bubble": 0, "text": 0}
    assert sorted(p.name for p in (out / "T9").glob("overlay-*.png")) == [
        "overlay-bubble-0.png",
        "overlay-text-0.png",
    ]
    assert not list((out / "T2").glob("overlay-*.png")), "nothing detected, nothing kept"
    saved = json.loads((out / "T2" / "overlay_detect.json").read_text(encoding="utf-8"))
    assert saved["masks"] == {"bubble": 0, "text": 0}
