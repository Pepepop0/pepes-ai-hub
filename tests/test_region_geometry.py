"""SPEC-204 and SPEC-307: the pure parts of the crop (Pillow; the stitch also needs numpy and
OpenCV, so it runs in the managed Open WebUI environment, not the Hub's own)."""

from __future__ import annotations

import io

import pytest
from PIL import Image, ImageChops, ImageDraw
from test_image_quality_preservation import png, quality, seam  # noqa: F401

SIZE = (768, 1152)


def mask_png(size, *shapes) -> bytes:
    image = Image.new("L", size, 0)
    draw = ImageDraw.Draw(image)
    for kind, box in shapes:
        (draw.ellipse if kind == "ellipse" else draw.rectangle)(box, fill=255)
    return png(image)


# --- the pure parts -----------------------------------------------------------------------


def test_the_crop_is_the_area_plus_its_context_inside_the_picture():
    module = quality()
    area = mask_png(SIZE, ("rect", (300, 400, 499, 599)))  # 200 x 200
    plan = module.crop_plan(area, 1_048_576)
    # 35% of 200 is 70, under the 128 px floor: the floor applies on each side.
    assert plan["box"] == [172, 272, 628, 728]
    assert all(side % 32 == 0 for side in plan["size"])
    assert abs(plan["size"][0] * plan["size"][1] - 1_048_576) < 0.05 * 1_048_576
    assert plan["scale"] == round(plan["size"][0] / 456, 4)
    assert plan["share"] == 456 * 456 / (SIZE[0] * SIZE[1])
    big = mask_png((4000, 4000), ("rect", (1000, 1000, 2999, 2999)))
    plan = module.crop_plan(big, 1_572_864)
    assert plan["box"] == [300, 300, 3700, 3700], "35% of 2000 is 700"
    assert plan["scale"] < 1, "a crop larger than native is scaled down"
    assert plan["share"] == 3400 * 3400 / 4000**2


def test_the_crop_stays_inside_the_picture_and_says_how_much_of_it_it_covers():
    module = quality()
    corner = mask_png(SIZE, ("rect", (0, 0, 119, 99)))
    plan = module.crop_plan(corner, 1_048_576)
    assert plan["box"][:2] == [0, 0], "no negative start"
    assert plan["box"][2] == 120 + 128 and plan["box"][3] == 100 + 128
    most = mask_png(SIZE, ("rect", (100, 100, 700, 1050)))
    assert module.crop_plan(most, 1_048_576)["share"] == 1.0
    with pytest.raises(module.PreservationError):
        module.crop_plan(png(Image.new("L", SIZE, 0)), 1_048_576)


def test_a_crop_is_used_only_under_half_of_the_picture():
    """Lab 204: the crop won where the area was small and broke where it covered most of the
    picture. The share is the crop's (area and context), not the area's."""
    module = quality()
    assert module.REGION_CROP_MAX_SHARE == 0.5
    under = mask_png(SIZE, ("rect", (192, 288, 537, 633)))  # crop 601 x 601: 0.408
    over = mask_png(SIZE, ("rect", (154, 173, 614, 691)))  # crop 716 x 774: 0.626
    assert module.crop_plan(under, 1_048_576)["share"] < module.REGION_CROP_MAX_SHARE
    assert module.crop_plan(over, 1_048_576)["share"] >= module.REGION_CROP_MAX_SHARE


def test_the_crop_source_is_that_part_scaled_to_the_native_size():
    module = quality()
    source = Image.new("RGB", SIZE, (10, 20, 30))
    source.paste((250, 0, 0), (300, 400, 500, 600))
    plan = module.crop_plan(mask_png(SIZE, ("rect", (300, 400, 499, 599))), 1_048_576)
    data = module.crop_source(png(source), plan)
    cropped = Image.open(io.BytesIO(data))
    assert list(cropped.size) == plan["size"]
    assert cropped.convert("RGB").getpixel((cropped.width // 2, cropped.height // 2)) == (250, 0, 0)
    assert cropped.convert("RGB").getpixel((2, 2)) == (10, 20, 30)


def test_the_stitch_gives_back_the_original_exactly_outside_the_area_and_the_crop(seam):  # noqa: F811
    module = seam
    source = Image.new("RGB", SIZE, (90, 120, 150))
    ImageDraw.Draw(source).rectangle((0, 0, 767, 200), fill=(40, 60, 80))
    area = mask_png(SIZE, ("ellipse", (330, 420, 480, 560)))
    plan = module.crop_plan(area, 1_048_576)
    # The model redraws the crop, at its own size, slightly different everywhere.
    redrawn = Image.new("RGB", tuple(plan["size"]), (200, 60, 60))
    data, facts = module.stitch_crop(png(source), png(redrawn), area, plan)
    result = Image.open(io.BytesIO(data)).convert("RGB")
    assert result.size == SIZE
    inside = Image.open(io.BytesIO(area)).convert("L")
    outside = inside.point(lambda value: 255 if value == 0 else 0)
    difference = ImageChops.difference(result, source.convert("RGB"))
    assert ImageChops.multiply(difference.convert("L").point(lambda v: 255 if v else 0), outside)
    left, top, right, bottom = plan["box"]
    outside_crop = Image.new("L", SIZE, 255)
    outside_crop.paste(0, (left, top, right, bottom))
    assert ImageChops.multiply(difference.convert("L"), outside_crop).getbbox() is None
    assert ImageChops.multiply(difference.convert("L"), outside).getbbox() is None, "exact outside"
    assert result.getpixel((405, 490)) != (90, 120, 150), "the area changed"
    assert facts["crop_box"] == plan["box"]
    assert "band_step_final" in facts and "band_step_source" in facts


def test_a_result_that_changed_shape_or_an_area_outside_the_crop_is_refused(seam):  # noqa: F811
    module = seam
    source = Image.new("RGB", SIZE, (90, 120, 150))
    area = mask_png(SIZE, ("rect", (330, 420, 480, 560)))
    plan = module.crop_plan(area, 1_048_576)
    with pytest.raises(module.PreservationError, match="formato"):
        module.stitch_crop(png(source), png(Image.new("RGB", (512, 1024), "red")), area, plan)
    wider = mask_png(SIZE, ("rect", (100, 420, 480, 560)))
    with pytest.raises(module.PreservationError, match="recorte"):
        module.stitch_crop(
            png(source), png(Image.new("RGB", tuple(plan["size"]), "red")), wider, plan
        )
