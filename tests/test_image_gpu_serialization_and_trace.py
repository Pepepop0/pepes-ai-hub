"""One card, one turn at a time; honest picture claims; retries, pronoun edits, pictures
shown again and size changes; the private turn trace. From the RTX 5090 session of 01/11."""

from __future__ import annotations

import asyncio
import io
import json
import os
import urllib.request

import pytest
from test_image_edit_dispatch import (
    APPLE,
    CHAT,
    capella_stacks,  # noqa: F401
    edit,
    edit_config,  # noqa: F401
    new,
    preset,  # noqa: F401
    routed,  # noqa: F401
    settings,  # noqa: F401
    world,  # noqa: F401
)
from test_open_webui_image_filter import load_module, run, ui_metadata, with_plan

OLLAMA = "http://127.0.0.1:11434"
COMFYUI = "http://127.0.0.1:8188"


def namespace(turn):
    return turn.filter.inlet.__func__.__globals__


def ref_boosts(comfyui) -> list:
    return [
        node["inputs"]["ref_boost"]
        for job in comfyui.jobs
        for node in job.values()
        if node.get("class_type") == "Krea2EditModelPatch"
    ]


class Card:
    """Ollama and ComfyUI on one card, as the filter's urllib calls reach them. Router
    calls land on ``calls`` as ``router:<the user's words>``."""

    def __init__(self, answers: dict, models=()) -> None:
        self.answers = answers
        self.models = list(models)
        self.calls: list = []
        self.timeouts: dict = {}

    def __call__(self, request, timeout=None):
        url = request if isinstance(request, str) else request.full_url
        body = None if isinstance(request, str) else json.loads(request.data)
        path = "/" + url.split("/", 3)[3]
        if path == "/api/chat":
            said = body["messages"][-1]["content"].rsplit("LAST USER MESSAGE: ", 1)[-1]
            self.calls.append(f"router:{said}")
            self.timeouts[said] = timeout
            content = json.dumps(with_plan(self.answers[said]))
            return io.BytesIO(json.dumps({"message": {"content": content}}).encode())
        self.calls.append(path if body is None else (path, body))
        if path == "/api/ps":
            return io.BytesIO(json.dumps({"models": self.models}).encode())
        return io.BytesIO(b"{}")


@pytest.fixture
def card(monkeypatch, tmp_path):
    def install(answers=None, models=()):
        fake = Card(answers or {}, models)
        monkeypatch.setattr(urllib.request, "urlopen", fake)
        monkeypatch.setenv("PEPITO_IMAGE_ROUTER", "true")
        monkeypatch.setenv("PEPITO_EXCLUSIVE_VRAM", "true")
        monkeypatch.setenv("PEPITO_OLLAMA_URL", OLLAMA)
        monkeypatch.setenv("PEPITO_COMFYUI_URL", COMFYUI)
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        return fake

    return install


def trace_records(data_dir) -> list:
    path = data_dir / "pepito-trace" / "turns.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def turn_metadata(chat_id: str, message_id: str) -> dict:
    metadata = ui_metadata()
    metadata.update(chat_id=chat_id, message_id=message_id)
    return metadata


# --- one card, one turn at a time ----------------------------------------------------------


def test_a_second_turn_waits_for_the_running_picture_before_its_router_call(card, tmp_path):
    """01/11: a request arriving mid-job loaded the 27B beside ComfyUI's 18 GB, 34/66 layers
    on the card, 4.7 tok/s for 14 minutes. The second turn now waits for the job, the free
    and ComfyUI's actual release before its router call loads anything."""
    fake = card({"desenha um farol": new("A lighthouse."), "oi, tudo bem?": CHAT})
    module = load_module()
    module["GPU_POLL_SECONDS"] = 0.01
    statuses: list = []

    async def emit_first(_event):
        return None

    async def emit_second(event):
        if event.get("type") == "status":
            statuses.append(event["data"]["description"])

    async def scenario():
        filter_ = module["Filter"]()
        started, finish = asyncio.Event(), asyncio.Event()

        async def slow_picture(job, metadata, request, user, emit):
            fake.calls.append("job-start")
            started.set()
            await finish.wait()
            fake.calls.append("job-end")

        filter_._generate = slow_picture
        user = {"id": "u"}
        first = turn_metadata("temporary:first", "msg-a")
        said = [{"role": "user", "content": "desenha um farol"}]
        body_a = {"model": "chat:27b", "messages": said}
        await filter_.inlet(body_a, first, __user__=user, __event_emitter__=emit_first)
        making = asyncio.create_task(
            filter_.request(
                body_a, first, __event_emitter__=emit_first, __request__=object(), __user__=user
            )
        )
        await started.wait()
        second = turn_metadata("temporary:second", "msg-b")
        body_b = {"model": "chat:27b", "messages": [{"role": "user", "content": "oi, tudo bem?"}]}
        answering = asyncio.create_task(
            filter_.inlet(body_b, second, __user__=user, __event_emitter__=emit_second)
        )
        await asyncio.sleep(0.1)
        assert "router:oi, tudo bem?" not in fake.calls, "no router call during the picture"
        assert statuses == [module["GPU_WAIT_STATUS"]]
        finish.set()
        await making
        await answering
        return second

    second = run(scenario())
    order = fake.calls
    freed = order.index(("/free", {"unload_models": True, "free_memory": True}))
    assert order.index("job-end") < freed
    assert order.index("/system_stats", freed) < order.index("router:oi, tudo bem?")
    assert second[module["STATE_KEY"]]["no_image"] is True
    assert module["_GPU_GATE"]["owner"] is None, "the card is free again"
    waits = [r for r in trace_records(tmp_path) if r["event"] == "gpu_wait"]
    assert any(r["reason"] == "another_turn" and r["chat_id"] == "temporary:second" for r in waits)


