"""SPEC-203 phase A: the ladder of "nada mudou" (boost, redraw, new) and the automatic retry of
an edit that came out almost the same, both behind valves that are off by default."""

from __future__ import annotations

import json

import pytest
from test_image_edit_dispatch import edit
from test_image_gpu_serialization_and_trace import trace_records
from test_image_redraw import DESCRIBE
from test_open_webui_image_filter import load_module

LADDER = {"boost": True, "redraw": "restage_current", "new": True}
NO_CHANGE = {"request_mode": "retry", "correction": "no_change", "changes": [], "preserve": []}
NO_CHANGE_TEXT = "nada mudou"
RETRY = {**edit("ignored"), "plan": NO_CHANGE}


def recipe(**facts) -> dict:
    return {
        "prompt": "Make the background blue.",
        "refs": [{"image": 1, "role": "A", "purpose": "the original source"}],
        "base": 1,
        "current": 2,
        "part": "edit",
        "boost": 3.0,
        "edit_mode": "local",
        "changes": ["a blue background"],
        "preserve": ["face"],
        **facts,
    }


def climb(previous: dict, ladder=LADDER) -> tuple[dict, dict | None]:
    decision = {**edit("The same, redrawn."), "plan": dict(NO_CHANGE)}
    return load_module()["retry_decision"](decision, previous, ladder)


# --- the rungs, from each state ---------------------------------------------------------


def test_above_the_floor_the_same_part_goes_one_boost_down():
    decision, correction = climb(recipe(boost=3.0))
    assert correction["retry_step"] == "boost" and correction["boost"] == 3.0
    assert decision["edit_mode"] == "local"
    # ``edit_strengths`` takes the step down from the last try, never below the floor.
    strengths = load_module()["edit_strengths"]
    assert strengths("subtle", 1, retry_from=correction["boost"]) == ([2.0], "correction")
    assert strengths("subtle", 1, retry_from=1.5) == ([1.0], "correction")


def test_at_the_floor_the_next_try_is_the_redraw_part_on_the_current_picture():
    decision, correction = climb(recipe(boost=1.0))
    assert correction["retry_step"] == "redraw" and correction["ladder_part"] == "restage_current"
    assert decision["edit_mode"] == "redraw" and decision["action"] == "edit_image"
    assert decision["references"] == [
        {"image": 2, "role": "A", "purpose": load_module()["REDRAW_PURPOSE"]}
    ]
    plan = decision["plan"]
    assert plan["base"] == 2 and plan["scope"] == "global" and plan["region_targets"] == []
    assert correction["fresh"] and correction["mask_png"] is None and correction["boost"] is None


def test_a_redraw_part_that_cannot_run_goes_straight_to_the_new_picture():
    _, correction = climb(recipe(boost=1.0), {**LADDER, "redraw": None})
    assert correction["retry_step"] == "new" and correction["ladder_part"] == "t2i_described"


def test_after_a_redraw_the_next_try_is_the_new_picture():
    for last in (
        recipe(boost=None, part="restage", edit_mode="redraw", redraw_part="restage_current"),
        recipe(boost=1.0, edit_mode="redraw", redraw_part="edit_free"),
    ):
        _, correction = climb(last)
        assert correction["retry_step"] == "new" and correction["ladder_part"] == "t2i_described"


def test_a_fixed_ref_boost_in_the_valves_skips_the_boost_rung():
    _, correction = climb(recipe(boost=3.0), {**LADDER, "boost": False})
    assert correction["retry_step"] == "redraw"


@pytest.mark.parametrize(
    "last",
    [
        recipe(part="swap", boost=None),
        recipe(part="remove", boost=None),
        recipe(boost=None, edit_method="qwen"),
        recipe(
            refs=[
                {"image": 1, "role": "A", "purpose": "x"},
                {"image": 3, "role": "B", "purpose": "y"},
            ]
        ),
    ],
)
def test_a_recipe_with_no_ladder_retries_as_before(last):
    _, correction = climb(last)
    assert "retry_step" not in correction and correction["escalate"] is True


def test_a_ladder_with_no_rung_left_repeats_at_the_floor_as_before():
    decision, correction = climb(recipe(boost=1.0), {"boost": True, "redraw": None, "new": False})
    assert "retry_step" not in correction and correction["boost"] == 1.0
    assert decision["edit_mode"] == "local"


def test_with_the_valve_off_the_floor_repeats_the_same_part():
    decision, correction = climb(recipe(boost=1.0), None)
    assert "retry_step" not in correction and decision["edit_mode"] == "local"
    assert "ladder_part" not in correction


def test_the_valves_default_to_off_and_the_ladder_follows_them():
    module = load_module()
    valves = module["Filter"].UserValves()
    assert valves.retry_ladder is False and valves.auto_retry is False
    assert module["retry_ladder"](valves, {"restage": {}}, "restage_current") is None
    stack = {"restage": {}, "generate": {}, "edit": {"strength": {"key": "ref_boost"}}}
    ladder = module["retry_ladder"]({"retry_ladder": True}, stack, "restage_current")
    assert ladder == {"boost": True, "redraw": "restage_current", "new": True}
    # A redraw on the valve's t2i_described IS the new-picture rung; a fixed boost never moves.
    ladder = module["retry_ladder"](
        {"retry_ladder": True, "ref_boost_manual": 2.0}, stack, "t2i_described"
    )
    assert ladder == {"boost": False, "redraw": None, "new": True}


# --- "nada mudou" three times, with the valve on and off -------------------------------


def steps(tmp_path) -> list[str]:
    return [r["step"] for r in trace_records(tmp_path) if r["event"] == "retry_step"]


def strong_edit(ollama):
    ollama.answers["deixa o fundo azul"] = {
        **edit("Make the background blue."),
        "edit_mode": "local",
        "change": "strong",
    }
    ollama.answers[NO_CHANGE_TEXT] = RETRY
    ollama.answers[DESCRIBE] = json.dumps({"description": "A woman in a red dress, in a kitchen."})


# --- the automatic retry ------------------------------------------------------------------


def request(ollama, change="strong") -> None:
    ollama.answers["deixa o fundo azul"] = {
        **edit("Make the background blue."),
        "edit_mode": "local",
        "change": change,
    }
