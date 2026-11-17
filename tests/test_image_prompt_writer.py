"""SPEC-309: prompt variants at the real filter/job boundary."""

import base64
import io
import json
import urllib.error

import pytest
from test_open_webui_image_filter import load_module, run


def test_valve_is_opt_in_and_unknown_value_keeps_production():
    module = load_module()
    assert module["Filter"].UserValves().prompt_writer == "router"
    field = module["Filter"].UserValves.model_json_schema()["properties"]["prompt_writer"]
    assert field["enum"] == ["router", "vision", "vision_think", "raw"]
    assert module["prompt_writer_of"]({"prompt_writer": "unknown"}) == "router"


def test_think_format_rejection_uses_recorded_vision_thinking_fallback(monkeypatch):
    module = load_module()
    monkeypatch.setenv(module["OLLAMA_URL_ENV"], "http://ollama")
    monkeypatch.setitem(module, "_ollama_ps", lambda *_: [])

    async def picture_bytes(_):
        return b"source pixels"

    monkeypatch.setitem(module, "picture_bytes", picture_bytes)
    requests = []

    def routing(url, payload, caps, timeout, raw):
        requests.append(json.loads(json.dumps(payload)))
        if len(requests) == 1:
            raise urllib.error.HTTPError(
                url, 400, "bad request", {}, io.BytesIO(b"think is incompatible with format")
            )
        return {"action": "edit_image", "prompt": "Blind prompt."}

    monkeypatch.setitem(module, "_route_sync", routing)
    trace = {}
    decision = run(
        module["route"](
            "chat",
            [{"role": "user", "content": "faz azul"}],
            [{"url": "source", "number": 1, "by": "user"}],
            trace=trace,
            prompt_writer="vision_think",
        )
    )
    assert requests[0]["think"] is True
    assert requests[1]["think"] is False
    assert "images" not in requests[1]["messages"][-1]
    assert trace["prompt_think_fallback"] is True
    writes = []
    monkeypatch.setitem(
        module, "prompt_writer_sync", lambda url, payload: writes.append(payload) or "A blue dress."
    )
    final = run(
        module["write_image_prompt"](
            "vision_think",
            decision["prompt"],
            "faz azul",
            decision,
            [],
            [
                {
                    "url": "source",
                    "slot_index": 1,
                    "chat_number": 1,
                    "is_canvas": True,
                    "roles": ["A"],
                    "purposes": [],
                }
            ],
            "chat",
            16384,
            trace,
            {},
        )
    )
    assert final == "A blue dress."
    assert writes[0]["think"] is True and "format" not in writes[0]
    assert writes[0]["messages"][-1]["images"]


def test_failed_thinking_router_does_not_silently_become_raw():
    module = load_module()
    with pytest.raises(ValueError, match="thinking router"):
        run(
            module["write_image_prompt"](
                "vision_think",
                "fallback",
                "pedido",
                {},
                [],
                [],
                "chat",
                None,
                {"failure": "timeout"},
                {},
            )
        )


@pytest.mark.parametrize("label", ["Image 1", "image 15", "Image: 15", "page 15"])
def test_reference_identifiers_are_not_minor_ages(label):
    module = load_module()
    assert module["_ages"](label.lower()) == []
    assert not module["minor_risk_rules"](f"An adult woman, nude. {label} is the source.")


@pytest.mark.parametrize(
    "age",
    [
        "age 15",
        "age: 15",
        "age=15",
        "aged 15",
        "idade de 15",
        "aos 15",
        "15 years old",
        "15-year-old",
        "15 y/o",
        "tem 15 anos",
        "fifteen years old",
    ],
)
def test_real_minor_age_formats_remain_blocked(age):
    module = load_module()
    assert module["_ages"](age) == [15]
    assert module["minor_risk_rules"](f"A nude person, {age}.")


@pytest.mark.parametrize("syntax", ["indexed", "plain"])
def test_reference_contract_uses_physical_slots_and_compiles_idempotently(syntax):
    module = load_module()
    plan = module["reference_plan"](
        [{"number": number, "url": f"source-{number}"} for number in (1, 2, 3)],
        [
            {"image": 1, "role": "B", "purpose": "identity only"},
            {"image": 2, "role": "A", "purpose": "canvas only"},
            {"image": 2, "role": "B", "purpose": "keep person"},
            {"image": 1, "role": "B", "purpose": "identity only"},
            {"image": 3, "role": "C", "purpose": "light only"},
        ],
        {"edit_refs": {"3": {"syntax": syntax}}},
        base=2,
    )
    slots = plan["slots"]
    assert [slot["chat_number"] for slot in slots] == [2, 1, 3]
    assert [slot["slot_index"] for slot in slots] == [1, 2, 3]
    assert [slot["is_canvas"] for slot in slots] == [True, False, False]
    assert slots[0]["roles"] == ["A", "B"]
    assert slots[1]["purposes"] == ["identity only"]
    compile_prompt = module["compile_reference_prompt"]
    final = compile_prompt(slots, syntax, "Change the light.")
    assert compile_prompt(slots, syntax, final) == final
    assert final.count("Take nothing else") == 1 and final.count("identity only") == 1
    assert ("<image1>" in final) is (syntax == "indexed")
    assert compile_prompt(slots[:1], syntax, "Keep this exact text.") == "Keep this exact text."