def test_a_turn_that_never_reaches_its_request_hook_cannot_hold_the_card_forever(card):
    card()
    module = load_module()
    module["GPU_POLL_SECONDS"] = 0.01

    async def scenario():
        stale = await module["acquire_gpu"](lease=0.05)
        token = await module["acquire_gpu"]()
        return stale, token

    stale, token = run(scenario())
    assert module["_GPU_GATE"]["owner"] == token != stale
    module["release_gpu"](stale)
    assert module["_GPU_GATE"]["owner"] == token, "an expired holder releases nothing"
    module["release_gpu"](token)
    assert module["_GPU_GATE"]["owner"] is None


# --- a chat model split between the card and RAM -------------------------------------------

PARTIAL = {
    "name": "qwen:27b",
    "size": 19_500_000_000,
    "size_vram": 11_000_000_000,
    "context_length": 16384,
}


def test_a_split_chat_model_is_unloaded_before_the_router_so_it_loads_whole(card):
    fake = card({"": CHAT}, models=[PARTIAL])
    module = load_module()
    trace: dict = {}
    run(module["route"]("qwen:27b", [], [], trace=trace, card_free=True))
    assert fake.calls[:2] == [
        "/api/ps",
        ("/api/generate", {"model": "qwen:27b", "keep_alive": 0}),
    ]
    assert fake.timeouts[""] == module["ROUTER_COLD_TIMEOUT_SECONDS"]
    assert trace["resident"] == "partial"


def test_a_split_resident_model_is_never_given_the_short_router_timeout(card):
    fake = card({"": CHAT}, models=[PARTIAL])
    module = load_module()
    run(module["route"]("qwen:27b", [], [], card_free=False))
    assert ("/api/generate", {"model": "qwen:27b", "keep_alive": 0}) not in fake.calls
    assert fake.timeouts[""] == module["ROUTER_COLD_TIMEOUT_SECONDS"]


@pytest.mark.parametrize(
    ("resident", "unloaded"),
    [
        (PARTIAL, True),
        ({**PARTIAL, "size_vram": PARTIAL["size"]}, False),
        ({"name": "qwen:27b"}, False),  # Ollama did not say: nothing is guessed
    ],
)
def test_before_a_chat_answer_only_a_split_model_is_unloaded(card, resident, unloaded):
    fake = card(models=[resident])
    module = load_module()
    assert run(module["release_partial_model"]("qwen:27b")) is unloaded
    assert (("/api/generate", {"model": "qwen:27b", "keep_alive": 0}) in fake.calls) is unloaded


# --- retries with changes are edits, never refusals ----------------------------------------

PREVIOUS = {
    "prompt": "Put the woman of the first image in the pose of the second.",
    "refs": [
        {"image": 1, "role": "A", "purpose": "the pose"},
        {"image": 2, "role": "B", "purpose": "the woman"},
    ],
    "base": 1,
    "boost": 2.5,
    "edit_mode": "local",
    "scope": "global",
    "region_targets": [],
    "part": "edit",
    "mask_png": "AAAA",
}


def retry_plan(**changes) -> dict:
    return {
        "request_mode": "retry",
        "scope": "global",
        "region_targets": [],
        "base": 0,
        "changes": [],
        "preserve": [],
        "correction": "none",
        "clarification": "",
        **changes,
    }


def test_a_retry_with_changes_is_an_edit_of_the_previous_sources():
    module = load_module()
    decision = {
        "action": "edit_image",
        "prompt": "Swap the woman fully; no blend of the two.",
        "references": [],
        "plan": retry_plan(changes=["the second woman entirely, not a hybrid"]),
    }
    converted, recipe = module["retry_decision"](decision, PREVIOUS)
    assert converted["action"] == "edit_image"
    assert converted["prompt"] == decision["prompt"], "the stated change, not the old prompt"
    assert converted["references"] == PREVIOUS["refs"]
    assert converted["plan"]["request_mode"] == "refine" and converted["plan"]["base"] == 1
    assert recipe["fresh"] is True and recipe["mask_png"] is None and recipe["boost"] is None


