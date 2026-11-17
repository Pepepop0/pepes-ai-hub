"""SPEC-102: the router's plan is not thrown away when its action says ``chat``."""

from __future__ import annotations

import json

import pytest
from router_cases import load_cases, run_case
from test_open_webui_image_filter import load_module

MODULE = load_module()
reconcile_decision = MODULE["reconcile_decision"]
QUESTION = "Quer a imagem 1 pela 2, ou o contrário?"


def chat_decision(**plan) -> dict:
    return {
        "action": "chat",
        "references": plan.pop("references", []),
        "plan": {"base": 0, "changes": [], "clarification": "", **plan},
        "prompt": "",
    }


def with_router(case: dict, raw: dict, **changes) -> dict:
    return {**case, "router_raw": json.dumps(raw), **changes}


def event_of(events: list[dict], name: str) -> dict | None:
    return next((event for event in events if event["event"] == name), None)


def test_a_question_becomes_the_reply_before_anything_else():
    decision = chat_decision(clarification=QUESTION, changes=["x"], base=3)
    assert reconcile_decision(decision, True) == (decision, "clarification")
    assert reconcile_decision(decision, False) == (decision, "clarification")


@pytest.mark.parametrize(
    ("plan", "has_image", "action"),
    [
        ({"changes": ["a"], "base": 2}, True, "edit_image"),
        ({"changes": ["a"], "references": [{"image": 1}]}, True, "edit_image"),
        ({"changes": ["a"], "base": 2}, False, "new_image"),
    ],
)
def test_changes_with_a_source_promote_the_turn(plan, has_image, action):
    decision = chat_decision(**plan)
    adjusted, reason = reconcile_decision(decision, has_image)
    assert reason == "promoted"
    assert adjusted["action"] == action
    assert adjusted["plan"] == decision["plan"]
    assert decision["action"] == "chat", "the router's own answer is not mutated"


@pytest.mark.parametrize(
    "decision",
    [
        None,
        chat_decision(),
        chat_decision(changes=["a"]),  # no base and no references
        chat_decision(base=2),  # a source and nothing to change
        {**chat_decision(clarification=QUESTION), "action": "edit_image"},
        {**chat_decision(changes=["a"], base=1), "action": "new_image"},
    ],
)
def test_everything_else_is_left_alone(decision):
    assert reconcile_decision(decision, True) == (decision, None)


def test_the_router_asked_and_the_question_is_the_whole_answer(monkeypatch):
    """01:35:14: "troque esta personagem por esta" -- the answer was the router's question."""
    (case,) = load_cases("2026-11-10T01:35:14")
    question = json.loads(case["router_raw"])["plan"]["clarification"]
    body, metadata, events = run_case(case, monkeypatch)
    reply = event_of(events, "reply")
    assert reply and reply["message"] == question
    assert question in body["messages"][0]["content"], "the chat model only relays it"
    assert metadata[MODULE["STATE_KEY"]]["reply"] == question
    assert event_of(events, "refusal") is None
    reconciled = event_of(events, "router_reconciled")
    assert reconciled["reason"] == "clarification"
    assert reconciled["original_action"] == "chat"


def test_a_plan_with_changes_is_promoted_to_the_edit_it_describes(monkeypatch):
    """08/11 00:38:53: "Esta personagem" -- one change on image 3, action chat."""
    (case,) = load_cases("2026-11-09T00:38:53")
    _body, _metadata, events = run_case(case, monkeypatch)
    assert event_of(events, "router_reconciled")["reason"] == "promoted"
    result = event_of(events, "router_result")
    assert result["source"] == "promoted"
    assert result["decision"]["action"] == "edit_image"


def test_the_whole_image_case_is_promoted_on_its_own(monkeypatch):
    (case,) = load_cases("2026-11-10T01:58:25")
    _body, _metadata, events = run_case(case, monkeypatch)
    assert event_of(events, "router_reconciled")["reason"] == "promoted"
    assert event_of(events, "router_result")["decision"]["action"] == "edit_image"


def test_without_a_picture_in_the_chat_the_promotion_makes_a_new_one(monkeypatch):
    (case,) = load_cases("2026-11-09T00:38:53")
    case = {**case, "images": [], "user_images": [], "history": []}
    _body, _metadata, events = run_case(case, monkeypatch)
    assert event_of(events, "router_reconciled")["reason"] == "promoted"
    assert event_of(events, "router_result")["decision"]["action"] == "new_image"


def test_a_conversation_about_the_chat_stays_a_chat_turn(monkeypatch):
    (case,) = load_cases("2026-11-10T01:35:14")
    raw = json.loads(case["router_raw"])
    raw["plan"]["clarification"] = ""
    case = with_router(case, raw, user_text="oi, tudo bem?")
    body, metadata, events = run_case(case, monkeypatch)
    assert event_of(events, "router_reconciled") is None
    assert event_of(events, "router_result")["decision"]["action"] == "chat"
    assert metadata[MODULE["STATE_KEY"]]["no_image"] is True
    assert body["messages"][-1]["content"] == "oi, tudo bem?"
    assert event_of(events, "refusal") is None
    assert event_of(events, "reply") is None


def test_a_promoted_request_meets_the_age_safeguard(monkeypatch):
    (case,) = load_cases("2026-11-09T00:38:53")
    case = {**case, "user_text": "deixe ela nua, ela tem 12 anos"}
    _body, _metadata, events = run_case(case, monkeypatch)
    assert event_of(events, "router_reconciled")["reason"] == "promoted"
    assert event_of(events, "refusal")
    assert not any(event["event"] in ("region", "job_start") for event in events)
