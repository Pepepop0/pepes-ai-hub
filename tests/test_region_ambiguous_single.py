"""SPEC-307: a confident visual count of one person permits an ambiguous region to edit the
whole picture. Nested boxes do not prove a count; the old Confirmo trace had no count."""

from __future__ import annotations

import json

import pytest
from router_cases import load_cases, run_case
from test_global_request_goes_ahead import prepare
from test_image_quality_preservation import png, quality
from test_open_webui_image_filter import load_module

REAL = {
    "status": "ambiguous",
    "confidence": 0.88,
    "boxes": [[350, 250, 650, 500], [230, 250, 770, 1000], [70, 60, 880, 1000], [0, 0, 1000, 1000]],
}


@pytest.mark.parametrize("confidence", [0.8, 0.88, 1])
def test_one_observed_person_goes_ahead_on_the_whole_picture(monkeypatch, confidence):
    answer = {**REAL, "people_count": 1, "confidence": confidence}
    job, events, refusal = prepare(monkeypatch, answer)
    assert refusal is None and not job.get("mask")
    skipped = [fields for event, fields in events if event == "region_skipped"]
    assert skipped == [{"reason": "ambiguous_single", "confidence": confidence, "people_count": 1}]
    refused = [fields for event, fields in events if event == "region_refused"]
    assert refused[0]["answer"] == answer, "the visual answer remains available for diagnosis"


@pytest.mark.parametrize(
    "answer",
    [
        REAL,
        {**REAL, "people_count": -1},
        {**REAL, "people_count": None},
        {**REAL, "people_count": 0},
        # Two overlapping people can produce the very same nested boxes as one person.
        {**REAL, "people_count": 2},
        {**REAL, "people_count": True},
        {**REAL, "people_count": "1"},
        {**REAL, "people_count": 1, "confidence": 0.79},
        {**REAL, "people_count": 1, "confidence": "0.88"},
        {**REAL, "people_count": 1, "confidence": float("nan")},
    ],
    ids=[
        "old_trace",
        "unknown",
        "null",
        "no_person",
        "two_overlapping",
        "bool",
        "text_count",
        "unsure",
        "text_confidence",
        "nan",
    ],
)
def test_unknown_multiple_or_uncertain_people_still_require_selection(monkeypatch, answer):
    job, events, refusal = prepare(monkeypatch, answer)
    assert refusal and "Editar área" in refusal
    assert not [1 for event, _ in events if event == "region_skipped"]
    assert not job.get("mask")


def test_the_previous_area_still_comes_first(monkeypatch):
    module = load_module()
    reused = []

    async def reuse(job, metadata, prior, reason, source):
        reused.append(reason)

    # prepare() uses its own module: replace the shared loader so it sees this spy.
    monkeypatch.setitem(module, "reuse_previous_area", reuse)
    monkeypatch.setattr("test_global_request_goes_ahead.load_module", lambda: module)
    prior = {"fallback": True, "area": b"", "region": b"", "fraction": 0}
    _job, events, refusal = prepare(monkeypatch, {**REAL, "people_count": 1}, prior=prior)
    assert refusal is None and len(reused) == 1
    assert not [1 for event, _ in events if event == "region_skipped"]


def test_the_historical_confirmo_keeps_its_unknown_count(monkeypatch):
    (case,) = load_cases("2026-11-12T23:15:18")
    assert case["grounding_answer"] == REAL, "never invent a historical visual observation"
    _body, metadata, events = run_case(case, monkeypatch)
    assert metadata["pepito_image_chat"].get("reply")
    assert not [event for event in events if event.get("reason") == "ambiguous_single"]


def test_confirmo_with_a_new_explicit_visual_count_edits_the_whole_picture(monkeypatch):
    (recorded,) = load_cases("2026-11-12T23:15:18")
    # Synthetic response, explicitly NOT the historical grounding_answer (which has no count).
    case = {**recorded, "grounding_answer": {**REAL, "people_count": 1}}
    _body, metadata, events = run_case(case, monkeypatch)
    assert not [event for event in events if event["event"] == "refusal"]
    skipped = next(event for event in events if event["event"] == "region_skipped")
    assert skipped["reason"] == "ambiguous_single" and skipped["people_count"] == 1
    job = metadata["pepito_image_chat"]["edit_image"]
    assert job["part"] == "edit" and not job.get("mask")
    assert job["images"][0].endswith("/4")
    assert recorded["grounding_answer"] == REAL


def test_the_existing_vision_call_requests_and_preserves_a_count(monkeypatch):
    import io

    from PIL import Image

    module = quality()
    calls = []
    answer = {**REAL, "people_count": 1}

    def response(request, timeout):
        calls.append(json.loads(request.data))
        return io.BytesIO(json.dumps({"message": {"content": json.dumps(answer)}}).encode())

    monkeypatch.setattr(module.urllib.request, "urlopen", response)
    with pytest.raises(module.RegionSelectionError) as raised:
        module.ground_regions(
            "http://ollama",
            "vision",
            png(Image.new("RGB", (256, 384))),
            ["chest"],
            "Make it larger.",
        )
    assert len(calls) == 1, "count in the same inference, not a second detector"
    schema = calls[0]["format"]
    assert "people_count" in schema["required"]
    assert schema["properties"]["people_count"]["minimum"] == -1
    assert raised.value.answer["people_count"] == 1
