"""Consumer-visible deformation boundaries and conservative seam correction."""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageDraw

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    path = ROOT / "assets" / "open-webui" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name + "_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def engine():
    return load("pepito_edit_methods")


@pytest.fixture(scope="module")
def preservation():
    return load("pepito_image_preservation")


def png(image):
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def picture(data):
    with Image.open(io.BytesIO(data)) as image:
        return image.convert("RGBA")


def assert_exterior_exact(source, result, mask):
    outside = mask.point(lambda value: 0 if value else 255)
    for difference in ImageChops.difference(source, result).split():
        assert ImageChops.multiply(difference, outside).getbbox() is None
    assert result.getchannel("A").tobytes() == source.getchannel("A").tobytes()


def marker(size=(160, 160)):
    image = Image.new("RGBA", size, (25, 35, 45, 128))
    ImageDraw.Draw(image).rectangle((68, 68, 91, 91), fill=(230, 50, 50, 128))
    return image


def red_bounds(image):
    return image.getchannel("R").point(lambda value: 255 if value > 150 else 0).getbbox()


@pytest.mark.parametrize("mode", ["magnify", "width", "height"])
@pytest.mark.parametrize("factor", [0.625, 1.6, 2.0])
def test_growth_and_shrink_follow_the_selected_axes(engine, mode, factor):
    source = marker()
    boxes = [[250, 250, 750, 750]]
    mask = engine.union_mask(source.size, boxes, mode=mode)
    result = picture(
        engine.warp_image(png(source), png(mask), {"mode": mode, "factor": factor}, boxes=boxes)
    )
    old, new = red_bounds(source), red_bounds(result)
    for axis, enabled in ((0, mode != "height"), (1, mode != "width")):
        previous, current = old[axis + 2] - old[axis], new[axis + 2] - new[axis]
        if not enabled:
            assert abs(current - previous) <= 2
        elif factor > 1:
            assert current > previous
        else:
            assert current < previous
    assert_exterior_exact(source, result, mask)


def test_extend_down_anchors_top_and_preserves_everything_above(engine):
    source = Image.new("RGBA", (160, 200), (30, 40, 50, 93))
    ImageDraw.Draw(source).rectangle((65, 60, 94, 99), fill=(230, 50, 50, 93))
    boxes = [[300, 250, 700, 550]]
    mask = engine.union_mask(source.size, boxes, mode="extend_down")
    grown = picture(
        engine.warp_image(
            png(source), png(mask), {"mode": "extend_down", "factor": 1.6}, boxes=boxes
        )
    )
    shrunk = picture(
        engine.warp_image(
            png(source), png(mask), {"mode": "extend_down", "factor": 0.625}, boxes=boxes
        )
    )
    assert red_bounds(grown)[3] > red_bounds(source)[3]
    assert red_bounds(shrunk)[3] < red_bounds(source)[3]
    assert grown.crop((0, 0, 160, 50)).tobytes() == source.crop((0, 0, 160, 50)).tobytes()
    assert_exterior_exact(source, grown, mask)
    assert_exterior_exact(source, shrunk, mask)


def test_one_sided_selection_does_not_move_the_other_target(engine):
    source = Image.new("RGBA", (240, 160), (25, 35, 45, 110))
    draw = ImageDraw.Draw(source)
    draw.rectangle((43, 68, 66, 91), fill=(230, 50, 50, 110))
    draw.rectangle((173, 68, 196, 91), fill=(230, 50, 50, 110))
    boxes = [[625, 250, 917, 750]]
    mask = engine.union_mask(source.size, boxes)
    result = engine.warp_regions(source, boxes, 1.6)
    assert result.crop((0, 0, 120, 160)).tobytes() == source.crop((0, 0, 120, 160)).tobytes()
    assert (
        red_bounds(result.crop((120, 0, 240, 160)))[2]
        - red_bounds(result.crop((120, 0, 240, 160)))[0]
        > 24
    )
    assert_exterior_exact(source, result, mask)


def test_mask_only_derives_separate_target_centers_not_one_enclosing_region(engine):
    source = Image.new("RGBA", (256, 128), (25, 35, 45, 77))
    draw = ImageDraw.Draw(source)
    draw.rectangle((31, 55, 40, 72), fill=(230, 50, 50, 77))
    draw.rectangle((215, 55, 224, 72), fill=(230, 50, 50, 77))
    mask = Image.new("L", source.size, 0)
    mask.paste(255, (16, 32, 56, 96))
    mask.paste(255, (200, 32, 240, 96))
    result = picture(engine.warp_image(png(source), png(mask), {"mode": "magnify", "factor": 1.6}))
    for bounds in ((16, 32, 56, 96), (200, 32, 240, 96)):
        old, new = red_bounds(source.crop(bounds)), red_bounds(result.crop(bounds))
        assert new[2] - new[0] > old[2] - old[0]
        assert abs((new[0] + new[2]) - (old[0] + old[2])) <= 2
    assert_exterior_exact(source, result, mask)


