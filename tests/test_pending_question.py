"""SPEC-103: a question the system asks is remembered, and the next short message answers it."""

from __future__ import annotations

import json
import time

import pytest
import router_cases
from router_cases import LAST_RECORD, load_cases, run_case
from test_open_webui_image_filter import load_module

MODULE = load_module()
short_answer = MODULE["short_answer"]
KEY = MODULE["PENDING_QUESTION_KEY"]
CHAT_WITHOUT_PLAN = json.dumps(
    {
        "action": "chat",
        "minor_risk": False,
        "explicit": True,
        "references": [],
        "style": "photo",
        "aspect": "",
        "count": 0,
        "change": "subtle",
        "prompt": "",
    }
)


def event_of(events: list[dict], name: str) -> dict | None:
    return next((event for event in events if event["event"] == name), None)


def decision_of(events: list[dict]) -> dict:
    return event_of(events, "router_result")["decision"]


def asked(case_id: str, monkeypatch, **changes) -> dict:
    """The pending question a turn leaves behind (the first turn of a real pair)."""
    (case,) = load_cases(case_id)
    run_case({**case, **changes}, monkeypatch)
    pending = LAST_RECORD["record"].get(KEY)
    assert pending, "the turn asked nothing"
    return pending


def answer(case_id: str, text: str, pending: dict | None, monkeypatch, **changes):
    """The turn that follows: the router says chat with no plan (what it did on the pod)."""
    (case,) = load_cases(case_id)
    case = {
        **case,
        "user_text": text,
        "router_raw": CHAT_WITHOUT_PLAN,
        **({"pending_question": pending} if pending else {}),
        **changes,
    }
    _body, metadata, events = run_case(case, monkeypatch)
    return metadata, events


# --- short_answer -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["Esta personagem", "toda a imagem", "a imagem toda", "a da esquerda", "a 2", "sim", "ela"],
)
def test_short_answers(text):
    assert short_answer(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "oi",
        "Oi, tudo bem?",
        "bom dia",
        "Boa tarde, tudo bem",
        "qual é a capital da França?",
        "pode trocar a personagem da esquerda pela outra por favor agora mesmo",
        "Tente denovo, mas com a mulher de cabelo preto no lugar da loira da cena",
    ],
)
def test_conversation_is_not_a_short_answer(text):
    assert not short_answer(text)


# --- storage, expiry and cleanup --------------------------------------------------------