def test_nothing_changed_with_the_request_restated_is_a_freer_edit_not_a_refusal():
    module = load_module()
    decision = {
        "action": "edit_image",
        "prompt": "Front view, arms open, a smile, larger arms.",
        "references": [],
        "plan": retry_plan(correction="no_change", changes=["front view", "larger arms"]),
    }
    _converted, recipe = module["retry_decision"](decision, PREVIOUS)
    assert recipe["escalate"] is True and recipe["boost"] == 2.5
    strengths, source = module["edit_strengths"]("moderate", 1, retry_from=recipe["boost"])
    assert strengths == [1.5] and source == "correction"


def test_a_retry_without_a_recipe_but_with_an_instruction_edits_the_current_picture():
    module = load_module()
    decision = {
        "action": "edit_image",
        "prompt": "Make her smile.",
        "references": [],
        "plan": retry_plan(changes=["a smile"]),
    }
    converted, recipe = module["retry_decision"](decision, None)
    assert recipe is None and converted["plan"]["request_mode"] == "refine"
    with pytest.raises(module["ImagePlanError"]):
        module["retry_decision"]({**decision, "plan": retry_plan()}, None)


def test_no_refusal_text_is_left_for_a_retry_with_changes():
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "assets"
        / "open-webui"
        / "pepito_image_chat.py"
    ).read_text(encoding="utf-8")
    assert "Uma repetição não pode incluir novas mudanças" not in source


# --- the rules after a router timeout --------------------------------------------------------

EVIDENCE = (
    "Faz agora dela de frente, abrindo os braços e com um sorriso grande, aumente "
    "os braços dela ainda mais e faça com que começe a rasgar ainda mais"
)


@pytest.mark.parametrize(
    ("words", "action"),
    [
        (EVIDENCE, "edit_image"),
        ("deixa ela de costas", "edit_image"),
        ("make her legs longer", "edit_image"),
        ("O cabelo dela é lindo", "chat"),
        ("Gostei dela de costas", "chat"),
        ("mostra a imagem 2 de novo", "show_image"),
        ("me exiba a imagem denovo", "show_image"),
    ],
)
def test_a_pronoun_for_the_picture_with_a_change_is_an_edit(words, action):
    assert load_module()["decide"](None, words, True)[0] == action


def test_unsure_words_after_a_router_timeout_ask_and_a_yes_makes_the_edit(routed):  # noqa: F811
    turn, comfyui, _, ollama = routed
    said = "agora com as pernas mais abertas"
    ollama.answers.update(
        {"uma maçã": new(APPLE), said: TimeoutError("slow"), "sim": TimeoutError("slow")}
    )
    turn.turn("uma maçã")
    jobs = len(comfyui.jobs)
    asked = turn.turn(said, answer="Claro! Ative o botão Image.")
    assert asked["content"] == namespace(turn)["CLARIFY_REPLY"] and len(comfyui.jobs) == jobs
    made = turn.turn("sim")
    assert len(comfyui.jobs) == jobs + 1 and len(made["files"]) == 1


# --- no picture is claimed that was not made ---------------------------------------------------


def test_the_filters_fixed_replies_reach_the_chat_model_as_system_notes():
    module = load_module()
    messages = [
        {"role": "user", "content": "faz uma foto"},
        {"role": "assistant", "content": "1 imagem gerada."},
        {"role": "assistant", "content": "2 imagens editadas. Diga qual deseja usar."},
        {"role": "assistant", "content": "Imagem 3."},
        {"role": "assistant", "content": "Que bom que gostou!"},
        {"role": "user", "content": "1 imagem gerada."},
    ]
    shown = module["neutral_history"](messages)
    assert [m["content"] for m in shown] == [
        "faz uma foto",
        "[imagem gerada anexada pelo sistema]",
        "[2 imagens editadas anexadas pelo sistema]",
        "[imagem 3 anexada de novo pelo sistema]",
        "Que bom que gostou!",
        "1 imagem gerada.",
    ]
    assert messages[1]["content"] == "1 imagem gerada.", "the request itself is not rewritten"


def test_a_chat_turn_after_a_picture_does_not_show_the_model_the_fixed_text(routed):  # noqa: F811
    turn, _, _, ollama = routed
    ollama.answers.update({"uma maçã": new(APPLE), "nossa, ficou linda!": CHAT})
    turn.turn("uma maçã")
    turn.turn("nossa, ficou linda!", answer="Que bom!")
    sent = [m["content"] for m in turn.model_requests[-1] if m["role"] == "assistant"]
    assert sent == ["[imagem gerada anexada pelo sistema]"]


