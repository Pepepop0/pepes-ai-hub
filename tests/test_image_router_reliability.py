"""Router residency, conservative fallback, output honesty and source provenance."""

from __future__ import annotations

import io
import json
import logging
import urllib.error

import pytest
from test_open_webui_image_filter import load_module, routed_env, run, ui_metadata, with_plan


class RouterServer:
    def __init__(self, models=(), answer=None):
        self.models = list(models)
        self.answer = answer if answer is not None else {"action": "chat", "prompt": ""}
        self.calls = []

    def __call__(self, request, timeout=None):
        url = request if isinstance(request, str) else request.full_url
        payload = None if isinstance(request, str) else json.loads(request.data)
        self.calls.append((url, timeout, payload))
        if url.endswith("/api/ps"):
            return io.BytesIO(json.dumps({"models": self.models}).encode())
        if isinstance(self.answer, Exception):
            raise self.answer
        answer = with_plan(self.answer)
        content = answer if isinstance(answer, str) else json.dumps(answer)
        return io.BytesIO(json.dumps({"message": {"content": content}}).encode())


@pytest.mark.parametrize("resident", [{"name": "qwen:27b"}, {"model": "qwen:27b"}])
def test_resident_context_wins_over_every_configured_context(monkeypatch, resident):
    module = load_module()
    server = RouterServer([{**resident, "context_length": 12288}])
    routed_env(monkeypatch, server)
    events = []

    async def emit(event):
        events.append(event)

    run(module["route"]("qwen:27b", [], [], 32768, stack={"num_ctx": 16384}, emit=emit))
    (_, ps_timeout, _), (_, timeout, payload) = server.calls
    assert ps_timeout <= 5
    assert timeout == 60
    # The resident context, and nothing that would make Ollama reload the weights.
    assert payload["options"]["num_ctx"] == 12288
    assert set(payload["options"]) == {"temperature", "num_predict", "num_ctx"}
    assert "keep_alive" not in payload
    assert events == []


@pytest.mark.parametrize(
    ("models", "stack", "configured", "expected", "timeout"),
    [
        ([], {"num_ctx": 16384}, 4096, 16384, 300),
        ([{"name": "different:8b", "context_length": 2048}], {}, 4096, 4096, 300),
        ([], {"num_ctx": "bad"}, 4096, 4096, 300),
        ([], {"num_ctx": True}, None, None, 300),
        ([{"name": "model:latest"}], {"num_ctx": 16384}, 4096, None, 60),
    ],
)
def test_cold_context_precedence_and_loading_status(
    monkeypatch, models, stack, configured, expected, timeout
):
    module = load_module()
    server = RouterServer(models)
    routed_env(monkeypatch, server)
    events = []

    async def emit(event):
        events.append(event)

    run(module["route"]("model", [], [], configured, stack=stack, emit=emit))
    _, actual_timeout, payload = server.calls[-1]
    assert actual_timeout == timeout
    assert payload["options"].get("num_ctx") == expected
    assert bool(events) is (timeout == 300)
    if events:
        assert events[0]["type"] == "status"
        assert events[0]["data"]["done"] is False
        assert "Carregando" in events[0]["data"]["description"]


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (TimeoutError("private request"), "timeout"),
        (urllib.error.URLError(TimeoutError("private request")), "timeout"),
        (OSError("private request"), "connection"),
        ("not JSON: private request", "invalid_answer"),
        ({"action": "unknown", "prompt": "private request"}, "invalid_answer"),
    ],
)
@pytest.mark.parametrize(
    ("words", "action"), [("Good evening!", "chat"), ("Draw a lake", "new_image")]
)
def test_failed_router_records_reason_and_only_draws_when_asked(
    monkeypatch, failure, reason, words, action
):
    module = load_module()
    server = RouterServer(answer=failure)
    routed_env(monkeypatch, server)
    trace = {}
    decision = run(module["route"]("model", [], [], trace=trace))
    assert decision is None
    assert trace["failure"] == reason
    assert trace["seconds"] >= 0
    assert module["decide"](decision, words, False)[0] == action


@pytest.mark.parametrize(
    ("words", "has_image", "action"),
    [
        ("Hey there, gorgeous", False, "chat"),
        ("Good morning", True, "chat"),
        ("Why does this image look blurry?", True, "chat"),
        ("I like this picture", True, "chat"),
        ("You look better than this picture", True, "chat"),
        ("quero uma imagem de montanhas", False, "new_image"),
        ("desenha um farol", False, "new_image"),
        ("gera uma foto de uma floresta", False, "new_image"),
        ("faz uma paisagem", False, "new_image"),
        ("a picture of a snowy valley", False, "new_image"),
        ("Render a wooden cabin", False, "new_image"),
        ("make it blue", True, "edit_image"),
    ],
)
def test_failure_rules_require_positive_picture_intent(words, has_image, action):
    assert load_module()["decide"](None, words, has_image)[0] == action


