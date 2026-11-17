"""The swap compositor's image math. Needs numpy and OpenCV, which the Hub's own
environment does not ship: the module runs in a throwaway environment, so these tests skip
without them (run them with that environment's pytest)."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")
Image = pytest.importorskip("PIL.Image")

MODULE = Path(__file__).resolve().parent.parent / "assets" / "open-webui" / "pepito_swap_compose.py"
spec = importlib.util.spec_from_file_location("pepito_swap_compose_under_test", MODULE)
comp = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = comp
spec.loader.exec_module(comp)

H, W = 240, 320


def rect(x0, y0, x1, y1):
    mask = np.zeros((H, W), bool)
    mask[y0:y1, x0:x1] = True
    return mask


def scene_and_raw(offset=-20.0):
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    scene = np.stack([xx / W * 200 + 20, yy / H * 200 + 20, np.full((H, W), 120.0)], axis=2)
    raw = scene + offset  # a model that darkens the whole frame, like Krea
    raw[60:200, 100:200] = (200, 30, 30)  # the new person
    return scene.astype(np.float32), raw.astype(np.float32)


def test_the_drift_correction_removes_a_frame_wide_darkening_inside_the_region():
    scene, raw = scene_and_raw()
    region = rect(80, 40, 220, 220)
    band = comp.dilate(region, 30) & ~comp.dilate(region, 4)
    corrected = raw + comp.drift_field(scene, raw, band)
    background_inside = region & ~rect(100, 60, 200, 200)
    assert np.abs(raw - scene)[background_inside].mean() > 19
    assert np.abs(corrected - scene)[background_inside].mean() < 2


def test_a_big_content_change_in_the_band_does_not_spread_as_a_burned_spot():
    scene, raw = scene_and_raw()
    region = rect(80, 40, 220, 220)
    band = comp.dilate(region, 30) & ~comp.dilate(region, 4)
    raw[226:240, 60:120] = (40, 20, 10)  # the old person's thigh, left outside her silhouette
    corrected = raw + comp.drift_field(scene, raw, band)
    background_inside = region & ~rect(100, 60, 200, 200)
    assert np.abs(corrected - scene)[background_inside].mean() < 2


def test_the_arm_in_front_goes_back_on_top_with_its_own_color_not_the_old_background():
    scene = np.full((H, W, 3), 250.0, np.float32)  # the old person's white shirt
    arm = rect(0, 100, W, 140)
    scene[arm] = (110, 160, 110)
    scene[140:143] = (180, 205, 180)  # the arm's soft edge, half shirt
    composite = np.full((H, W, 3), 20.0, np.float32)  # the new person's dark jacket
    out = comp.occluder_over(composite, scene, arm, 6)
    assert np.abs(out[120, 50] - (110, 160, 110)).max() < 1, "the arm itself is the scene's"
    edge = out[141, 50]
    assert edge[1] < 180 and edge[0] > 30, "the edge mixes the arm into the jacket, no halo"
    assert np.abs(out[160, 50] - 20).max() < 1, "past the edge, the new composite"


def test_someone_in_front_of_the_replaced_person_is_protected_and_someone_behind_is_not():
    old = rect(100, 40, 200, 220)
    arm = rect(150, 120, 260, 150)  # crosses the old person's body
    near = np.ones((H, W), np.float32)
    near[arm] = 2.0  # nearer than the old person
    protected, order = comp.protection(old, [arm], near, 4)
    assert order == ["front"] and protected[130, 230]
    near[arm] = 0.5  # farther: behind
    protected, order = comp.protection(old, [arm], near, 4)
    assert order == ["behind"] and not protected.any()


def test_the_composite_keeps_the_scene_outside_the_people():
    scene, raw = scene_and_raw()
    old = rect(110, 50, 190, 210)
    new = rect(100, 60, 200, 200)
    near = np.ones((H, W), np.float32)
    result = comp.compose(scene, raw, old, new, [], near)
    far = rect(0, 0, 40, 40)
    final = result.finals[comp.FINAL]
    assert np.abs(final[far] - scene[far]).max() < 1
    assert result.alpha[55, 150] > 0.99, "the old person's top is replaced"
    assert np.abs(final[120, 150] - (200, 30, 30)).max() < 25


def test_a_hat_attached_to_the_new_person_is_kept_and_a_far_change_is_not():
    scene, raw = scene_and_raw(offset=0)
    new = rect(120, 100, 200, 220)  # the detector's silhouette, without the hat
    raw[60:100, 110:210] = (30, 40, 200)  # the hat on top of it
    raw[10:30, 280:310] = (30, 40, 200)  # an unrelated change far away
    others = np.zeros((H, W), bool)
    hat = comp.extend_person(new, np.zeros((H, W), bool), raw, scene, others)
    assert hat[80, 160] and not hat[20, 295]
    result = comp.compose(scene, raw, None, new, [], np.ones((H, W), np.float32))
    final = result.finals[comp.FINAL]
    assert np.abs(final[80, 160] - (30, 40, 200)).max() < 25, "hat not cut"
    assert np.abs(final[20, 295] - scene[20, 295]).max() < 1


def test_a_colored_hat_tail_over_a_neighbor_is_the_new_person_and_a_gray_redraw_is_not():
    scene, raw = scene_and_raw(offset=0)
    new = rect(60, 60, 140, 200)
    neighbor = rect(140, 40, 260, 220)
    scene[neighbor] = (30, 30, 30)  # a dark shirt
    raw[neighbor] = (30, 30, 30)
    raw[60:200, 140:170] = (70, 25, 30)  # the hat's tail, dark red: brighter by only 40
    raw[60:200, 220:250] = (70, 70, 70)  # the model's redraw of the shirt, as bright
    found = comp.extend_person(new, np.zeros((H, W), bool), raw, scene, neighbor)
    assert found[130, 155], "the tail hangs over the neighbor"
    assert not found[130, 235], "a redraw without a new color is not the new person"


def test_a_person_in_front_comes_back_only_where_the_model_changed_them():
    scene, raw = scene_and_raw()
    old = rect(100, 40, 200, 220)
    new = rect(100, 60, 200, 200)
    arm = rect(40, 120, 260, 150)  # crosses the old person, nearer
    near = np.ones((H, W), np.float32)
    near[arm] = 2.0
    scene[arm] = (110, 160, 110)
    scene[120:150, 170:180] = (60, 40, 50)  # the old person's strap, given to the arm
    scene[125:145, 126:129] = (40, 40, 40)  # the arm's own watch
    raw[arm] = scene[arm] - 20  # the model kept the arm
    raw[126:144, 140:158] = (20, 20, 20)  # ... but drew a buckle over it here
    raw[120:150, 120:135] = (200, 30, 30)  # ... and the new person's coat over the watch
    raw[120:150, 170:180] = (200, 30, 30)  # and over the strap
    result = comp.compose(scene, raw, old, new, [arm], near)
    final = result.finals[comp.FINAL]
    assert result.alpha[135, 110] > 0.9, "where the model kept the arm, its own arm stays"
    assert np.abs(final[135, 149] - (110, 160, 110)).max() < 25, "the buckle goes"
    assert np.abs(final[135, 127] - (40, 40, 40)).max() < 25, "the watch is the arm's own"
    assert np.abs(final[135, 175] - (200, 30, 30)).max() < 40, "the coat wins over the strap"


def test_a_raw_output_zoomed_and_shifted_by_the_model_is_aligned_to_the_scene():
    rng = np.random.default_rng(1)
    texture = cv2.GaussianBlur(rng.uniform(0, 255, (H, W)).astype(np.float32), (0, 0), 3)
    texture = (texture - texture.min()) / (texture.max() - texture.min()) * 255
    scene = np.repeat(texture[..., None], 3, axis=2)
    warp = np.float32([[1.004, 0, 3.0], [0, 0.996, -2.0]])  # what Qwen-Edit does, roughly
    raw = cv2.warpAffine(scene, warp, (W, H), borderMode=cv2.BORDER_REFLECT)
    background = np.ones((H, W), bool)
    aligned, _, moved = comp.align_to_scene(scene, raw, None, background)
    inside = (slice(20, H - 20), slice(20, W - 20))
    before = np.abs(raw - scene)[inside].mean()
    assert np.abs(aligned - scene)[inside].mean() < before / 5
    assert 2 < moved < 6


def test_a_misaligned_stroke_in_the_seam_takes_one_side_instead_of_showing_twice():
    soft = np.tile(np.linspace(0, 1, W, dtype=np.float32), (H, 1))
    base = np.full((H, W, 3), 100.0, np.float32)
    top = base.copy()
    base[50:150, 100:104] = 220  # the scene's text stroke
    top[50:150, 106:110] = 220  # the model's copy, a few pixels off
    settled = comp.settle_seam(soft, top, base)
    assert settled[100, 102] < 0.05 and settled[100, 108] < 0.05, "one side: the scene's"
    assert settled[100, 250] == soft[100, 250], "away from the stroke the seam is unchanged"
    shaded = comp.settle_seam(soft, base + 30, base)
    assert np.array_equal(shaded, soft), "a color difference keeps the soft seam"


def test_a_speech_bubble_is_kept_by_its_shape_not_by_its_box():
    scene, raw = scene_and_raw()
    yy, xx = np.mgrid[0:H, 0:W]
    bubble = (yy - 40) ** 2 / 20**2 + (xx - 60) ** 2 / 40**2 <= 1
    scene[bubble] = (10, 10, 10)
    raw[bubble] = (0, 0, 0)  # the model kept the bubble (darkened, like the rest)
    box = [10 / W * 1000, 10 / H * 1000, 110 / W * 1000, 75 / H * 1000]
    overlay = comp.overlay_mask(scene, [box])
    assert overlay[40, 60] and overlay[40, 30], "the bubble, letters' holes included"
    assert not overlay[14, 14], "the box's corner, outside the bubble, is not kept"
    old = rect(0, 0, 140, 120)  # the replaced person under the whole box
    near = np.ones((H, W), np.float32)
    result = comp.compose(scene, raw, old, None, [], near, [box])
    assert result.alpha[40, 60] < 0.01, "the bubble the model kept stays the scene's own"
    assert result.alpha[14, 14] > 0.5, "the box's corner follows the composite"
    raw[bubble] = scene[bubble] + 60  # the model replaced it: a strand, not a bubble
    result = comp.compose(scene, raw, old, None, [], near, [box])
    assert result.alpha[40, 60] > 0.5, "content the model replaced near the old person goes"


def test_other_people_are_kept_unless_the_new_person_covers_them():
    scene, raw = scene_and_raw()
    new = rect(100, 60, 200, 200)
    neighbor = rect(190, 60, 260, 220)  # behind; the new person overlaps it at x 190-200
    raw[60:220, 200:260] = scene[60:220, 200:260] + 15  # a light redraw of the neighbor
    near = np.ones((H, W), np.float32)
    result = comp.compose(scene, raw, rect(110, 60, 190, 200), new, [neighbor], near)
    assert result.alpha[150, 240] < 0.01, "the neighbor is not repainted"
    assert result.alpha[150, 195] > 0.5, "where the new person covers them, the new person wins"


def png(array, mode=None):
    buffer = io.BytesIO()
    Image.fromarray(np.clip(array, 0, 255).astype(np.uint8), mode).save(buffer, "PNG")
    return buffer.getvalue()


def mask_png(mask, size=None):
    image = Image.fromarray(mask.astype(np.uint8) * 255)
    if size:
        image = image.resize(size, Image.Resampling.NEAREST)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def box_of(x0, y0, x1, y1):
    return [x0 / W * 1000, y0 / H * 1000, x1 / W * 1000, y1 / H * 1000]


def test_the_person_in_the_box_is_picked_among_several_masks():
    left, middle, right = rect(10, 40, 70, 220), rect(110, 50, 190, 210), rect(230, 40, 300, 220)
    old, others = comp.pick_person([left, middle, right], box_of(100, 40, 200, 220), (W, H))
    assert old is middle
    assert len(others) == 2 and any(o is left for o in others) and any(o is right for o in others)
    none, rest = comp.pick_person([left, right], box_of(100, 40, 200, 220), (W, H))
    assert none is None and len(rest) == 2


def test_compose_swap_returns_a_png_of_the_scene_size_from_a_raw_of_another_size():
    scene, raw = scene_and_raw()
    big_raw = cv2.resize(raw, (W * 2, H * 2), interpolation=cv2.INTER_NEAREST)
    old, neighbor = rect(110, 50, 190, 210), rect(240, 40, 310, 220)
    new = rect(100, 60, 200, 200)
    depth = np.full((H, W), 128, np.uint8)
    final, facts = comp.compose_swap(
        png(scene),
        png(big_raw),
        [mask_png(neighbor), mask_png(old)],
        [mask_png(new, (W * 2, H * 2))],
        png(depth),
        box_of(100, 40, 200, 220),
    )
    image = Image.open(io.BytesIO(final))
    assert image.format == "PNG" and image.size == (W, H)
    assert facts["old_found"] and facts["new_found"] and facts["others"] == 1
    assert facts["others_order"] == ["apart"] and "aligned_px" in facts
    out = np.asarray(image.convert("RGB"), dtype=np.float32)
    assert np.abs(out[120, 150] - (200, 30, 30)).max() < 25, "the new person is in"
    assert np.abs(out[10, 10] - scene[10, 10]).max() < 2, "the scene stays outside"


def test_a_large_scene_is_analyzed_reduced_and_composed_on_its_full_pixels(monkeypatch):
    scene, raw = scene_and_raw()
    raw[60:200:2, 100:200] = (230, 60, 60)  # one-pixel stripes: lost by any downscale
    old, neighbor = rect(110, 50, 190, 210), rect(240, 40, 310, 220)
    args = (
        png(scene),
        png(raw),
        [mask_png(neighbor), mask_png(old)],
        [mask_png(rect(100, 60, 200, 200))],
        png(np.full((H, W), 128, np.uint8)),
        box_of(100, 40, 200, 220),
    )
    whole_png, whole_facts = comp.compose_swap(*args)
    monkeypatch.setattr(comp, "ANALYSIS_PIXELS", W * H // 4)
    reduced_png, facts = comp.compose_swap(*args)
    assert facts["size"] == [W, H] and facts["analysis_size"] == [W // 2, H // 2]
    assert whole_facts["analysis_size"] == [W, H]
    whole, reduced = (
        np.asarray(Image.open(io.BytesIO(data)).convert("RGB"), dtype=np.float32)
        for data in (whole_png, reduced_png)
    )
    assert reduced.shape == (H, W, 3)
    assert np.abs(reduced[120, 150] - reduced[121, 150]).max() > 20, "full-size detail stays"
    assert np.abs(reduced - whole).max(axis=2).mean() < 2, "the same composite"


def test_compose_swap_without_the_person_in_the_scene_raises_in_portuguese():
    scene, raw = scene_and_raw()
    with pytest.raises(ValueError, match="Não encontrei a pessoa a trocar na cena."):
        comp.compose_swap(
            png(scene),
            png(raw),
            [mask_png(rect(240, 40, 310, 220))],
            [],
            png(np.zeros((H, W), np.uint8)),
            box_of(100, 40, 200, 220),
        )


def overlay_scene():
    """An old person across the frame with two bubbles on her, a new person lower down.
    Bubble 1 is partly under the new person's silhouette; bubble 2 is apart from it and the
    model rewrote its letters. The detector leaves the bubbles out of the person's mask."""
    scene, raw = scene_and_raw()
    first, second = rect(120, 60, 190, 120), rect(230, 60, 255, 90)
    bubbles = first | second
    old = rect(60, 40, 260, 220) & ~bubbles
    new = rect(100, 100, 200, 200)
    raw = scene - 20
    raw[second] = (255, 255, 0)  # the model's handwriting over the second bubble
    raw[new] = (200, 30, 30)
    near = np.ones((H, W), np.float32)
    near[old] = 2.0
    return scene, raw, old, new, near, first, second