@pytest.mark.parametrize("count", [2, 3])
def test_all_visual_writers_share_n_sources_map_and_separate_area(count):
    from PIL import Image

    module = load_module()
    sources = []
    for color in ("red", "blue", "green")[:count]:
        image = io.BytesIO()
        Image.new("RGB", (32, 32), color).save(image, "PNG")
        sources.append(image.getvalue())
    mask = io.BytesIO()
    Image.new("L", (32, 32), 255).save(mask, "PNG")
    slots = [
        {
            "slot_index": index,
            "chat_number": number,
            "roles": [role],
            "purposes": [f"purpose {index}"],
            "is_canvas": index == 1,
        }
        for index, (number, role) in enumerate(zip((8, 2, 15), ("A", "B", "C")), 1)
    ][:count]
    decision = {"action": "edit_image", "prompt": "Change the light."}
    original = json.dumps(decision)
    history = [{"role": "user", "content": "change light"}]
    payloads = [
        module["prompt_writer_payload"]("model", "change light", decision, history, sources, slots),
        module["prompt_revision_payload"](
            "model", "change light", decision, history, sources, slots, area=mask.getvalue()
        ),
        module["frozen_route_payload"](
            "model", history, sources, slots, decision, "photo", module["RouterCapabilities"]()
        ),
    ]
    for payload in payloads:
        user = payload["messages"][-1]
        assert [base64.b64decode(image) for image in user["images"][:count]] == sources
        assert "reference_slots" in user["content"]
        assert "Never renumber" in payload["messages"][0]["content"]
    revised = json.loads(payloads[1]["messages"][-1]["content"])
    assert len(revised["reference_slots"]) == count
    assert revised["area_context"] == {"image_index": count + 1, "canvas_slot": 1}
    assert len(payloads[1]["messages"][-1]["images"]) == count + 1
    assert json.dumps(decision) == original
    with pytest.raises(ValueError, match="Every reference"):
        module["prompt_revision_payload"]("model", "", decision, history, sources[:1], slots)


@pytest.mark.parametrize("syntax", ["indexed", "plain"])
def test_n_source_pair_payloads_and_final_prompts_use_the_same_contract(monkeypatch, syntax):
    module = load_module()
    monkeypatch.setenv(module["OLLAMA_URL_ENV"], "http://unused")
    slots = [
        {
            "slot_index": 1,
            "chat_number": 9,
            "roles": ["A"],
            "purposes": ["scene"],
            "is_canvas": True,
        },
        {
            "slot_index": 2,
            "chat_number": 2,
            "roles": ["B"],
            "purposes": ["identity"],
            "is_canvas": False,
        },
    ]
    sources = [b"synthetic-scene", b"synthetic-character"]
    calls = []

    def writer(_url, payload, _timeout):
        calls.append(payload)
        if "format" not in payload:
            return "Authored vision."
        return json.dumps({"prompt": "Authored E.", "fits_area": True, "outside": ""})

    monkeypatch.setitem(module, "prompt_writer_sync", writer)
    pair = run(
        module["write_prompt_pair"](
            "model",
            "combine",
            {"prompt": "Router."},
            [{"role": "user", "content": "combine"}],
            sources,
            slots,
            area=None,
            syntax=syntax,
            style="photo",
            capabilities=module["RouterCapabilities"](),
            num_ctx=16384,
            router_prompt="Router.",
            metadata={},
            trace={},
        )
    )
    assert pair["writers"] == ["vision", "revise"]
    for call in calls:
        assert [base64.b64decode(image) for image in call["messages"][-1]["images"]] == sources
    for prompt, authored in zip(pair["prompts"], ("Authored vision.", "Authored E."), strict=True):
        assert prompt.count("Take nothing else") == 1 and prompt.endswith(authored)
        assert module["compile_reference_prompt"](slots, syntax, prompt) == prompt