def test_the_outlet_replaces_a_claim_even_with_fresh_metadata(monkeypatch):
    """Open WebUI calls the outlet with fresh metadata; the turn is found by its message id."""
    module = load_module()
    monkeypatch.setitem(module, "image_stack", lambda _model: {"num_ctx": 8192})
    filter_ = module["Filter"]()
    metadata = ui_metadata(image=False)
    body = {"model": "stack-model", "messages": [{"role": "user", "content": "e aí?"}]}
    run(filter_.inlet(body, metadata))
    reply = {"messages": [{"role": "assistant", "content": "1 imagem gerada."}]}
    run(filter_.outlet(reply, {"chat_id": "chat-1", "message_id": metadata["message_id"]}))
    assert reply["messages"][0]["content"] == module["NO_PICTURE_NOTICE"]


# --- a picture shown again ---------------------------------------------------------------------


def test_show_image_attaches_an_existing_picture_without_any_job_or_swap(routed):  # noqa: F811
    turn, comfyui, _, ollama = routed
    ollama.answers["uma maçã"] = new(APPLE)
    first = turn.turn("uma maçã")
    jobs = len(comfyui.jobs)
    ollama.timeline.clear()
    reply = turn.turn("Não estou vendo, me mostra a imagem 1 de novo", answer="Aqui está!")
    assert reply["content"] == "Imagem 1."
    assert [item["url"] for item in reply["files"]] == [first["files"][0]["url"]]
    assert len(comfyui.jobs) == jobs and ollama.timeline == [], "no router, unload or engine"
    missing = turn.turn("mostra a imagem 7")
    assert missing["content"] == "Não encontrei a imagem 7 neste chat. Imagens disponíveis: 1."
    assert missing["files"] == []


def test_the_router_can_choose_to_show_a_picture():
    module = load_module()
    decision = {"action": "show_image", "prompt": "", "plan": {"base": 2}, "references": []}
    assert module["decide"](decision, "cadê a segunda?", True) == ("show_image", "", "model")
    assert module["show_number"]("cadê a segunda?", decision) == 2


# --- a change of size or shape -----------------------------------------------------------------


def test_size_words_and_the_router_mark_a_shape_change():
    module = load_module()
    assert module["shape_change"]("aumente os braços dela ainda mais")
    assert module["shape_change"]("make the arms even larger")
    assert module["shape_change"]("muda a pose", {"shape_change": True})
    assert not module["shape_change"]("deixa o fundo azul")
    # 15/11: proportion words with no size word went to the Krea General Edit and nothing changed.
    assert module["shape_change"]("Deixa ela com o corpo mais feminino e mais jovem")
    assert module["shape_change"]("deixe o corpo mais magro")
    assert module["freer"]("moderate") == "strong" and module["freer"]("strong") == "strong"


def test_removing_something_worn_is_an_edit_and_removing_a_person_is_a_removal():
    module = load_module()
    # 15/11: the Raw removal of "the cape" took the arms with it.
    assert module["worn_removal"]({"region_targets": ["the cape"]}, "consegue tirar essa capa?")
    assert module["worn_removal"]({"region_targets": []}, "tira o chapéu dela")
    assert not module["worn_removal"](
        {"region_targets": ["the man in the red shirt"]}, "tira o homem de camisa vermelha"
    )
    assert not module["worn_removal"]({"region_targets": ["the chair"]}, "tira a cadeira")


def test_an_automatic_area_grows_feathered_and_stays_inside_the_picture():
    from PIL import Image, ImageDraw

    module = load_module()
    mask = Image.new("L", (100, 120), 0)
    ImageDraw.Draw(mask).rectangle((40, 40, 59, 59), fill=255)
    raw = io.BytesIO()
    mask.save(raw, "PNG")
    grown, radius, coverage = module["dilate_mask"](raw.getvalue(), 0.2)
    result = Image.open(io.BytesIO(grown))
    assert result.size == (100, 120) and radius == 4
    assert result.getpixel((50, 50)) == 255, "the found area stays whole"
    assert result.getpixel((37, 50)) > 0, "the area reaches past its old outline"
    assert result.getpixel((0, 0)) == 0 and result.getpixel((99, 119)) == 0
    assert coverage > 400 / (100 * 120)
    edge = [result.getpixel((x, 50)) for x in range(28, 40)]
    assert any(0 < value < 255 for value in edge), "the new edge is feathered"


# --- corrections keep their area (RTX 5090, 01/11) ---------------------------------------------


