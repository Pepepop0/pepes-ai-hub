"""The recorded router cases: they load, they run the filter, and they hold nothing private."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest
from router_cases import CASES_PATH, load_cases, run_case
from test_open_webui_image_filter import load_module

# The turns that went wrong in the pod sessions, by the UTC start of the turn. The
# 2026-11-09T00:38:14 turn is the one refused at 00:38:23.
REQUIRED = (
    "2026-11-09T00:38:14",
    "2026-11-09T00:38:53",
    "2026-11-10T01:35:14",
    "2026-11-10T01:36:04",
    "2026-11-10T01:47:08",
    "2026-11-10T01:48:47",
    "2026-11-10T01:49:37",
    "2026-11-10T01:50:22",
    "2026-11-10T01:51:18",
    "2026-11-10T01:58:10",
    "2026-11-10T01:58:25",
    "2026-11-10T02:00:09",
    "2026-11-10T02:02:01",
    "2026-11-10T02:08:06",
    "2026-11-10T02:09:11",
)
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "router_cases.py"


def load_script():
    spec = importlib.util.spec_from_file_location("router_cases_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_cases_cover_the_turns_that_went_wrong():
    cases = load_cases(*REQUIRED)
    assert [case["id"] for case in cases] == list(REQUIRED)
    ignored = next(case for case in cases if case["id"] == "2026-11-10T01:36:04")
    assert ignored["user_text"].startswith("[Ignorar:")
    with pytest.raises(KeyError):
        load_cases("1999-01-01T00:00:00")


@pytest.mark.parametrize("case", load_cases(), ids=lambda case: case["id"])
def test_every_case_runs_the_filter_without_the_network(case, monkeypatch):
    body, metadata, events = run_case(case, monkeypatch)
    assert body["messages"], "the turn still has a conversation to answer or refuse"
    assert events and events[0]["event"] == "turn_start"
    assert events[0]["user_text"] == load_module()["strip_ignored"](case["user_text"])[0]
    assert any(event["event"] == "router_result" for event in events), "the router was heard"
    assert metadata["params"]["function_calling"] in ("native", "legacy")


def test_a_recorded_global_verdict_is_reached_through_the_recorded_localizer(monkeypatch):
    """02:00:09: the localizer said "global" for the plate, the shirt and the position."""
    (case,) = load_cases("2026-11-10T02:00:09")
    _body, _metadata, events = run_case(case, monkeypatch)
    refused = next(event for event in events if event["event"] == "region_refused")
    assert refused["answer"] == case["grounding_answer"]
    assert not any(event["event"] == "refusal" for event in events)


def test_the_hygiene_of_the_fixture_file():
    text = CASES_PATH.read_text(encoding="utf-8")
    assert "data:" not in text
    assert "/files/" not in text
    assert not re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", text, re.IGNORECASE
    )


def trace_line(turn: str, event: str, **fields) -> dict:
    return {
        "ts": f"2026-11-10T01:00:{ord(turn) - ord('a'):02d}.000+00:00",
        "chat_id": "chat",
        "turn": turn,
        "event": event,
        **fields,
    }


def test_the_script_makes_one_case_per_routed_turn_and_scrubs_the_rest(tmp_path):
    uuid = "123e4567-e89b-12d3-a456-426614174000"
    lines = [
        trace_line("a", "turn_start", user_text=f"olha /api/v1/files/{uuid}/content", model="m"),
        trace_line(
            "a", "router_result", raw_json='{"action": "chat"}', decision={"action": "chat"}
        ),
        trace_line("a", "turn_end", action="chat"),
        trace_line("b", "turn_start", user_text="sem roteador", model="m"),
        trace_line("b", "turn_end", action="chat"),
        trace_line("c", "turn_start", user_text="data:image/png;base64,AAAA", model="m"),
        trace_line(
            "c", "router_result", raw_json='{"action": "chat"}', decision={"action": "chat"}
        ),
        trace_line("c", "refusal", message="Não."),
        trace_line("c", "turn_end", action="chat"),
    ]
    turns = tmp_path / "sessao-x" / "turns.jsonl"
    turns.parent.mkdir()
    turns.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    out = tmp_path / "cases.json"
    assert load_script().main([str(turns), "--out", str(out)]) == 0
    cases = json.loads(out.read_text(encoding="utf-8"))
    assert [case["user_text"] for case in cases] == ["olha [arquivo]", "[imagem]"]
    assert {case["session"] for case in cases} == {"sessao-x"}
    # The turn without a router is still part of the chat the next case sees.
    assert [message["content"] for message in cases[1]["history"]] == [
        "olha [arquivo]",
        "(resposta do modelo)",
        "sem roteador",
        "(resposta do modelo)",
    ]
    assert cases[1]["observed"] == {"action": "chat", "refusal": "Não."}