def test_short_confirmation_recovers_the_pending_picture_even_if_model_says_chat():
    module = load_module()
    messages = [
        {"role": "user", "content": "Draw a lighthouse on a cliff"},
        {"role": "assistant", "content": "I can draw that picture. Shall I?"},
        {"role": "user", "content": "go ahead"},
    ]
    for decision in (None, {"action": "chat", "prompt": ""}):
        assert module["decide"](decision, "go ahead", False, messages) == (
            "new_image",
            "Draw a lighthouse on a cliff",
            "rules",
        )
    messages[-2]["content"] = "Would you like me to explain the geology?"
    assert module["decide"](None, "go ahead", False, messages)[0] == "chat"


def test_image_on_chat_turn_never_asks_the_user_to_switch_image_on(monkeypatch):
    """09/11 01:35:14: Image was on, the router answered chat to an ambiguous edit request,
    and the model told the user to turn on a switch that was already on."""
    module = load_module()
    routed_env(monkeypatch, RouterServer(answer={"action": "chat", "prompt": ""}))
    body = {
        "model": "model",
        "messages": [
            {"role": "user", "content": "faz uma foto de uma praia"},
            {"role": "assistant", "content": "Pronto, a imagem foi criada."},
            {"role": "user", "content": "agora deixa ela mais bonita"},
        ],
    }
    metadata = ui_metadata()
    run(module["Filter"]().inlet(body, metadata))
    context = "\n".join(
        m["content"] for m in body["messages"] if m["role"] == "system" and m["content"]
    )
    assert metadata[module["STATE_KEY"]]["no_image"] is True
    assert "is ON" in context
    assert "with Image on" not in context


def test_image_on_decision_log_contains_only_fixed_fields(monkeypatch, caplog):
    module = load_module()
    routed_env(monkeypatch, RouterServer(answer=TimeoutError("SECRET-ERROR")))
    body = {"model": "model", "messages": [{"role": "user", "content": "SECRET-GREETING"}]}
    metadata = ui_metadata()
    with caplog.at_level(logging.INFO):
        run(module["Filter"]().inlet(body, metadata))
    lines = [
        record.getMessage() for record in caplog.records if "image_turn" in record.getMessage()
    ]
    assert len(lines) == 1
    line = lines[0]
    assert "action=chat source=rules failure=timeout" in line
    assert "router_seconds=" in line
    assert "references=0 " in line and "minor_risk=False consent_required=no" in line
    assert "SECRET" not in line
    assert "generate" not in metadata[module["STATE_KEY"]]