SMILE = "Give her a seductive smile. Keep her face, pose and framing unchanged."
PREVIOUS_SMILE = {
    "prompt": SMILE,
    "refs": [{"image": 1, "role": "A", "purpose": "the original source"}],
    "base": 1,
    "boost": 2.5,
    "edit_mode": "local",
    "scope": "local",
    "region_targets": ["mouth"],
    "changes": ["a seductive smile"],
    "preserve": ["her face"],
    "user_intent": "faz ela sorrir",
    "part": "edit",
    "automatic_region": True,
    "mask_png": "AAAA",
}


@pytest.mark.parametrize(
    "plan",
    [
        {"changes": ["a seductive smile"]},
        {"changes": [SMILE]},  # the router echoing the last prompt
        {"changes": ["seductive smile"], "preserve": ["face", "pose"]},  # a subset
        {"changes": ["repeat the last edit exactly"], "correction": "no_change"},
    ],
)
def test_a_retry_that_only_repeats_the_recipe_is_a_pure_retry_with_its_area(plan):
    module = load_module()
    decision = {"action": "edit_image", "prompt": SMILE, "references": []}
    decision["plan"] = retry_plan(**plan)
    converted, recipe = module["retry_decision"](decision, PREVIOUS_SMILE)
    assert not recipe.get("fresh") and recipe["mask_png"] == "AAAA"
    assert converted["prompt"] == SMILE and converted["plan"]["changes"] == ["a seductive smile"]


def test_a_retry_with_a_genuinely_new_change_is_still_a_new_delta():
    module = load_module()
    decision = {
        "action": "edit_image",
        "prompt": "An open-mouthed smile showing teeth.",
        "references": [],
        "plan": retry_plan(changes=["an open-mouthed smile showing teeth"]),
    }
    _converted, recipe = module["retry_decision"](decision, PREVIOUS_SMILE)
    assert recipe["fresh"] is True and recipe["mask_png"] is None


# --- sides, scopes and the delivered picture (session 02, 02/11) --------------------------------

SYMMETRIC = (
    "Make both arms significantly larger and perfectly symmetrical, ensuring the left arm "
    "matches the size of the right one. Keep her face unchanged."
)
# The user's words of 02/11 02:01 ("you only enlarged the left one, ... symmetrical").
ONLY_LEFT = "Você só aumentou o da esquerda, quero eles ainda maiores, beeem maiores e simétricos"
PREVIOUS_SYMMETRY = {
    "prompt": SYMMETRIC,
    "refs": [{"image": 3, "role": "A", "purpose": "the original source"}],
    "base": 3,
    "boost": 1.5,
    "edit_mode": "local",
    "scope": "local",
    "region_targets": ["arms"],
    "changes": [
        "make both arms significantly larger",
        "ensure symmetry between left and right arms",
    ],
    "preserve": ["face"],
    "user_intent": "quero eles ainda maiores e simétricos",
    "part": "edit",
    "automatic_region": True,
    "mask_png": "AAAA",
    "current": 4,
}


@pytest.mark.parametrize(
    "change",
    [
        "make the right arm significantly larger",
        "only the right one",
        "make just the left arm larger",
        "do not make the left arm larger",
        "só o direito",
        "aumente apenas um dos braços",
    ],
)
def test_a_side_a_scope_or_a_negation_makes_a_retry_change_new(change):
    module = load_module()
    assert module["new_changes"]({"changes": [change], "preserve": []}, PREVIOUS_SYMMETRY) == [
        change
    ]


@pytest.mark.parametrize("change", PREVIOUS_SYMMETRY["changes"])
def test_the_recipes_own_sided_changes_still_only_repeat_it(change):
    module = load_module()
    assert module["new_changes"]({"changes": [change], "preserve": []}, PREVIOUS_SYMMETRY) == []


def test_only_the_right_one_after_both_symmetric_is_a_new_request_not_a_replay():
    """02/11 02:04: "only the right one" was a subset of "both ... left ... right" and the old
    recipe was replayed instead."""
    module = load_module()
    decision = {
        "action": "edit_image",
        "prompt": "Make only the right arm significantly larger.",
        "references": [],
        "plan": retry_plan(
            scope="local",
            region_targets=["arms"],
            changes=["make only the right arm significantly larger"],
        ),
    }
    converted, recipe = module["retry_decision"](decision, PREVIOUS_SYMMETRY)
    assert recipe["fresh"] is True and converted["prompt"] == decision["prompt"]
    assert converted["plan"]["changes"] == ["make only the right arm significantly larger"]


