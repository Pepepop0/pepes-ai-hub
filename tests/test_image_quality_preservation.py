"""Pixel-exact preservation, geometry boundaries and conservative source grounding."""

from __future__ import annotations

import base64
import glob
import importlib.util
import io
import json
import os
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageDraw, ImageStat

from local_ai_stack.resources import resource_directory


def quality():
    path = resource_directory("assets") / "open-webui" / "pepito_image_preservation.py"
    spec = importlib.util.spec_from_file_location("quality_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def png(image):
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


@pytest.fixture
def seam(monkeypatch):
    """The module with its numeric dependencies: the Hub's own interpreter has none of them
    (the compositor's tests run in the managed Open WebUI environment, as these do)."""
    pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    path = resource_directory("assets") / "open-webui" / "pepito_swap_compose.py"
    spec = importlib.util.spec_from_file_location("open_webui.pepito_swap_compose", path)
    compose = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, compose)  # its dataclasses look themselves up
    spec.loader.exec_module(compose)
    return quality()


def test_local_composite_preserves_hq_rgba_pixels_and_dimensions_outside_selection(seam):
    module = seam
    source = Image.new("RGBA", (1280, 1920), (190, 30, 40, 128))
    generated = Image.new("RGBA", (1024, 1536), (20, 40, 190, 255))
    mask = Image.new("L", source.size, 0)
    mask.paste(255, (300, 800, 700, 1200))
    data, facts = module.preserve_pixels(png(source), png(generated), png(mask))
    mixed = Image.open(io.BytesIO(data))
    assert mixed.size == source.size
    assert mixed.getpixel((500, 1000)) == (20, 40, 190, 128)
    outside = ImageChops.invert(mask)
    difference = ImageChops.difference(mixed.convert("RGB"), source.convert("RGB"))
    assert difference.getbbox() is not None
    for channel in difference.split():
        assert ImageChops.multiply(channel, outside).getbbox() is None
    assert mixed.getchannel("A").tobytes() == source.getchannel("A").tobytes()
    metrics = module.preservation_metrics(png(source), png(mixed), png(mask))
    assert metrics["outside_exact"] is True and metrics["outside_rgb_mad"] == 0
    assert metrics["inside_rgb_mad"] > 0
    assert set(facts) >= {
        "band_step_source",
        "band_step_raw",
        "band_step_final",
        "outer_ring_mad",
        "seam_mode",
    }


# --- the seam of a masked edit -----------------------------------------------------------------

SIZE = 384


def band_step(rgb, hard):
    """Luminance step across the mask edge, the G2 metric: Gaussian (4 px) mean just inside
    minus just outside, over boundary pixels. Independent of the module under test."""
    import cv2
    import numpy as np

    mask = hard.astype(np.uint8)
    box = np.ones((5, 5), np.uint8)

    def local_mean(weight):
        num = cv2.GaussianBlur(rgb * weight[..., None], (0, 0), 4.0)
        den = cv2.GaussianBlur(weight, (0, 0), 4.0)
        return num / np.maximum(den, 1e-3)[..., None], den

    inner, inner_w = local_mean(cv2.erode(mask, box).astype(np.float32))
    outer, outer_w = local_mean((1 - cv2.dilate(mask, box)).astype(np.float32))
    small = np.ones((3, 3), np.uint8)
    edge = (cv2.dilate(mask, small) - cv2.erode(mask, small)) > 0
    edge &= (inner_w > 0.15) & (outer_w > 0.15)
    edge[:6] = edge[-6:] = False
    edge[:, :6] = edge[:, -6:] = False
    return float(np.abs((inner - outer)[edge] @ np.array([0.299, 0.587, 0.114])).mean())


def gradient_scene():
    import numpy as np

    y, x = np.mgrid[0:SIZE, 0:SIZE].astype(np.float32)
    rgb = np.stack([40 + x * 0.4 + y * 0.1, 60 + y * 0.4, 90 + x * 0.2 + y * 0.2], -1)
    return Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8)).convert("RGBA")


