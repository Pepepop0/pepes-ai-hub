"""Final flow: assisted multi-reference edits compare LAB-04 V2 and V3/E."""

import json

import pytest
from test_open_webui_image_filter import load_module, run


@pytest.mark.parametrize("count,syntax", [(2, "plain"), (2, "indexed"), (3, "indexed")])
@pytest.mark.parametrize("failed", [None, "vision", "revise"])
def test_reference_pair_preserves_slots_compiles_each_prompt_and_records_fallback(
    monkeypatch, count, syntax, failed
):
    module = load_module()
    monkeypatch.setenv(module["OLLAMA_URL_ENV"], "http://ollama")
    sources = [bytes([i]) for i in range(1, count + 1)]
    slots = [
        {
            "slot_index": i,
            "chat_number": count - i + 1,
            "number": count - i + 1,
            "roles": ["A" if i == 1 else "B"],
            "purposes": ["canvas" if i == 1 else "identity"],
            "is_canvas": i == 1,
        }
        for i in range(1, count + 1)
    ]
    calls = []

    def write(url, payload, timeout):
        writer = "revise" if "format" in payload else "vision"
        calls.append(payload)
        if failed == writer:
            raise TimeoutError(writer)
        if writer == "vision":
            return "Place the adult person in the scene."
        return json.dumps(
            {"prompt": "Use the same adult identity and scene.", "fits_area": True, "outside": ""}
        )

    monkeypatch.setitem(module, "prompt_writer_sync", write)
    trace = {}
    result = run(
        module["write_prompt_pair"](
            "model",
            "combine the sources",
            {"prompt": "Router draft."},
            [],
            sources,
            slots,
            area=None,
            syntax=syntax,
            style="photo",
            capabilities=module["RouterCapabilities"](),
            num_ctx=16384,
            router_prompt="Router draft.",
            metadata={},
            trace=trace,
        )
    )
    assert [p["think"] for p in calls] == [False, False]
    assert "format" not in calls[0] and "format" in calls[1]
    for payload in calls:
        content = json.loads(payload["messages"][-1]["content"])
        assert [s["chat_number"] for s in content["reference_slots"]] == list(range(count, 0, -1))
        assert len(payload["messages"][-1]["images"]) == count
    expected = (
        ["router", "revise"]
        if failed == "vision"
        else ["vision"]
        if failed == "revise"
        else ["vision", "revise"]
    )
    assert result["writers"] == expected
    assert trace["prompt_pair"] == expected
    envelope = module["reference_prompt"](slots, syntax)
    assert all(p.startswith(envelope) and p.count(envelope) == 1 for p in result["prompts"])
    if failed:
        assert result["fallback"] == (
            "primary_failed" if failed == "vision" else "alternative_failed"
        )