def test_nothing_changed_redoes_the_recipe_on_the_delivered_picture_and_repeat_on_its_source():
    module = load_module()
    decision = {
        "action": "edit_image",
        "prompt": "(the router's echo)",
        "references": [],
        "plan": retry_plan(correction="no_change", changes=["make both arms significantly larger"]),
    }
    converted, recipe = module["retry_decision"](decision, PREVIOUS_SYMMETRY)
    assert not recipe.get("fresh") and recipe["on_current"] is True
    assert [ref["image"] for ref in converted["references"]] == [4]
    assert converted["plan"]["base"] == 4 and recipe["refs"] == converted["references"]
    assert converted["prompt"] == SYMMETRIC and recipe["mask_png"] == "AAAA"
    exact = {**decision, "plan": retry_plan(changes=["make both arms significantly larger"])}
    converted, recipe = module["retry_decision"](exact, PREVIOUS_SYMMETRY)
    assert [ref["image"] for ref in converted["references"]] == [3]
    assert converted["plan"]["base"] == 3 and recipe["on_current"] is False


def test_nothing_changed_after_a_several_source_edit_redoes_it_on_its_own_sources():
    module = load_module()
    decision = {
        "action": "edit_image",
        "prompt": "",
        "references": [],
        "plan": retry_plan(correction="no_change"),
    }
    converted, recipe = module["retry_decision"](decision, {**PREVIOUS, "current": 3})
    assert converted["references"] == PREVIOUS["refs"] and recipe["on_current"] is False


def test_a_keep_that_names_what_a_change_touches_leaves_the_plan_and_the_prompt():
    """01/11: "stretching the fabric of her shirt" beside "Keep unchanged: clothing"."""
    module = load_module()
    intent = {
        "changes": ["much larger arms", "stretch the fabric of her top"],
        "preserve": ["face", "clothing", "her pose and body proportions"],
        "region_targets": ["arms"],
    }
    prompt = (
        "Make her arms much larger, stretching the fabric of her top. "
        "Keep her face, the hat, her clothing and the background unchanged."
    )
    out, fixed, dropped = module["consistent_plan"](prompt, intent, "aumente os braços")
    assert fixed["preserve"] == ["face", "her pose"]
    assert dropped == {"preserve": ["clothing", "her pose and body proportions"]}
    assert out.endswith("Keep her face, the hat, the background unchanged.")
    purpose = "the character, pose, clothing and style to keep"
    assert (
        module["kept_part"](purpose, module["touched_things"](intent))
        == "the character, pose, style to keep"
    )


@pytest.mark.parametrize(
    ("said", "side"),
    [
        (ONLY_LEFT, "left"),
        (
            (
                "Mudiu pouco e foi só um dos braços dela, o braço direito ta no mesmo tamanho, "
                "deixe eles iguais e beeeem maiores"
            ),
            "left",
        ),
        ("the right one grew but the left one did not", "right"),
        ("aumente os dois", None),
        ("aumente o direito", None),
    ],
)
def test_the_side_the_user_says_already_grew(said, side):
    assert load_module()["grown_side"](said) == side


def test_after_one_side_grew_no_clause_asks_it_to_shrink_or_match_the_other():
    """02/11 02:01: after "you only enlarged the left one" the prompt asked the left one to
    match the right one."""
    module = load_module()
    said = ONLY_LEFT
    intent = {
        "changes": [
            "make both arms significantly larger",
            "ensure symmetry between left and right arms",
            "make the right arm match the left one",
        ],
        "preserve": ["face"],
        "region_targets": ["arms"],
    }
    out, fixed, dropped = module["consistent_plan"](SYMMETRIC, intent, said)
    assert dropped == {"side": "left"}
    assert out == (
        "Make both arms significantly larger and perfectly symmetrical. Keep her face "
        "unchanged. " + module["GROWN_SIDE_GUARD"].format(side="left", other="right")
    )
    assert fixed["changes"] == [
        "make both arms significantly larger",
        "make the right arm match the left one",
    ]
    assert module["intent_prompt"](out, fixed, said).endswith(
        'Sides exactly as the user said them: "Você só aumentou o da esquerda". '
        "(esquerdo/esquerda = left)."
    )


def test_no_correction_goes_below_a_ref_boost_of_one():
    """ref_boost < 1 is unsupported by the adapter's author and gained nothing in session 02."""
    module = load_module()
    assert module["edit_strengths"]("strong", 1, retry_from=1.5) == ([1.0], "correction")
    assert module["edit_strengths"]("strong", 1, retry_from=1.0) == ([1.0], "correction")
    assert module["edit_strengths"]("strong", 2, retry_from=1.5)[0] == [1.0, 1.75]


