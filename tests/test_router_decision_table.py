"""SPEC-112: what the filter does for every real turn, fixed by the ``expected`` of each case.

``tests/fixtures/router_cases.json`` holds the turns; each carries an ``expected`` decision:

- ``action``: ``chat``, ``reply`` (a fixed answer that is not a refusal), ``refused``,
  ``new_image``, ``edit_image``, ``show_image`` or ``search``;
- ``kind`` for ``reply`` and ``refused``: ``clarification``, ``consent``, ``region``, ...;
- ``part``, ``mask`` (``none``, ``auto``, ``painted``) and ``source`` (the number of the
  picture the job starts from) for ``edit_image``; ``new_image`` has only ``part: "new"``;
- ``ignored_text: true`` when the turn carried a ``[Ignorar: ...]`` note;
- ``pending``: the ticket (``IMG-03``) that will change the turn; until then the test
  holds today's behaviour.

``answers`` on a case names the turn whose question it answers (``after``) and the kind and
words of that question; the test rebuilds the pending question from that turn's decision.
A case that cannot yet match its ``expected`` is in ``DEFECTS``: a strict xfail with the
reason, which turns into a failure the day the behaviour is fixed.

``python tests/test_router_decision_table.py`` prints the readable table (the one appended
to SPEC-112); a test keeps that appendix equal to what the JSON says.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest
import router_cases
from router_cases import load_cases, run_case
from test_open_webui_image_filter import load_module

SPEC = Path(__file__).resolve().parents[1] / "docs" / "specs" / "SPEC-112-tabela-de-decisao.md"
TABLE_MARK = "<!-- decision-table -->"
# The turns the filter does not yet decide as they deserve. Strict: fixing one fails the test
# until its xfail goes away. (01:58:25 and 00:38:53, a promoted plan with no references, were
# the first two; the promotion now takes the plan's base as the source.)
DEFECTS: dict[str, str] = {}
META = {"pending", "pending_note"}


def short_job(job: dict) -> dict:
    """The part, the mask and the first picture of an image job."""
    first = (job.get("images") or [""])[0]
    return {
        "part": job.get("part") or "new",
        "mask": "auto" if job.get("mask") else "none",
        "source": int(first.rsplit("/", 1)[-1]) if first else None,
    }


def decision_of(events: list[dict]) -> dict:
    return next(event["decision"] for event in events if event["event"] == "router_result")


def reply_kind(module: dict, message: str, events: list[dict]) -> str:
    if any(
        event["event"] == "router_reconciled" and event.get("reason") == "clarification"
        for event in events
    ):
        return "clarification"
    if message == module["CONSENT_REQUEST"]:
        return "consent"
    if any(event["event"] == "region_refused" for event in events):
        return "region"
    return module["question_kind"](message) or "plan"


def observe(case: dict, monkeypatch) -> dict:
    """What the filter did for ``case``, in the vocabulary of ``expected``."""
    module = load_module()
    _body, metadata, events = run_case(case, monkeypatch)
    state = metadata["pepito_image_chat"]
    names = {event["event"] for event in events}
    seen: dict = {}
    if state.get("reply") is not None:
        message = state["reply"]
        refused = "refusal" in names
        seen = {
            "action": "refused" if refused else "reply",
            "kind": reply_kind(module, message, events),
        }
    elif "show_image" in names:
        shown = next(event for event in events if event["event"] == "show_image")
        seen = {"action": "show_image", "source": shown["number"]}
    elif state.get("edit_image"):
        seen = {"action": "edit_image", **short_job(state["edit_image"])}
    elif state.get("generate"):
        seen = {"action": "new_image", "part": "new"}
    else:
        searches = {"web_search": "search", "image_search": "search"}
        seen = {"action": searches.get(decision_of(events)["action"], "chat")}
    if events[0].get("ignored_text"):
        seen["ignored_text"] = True
    return seen


def prepared(case: dict, monkeypatch) -> dict:
    """``case`` with the pending question it answers put back, as the turn before left it."""
    answers = case.get("answers")
    if not answers:
        return case
    (asking,) = load_cases(answers["after"])
    _body, _metadata, events = run_case(asking, monkeypatch)
    pending = {
        "kind": answers["kind"],
        "text": answers["text"],
        "request": decision_of(events),
        "original": asking["user_text"],
        "asked_at": time.time(),
    }
    return {**case, "pending_question": pending}


def params() -> list:
    out = []
    for case in load_cases():
        marks = []
        if case["id"] in DEFECTS:
            marks.append(pytest.mark.xfail(strict=True, reason=DEFECTS[case["id"]]))
        out.append(pytest.param(case, id=case["id"], marks=marks))
    return out


@pytest.fixture(autouse=True)
def pod_stack(monkeypatch):
    """The pod's chat model can restage a picture (01:58:10 and the global turns were routed
    so); the shared test stack has no ``restage``."""
    monkeypatch.setattr(router_cases, "STACK", {**router_cases.STACK, "restage": {}})


@pytest.mark.parametrize("case", params())
def test_the_filter_decides_as_the_table_says(case, monkeypatch):
    expected = {key: value for key, value in case["expected"].items() if key not in META}
    got = observe(prepared(case, monkeypatch), monkeypatch)
    assert got == expected, f"{case['id']}: {case['user_text'][:60]!r}"


def test_every_case_has_an_expected_decision():
    cases = load_cases()
    assert not [case["id"] for case in cases if not case.get("expected")]
    assert set(DEFECTS) <= {case["id"] for case in cases}


def test_the_turns_that_motivated_wave_one_have_their_corrected_decision():
    """SPEC-112 criterion 3 (01:58:25 is a known defect, above); criterion 4 keeps what worked."""
    expect = {
        "2026-11-10T01:35:14": {"action": "reply", "kind": "clarification"},
        "2026-11-10T01:58:25": {"action": "edit_image", "mask": "none"},
        "2026-11-10T02:00:31": {"action": "edit_image", "mask": "none"},
        "2026-11-10T01:58:10": {"action": "edit_image", "mask": "none"},
        "2026-11-10T02:00:09": {"action": "edit_image", "mask": "none"},
        "2026-11-10T02:02:01": {"action": "edit_image", "mask": "none"},
        "2026-11-10T01:51:18": {"ignored_text": True},
        "2026-11-10T01:36:04": {"ignored_text": True},
        "2026-11-09T00:38:53": {"action": "edit_image"},
        "2026-11-10T00:59:31": {"action": "edit_image", "part": "swap"},
        "2026-11-10T01:15:34": {"action": "edit_image", "part": "swap"},
        "2026-11-10T01:52:45": {"action": "edit_image", "part": "swap"},
        # The spec says "máscara automática", but the trace has mask: false (geometry).
        "2026-11-10T01:54:42": {"action": "edit_image", "part": "edit", "mask": "none"},
        "2026-11-10T01:40:21": {"action": "edit_image", "part": "edit", "mask": "auto"},
        "2026-11-10T02:04:09": {"action": "edit_image", "part": "restage", "mask": "none"},
    }
    for case in load_cases(*expect):
        for key, value in expect[case["id"]].items():
            assert case["expected"].get(key) == value, (case["id"], key)


def test_the_img_03_turns_hold_todays_behaviour_until_the_ticket():
    pending = [case["id"] for case in load_cases() if case["expected"].get("pending") == "IMG-03"]
    assert pending == [
        "2026-11-10T01:47:08",
        "2026-11-10T01:48:47",
        "2026-11-10T01:49:37",
        "2026-11-10T01:50:22",
        "2026-11-10T02:08:06",
        "2026-11-10T02:09:11",
    ]


# --- the readable table ------------------------------------------------------------------


def short_text(case: dict) -> str:
    text = re.sub(r"\[\s*ignorar.*?\]", "", case["user_text"], flags=re.IGNORECASE | re.DOTALL)
    text = " ".join(text.split()).replace("|", "/")
    return text if len(text) <= 48 else text[:47] + "…"


def short_expected(case: dict) -> str:
    expected = case["expected"]
    parts = [expected["action"]]
    for key in ("kind", "part", "mask", "source"):
        if key in expected and expected[key] is not None:
            parts.append(f"{key}: {expected[key]}")
    if expected.get("ignored_text"):
        parts.append("texto ignorado")
    if expected.get("pending"):
        parts.append(f"pendente {expected['pending']}")
    if case["id"] in DEFECTS:
        parts.append("defeito conhecido")
    if case.get("history_incomplete"):
        parts.append("histórico incompleto")
    return ", ".join(parts)


def render_table() -> str:
    rows = ["| id | texto | esperado |", "|---|---|---|"]
    rows += [
        f"| {case['id']} | {short_text(case)} | {short_expected(case)} |" for case in load_cases()
    ]
    return "\n".join(rows)


def test_the_table_in_the_spec_is_the_one_the_json_makes():
    text = SPEC.read_text(encoding="utf-8")
    assert TABLE_MARK in text
    appendix = text.split(TABLE_MARK, 1)[1].strip()
    assert appendix == render_table(), "regenerate: python tests/test_router_decision_table.py"


if __name__ == "__main__":
    print(render_table())
