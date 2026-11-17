"""A stopped picture never leaves the card busy; honest chat replies; the native gallery,
the attached area and the validated plan."""

from __future__ import annotations

import asyncio
import io
import json
import sys
import types
import urllib.request

import pytest
import test_image_edit_dispatch as dispatch
from test_image_edit_dispatch import (
    capella_stacks,  # noqa: F401
    edit_config,  # noqa: F401
    preset,  # noqa: F401
    routed,  # noqa: F401
    settings,  # noqa: F401
    world,  # noqa: F401
)
from test_open_webui_image_filter import load_module, run, ui_metadata

COMFYUI = "http://127.0.0.1:8188"


class ComfyUI:
    """ComfyUI's /queue, /system_stats, /interrupt and /free as urllib reaches them."""

    def __init__(self, running: int = 0, held_bytes: int = 0) -> None:
        self.running = running
        self.held_bytes = held_bytes
        self.calls: list[str] = []

    def __call__(self, request, timeout=None):
        url = request if isinstance(request, str) else request.full_url
        path = url.removeprefix(COMFYUI)
        self.calls.append(path)
        if path == "/queue" and isinstance(request, str):
            running = [["job"]] * self.running
            return io.BytesIO(json.dumps({"queue_running": running, "queue_pending": []}).encode())
        if path == "/system_stats":
            device = {"torch_vram_total": self.held_bytes}
            return io.BytesIO(json.dumps({"devices": [device]}).encode())
        return io.BytesIO(b"{}")


@pytest.fixture
def card(monkeypatch):
    def install(**state):
        comfy = ComfyUI(**state)
        monkeypatch.setattr(urllib.request, "urlopen", comfy)
        monkeypatch.setenv("PEPITO_COMFYUI_URL", COMFYUI)
        monkeypatch.setenv("PEPITO_EXCLUSIVE_VRAM", "true")
        return comfy

    return install


def test_a_job_a_stopped_turn_left_running_is_stopped_before_the_chat_model_loads(card):
    module = load_module()
    comfy = card(running=1)
    statuses: list = []

    async def emit(event):
        statuses.append(event["data"]["description"])

    assert run(module["reclaim_comfyui"](emit)) is True
    assert comfy.calls == ["/queue", "/queue", "/interrupt", "/free"]
    assert statuses == ["Liberando a GPU da imagem cancelada"]


def test_models_a_finished_orphan_still_holds_are_freed_and_an_idle_card_is_only_asked(card):
    module = load_module()
    comfy = card(held_bytes=13 << 30)
    assert run(module["reclaim_comfyui"]()) is False
    assert comfy.calls == ["/queue", "/system_stats", "/free"]
    idle = card(held_bytes=200 << 20)
    run(module["reclaim_comfyui"]())
    assert idle.calls == ["/queue", "/system_stats"], "nothing held: nothing is freed"


def test_a_picture_being_made_by_another_turn_is_waited_for_and_never_interrupted(card):
    module = load_module()
    module["GPU_WAIT_SECONDS"], module["GPU_POLL_SECONDS"] = 0.05, 0.01
    comfy = card(running=1)
    module["_ACTIVE_JOBS"][0] = 1
    statuses: list = []

    async def emit(event):
        statuses.append(event["data"]["description"])

    assert run(module["reclaim_comfyui"](emit)) is False
    assert comfy.calls == [], "still running after the bounded wait: left alone"
    assert statuses == [module["GPU_WAIT_STATUS"]]


def test_a_picture_that_ends_during_the_wait_lets_the_card_be_checked_after_it(card):
    module = load_module()
    module["GPU_POLL_SECONDS"] = 0.01
    comfy = card(held_bytes=200 << 20)
    module["_ACTIVE_JOBS"][0] = 1

    async def scenario():
        async def finish():
            await asyncio.sleep(0.05)
            assert comfy.calls == [], "nothing is asked while the picture is being made"
            module["_ACTIVE_JOBS"][0] = 0

        ending = asyncio.create_task(finish())
        busy = await module["reclaim_comfyui"]()
        await ending
        return busy

    assert run(scenario()) is False
    assert comfy.calls == ["/queue", "/system_stats"]