def test_the_ledger_records_every_strength_the_filter_can_use(tmp_path):
    import importlib.util
    import itertools

    from local_ai_stack.resources import resource_directory

    path = resource_directory("assets") / "open-webui" / "pepito_calibration.py"
    spec = importlib.util.spec_from_file_location("pepito_calibration_range", path)
    ledger = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ledger)
    module = load_module()
    used = set()
    for change, count, retry_from in itertools.product(
        ("subtle", "moderate", "strong"), (1, 2, 3), (None, 0.5, 1.0, 1.5, 2.5, 4.0, 5.0, 7.0)
    ):
        for bias in (-2.5, 0.0, 2.5):
            used.update(
                module["edit_strengths"](change, count, None, bias, retry_from=retry_from)[0]
            )
    for manual in (0.1, 1.0, 3.3, 9.0):
        used.update(module["edit_strengths"]("strong", 1, {"ref_boost_manual": manual})[0])
    for level in module["FIDELITY_LEVELS"]:
        used.update(module["edit_strengths"]("strong", 1, {"fidelidade_edicao": level})[0])
    for index, strength in enumerate(sorted(used)):
        facts = {
            "family": "krea2",
            "workflow": "edit/1/x",
            "change": "strong",
            "operation": "edit:local",
            "strength": strength,
            "fidelity": "correction",
            "request_id": f"r{index}",
            "source_numbers": [1],
            "measurement": None,
            "requested_count": 1,
            "status": "completed",
            "delivered": True,
        }
        ledger.register_attempt(tmp_path, "alice", "chat", f"a{index}", facts)
    assert min(used) >= 1.0


def solid_png(color=(200, 120, 110), size=(64, 96)) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "PNG")
    return out.getvalue()


def coverage(mask: bytes) -> float:
    from PIL import Image, ImageStat

    return ImageStat.Stat(Image.open(io.BytesIO(mask)).convert("L")).mean[0] / 255


def local_edit(prompt: str, targets: list, changes: list, change: str = "moderate") -> dict:
    return {
        **edit(prompt),
        "change": change,
        "plan": {
            "request_mode": "refine",
            "scope": "local",
            "region_targets": targets,
            "changes": changes,
        },
    }


def local_retry(targets: list, changes: list, correction: str = "none") -> dict:
    return {
        **edit("(the router's own words)"),
        "plan": {
            "request_mode": "retry",
            "scope": "local",
            "region_targets": targets,
            "changes": changes,
            "correction": correction,
        },
    }


def pixel(data: bytes) -> tuple:
    from PIL import Image

    return Image.open(io.BytesIO(data)).convert("RGB").getpixel((0, 0))


def test_growth_is_capped_and_far_apart_targets_do_not_widen_one_another():
    from PIL import Image, ImageDraw

    module = load_module()
    mask = Image.new("L", (200, 300), 0)
    ImageDraw.Draw(mask).rectangle((10, 10, 29, 29), fill=255)
    ImageDraw.Draw(mask).rectangle((170, 270, 189, 289), fill=255)
    raw = io.BytesIO()
    mask.save(raw, "PNG")
    found = coverage(raw.getvalue())
    _grown, radius, covered = module["dilate_mask"](raw.getvalue(), 0.3)
    assert radius <= round(0.3 * (800**0.5)), "the size of the area, not of the box around both"
    assert found < covered <= found * module["MAX_AREA_GROWTH"] * 1.05


# --- the private turn trace ---------------------------------------------------------------------