def test_a_question_reply_records_the_pending_question(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    decision = {"action": "edit_image", "plan": {"changes": ["x"], "base": 3}}
    metadata = {
        "chat_id": "temporary:spec-103-store",
        MODULE["TURN_REQUEST_KEY"]: {"decision": decision, "text": "troque a personagem"},
    }
    before = time.time()
    MODULE["fixed_reply"](
        {}, metadata, "Qual personagem devo trocar?", event="reply", question="which_person"
    )
    pending = MODULE["picture_record"]("temporary:spec-103-store")[KEY]
    assert pending["kind"] == "which_person"
    assert pending["text"] == "Qual personagem devo trocar?"
    assert pending["request"] == decision
    assert before <= pending["asked_at"] <= time.time()


def test_a_reply_that_is_not_a_question_records_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    metadata = {
        "chat_id": "temporary:spec-103-none",
        MODULE["TURN_REQUEST_KEY"]: {"decision": {"action": "chat"}, "text": "oi"},
    }
    MODULE["fixed_reply"]({}, metadata, "Anotado.", event="reply")
    assert KEY not in MODULE["picture_record"]("temporary:spec-103-none")


def test_a_pending_question_is_taken_once_and_only_while_fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    take = MODULE["take_pending_question"]
    now = time.time()
    for age, kept in ((29 * 60, True), (31 * 60, False)):
        chat = f"temporary:spec-103-age-{age}"
        MODULE["remember_question"](chat, "region", "Qual?", {"action": "edit_image"}, "pedido")
        MODULE["picture_record"](chat)[KEY]["asked_at"] = now - age
        assert bool(take(chat)) is kept
        assert take(chat) is None, "taking it clears it"


def test_the_router_is_told_the_pending_question():
    pending = {"text": "Qual personagem devo trocar?", "request": {"plan": {"changes": ["swap"]}}}
    pending["original"] = "troque a personagem"
    planning = MODULE["pending_planning"](pending)
    assert planning == {
        "pending_question": "Qual personagem devo trocar?",
        "original_request": "troque a personagem",
    }
    messages = MODULE["router_messages"](
        [{"role": "user", "content": "Esta"}], [], planning=planning
    )
    assert "Qual personagem devo trocar?" in messages[1]["content"]
    assert "pending_question" in MODULE["router_system"]()
    assert MODULE["pending_planning"](None) == {}


# --- the real pairs ---------------------------------------------------------------------


def test_this_character_answers_which_character_to_swap(monkeypatch):
    """09/11 00:38:14 asked "Qual personagem devo trocar?"; 00:38:53 answered "Esta personagem"."""
    (first,) = load_cases("2026-11-09T00:38:14")
    _body, _metadata, events = run_case(first, monkeypatch)
    pending = {
        "kind": "which_person",
        "text": "Qual personagem devo trocar?",
        "request": decision_of(events),
        "original": first["user_text"],
        "asked_at": time.time(),
    }
    local = {"status": "local", "confidence": 0.95, "boxes": [[300, 300, 700, 700]]}
    metadata, events = answer(
        "2026-11-09T00:38:53", "Esta personagem", pending, monkeypatch, grounding_answer=local
    )
    assert event_of(events, "pending_answered")["kind"] == "which_person"
    decision = decision_of(events)
    assert decision["action"] == "edit_image"
    assert decision["plan"]["changes"][-1] == "user answer: Esta personagem"
    assert event_of(events, "refusal") is None
    assert metadata["pepito_image_chat"]["edit"] is True, "the edit goes ahead"
    assert KEY not in LAST_RECORD["record"], "answered: nothing is pending any more"


def test_the_whole_image_answers_a_region_the_system_could_not_isolate(monkeypatch):
    """02:00:09 could not isolate a region; 02:00:31 said "toda a imagem"."""
    monkeypatch.setattr(router_cases, "STACK", {**router_cases.STACK, "restage": {}})
    unsure = {"status": "ambiguous", "confidence": 0.3, "boxes": []}
    pending = asked("2026-11-10T02:00:09", monkeypatch, grounding_answer=unsure)
    assert pending["kind"] == "region"
    assert pending["request"]["action"] == "edit_image"
    for text in ("toda a imagem", "a imagem toda"):
        metadata, events = answer("2026-11-10T02:00:31", text, pending, monkeypatch)
        assert event_of(events, "pending_answered")
        names = [event["event"] for event in events]
        assert "refusal" not in names and "region_refused" not in names
        job = metadata["pepito_image_chat"]["edit_image"]
        assert job["scope"] == "global" and not job.get("mask")


def test_an_answer_to_the_routers_clarification_runs_the_request(monkeypatch):
    """01:35:14: the router asked which of the two to swap; "a loira pela outra" answers it."""
    pending = asked("2026-11-10T01:35:14", monkeypatch)
    assert pending["kind"] == "which_source"
    _metadata, events = answer(
        "2026-11-10T01:35:14", "a loira pela de cabelo preto", pending, monkeypatch
    )
    assert event_of(events, "pending_answered")
    assert decision_of(events)["action"] in ("edit_image", "new_image")
    assert event_of(events, "reply") is None


# --- what is not an answer --------------------------------------------------------------


@pytest.fixture
def pending(monkeypatch):
    unsure = {"status": "ambiguous", "confidence": 0.3, "boxes": []}
    return asked("2026-11-10T02:00:09", monkeypatch, grounding_answer=unsure)


def test_without_a_pending_question_the_same_words_stay_chat(monkeypatch):
    _metadata, events = answer("2026-11-10T02:00:31", "toda a imagem", None, monkeypatch)
    assert event_of(events, "pending_answered") is None
    assert decision_of(events)["action"] == "chat"


@pytest.mark.parametrize("text", ["oi, tudo bem?", "Bom dia", "me conta uma piada sobre gatos?"])
def test_a_change_of_subject_forgets_the_question(monkeypatch, pending, text):
    _metadata, events = answer("2026-11-10T02:00:31", text, pending, monkeypatch)
    assert event_of(events, "pending_answered") is None
    assert decision_of(events)["action"] == "chat"
    assert KEY not in LAST_RECORD["record"]


def test_an_old_question_is_forgotten(monkeypatch, pending):
    pending = {**pending, "asked_at": time.time() - 31 * 60}
    _metadata, events = answer("2026-11-10T02:00:31", "toda a imagem", pending, monkeypatch)
    assert event_of(events, "pending_answered") is None
    assert decision_of(events)["action"] == "chat"
    assert KEY not in LAST_RECORD["record"]


def test_the_router_that_answers_by_itself_is_not_overruled(monkeypatch, pending):
    (case,) = load_cases("2026-11-10T02:00:31")
    case = {**case, "pending_question": pending}
    _body, _metadata, events = run_case(case, monkeypatch)
    assert event_of(events, "pending_answered") is None
    assert decision_of(events)["action"] == "edit_image"


def test_a_retry_after_a_refusal_still_repeats_the_refused_request(monkeypatch):
    """09/11 00:38:14: "tente de novo" after a refusal (the ``failed_edit`` path)."""
    (case,) = load_cases("2026-11-09T00:38:14")
    _body, _metadata, events = run_case(case, monkeypatch)
    assert event_of(events, "retry_of_refused")
