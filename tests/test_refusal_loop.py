"""SPEC-303: a plan refusal never loops (12/11 23:41-23:45, the portrait chat).

The real turns are in ``tests/fixtures/router_cases.json`` with the chat's state as the
session left it: the recipe delivered last (``last_edit``, its purposes joined by the old
filter) and the pending question the refusals left behind.
"""

from __future__ import annotations

import json

import pytest
import router_cases
from router_cases import LAST_RECORD, load_cases, run_case
from test_open_webui_image_filter import load_module

MODULE = load_module()
REFUSALS_KEY = MODULE["REFUSALS_KEY"]
PENDING_KEY = MODULE["PENDING_QUESTION_KEY"]
OLD_REFUSAL = "O plano não explicou o que usar de cada referência"
LONG = "the character's face, hair, body type, clothing and jewelry, " * 4


@pytest.fixture(autouse=True)
def pod_stack(monkeypatch):
    """The pod's chat model restages a picture (the redraw part of these turns)."""
    monkeypatch.setattr(router_cases, "STACK", {**router_cases.STACK, "restage": {}})


def events_named(events: list[dict], name: str) -> list[dict]:
    return [event for event in events if event["event"] == name]


def rerouted(case: dict, **answer) -> dict:
    """``case`` with the router's answer changed by ``answer`` (top-level keys)."""
    raw = json.loads(case["router_raw"])
    return {**case, "router_raw": json.dumps({**raw, **answer})}


def turn(case: dict, monkeypatch, streak: dict | None = None):
    """One turn of ``case`` starting from ``streak``: (state, events, streak it leaves)."""
    _body, metadata, events = run_case({**case, "refusals": streak}, monkeypatch)
    return metadata["pepito_image_chat"], events, LAST_RECORD["record"].get(REFUSALS_KEY)


@pytest.mark.parametrize(
    "case_id",
    [
        "2026-11-12T23:41:03",
        "2026-11-12T23:42:17",
        "2026-11-12T23:42:41",
        "2026-11-12T23:43:02",
        "2026-11-12T23:45:25",
    ],
)
def test_the_refused_turns_of_12_11_make_a_picture(case_id, monkeypatch):
    (case,) = load_cases(case_id)
    assert case["observed"]["refusal"], "the session refused it"
    state, events, _streak = turn(case, monkeypatch)
    assert not events_named(events, "refusal")
    assert state.get("edit_image"), "an edit is queued"


def test_a_retry_of_a_recipe_with_long_joined_purposes_is_not_refused(monkeypatch):
    """23:45:25 "Não mudou nada, refaça": the recipe's purpose came back as 246 characters."""
    (case,) = load_cases("2026-11-12T23:45:25")
    joined = " ".join(case["last_edit"]["slots"][0]["purposes"])
    assert len(joined) > MODULE["PURPOSE_LIMIT"]
    state, _events, _streak = turn(case, monkeypatch)
    assert state["edit_image"]["images"] == [router_cases.picture_url(case, 4)]


def test_a_redraw_whose_base_is_not_among_its_references_starts_from_the_reference(
    monkeypatch,
):
    """23:41:03: base 2, the only reference picture 1."""
    (case,) = load_cases("2026-11-12T23:41:03")
    state, events, _streak = turn(case, monkeypatch)
    assert state["edit_image"]["images"] == [router_cases.picture_url(case, 1)]
    decision = events_named(events, "router_result")[0]["decision"]
    assert decision["plan"]["base"] == 2, "the router's plan is traced as it came"


def test_a_long_purpose_the_router_wrote_is_still_refused_but_asks_nothing(monkeypatch):
    (case,) = load_cases("2026-11-12T23:41:03")
    raw = json.loads(case["router_raw"])
    refs = [{**raw["references"][0], "purpose": LONG}]
    state, events, _streak = turn(rerouted(case, references=refs), monkeypatch)
    assert OLD_REFUSAL in events_named(events, "refusal")[0]["message"]
    assert "?" not in state["reply"]
    assert PENDING_KEY not in LAST_RECORD["record"], "no answer can fix the plan"


def test_the_same_refusal_a_third_time_runs_on_safe_defaults(monkeypatch):
    (case,) = load_cases("2026-11-12T23:41:03")
    raw = json.loads(case["router_raw"])
    refused = rerouted(case, references=[{**raw["references"][0], "purpose": LONG}])
    first, _events, streak = turn(refused, monkeypatch)
    second, _events, streak = turn(refused, monkeypatch, streak)
    assert first["reply"] == second["reply"]
    third, events, streak = turn(refused, monkeypatch, streak)
    assert not events_named(events, "refusal")
    assert events_named(events, "safe_defaults")
    assert third["edit_image"]["images"] == [router_cases.picture_url(case, 1)]
    assert MODULE["SAFE_PURPOSE"] in third["edit_image"]["prompt"]
    assert streak is None, "a turn that refuses nothing ends the streak"


def test_the_same_refusal_a_third_time_says_what_failed_without_asking(monkeypatch):
    """Two different pictures: no single source to fall back on."""
    (case,) = load_cases("2026-11-12T23:41:03")
    refs = [
        {"image": 1, "role": "B", "purpose": LONG},
        {"image": 2, "role": "A", "purpose": "the scene"},
    ]
    refused = rerouted(case, references=refs)
    first, _events, streak = turn(refused, monkeypatch)
    _second, _events, streak = turn(refused, monkeypatch, streak)
    third, events, streak = turn(refused, monkeypatch, streak)
    assert events_named(events, "refusal")
    assert third["reply"] != first["reply"]
    assert "pelo mesmo motivo" in third["reply"]
    assert "O plano não explicou o que usar de cada referência." in third["reply"]
    assert PENDING_KEY not in LAST_RECORD["record"]
    assert streak["count"] == 3


def test_a_question_repeated_a_third_time_is_not_asked_again(monkeypatch):
    """A refusal that is a question (the picture to edit is not in the chat) stops too."""
    (case,) = load_cases("2026-11-12T23:41:03")
    raw = json.loads(case["router_raw"])
    refs = [
        {"image": 1, "role": "B", "purpose": "the character"},
        {"image": 2, "role": "A", "purpose": "the scene"},
    ]
    missing = rerouted(case, edit_mode="local", references=refs, plan={**raw["plan"], "base": 9})
    first, _events, streak = turn(missing, monkeypatch)
    assert first["reply"].endswith("?")
    assert LAST_RECORD["record"][PENDING_KEY]["text"] == first["reply"]
    _second, _events, streak = turn(missing, monkeypatch, streak)
    third, _events, streak = turn(missing, monkeypatch, streak)
    assert "?" not in third["reply"]
    assert "A imagem 9 não está disponível nesta conversa." in third["reply"]
    assert PENDING_KEY not in LAST_RECORD["record"]


def test_a_turn_between_two_refusals_breaks_the_streak(monkeypatch):
    (case,) = load_cases("2026-11-12T23:41:03")
    raw = json.loads(case["router_raw"])
    refused = rerouted(case, references=[{**raw["references"][0], "purpose": LONG}])
    _first, _events, streak = turn(refused, monkeypatch)
    _made, _events, streak = turn(case, monkeypatch, streak)
    assert streak is None
    _again, _events, streak = turn(refused, monkeypatch, streak)
    assert streak["count"] == 1