def test_the_trace_never_holds_credentials_or_picture_bytes(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    module = load_module()
    metadata = {"chat_id": "chat-9", "message_id": "m"}
    module["start_trace"](metadata, user_text="faz uma foto")
    picture = "data:image/png;base64," + "iVBORw0KGgo" * 200
    module["trace_event"](
        metadata,
        "error",
        message="upstream said Authorization: Bearer sk-secret-123456",
        headers={"Authorization": "Bearer sk-secret-123456"},
        api_key="sk-secret-123456",
        image=picture,
        png=b"\x89PNG" * 10,
        nested={"token": "abc", "url": picture},
    )
    text = (tmp_path / "pepito-trace" / "turns.jsonl").read_text(encoding="utf-8")
    assert "sk-secret" not in text and "iVBORw0KGgo" not in text
    record = trace_records(tmp_path)[-1]
    assert record["headers"] == "[redacted]" and record["api_key"] == "[redacted]"
    assert record["png"] == "[40 bytes]" and record["image"].startswith("[data uri")
    assert record["nested"]["token"] == "[redacted]"
    assert record["turn"] == metadata[module["TRACE_KEY"]]["turn"]
    if os.name == "posix":
        mode = (tmp_path / "pepito-trace" / "turns.jsonl").stat().st_mode
        assert mode & 0o077 == 0, "the owner's only"


def test_the_trace_rotates_and_keeps_one_older_file(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    module = load_module()
    module["TRACE_MAX_BYTES"] = 400
    metadata = {"chat_id": "chat-9"}
    module["start_trace"](metadata, user_text="x")
    for index in range(30):
        module["trace_event"](metadata, "ollama_ps", models=[], index=index)
    directory = tmp_path / "pepito-trace"
    assert sorted(path.name for path in directory.iterdir()) == ["turns.jsonl", "turns.jsonl.1"]
    assert (directory / "turns.jsonl").stat().st_size <= 400
    for name in ("turns.jsonl", "turns.jsonl.1"):
        for line in (directory / name).read_text(encoding="utf-8").splitlines():
            assert json.loads(line)["chat_id"] == "chat-9"
    last = trace_records(tmp_path)[-1]
    assert last["index"] == 29


def test_a_trace_that_cannot_be_written_never_breaks_the_turn(monkeypatch, tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("DATA_DIR", str(blocked))
    module = load_module()
    metadata = {"chat_id": "chat-9"}
    module["start_trace"](metadata, user_text="x")
    module["trace_event"](metadata, "turn_end", total_seconds=1.0)


@pytest.mark.parametrize(
    ("mode", "text", "origin", "vision", "expected", "looks"),
    [
        ("art", "aumenta", {"style": "photo"}, "photo", ("art", False), False),
        ("auto", "aumenta, em anime", {"style": "photo"}, "photo", ("art", False), False),
        ("auto", "aumenta", {"style": "art"}, "photo", ("art", False), False),
        ("auto", "aumenta", {"style": ""}, "art", ("art", True), True),
        ("auto", "aumenta", None, "photo", ("photo", True), True),
        # The vision model unsure or the source unreadable: Qwen alone, right for either.
        ("auto", "aumenta", None, "unsure", ("photo", False), True),
        ("auto", "aumenta", None, "unreadable", ("photo", False), False),
    ],
)
def test_the_shape_edit_medium_prefers_what_is_known_over_what_is_seen(
    monkeypatch, mode, text, origin, vision, expected, looks
):
    import asyncio
    import sys
    import types

    module = load_module()
    looked = []

    def classify_medium(base_url, model, source, *, num_ctx=None):
        looked.append(source)
        if vision == "unsure":
            raise ValueError("unsure")
        return vision

    async def picture_bytes(url):
        return None if vision == "unreadable" else b"png"

    stub = types.SimpleNamespace(classify_medium=classify_medium)
    monkeypatch.setitem(sys.modules, "open_webui.pepito_image_preservation", stub)
    monkeypatch.setitem(module, "picture_bytes", picture_bytes)
    found = asyncio.run(
        module["edit_medium"](mode, text, origin, "/source.png", "vision", None, {})
    )
    assert found == expected
    assert bool(looked) == looks


def _methods(module, preferred="auto", **overrides):
    arguments = {
        "reshaping": True,
        "pure": False,
        "escalate": False,
        "previous": None,
        "correction": None,
        "medium": "art",
        **overrides,
    }
    return module["edit_methods"](preferred, **arguments)


def test_auto_shape_order_follows_the_source_medium():
    module = load_module()
    assert _methods(module) == ("warp_qwen", "warp", "qwen")
    assert _methods(module, medium="photo") == ("qwen",)
    assert _methods(module, medium="") == ("qwen",)
    assert _methods(module, reshaping=False) == ()


@pytest.mark.parametrize(
    ("medium", "previous", "escalate", "pure", "expected"),
    [
        ("art", "warp_qwen", True, False, ("warp", "qwen")),
        ("art", "warp", True, False, ("qwen",)),
        ("art", "qwen", True, False, ("qwen",)),
        ("art", "warp", False, True, ("warp", "qwen")),
        ("photo", "qwen", True, False, ("qwen",)),
        # A photo never escalates into a warp, even after one was chosen by hand.
        ("photo", "warp", True, False, ("qwen",)),
    ],
)
def test_nothing_changed_moves_past_the_previous_method(medium, previous, escalate, pure, expected):
    module = load_module()
    found = _methods(
        module,
        medium=medium,
        escalate=escalate,
        pure=pure,
        previous={"edit_method": previous, "shape_change": True},
    )
    assert found == expected


def test_explicit_and_repeated_methods_are_the_only_candidate():
    module = load_module()
    assert _methods(module, "qwen") == ("qwen",)
    assert _methods(module, "warp_qwen", medium="photo") == ("warp_qwen",)
    repeat = _methods(
        module,
        "qwen",
        pure=True,
        previous={"edit_method": "warp"},
        correction={"edit_method": "warp"},
    )
    assert repeat == ("warp",)
    with pytest.raises(module["ImagePlanError"]):
        _methods(module, "spin")


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"mode": "magnify"},
        {"mode": "spin", "factor": 1.6},
        {"mode": "width", "factor": 1},
    ],
)
def test_a_warp_requires_an_explicit_bounded_deformation(value):
    module = load_module()
    with pytest.raises(module["ImagePlanError"]):
        module["validate_deformation"](value)