def region(kind):
    mask = Image.new("L", (SIZE, SIZE), 0)
    draw = ImageDraw.Draw(mask)
    if kind == "rect":
        draw.rectangle((100, 90, 290, 300), fill=255)
    else:
        draw.ellipse((90, 80, 260, 230), fill=255)
        draw.ellipse((170, 150, 310, 310), fill=255)
    return mask


def model_result(source, shift):
    """What the model hands back: the whole frame drifted, plus new content mid-selection."""
    import numpy as np

    rgb = np.asarray(source).astype(np.int16).copy()
    rgb[..., 0] += shift[0]
    rgb[..., 1] += shift[1]
    result = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8))
    ImageDraw.Draw(result).rectangle((150, 140, 200, 190), fill=(220, 30, 30, 255))
    return result


def composite(module, source, result, mask):
    import numpy as np

    data, facts = module.preserve_pixels(png(source), png(result), png(mask))
    return Image.open(io.BytesIO(data)).convert("RGBA"), facts, np.asarray(mask) > 0


def rgb_of(image):
    import numpy as np

    return np.asarray(image.convert("RGB")).astype(np.float32)


@pytest.mark.parametrize("kind", ["rect", "blob"])
def test_a_drifted_edit_joins_the_source_without_a_tonal_step(seam, kind):
    source, mask = gradient_scene(), region(kind)
    result = model_result(source, (20, 10))
    mixed, facts, hard = composite(seam, source, result, mask)
    natural = band_step(rgb_of(source), hard)
    assert band_step(rgb_of(mixed), hard) <= 1.25 * natural
    # Pasted as it was, the model's frame would leave a step the source never had.
    pasted = Image.composite(result, source, mask)
    assert band_step(rgb_of(pasted), hard) > 2 * natural
    assert facts["seam_mode"] == "drift"
    assert facts["band_step_final"] <= 1.25 * facts["band_step_source"] + 0.01


@pytest.mark.parametrize("kind", ["rect", "blob"])
@pytest.mark.parametrize("shift", [(20, 10), (3, 3), (0, 0)])
def test_outside_the_selection_is_the_source_exactly_and_the_core_keeps_the_models_colors(
    seam, kind, shift
):
    import cv2
    import numpy as np

    source, mask = gradient_scene(), region(kind)
    result = model_result(source, shift)
    mixed, facts, hard = composite(seam, source, result, mask)
    outside = ~hard
    assert (np.asarray(mixed)[outside] == np.asarray(source)[outside]).all()
    assert mixed.getchannel("A").tobytes() == source.getchannel("A").tobytes()
    core = cv2.erode(hard.astype(np.uint8), np.ones((31, 31), np.uint8)) > 0
    assert np.abs(rgb_of(mixed) - rgb_of(result))[core].mean() <= 2
    assert facts["seam_mode"] == ("plain" if shift == (0, 0) else "drift")


def test_a_small_drift_changes_the_result_by_at_most_one_level_in_the_core(seam):
    import cv2
    import numpy as np

    source, mask = gradient_scene(), region("rect")
    result = model_result(source, (3, 3))
    mixed, _, hard = composite(seam, source, result, mask)
    core = cv2.erode(hard.astype(np.uint8), np.ones((31, 31), np.uint8)) > 0
    assert np.abs(rgb_of(mixed) - rgb_of(result))[core].max() <= 1


def test_a_selection_filling_the_frame_is_just_the_result(seam):
    source = gradient_scene()
    result = model_result(source, (20, 10))
    mask = Image.new("L", source.size, 255)
    mixed, facts, _ = composite(seam, source, result, mask)
    assert facts["seam_mode"] == "plain" and facts["outer_ring_mad"] is None
    assert mixed.getpixel((200, 200)) == result.getpixel((200, 200))