def test_a_turn_stopped_before_the_decision_is_logged_as_interrupted_not_as_chat(
    monkeypatch, caplog
):
    """23/10: Stop pressed during the router logged "action=chat source=rules", the empty
    trace's defaults, which read as a router that chose to chat."""
    import asyncio

    module = load_module()
    routed_env(monkeypatch, RouterServer())

    async def stopped(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setitem(module, "route", stopped)
    body = {"model": "model", "messages": [{"role": "user", "content": "faz 4 versões"}]}
    with caplog.at_level(logging.INFO), pytest.raises(asyncio.CancelledError):
        run(module["Filter"]().inlet(body, ui_metadata()))
    (line,) = [r.getMessage() for r in caplog.records if "image_turn" in r.getMessage()]
    assert "action=interrupted" in line and "action=chat" not in line


def test_image_off_context_cleans_stream_and_persisted_markup_but_keeps_links(monkeypatch, caplog):
    module = load_module()
    monkeypatch.setitem(module, "image_stack", lambda _model: {"num_ctx": 8192})
    filter_ = module["Filter"]()
    metadata = ui_metadata(image=False)
    body = {"model": "stack-model", "messages": [{"role": "user", "content": "PRIVATE"}]}
    with caplog.at_level(logging.INFO):
        run(filter_.inlet(body, metadata))
    assert module["IMAGE_OFF_CONTEXT"] in body["messages"][0]["content"]
    assert metadata["params"]["function_calling"] == "native"
    lines = [
        record.getMessage() for record in caplog.records if "image_turn" in record.getMessage()
    ]
    assert len(lines) == 1 and "enabled=no" in lines[0] and "PRIVATE" not in lines[0]
    text = 'Read [the guide](https://example.org). ![fake](https://bad.invalid/x) <img src="fake">'
    streamed = []
    for piece in (text[:42], text[42:67], text[67:]):
        event = {"choices": [{"delta": {"content": piece}, "finish_reason": None}]}
        delta = run(filter_.stream(event, metadata))["choices"][0]["delta"]
        streamed.append(delta.get("content", ""))
    event = {"choices": [{"delta": {}, "finish_reason": "stop"}]}
    streamed.append(run(filter_.stream(event, metadata))["choices"][0]["delta"]["content"])
    expected = "Read [the guide](https://example.org)."
    # Streamed live: the link arrives as written, the two made-up pictures never do.
    assert "".join(streamed).strip() == expected
    assert "bad.invalid" not in "".join(streamed) and "<img" not in "".join(streamed)
    reply = {"messages": [{"role": "assistant", "content": text}]}
    run(filter_.outlet(reply, metadata))
    assert reply["messages"][0]["content"] == expected
    reply["messages"][0]["content"] = "![fake](bad)"
    run(filter_.outlet(reply, metadata))
    assert reply["messages"][0]["content"] == "", "never substitute a success announcement"


@pytest.mark.parametrize(
    "pieces",
    [
        ["Olha só <im", 'g src="x.png"> e pronto.'],
        ["Aqui ![a", "lt](https://imgur.invalid/fake.png) pronto."],
        ["Uma exclamação! E ", "um ![nota] sem link."],
    ],
)
def test_chat_stream_drops_markup_split_across_chunks_and_keeps_plain_text(pieces):
    module = load_module()
    state = {"text": "", "no_image": True}
    out = [module["stream_clean"](state, piece, False) for piece in pieces]
    out.append(module["stream_clean"](state, "", True))
    joined = "".join(out)
    assert "<im" not in joined and "imgur.invalid" not in joined
    assert joined.replace(" ", "") == "".join(pieces).replace(" ", "").replace(
        '<imgsrc="x.png">', ""
    ).replace("![alt](https://imgur.invalid/fake.png)", "")
    assert out[0], "text before the markup streams at once"


def test_image_off_without_session_or_stack_does_not_touch_chat(monkeypatch):
    module = load_module()
    for session, stack in ((False, {}), (True, None)):
        monkeypatch.setitem(module, "image_stack", lambda _model, stack=stack: stack)
        metadata = ui_metadata(image=False, session=session)
        body = {"model": "model", "messages": [{"role": "user", "content": "hello"}]}
        run(module["Filter"]().inlet(body, metadata))
        assert len(body["messages"]) == 1
        assert module["STATE_KEY"] not in metadata


@pytest.mark.parametrize("source", ["own", "upload", "mixed", "web", "other-chat"])
def test_consent_is_skipped_only_when_every_final_source_was_generated_here(
    monkeypatch, tmp_path, source
):
    module = load_module()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    module["record_generated"]("chat-1", ["own"])
    module["record_derived"]("chat-1", ["edited"])
    module["record_generated"]("chat-1", ["edited"])
    assert module["generated_urls"]("chat-1") == {"own", "edited"}
    assert module["derived_urls"]("chat-1") == {"edited"}
    entries = [
        {
            "url": "own" if source in ("own", "other-chat") else "upload",
            "by": "user",
            "group": 0,
        }
    ]
    slots = [{"url": entries[0]["url"], "number": 1}]
    if source == "mixed":
        slots.append({"url": "own", "number": 2})
    if source == "web":
        slots[0] = {
            "url": "own",
            "number": 1,
            "candidate": {"image_url": "https://example.org/photo"},
        }

    async def find_entries(*_args):
        return entries

    async def no_search(*_args):
        return False

    monkeypatch.setitem(module, "chat_image_entries", find_entries)
    monkeypatch.setitem(module, "web_search_available", no_search)

    def contract(items):
        # What the real ``reference_plan`` gives every slot (SPEC-320).
        return [
            {
                "roles": ["A"],
                **slot,
                "slot_index": index,
                "chat_number": slot["number"],
                "is_canvas": index == 1,
            }
            for index, slot in enumerate(items, 1)
        ]

    monkeypatch.setitem(
        module,
        "reference_plan",
        lambda *_args, **_kwargs: {"slots": contract(slots), "dropped": []},
    )
    routed_env(
        monkeypatch,
        RouterServer(
            answer={
                "action": "edit_image",
                "prompt": "Remove the adult person's clothes.",
                "explicit": True,
            }
        ),
    )
    metadata = ui_metadata()
    if source == "other-chat":
        metadata["chat_id"] = "chat-2"
    body = {"model": "model", "messages": [{"role": "user", "content": "edit the picture"}]}
    run(module["Filter"]().inlet(body, metadata, {"id": "user"}))
    state = metadata[module["STATE_KEY"]]
    if source == "own":
        assert "edit_image" in state and "reply" not in state
    else:
        assert state["reply"] == module["CONSENT_REQUEST"]


def test_styled_rule_fallback_does_not_schedule_a_second_model_refinement(monkeypatch):
    module = load_module()
    monkeypatch.setitem(
        module,
        "image_stack",
        lambda _model: {"generate": {"style": {"node_ids": ["1"], "key": "strength"}}},
    )
    routed_env(monkeypatch, RouterServer(answer=TimeoutError()))
    metadata = ui_metadata()
    body = {"model": "model", "messages": [{"role": "user", "content": "Draw a forest"}]}
    run(module["Filter"]().inlet(body, metadata))
    assert metadata[module["STATE_KEY"]]["generate"]["refine"] is False