def test_a_detected_bubble_stays_the_scenes_unless_the_new_person_covers_it_in_front():
    scene, raw, old, new, near, first, second = overlay_scene()
    detected = first | second
    result = comp.compose(scene, raw, old, new, [], near, [], detected)
    assert result.alpha[75, 240] < 0.01, "a bubble the model rewrote is the scene's again"
    assert result.alpha[80, 150] < 0.01, "the bubble outside the new person stays"
    assert result.alpha[110, 150] > 0.5, "the new person is in front of the bubble: she covers it"
    assert sorted(result.info["overlay_order"]) == ["clear", "front"]
    bare = comp.compose(scene, raw, old, new, [], near, [])
    assert bare.alpha[75, 240] > 0.5, "without a detection the rewritten bubble is replaced"


def test_a_detected_bubble_nearer_than_the_person_is_kept_even_under_her_silhouette():
    scene, raw, old, new, near, first, second = overlay_scene()
    near[first | second] = 3.0  # the bubble is nearer than the person it hangs over
    result = comp.compose(scene, raw, old, new, [], near, [], first | second)
    assert result.alpha[110, 150] < 0.01, "behind the bubble: the bubble stays whole"
    assert "behind" in result.info["overlay_order"] and "front" not in result.info["overlay_order"]


def test_without_a_detection_the_composite_is_the_one_by_shape():
    scene, raw, old, new, near, *_ = overlay_scene()
    box = [10 / W * 1000, 10 / H * 1000, 270 / W * 1000, 230 / H * 1000]
    today = comp.compose(scene, raw, old, new, [], near, [box])
    for nothing in (None, np.zeros((H, W), bool)):
        same = comp.compose(scene, raw, old, new, [], near, [box], nothing)
        assert np.array_equal(same.finals[comp.FINAL], today.finals[comp.FINAL])
        assert np.array_equal(same.alpha, today.alpha)
        assert same.info["overlay_source"] == "shape" and same.info["overlay_detected_share"] == 0


