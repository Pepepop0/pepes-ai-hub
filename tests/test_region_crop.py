"""SPEC-307: the edit of a crop of the source, chosen by the size of the area (SPEC-204 made
the crop; lab 204 decided when it pays). A small area is edited on a crop with context at the
model's own size; an area whose crop covers half the picture or more edits the whole picture
with the area. The silhouette (SAM) area is gone: an edit never asks a detector for it."""

from __future__ import annotations

import pytest
from PIL import Image


@pytest.mark.parametrize(("right", "used"), [(368, True), (369, False)])
def test_exactly_half_is_not_a_crop_but_under_half_is(monkeypatch, right, used):
    import sys

    from PIL import ImageDraw
    from test_image_quality_preservation import png, quality
    from test_open_webui_image_filter import load_module, run

    module = load_module()
    preservation = quality()
    source = png(Image.new("RGB", (1000, 1000), (90, 120, 150)))
    area = Image.new("L", (1000, 1000), 0)
    ImageDraw.Draw(area).rectangle((0, 0, right, 871), fill=255)

    async def picture_bytes(_url):
        return source

    monkeypatch.setitem(module, "picture_bytes", picture_bytes)
    monkeypatch.setitem(sys.modules, "open_webui.pepito_image_preservation", preservation)
    plan, cropped = run(module["crop_area"]({"family": "qwen21"}, "memory://source", png(area), {}))
    assert (cropped is not None) is used
    if not used:
        assert plan["share"] == 0.5
    else:
        assert plan["share"] < 0.5