def test_full_resolution_soft_mask_remains_the_final_authority(engine):
    source = marker()
    mask = Image.new("L", source.size, 0)
    mask.paste(255, (80, 50, 110, 110))
    mask.putpixel((84, 80), 0)  # A hole inside a broad supplied box must remain exact.
    mask.putpixel((85, 80), 64)
    result = picture(
        engine.warp_image(
            png(source), png(mask), {"mode": "width", "factor": 1.6}, boxes=[[250, 250, 750, 750]]
        )
    )
    assert result.getpixel((84, 80)) == source.getpixel((84, 80))
    assert_exterior_exact(source, result, mask)


@pytest.mark.parametrize(
    "deformation",
    [
        None,
        {},
        {"mode": "width"},
        {"mode": "spin", "factor": 1.6},
        {"mode": "width", "factor": float("nan")},
        {"mode": "width", "factor": True},
        {"mode": "width", "factor": 2.1},
    ],
)
def test_missing_or_invalid_shape_plan_fails_instead_of_guessing(engine, deformation):
    source = marker()
    with pytest.raises(ValueError):
        engine.warp_image(png(source), png(Image.new("L", source.size, 255)), deformation)


@pytest.mark.parametrize(
    "boxes", [[], [[500, 500, 500, 600]], [[-1, 0, 500, 500]], [[0, 0, float("inf"), 500]]]
)
def test_invalid_boxes_do_not_authorize_a_default_warp(engine, boxes):
    source = marker()
    with pytest.raises(ValueError):
        engine.warp_image(
            png(source),
            png(Image.new("L", source.size, 255)),
            {"mode": "magnify", "factor": 1.6},
            boxes=boxes,
        )


def test_wrong_size_and_empty_masks_are_rejected(engine):
    source = marker()
    for mask in (Image.new("L", (16, 16), 255), Image.new("L", source.size, 0)):
        with pytest.raises(ValueError):
            engine.warp_image(png(source), png(mask), {"mode": "magnify", "factor": 1.6})


def test_harmonization_does_not_replay_an_enlargement_request(engine):
    instruction = "Make the right sleeve twice as wide and much longer."
    prompt = engine.harmonize_prompt(instruction)
    assert instruction not in prompt
    assert "CURRENT size" in prompt and "proportions" in prompt
    assert "do not enlarge" in prompt


def seam_fixture():
    source = Image.new("RGBA", (192, 192), (140, 120, 100, 128))
    mask = Image.new("L", source.size, 0)
    mask.paste(255, (48, 48, 144, 144))
    return source, mask


@pytest.fixture
def numeric_preservation(monkeypatch, preservation):
    """The seam needs numpy and OpenCV: the managed Open WebUI environment has them."""
    pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    path = ROOT / "assets" / "open-webui" / "pepito_swap_compose.py"
    spec = importlib.util.spec_from_file_location("open_webui.pepito_swap_compose", path)
    compose = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, compose)  # its dataclasses look themselves up
    spec.loader.exec_module(compose)
    return preservation


@pytest.mark.parametrize("color", [(40, 80, 210, 255), (120, 100, 80, 255)])
def test_purposeful_recolor_survives_inside_and_the_exterior_stays_exact(
    numeric_preservation, color
):
    source, mask = seam_fixture()
    edited = source.copy()
    edited.paste(color, (48, 48, 144, 144))
    data, _ = numeric_preservation.preserve_pixels(png(source), png(edited), png(mask))
    result = picture(data)
    assert result.getpixel((96, 96))[:3] == color[:3]
    assert_exterior_exact(source, result, mask)


def test_no_external_support_keeps_normal_blending(numeric_preservation):
    source, _ = seam_fixture()
    mask = Image.new("L", source.size, 255)
    edited = Image.new("RGBA", source.size, (120, 100, 80, 255))
    data, _ = numeric_preservation.preserve_pixels(png(source), png(edited), png(mask))
    assert picture(data).getpixel((0, 0)) == (120, 100, 80, 128)