def test_the_trace_facts_tell_the_detected_and_the_kept_overlay():
    scene, raw, old, new, near, first, second = overlay_scene()
    result = comp.compose(scene, raw, old, new, [], near, [], first | second)
    info = result.info
    assert info["overlay_source"] == "detected"
    detected = (first | second).mean()
    assert detected <= info["overlay_detected_share"] < detected * 1.6
    assert 0 < info["overlay_kept_share"] < info["overlay_detected_share"], (
        "the covered part yields"
    )


def test_compose_swap_reads_the_overlay_masks_as_png_at_any_size():
    scene, raw, old, new, near, first, second = overlay_scene()
    depth = np.clip(near * 100, 0, 255).astype(np.uint8)
    args = (
        png(scene),
        png(raw),
        [mask_png(old)],
        [mask_png(new)],
        png(depth),
        box_of(60, 40, 260, 220),
    )
    _, shape = comp.compose_swap(*args)
    final, facts = comp.compose_swap(
        *args, [], [mask_png(first, (W // 2, H // 2)), mask_png(second)]
    )
    assert shape["overlay_source"] == "shape" and facts["overlay_source"] == "detected"
    out = np.asarray(Image.open(io.BytesIO(final)).convert("RGB"), dtype=np.float32)
    assert np.abs(out[75, 240] - scene[75, 240]).max() < 2, "the second bubble is the scene's"


def test_the_composite_page_lists_each_items_finals_with_the_rated_one_as_base(tmp_path):
    script = Path(__file__).resolve().parent.parent / "scripts" / "swap_composite.py"
    page_spec = importlib.util.spec_from_file_location("swap_composite_under_test", script)
    composite = importlib.util.module_from_spec(page_spec)
    sys.modules[page_spec.name] = composite
    page_spec.loader.exec_module(composite)
    for item, finals in (("Q3", ("F2", "G2")), ("K3", ("G2",))):
        (tmp_path / item).mkdir()
        for final in finals:
            Image.new("RGB", (32, 48)).save(tmp_path / item / f"{final}_final.png")
    path = composite.write_review(tmp_path, [{"id": "Q3", "ask": "a"}, {"id": "K3", "ask": "b"}])
    text = path.read_text(encoding="utf-8")
    q3, k3 = json.loads(text.split("const DATA = ", 1)[1].split(";\nconst KEY", 1)[0])
    assert [(v["id"], v["base"]) for v in q3["variants"]] == [("F2", True), ("G2", False)]
    assert [(v["id"], v["base"]) for v in k3["variants"]] == [("G2", False)], "no 'not run' F2"
    assert "pepito-review:composicao:" in text