def session_pair(root, origin, delivered, raw):
    import cv2
    import numpy as np

    def find(folder, prefix):
        return glob.glob(os.path.join(root, *folder, prefix + "*"))[0]

    uploads = ("data", "open-webui", "uploads")
    source_path = find(uploads, origin)
    final_path = find(uploads, delivered)
    raw_path = os.path.join(root, "apps", "comfyui", "output", raw)

    def read(path):
        return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)

    src, fin = read(source_path), read(final_path)
    # The mask the user painted is not stored; it is where the delivered picture differs.
    changed = (np.abs(fin.astype(np.int16) - src).max(2) > 0).astype(np.uint8)
    changed = cv2.morphologyEx(changed, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(changed)
    hard = np.zeros_like(changed)
    for index in range(1, count):
        if stats[index, 4] > 0.002 * changed.size:
            hard[labels == index] = 1
    mask = Image.fromarray(hard * 255, "L")
    return Path(source_path).read_bytes(), Path(raw_path).read_bytes(), mask, hard.astype(bool)


@pytest.mark.parametrize(
    ("origin", "delivered", "raw", "limit"),
    [
        ("55a845a6", "e82e87c5", "capella_edit_00006_.png", 16.1),
        ("e82e87c5", "f498fd87", "capella_edit_00007_.png", 14.4),
    ],
)
def test_real_krea_edits_join_within_a_quarter_of_the_natural_step(
    seam, origin, delivered, raw, limit
):
    root = os.environ.get("PEPITO_SESSION_DIR")
    if not root or not Path(root).is_dir():
        pytest.skip("PEPITO_SESSION_DIR does not point to the recorded session")
    import numpy as np

    source, model, mask, hard = session_pair(root, origin, delivered, raw)
    data, _ = seam.preserve_pixels(source, model, png(mask))
    mixed = Image.open(io.BytesIO(data)).convert("RGB")
    natural = band_step(rgb_of(Image.open(io.BytesIO(source))), hard)
    assert band_step(rgb_of(mixed), hard) <= 1.25 * natural
    assert band_step(rgb_of(mixed), hard) <= limit
    original = np.asarray(Image.open(io.BytesIO(source)).convert("RGB"))
    assert (np.asarray(mixed)[~hard] == original[~hard]).all()


def test_geometry_mismatch_and_wrong_size_mask_never_silently_distort_the_source():
    module = quality()
    source = png(Image.new("RGB", (128, 192), "red"))
    mask = png(Image.new("L", (128, 192), 255))
    with pytest.raises(module.PreservationError, match="formato"):
        module.preserve_pixels(source, png(Image.new("RGB", (192, 128), "blue")), mask)
    with pytest.raises(module.PreservationError, match="dimensões"):
        module.preserve_pixels(source, source, png(Image.new("L", (16, 16), 255)))
    with pytest.raises(module.PreservationError, match="vazia"):
        module.preserve_pixels(source, source, png(Image.new("L", (128, 192), 0)))


def test_grounded_mask_is_source_resolution_and_feather_does_not_leak_outside_boxes():
    module = quality()
    data, facts = module.region_mask(
        (1000, 1500), {"status": "local", "confidence": 0.95, "boxes": [[200, 300, 600, 700]]}
    )
    mask = Image.open(io.BytesIO(data))
    assert mask.size == (1000, 1500)
    assert mask.getpixel((0, 0)) == 0 and mask.getpixel((601, 1051)) == 0
    assert mask.getpixel((400, 750)) == 255
    assert mask.getpixel((200, 450)) < 255
    assert facts["coverage"] == pytest.approx(0.16)


@pytest.mark.parametrize(
    "answer",
    [
        {"status": "ambiguous", "confidence": 1, "boxes": [[100, 100, 500, 500]]},
        {"status": "local", "confidence": 0.89, "boxes": [[100, 100, 500, 500]]},
        {"status": "local", "confidence": True, "boxes": [[100, 100, 500, 500]]},
        {"status": "local", "confidence": 0.98, "boxes": [[500, 100, 200, 500]]},
        {"status": "local", "confidence": 0.98, "boxes": [[-1, 100, 200, 500]]},
        {"status": "local", "confidence": 0.98, "boxes": [[0, 0, 1000, 1000]]},
        {"status": "local", "confidence": 0.98, "boxes": [[10, 10, float("nan"), 500]]},
    ],
)
def test_ambiguous_or_invalid_regions_require_manual_selection(answer):
    module = quality()
    with pytest.raises(module.RegionSelectionError):
        module.region_mask((1000, 1500), answer)


def test_source_only_grounding_refuses_an_unusable_model_response(monkeypatch):
    module = quality()
    responses = [b'{"message":{"content":"not JSON"}}', b"x" * (module.MAX_RESPONSE_BYTES + 1)]
    for response in responses:
        monkeypatch.setattr(
            module.urllib.request, "urlopen", lambda *a, value=response, **k: io.BytesIO(value)
        )
        with pytest.raises(module.RegionSelectionError, match="segurança"):
            module.ground_regions(
                "http://127.0.0.1:11434",
                "test-vision",
                png(Image.new("RGB", (256, 384))),
                ["coat"],
                "Make the coat blue.",
            )


def test_grounding_contract_accepts_complete_bounded_regions_without_changing_source(monkeypatch):
    module = quality()
    source = png(Image.new("RGB", (1024, 1536), "red"))
    answer = {"status": "local", "confidence": 0.99, "boxes": [[200, 400, 600, 800]]}
    response = json.dumps({"message": {"content": json.dumps(answer)}}).encode()
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(response))
    mask_png, facts = module.ground_regions(
        "http://127.0.0.1:11434", "vision", source, ["coat"], "Make the coat blue."
    )
    assert Image.open(io.BytesIO(mask_png)).size == (1024, 1536)
    assert facts["boxes"] == answer["boxes"]
    assert Image.open(io.BytesIO(source)).getpixel((500, 750)) == (255, 0, 0)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (json.dumps({"medium": "art"}), "art"),
        (json.dumps({"medium": "photo"}), "photo"),
        (json.dumps({"medium": "anime"}), None),
        (json.dumps(["art"]), None),
        ("not JSON", None),
    ],
)
def test_medium_classification_accepts_only_photo_or_art(monkeypatch, content, expected):
    module = quality()
    sent = []

    def answer(request, **_kwargs):
        sent.append(json.loads(request.data))
        return io.BytesIO(json.dumps({"message": {"content": content}}).encode())

    monkeypatch.setattr(module.urllib.request, "urlopen", answer)
    source = png(Image.new("RGB", (2048, 1024), "blue"))
    if expected is None:
        with pytest.raises(module.RegionSelectionError):
            module.classify_medium("http://127.0.0.1:11434", "vision", source)
        return
    assert module.classify_medium("http://127.0.0.1:11434", "vision", source) == expected
    (payload,) = sent
    assert payload["format"] == module.MEDIUM_SCHEMA
    shown = Image.open(io.BytesIO(base64.b64decode(payload["messages"][1]["images"][0])))
    assert max(shown.size) <= 768, "only a small copy is shown to the vision model"