def test_after_a_free_the_card_is_polled_until_comfyui_really_released_it(card):
    module = load_module()
    module["GPU_POLL_SECONDS"] = 0.01
    comfy = card(held_bytes=18 << 30)
    statuses: list = []

    async def emit(event):
        statuses.append(event["data"]["description"])

    async def scenario():
        async def release():
            await asyncio.sleep(0.05)
            comfy.held_bytes = 300 << 20

        releasing = asyncio.create_task(release())
        released = await module["wait_comfyui_release"](emit)
        await releasing
        return released

    assert run(scenario()) is True
    assert comfy.calls.count("/system_stats") >= 2
    assert statuses == [module["GPU_WAIT_STATUS"]]


def test_stop_during_a_picture_interrupts_comfyui_and_frees_the_card(card):
    module = load_module()
    comfy = card(running=1)
    filter_ = module["Filter"]()

    async def stopped(*_args):
        raise asyncio.CancelledError

    filter_._generate = stopped
    metadata = ui_metadata()
    metadata[module["STATE_KEY"]] = {"text": "", "generate": {"prompt": "x"}, "swapped": True}
    body = {"messages": [{"role": "user", "content": "x"}]}
    with pytest.raises(asyncio.CancelledError):
        run(filter_.request(body, metadata))
    assert comfy.calls == ["/queue", "/queue", "/interrupt", "/free"]
    assert module["_ACTIVE_JOBS"][0] == 0


@pytest.mark.parametrize(
    ("prompt", "decision", "expected"),
    [
        ("A photo of a gothic punk woman in a coat.", "art", "photo"),
        ("An anime illustration with cel shading of a woman.", "photo", "art"),
        ("A woman standing in a doorway.", "art", "art"),
        ("A woman standing in a doorway.", "", "photo"),
    ],
)
def test_a_new_pictures_style_follows_the_medium_its_prompt_opens_with(prompt, decision, expected):
    resolve = load_module()["resolve_style"]
    assert resolve("auto", "me faz uma gótica punk", {"style": decision}, None, prompt) == expected


