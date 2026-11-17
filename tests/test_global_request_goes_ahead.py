"""SPEC-104: a request that changes the whole picture goes ahead, in the same turn."""

from __future__ import annotations

import sys

import pytest
import router_cases
from router_cases import load_cases, localizer, picture_png, run_case
from test_open_webui_image_filter import load_module, run

PICTURE = "memory://spec-104/1"
ASKS_FOR_THE_AREA = "Use Editar área"


def prepare(monkeypatch, answer, *, part="edit", prior=None):
    """``prepare_local_preservation`` with the localizer answering ``answer``:
    ``(job, trace events, the refusal if it raised)``."""
    module = load_module()
    events: list[tuple[str, dict]] = []

    async def picture_bytes(_url):
        return picture_png()

    module["picture_bytes"] = picture_bytes
    module["trace_event"] = lambda _metadata, event, **fields: events.append((event, fields))
    monkeypatch.setitem(
        sys.modules,
        "open_webui.pepito_image_preservation",
        localizer({"grounding_answer": answer}),
    )
    job = {
        "images": [PICTURE],
        "scope": "local",
        "part": part,
        "prompt": "Move her to the middle.",
        "region_targets": ["the plate"],
        "recipe": {},
    }
    refusal = None
    try:
        run(
            module["prepare_local_preservation"](
                job, {}, {"auto_region": True}, "model", None, None, prior
            )
        )
    except module["ImagePlanError"] as exc:
        refusal = str(exc)
    return job, events, refusal


@pytest.mark.parametrize("part", ["restage", "pose"])
def test_a_part_that_takes_no_area_goes_ahead_without_one(monkeypatch, part):
    job, events, refusal = prepare(monkeypatch, None, part=part)
    assert refusal is None and not job.get("mask")
    assert events == [("region_skipped", {"reason": "part_not_maskable", "part": part})]


def test_a_global_verdict_goes_ahead_on_the_whole_picture(monkeypatch):
    answer = {"status": "global", "confidence": 0.98, "boxes": []}
    job, events, refusal = prepare(monkeypatch, answer)
    assert refusal is None and not job.get("mask")
    skipped = [fields for event, fields in events if event == "region_skipped"]
    assert skipped == [{"reason": "vision_global", "confidence": 0.98}]


def test_a_global_verdict_still_yields_to_the_previous_area(monkeypatch):
    answer = {"status": "global", "confidence": 0.98, "boxes": []}
    reused = []
    module_events = []

    def spy(prior):
        async def reuse(job, metadata, area, reason, source):
            reused.append(reason)

        return reuse

    module = load_module()

    async def picture_bytes(_url):
        return picture_png()

    module["picture_bytes"] = picture_bytes
    module["reuse_previous_area"] = spy(None)
    module["trace_event"] = lambda _m, event, **fields: module_events.append(event)
    monkeypatch.setitem(
        sys.modules,
        "open_webui.pepito_image_preservation",
        localizer({"grounding_answer": answer}),
    )
    job = {
        "images": [PICTURE],
        "scope": "local",
        "part": "edit",
        "prompt": "Again.",
        "region_targets": ["the plate"],
        "recipe": {},
    }
    prior = {"fallback": True, "area": b"", "region": b"", "fraction": 0}
    run(
        module["prepare_local_preservation"](
            job, {}, {"auto_region": True}, "model", None, None, prior
        )
    )
    assert len(reused) == 1
    assert "region_skipped" not in module_events


@pytest.mark.parametrize(
    "answer",
    [
        {"status": "ambiguous", "confidence": 0.95, "boxes": []},
        {"status": "local", "confidence": 0.5, "boxes": [[100, 100, 300, 300]]},
        {"status": "local", "confidence": 0.95, "boxes": [[300, 300, 100, 100]]},
    ],
    ids=["ambiguous", "low_confidence", "invalid_coordinates"],
)
def test_an_uncertain_localization_still_asks_for_the_area(monkeypatch, answer):
    job, events, refusal = prepare(monkeypatch, answer)
    assert refusal
    assert not [1 for event, _ in events if event == "region_skipped"]
    assert not job.get("mask")


def test_a_local_verdict_still_makes_the_area(monkeypatch):
    answer = {"status": "local", "confidence": 0.95, "boxes": [[300, 300, 700, 700]]}
    job, events, refusal = prepare(monkeypatch, answer)
    assert refusal is None and job["mask"]
    assert not [1 for event, _ in events if event == "region_skipped"]


def edit_job_of(case, monkeypatch):
    body, metadata, events = run_case(case, monkeypatch)
    state = metadata["pepito_image_chat"]
    return body, state, events


@pytest.mark.parametrize(
    ("turn", "reason"),
    [
        ("2026-11-10T01:58:10", "part_not_maskable"),
        ("2026-11-10T02:00:09", "vision_global"),
        ("2026-11-10T02:02:01", "vision_global"),
    ],
)
def test_the_three_real_global_requests_become_an_image_job(monkeypatch, turn, reason):
    # 01:58:10 was routed to the restaging part, which the shared test stack lacks.
    monkeypatch.setattr(router_cases, "STACK", {**router_cases.STACK, "restage": {}})
    (case,) = load_cases(turn)
    _body, state, events = edit_job_of(case, monkeypatch)
    names = [event["event"] for event in events]
    assert "refusal" not in names and "reply" not in names
    skipped = next(event for event in events if event["event"] == "region_skipped")
    assert skipped["reason"] == reason
    job = state["edit_image"]
    assert job["scope"] == "local" and not job.get("mask")
    assert state["edit"] is True and "reply" not in state
    if reason == "vision_global":
        assert skipped["confidence"] == case["grounding_answer"]["confidence"]
    else:
        assert job["part"] == "restage"