def test_rgb_telemetry_sees_a_color_change_that_grayscale_can_miss():
    module = quality()
    first = Image.new("RGB", (64, 96), (255, 0, 0))
    second = Image.new("RGB", first.size, (0, 130, 0))
    assert abs(first.convert("L").getpixel((0, 0)) - second.convert("L").getpixel((0, 0))) < 2
    assert module.preservation_metrics(png(first), png(second))["rgb_mad"] > 100


def test_qwen_reframing_pads_the_canvas_without_stretching_the_source():
    module = quality()
    source = png(Image.new("RGBA", (256, 384), (190, 40, 60, 255)))
    result = Image.open(io.BytesIO(module.source_for_canvas(source, (512, 384), reframe=True)))
    assert result.size == (512, 384)
    assert result.getpixel((0, 200))[3] == 0
    assert result.getpixel((256, 200)) == (190, 40, 60, 255)
    with pytest.raises(module.PreservationError):
        module.source_for_canvas(source, (512, 384))


def scene(size, band=0):
    """A picture with no dark pixel at its edges (a gradient), or with ``band`` rows of
    pure black at the top and the bottom, as the Qwen edits of 12/11 came out."""
    width, height = size
    image = Image.linear_gradient("L").resize(size).point(lambda value: 60 + value // 2)
    picture = Image.merge(
        "RGB", (image, image.transpose(Image.Transpose.ROTATE_90).resize(size), image)
    )
    if band:
        ImageDraw.Draw(picture).rectangle((0, 0, width - 1, band - 1), fill=(0, 0, 0))
        ImageDraw.Draw(picture).rectangle((0, height - band, width - 1, height - 1), fill=(0, 0, 0))
    return png(picture)


def dark_edges(data):
    """How many rows from the top and bottom, and columns from the left and right, are
    dark on average (mean < 5): the black bands, and transparent padding once flattened."""
    image = Image.open(io.BytesIO(data)).convert("RGBA")
    flat = Image.new("RGB", image.size, (0, 0, 0))
    flat.paste(image, mask=image.getchannel("A"))
    gray = flat.convert("L")
    width, height = gray.size

    def run(lines):
        count = 0
        for box in lines:
            if ImageStat.Stat(gray.crop(box)).mean[0] >= 5:
                break
            count += 1
        return count

    return (
        run((0, y, width, y + 1) for y in range(height)),
        run((0, y, width, y + 1) for y in reversed(range(height))),
        run((x, 0, x + 1, height) for x in range(width)),
        run((x, 0, x + 1, height) for x in reversed(range(width))),
    )


@pytest.mark.parametrize("size", [(736, 1072), (1097, 1817), (2438, 2183)])
@pytest.mark.parametrize("reframe", [False, True])
def test_a_qwen_canvas_rounded_to_32_never_pads_and_the_result_returns_to_the_source(size, reframe):
    """12/11: the 736×1072 scene went to Qwen as 704×1088 padded with transparency, which
    came out as pure black bands of 24-30 pixels at the top and the bottom."""
    module = quality()
    source = scene(size)
    canvas = module.bounded_canvas(size)
    assert canvas[0] % 32 == canvas[1] % 32 == 0
    assert canvas[0] * canvas[1] <= module.MAX_CANVAS_PIXELS
    assert canvas[0] / canvas[1] == pytest.approx(size[0] / size[1], rel=0.016)
    prepared = module.source_for_canvas(source, canvas, reframe=reframe)
    sent = Image.open(io.BytesIO(prepared))
    assert sent.size == canvas
    assert dark_edges(prepared) == (0, 0, 0, 0), "no band of black or transparency"
    # Qwen at resolution 0 answers on the first reference's canvas: an unchanged answer.
    delivered = module.source_proportion(source, prepared)
    result = Image.open(io.BytesIO(delivered))
    assert result.width / result.height == pytest.approx(size[0] / size[1], rel=0.003)
    if size[0] * size[1] <= module.MAX_CANVAS_PIXELS:
        assert result.size == size
    else:
        assert result.width * result.height <= module.MAX_CANVAS_PIXELS * 1.01
    assert dark_edges(delivered) == (0, 0, 0, 0)
    # Not stretched: the picture is the source's, scaled.
    expected = Image.open(io.BytesIO(source)).convert("RGB").resize(result.size)
    difference = ImageChops.difference(result.convert("RGB"), expected)
    assert max(high for _low, high in difference.getextrema()) < 24


def test_the_12_11_canvas_is_the_closest_one_to_the_scene_not_a_floor_of_each_side():
    module = quality()
    assert module.bounded_canvas((736, 1072)) == (736, 1088)
    assert not module.rounding_only((736, 1072), (704, 1088)), "5.8% is no rounding"
    assert module.rounding_only((736, 1072), (736, 1088))


def test_an_edit_of_a_picture_that_already_has_black_bands_does_not_widen_them():
    module = quality()
    source = scene((736, 1072), band=26)
    before = dark_edges(source)
    prepared = module.source_for_canvas(source, module.bounded_canvas((736, 1072)), reframe=True)
    delivered = module.source_proportion(source, prepared)
    after = dark_edges(delivered)
    assert after[0] <= before[0] + 1 and after[1] <= before[1] + 1
    assert after[2:] == (0, 0)


def test_a_result_in_a_requested_new_proportion_is_left_as_it_is():
    module = quality()
    source = scene((256, 384))
    widened = module.source_for_canvas(source, (512, 384), reframe=True)
    assert module.source_proportion(source, widened) is None


def _gradient(size=(200, 160)):
    image = Image.new("RGB", size)
    image.putdata(
        [(x % 256, (x + y) % 256, y % 256) for y in range(size[1]) for x in range(size[0])]
    )
    return image


def test_an_edit_that_drifted_darker_is_matched_back_outside_the_area():
    # 15/11: each edit came back 1-3 levels darker, -6% over four edits of one picture.
    module = quality()
    source = _gradient()
    edited = source.point(lambda value: max(0, round(value * 0.96)))
    ImageDraw.Draw(edited).rectangle((60, 40, 139, 119), fill=(0, 0, 255))  # the change
    mask = Image.new("L", source.size, 0)
    ImageDraw.Draw(mask).rectangle((60, 40, 139, 119), fill=255)
    matched, facts = module.tone_match(png(source), png(edited), png(mask))
    assert facts["applied"] and matched is not None
    result = Image.open(io.BytesIO(matched)).convert("RGB")
    outside = ImageChops.invert(mask)
    for want, got in zip(
        ImageStat.Stat(source, outside).mean, ImageStat.Stat(result, outside).mean
    ):
        assert abs(want - got) < 0.6
    assert result.getpixel((100, 80))[2] > 240, "the requested change survives"


def test_a_global_change_or_an_undrifted_edit_is_left_alone():
    module = quality()
    source = _gradient()
    night = source.point(lambda value: value // 3)
    assert module.tone_match(png(source), png(night))[1]["reason"] == "reference_changed"
    assert module.tone_match(png(source), png(source))[1]["reason"] == "no_drift"
    wide = source.resize((320, 160))
    assert module.tone_match(png(source), png(wide))[1]["reason"] == "geometry_changed"


def test_an_unmasked_edit_is_measured_on_the_side_bands_not_on_the_whole_picture():
    # Open WebUI's own edits (no area): the reference is the two side bands, so a big change
    # of clothes in the middle neither blocks the correction nor skews its curve.
    module = quality()
    source = _gradient((200, 160))
    drifted = source.point(lambda value: min(255, value + 12))
    ImageDraw.Draw(drifted).rectangle((50, 20, 149, 139), fill=(0, 0, 255))  # new clothes
    whole = ImageStat.Stat(ImageChops.difference(source, drifted)).mean
    assert sum(whole) / 3 > module.TONE_MAX_UNMASKED_MAD, "the whole picture would refuse it"
    matched, facts = module.tone_match(png(source), png(drifted))
    assert facts["applied"] is True and matched is not None
    result = Image.open(io.BytesIO(matched)).convert("RGB")
    sides = module._side_bands(source.size)
    for want, got in zip(ImageStat.Stat(source, sides).mean, ImageStat.Stat(result, sides).mean):
        assert abs(want - got) < 0.6
    assert result.getpixel((100, 80))[2] > 240, "the requested change survives"


def test_an_unmasked_new_scene_is_not_matched():
    module = quality()
    source = _gradient()
    scene = ImageChops.invert(source)
    assert module.tone_match(png(source), png(scene))[1]["reason"] == "reference_changed"


def test_the_sides_are_a_narrow_uniform_band_and_the_masked_path_ignores_them():
    module = quality()
    bands = module._side_bands((200, 100))
    assert ImageStat.Stat(bands).mean[0] / 255 == pytest.approx(2 * 0.15)
    assert bands.getpixel((0, 50)) == bands.getpixel((199, 50)) == 255
    assert bands.getpixel((100, 50)) == 0
    # With an area the reference stays the picture outside it, whatever the sides do.
    source = _gradient()
    edited = source.point(lambda value: max(0, round(value * 0.96)))
    mask = Image.new("L", source.size, 0)
    ImageDraw.Draw(mask).rectangle((0, 0, 99, 159), fill=255)  # the whole left half
    matched, facts = module.tone_match(png(source), png(edited), png(mask))
    assert facts["applied"] and "reference_mad" in facts
    result = Image.open(io.BytesIO(matched)).convert("RGB")
    outside = ImageChops.invert(mask)
    for want, got in zip(
        ImageStat.Stat(source, outside).mean, ImageStat.Stat(result, outside).mean
    ):
        assert abs(want - got) < 0.6