def stream_reply(module, text: str) -> str:
    filter_ = module["Filter"]()
    metadata = {module["STATE_KEY"]: {"text": "", "no_image": True}}
    shown = []
    for piece in (text[: len(text) // 2], text[len(text) // 2 :]):
        event = {"choices": [{"delta": {"content": piece}, "finish_reason": None}]}
        shown.append(run(filter_.stream(event, metadata))["choices"][0]["delta"]["content"])
    event = {"choices": [{"delta": {}, "finish_reason": "stop"}]}
    shown.append(run(filter_.stream(event, metadata))["choices"][0]["delta"]["content"])
    return "".join(shown)


@pytest.mark.parametrize(
    "claim",
    [
        "Perfeito! Aqui está a imagem gerada: uma mulher na praia.",
        "Já gerei a imagem com tudo o que você pediu.",
        "A imagem que você pediu foi criada e está no chat.",
        "Here is the image you asked for.",
        # 01/11: the filter's own fixed text, imitated with no picture, and the follow-up.
        "1 imagem gerada.",
        "Pode conferir a imagem que acabou de ser gerada no seu chat. Ela tem todos os detalhes.",
    ],
)
def test_a_reply_claiming_a_picture_that_was_never_made_is_replaced_by_the_notice(claim):
    module = load_module()
    assert stream_reply(module, claim) == module["NO_PICTURE_NOTICE"]


def test_a_claim_after_the_opening_stops_the_stream_and_ends_with_the_notice():
    module = load_module()
    text = "Claro, vamos conversar sobre a cena que você descreveu ontem à noite. " * 2
    text += "Aqui está a imagem gerada."
    shown = stream_reply(module, text)
    assert shown == text[: len(text) // 2] + "\n\n" + module["NO_PICTURE_NOTICE"]


@pytest.mark.parametrize(
    "honest",
    [
        "A imagem 3 foi gerada antes da troca de estilo; quer que eu refaça?",
        "Já fiz esse exercício de lógica ontem.",
        "Posso gerar outra se você ligar o botão Image.",
    ],
)
def test_an_honest_reply_is_left_alone(honest):
    module = load_module()
    assert stream_reply(module, honest) == honest


# --- the native gallery, the attached area and the validated plan ---------------------


class Native:
    """open_webui.pepito_native as the managed Open WebUI loads it: what the filter calls."""

    def __init__(self) -> None:
        self.published: list = []
        self.selected: int | None = None
        self.picks: list = []
        self.draft: dict | None = None
        self.consumed_text: str | None = None

    async def publish_gallery(self, chat_id, user_id, message_id, entries):
        self.published.append((chat_id, user_id, message_id, [dict(entry) for entry in entries]))
        self.selected = entries[-1]["number"]
        return "a" * 32

    async def read_selection(self, chat_id, user_id):
        return self.selected

    async def select_image(self, chat_id, user_id, number):
        self.picks.append(number)
        self.selected = number
        return number

    async def consume_draft(self, chat_id, user_id, text):
        self.consumed_text = text
        draft, self.draft = self.draft, None
        return draft


@pytest.fixture
def native(monkeypatch):
    fake = Native()
    module = types.ModuleType("open_webui.pepito_native")
    for name in ("publish_gallery", "read_selection", "select_image", "consume_draft"):
        setattr(module, name, getattr(fake, name))
    monkeypatch.setitem(sys.modules, "open_webui.pepito_native", module)
    return fake


def solid(color, size=(64, 96)) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "PNG")
    return out.getvalue()


def mask_png(size=(64, 96), lower_half=True) -> bytes:
    from PIL import Image

    image = Image.new("L", size, 0)
    if lower_half:
        image.paste(255, (0, size[1] // 2, size[0], size[1]))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def pixels(data: bytes, *points):
    from PIL import Image

    image = Image.open(io.BytesIO(data)).convert("RGB")
    return [image.getpixel(point) for point in points]


def made_with(monkeypatch, color, turn, text):
    monkeypatch.setattr(dispatch, "PNG", solid(color))
    return turn.turn(text)


ENTRIES = [{"number": 1, "url": "u1"}, {"number": 2, "url": "u2"}, {"number": 3, "url": "u3"}]


def plan(**changes):
    return {
        "request_mode": "refine",
        "scope": "local",
        "region_targets": ["coat"],
        "base": 1,
        "changes": [],
        "preserve": [],
        "correction": "none",
        "clarification": "",
        **changes,
    }


def refs(*pairs):
    return [{"image": n, "role": role, "purpose": "what it is for"} for n, role in pairs]


def invalid(module, **decision):
    with pytest.raises(module["ImagePlanError"]) as raised:
        module["validate_intent"](decision, ENTRIES)
    return str(raised.value)


def test_a_plan_must_be_complete_and_consistent_before_anything_is_made():
    module = load_module()
    good = {"plan": plan(changes=["blue coat"], preserve=["face"]), "references": refs((1, "A"))}
    assert module["validate_intent"](good, ENTRIES)["base"] == 1
    assert "plano" in invalid(module, references=refs((1, "A")))
    assert "9" in invalid(module, plan=plan(base=9), references=refs((9, "A")))
    assert "imagem que seria editada" in invalid(
        module, plan=plan(base=1), references=refs((2, "B"))
    )
    assert "Qual das duas?" == invalid(
        module, plan=plan(clarification="Qual das duas?"), references=refs((1, "A"))
    )
    assert "alterar e preservar" in invalid(
        module,
        plan=plan(changes=["Her hair"], preserve=["her hair"]),
        references=refs((1, "A")),
    )
    assert "o que usar de cada referência" in invalid(
        module, plan=plan(), references=[{"image": 1, "role": "A", "purpose": " "}]
    )
    assert "correção" in invalid(module, plan=plan(correction="again"), references=refs((1, "A")))


def test_the_sources_are_exactly_those_the_plan_names_and_a_surplus_is_asked_about():
    module = load_module()
    one = {"edit": {}}
    two = {"edit": {}, "edit_refs": {"2": {}}}
    plan_of = module["reference_plan"]
    merged = plan_of(ENTRIES, refs((2, "A"), (1, "B"), (1, "C")), two)
    assert [slot["number"] for slot in merged["slots"]] == [2, 1], (
        "a source needed twice is one slot"
    )
    assert merged["slots"][1]["roles"] == ["B", "C"]
    alone = plan_of(ENTRIES, [], one)["slots"]
    assert [slot["number"] for slot in alone] == [3], "no reference named: only the latest picture"
    with pytest.raises(module["ImagePlanError"], match="precisa de 3 imagens"):
        plan_of(ENTRIES, refs((1, "A"), (2, "B"), (3, "C")), two)
    with pytest.raises(module["ImagePlanError"], match="não está disponível"):
        plan_of(ENTRIES, refs((7, "A")), two)


def test_a_wrong_source_complaint_asks_which_picture_when_the_plan_names_none():
    module = load_module()
    decision = {"plan": plan(base=0, correction="wrong_source"), "references": []}
    assert "Qual imagem devo usar como origem" in invalid(module, **decision)
    named = {"plan": plan(base=2, correction="wrong_source"), "references": refs((2, "A"))}
    assert module["validate_intent"](named, ENTRIES)["base"] == 2
