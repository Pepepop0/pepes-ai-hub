"""Image composer choices, persistent picture context and real filter dispatch."""

from __future__ import annotations

import json

import pytest
from test_image_edit_dispatch import (
    capella_stacks,  # noqa: F401
    edit_config,  # noqa: F401
    new,
    preset,  # noqa: F401
    routed,  # noqa: F401
    settings,  # noqa: F401
    world,  # noqa: F401
)
from test_open_webui_image_filter import load_module, run


def namespace(turn):
    return turn.filter.inlet.__func__.__globals__


@pytest.mark.parametrize(
    ("switches", "expected"),
    [
        ([], "auto"),
        (["pepito_image_art"], "art"),
        (["pepito_image_photo"], "photo"),
        (["pepito_image_art", "pepito_image_photo"], "auto"),
    ],
)
def test_style_switches_have_three_unambiguous_modes(switches, expected):
    assert load_module()["style_mode"](switches) == expected


@pytest.mark.parametrize(
    ("text", "style"),
    [
        ("quero fotorrealista", "photo"),
        ("uma foto realista", "photo"),
        ("a realistic photo", "photo"),
        ("anime com traços suaves", "art"),
        ("um desenho", "art"),
        ("uma ilustração", "art"),
        ("arte digital", "art"),
        ("cartoon", "art"),
        ("mangá", "art"),
        ("hentai", "art"),
    ],
)
def test_auto_style_uses_explicit_words_before_source_style(text, style):
    module = load_module()
    assert module["resolve_style"]("auto", text, {}, {"style": "art"}) == style


def test_auto_edits_keep_source_style_and_new_pictures_use_router_or_photo_default():
    resolve = load_module()["resolve_style"]
    assert resolve("auto", "change the background", {}, {"style": "art"}) == "art"
    # "a foto" names the picture being edited; it is no request for a photograph.
    assert resolve("auto", "muda o cabelo na foto", {"style": "photo"}, {"style": "art"}) == "art"
    assert resolve("auto", "deixa ela realista", {}, {"style": "art"}) == "photo"
    assert resolve("auto", "a scene", {"style": "art"}) == "art"
    assert resolve("auto", "a scene", {}) == "photo"
    assert resolve("photo", "anime", {"style": "art"}, {"style": "art"}) == "photo"
    assert resolve("art", "photo", {}, {"style": "photo"}) == "art"


@pytest.mark.parametrize("aspect", ["portrait", "landscape", "square", "tall", "wide"])
@pytest.mark.parametrize("base", ["1536x1024", "1024x1024", "1280x768"])
def test_aspect_sizes_keep_pixel_budget_and_are_multiples_of_sixteen(aspect, base):
    module = load_module()
    width, height = map(int, module["aspect_size"](base, aspect).split("x"))
    bw, bh = map(int, base.split("x"))
    assert width % 16 == height % 16 == 0
    assert width * height == pytest.approx(bw * bh, rel=0.025)
    assert width / height == pytest.approx(module["ASPECT_RATIOS"][aspect], rel=0.025)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("full body", "portrait"),
        ("corpo inteiro horizontal", "landscape"),
        ("um retrato 16:9", "wide"),
        ("um wallpaper", "wide"),
        ("vertical 9:16", "tall"),
        ("imagem quadrada", "square"),
        # Without a frame word or the router's answer, the stack's own size stays.
        ("close-up", ""),
        ("uma cena na floresta", ""),
    ],
)
def test_aspect_words_override_inference(text, expected):
    assert load_module()["requested_aspect"](text, {}) == expected


def test_router_feature_fields_survive_parsing(monkeypatch):
    from test_open_webui_image_filter import FakeRouter, routed_env

    module = load_module()
    decision = {
        **new("An illustrated landscape."),
        "style": "art",
        "aspect": "wide",
        "count": 3,
        "change": "strong",
    }
    routed_env(monkeypatch, FakeRouter(json.dumps(decision)))
    answer = run(module["route"]("model", [], []))
    assert {key: answer[key] for key in ("style", "aspect", "count", "change")} == {
        "style": "art",
        "aspect": "wide",
        "count": 3,
        "change": "strong",
    }
    assert module["feature_decision"]({"count": True, "aspect": "huge"})["count"] == 0


@pytest.mark.parametrize(
    ("text", "decision", "valve", "expected"),
    [
        ("gere três imagens", {"count": 4}, 2, 3),
        ("make one picture", {"count": 3}, 4, 1),
        # "uma" is the article in "faz uma imagem": the preference still applies.
        ("faz uma imagem dela", {"count": 0}, 3, 3),
        ("make variations", {"count": 2}, 4, 2),
        ("make a scene", {"count": 0}, 3, 3),
        ("usa a imagem 4", {}, 0, 0),
        ("make a scene", {}, 0, 0),
    ],
)
def test_explicit_count_beats_valve_and_reference_numbers_are_not_counts(
    text, decision, valve, expected
):
    module = load_module()
    valves = module["Filter"].UserValves(images_per_turn=valve)
    assert module["requested_count"](text, decision, valves) == expected


def test_new_upload_expires_selection_and_inline_pixels_are_never_persisted(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    module = load_module()
    entries = module["described_entries"](
        "chat",
        [
            {"url": "data:image/png;base64,cGl4ZWxz", "by": "user", "group": 1},
            {"url": "/second", "by": "assistant", "group": 2},
        ],
    )
    module["select_picture"]("chat", entries[0], entries)
    assert module["described_entries"]("chat", entries)[0]["current"] is True
    entries.append({"url": "/new-upload", "by": "user", "group": 3})
    described = module["described_entries"]("chat", entries)
    assert described[-1]["current"] is True
    assert "selected" not in module["picture_record"]("chat")
    text = next((tmp_path / "pepito-characters").glob("*.pictures.json")).read_text()
    assert "data:image" not in text and "cGl4ZWxz" not in text


def test_chat_model_context_lists_a_bounded_window_and_always_the_current_picture():
    module = load_module()
    entries = [
        {"url": f"/p{n}", "number": n, "by": "assistant", "description": f"picture {n} <b>"}
        for n in range(1, 13)
    ]
    entries[2]["current"] = True
    context = module["pictures_context"](entries)
    listed = [n for n in range(1, 13) if f"picture {n} " in context]
    assert listed == [1, 3, 6, 7, 8, 9, 10, 11, 12]
    assert "3 older pictures not listed" in context
    assert "<b>" not in context and context.count("</context>") == 1
