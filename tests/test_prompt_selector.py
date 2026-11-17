"""SPEC-308: a picture's prompt is readable by its owner, and regenerating it with the prompt
edited (or as it was) runs its recipe without the router, the age safeguard on the words sent."""

from __future__ import annotations

import asyncio
import sys
import types

import pytest
from test_image_gpu_and_native_flow import Native, solid
from test_image_swap import SAID as SWAP_SAID
from test_image_swap import (
    capella_stacks,  # noqa: F401
    edit_config,  # noqa: F401
    preset,  # noqa: F401
    routed,  # noqa: F401
    settings,  # noqa: F401
    swapping,  # noqa: F401
    world,  # noqa: F401
)
from test_open_webui_native import ALICE, BOB, native, publish  # noqa: F401

RECIPE = {
    "kind": "edit",
    "part": "edit",
    "prompt": "Make the dress blue. Keep her face.",
    "edit_mode": "local",
    "source_urls": ["/api/v1/files/alice-extra/content"],
    "sources": [2],
    "mask_png": None,
    "patch": {"width": 832, "height": 1216, "seed": 7},
    "seed": 7,
}


def record(hub, number=1, recipe=RECIPE, chat_id="chat-a", user_id="alice"):
    return asyncio.run(hub.module.record_recipes(chat_id, user_id, {number: recipe}))


def regenerate(hub, headers=ALICE, **overrides):
    payload = {"chat_id": "chat-a", "number": 1, "mode": "edited", "prompt": "Make it red."}
    payload.update(overrides)
    return hub.client.post("/api/v1/pepito/images/regenerate", headers=headers, json=payload)


def consume(hub, text, chat_id="chat-a", user_id="alice"):
    return asyncio.run(hub.module.consume_regeneration(chat_id, user_id, text))


# --- the native store: the recipe by number, only for its owner ---------------------------


def test_the_recipe_of_a_picture_is_read_only_by_the_owner_of_its_chat(native):  # noqa: F811
    publish(native)
    publish(native, number=2, file_id="alice-extra")
    assert record(native) == 1
    assert record(native, number=9) == 0, "a number the chat never published takes no recipe"
    path = "/api/v1/pepito/images/recipe/1"
    assert native.client.get(path, params={"chat_id": "chat-a"}).status_code == 401
    assert native.client.get(path, params={"chat_id": "chat-a"}, headers=BOB).status_code == 404
    assert native.client.get(path, params={"chat_id": "chat-a2"}, headers=ALICE).status_code == 404
    shown = native.client.get(path, params={"chat_id": "chat-a"}, headers=ALICE).json()
    assert shown["prompt"] == RECIPE["prompt"] and shown["part"] == "edit"
    assert shown["sources"] == [2] and shown["seed"] == 7 and shown["area_png"] is None
    assert shown["canvas"] == {"width": 832, "height": 1216} and shown["regenerable"] is True
    missing = native.client.get(
        "/api/v1/pepito/images/recipe/2", params={"chat_id": "chat-a"}, headers=ALICE
    )
    assert missing.status_code == 404, "a picture without a recorded recipe"


def test_only_the_owner_can_arm_a_regeneration_and_it_is_used_once_for_its_message(native):  # noqa: F811
    publish(native)
    record(native)
    assert regenerate(native, headers=BOB).status_code == 404
    assert regenerate(native, prompt="   ").status_code == 400
    armed = regenerate(native).json()
    assert armed["text"] == "Regerar a imagem 1 com o prompt editado"
    assert consume(native, "Regerar a imagem 2 com o prompt editado")["issue"] == "text"
    with pytest.raises(native.module.HTTPException):
        consume(native, armed["text"], user_id="bob")
    used = consume(native, " Regerar a imagem 1  com o prompt editado ")
    assert used["prompt"] == "Make it red." and used["recipe"]["prompt"] == RECIPE["prompt"]
    assert consume(native, armed["text"]) is None, "used once"
    same = regenerate(native, mode="same", prompt=None).json()
    assert consume(native, same["text"])["prompt"] is None, "the recipe's own words"


def test_an_expired_regeneration_is_reported_once_and_removed(native, monkeypatch):  # noqa: F811
    publish(native)
    record(native)
    now = native.module.time.time()
    text = regenerate(native).json()["text"]
    monkeypatch.setattr(native.module.time, "time", lambda: now + 301)
    assert consume(native, text) == {"number": 1, "mode": "edited", "issue": "expired"}
    assert consume(native, text) is None


def test_a_recipe_whose_source_left_the_chat_cannot_be_armed(native):  # noqa: F811
    publish(native)
    record(native, recipe={**RECIPE, "source_urls": ["data:image/png;base64,AAAA"]})
    assert regenerate(native).status_code == 409


# --- the filter: the armed recipe made again without the router ---------------------------


class RecipeNative(Native):
    """The native module the filter calls, with the recipe store and one armed regeneration."""

    def __init__(self) -> None:
        super().__init__()
        self.recipes: dict = {}
        self.raws: dict = {}
        self.armed: dict | None = None

    async def record_recipes(self, chat_id, user_id, recipes, raws=None):
        self.recipes.update(recipes)
        self.raws.update(raws or {})
        return len(recipes)

    async def consume_regeneration(self, chat_id, user_id, text):
        armed, self.armed = self.armed, None
        return armed

    def arm(self, number, mode="edited", prompt=None):
        recipe = self.recipes[number]
        step = recipe.get("method") or "prompt"
        self.armed = {
            "number": number,
            "mode": mode,
            "step": step,
            "prompt": prompt,
            "recipe": recipe,
        }


def strings(job: dict) -> list[str]:
    return [v for node in job.values() for v in node["inputs"].values() if isinstance(v, str)]


def install(monkeypatch) -> RecipeNative:
    fake = RecipeNative()
    module = types.ModuleType("open_webui.pepito_native")
    for name in (
        "publish_gallery",
        "read_selection",
        "select_image",
        "consume_draft",
        "record_recipes",
        "consume_regeneration",
    ):
        setattr(module, name, getattr(fake, name))
    monkeypatch.setitem(sys.modules, "open_webui.pepito_native", module)
    return fake


def test_a_swap_variation_is_redone_alone_with_its_edited_step(swapping, monkeypatch):  # noqa: F811
    turn, _files, ollama, _ns, state, sources, _scene, _ = swapping
    store = install(monkeypatch)
    turn.turn(SWAP_SAID, attach=sources)
    qwen = next(n for n, recipe in store.recipes.items() if recipe["method"] == "whole")
    assert set(store.recipes[qwen]["prompts"]) == {"whole"}, "no clean reference to record"
    routed_before, jobs = len(ollama.router_requests), len(state["jobs"])
    edited = "Draw the woman from the second picture in place of the first one, smiling."
    store.arm(qwen, prompt=edited)
    reply = turn.turn(f"Regerar a imagem {qwen} com o prompt editado")
    assert len(ollama.router_requests) == routed_before and not state["vision"][1:]
    # The whole picture runs on the two-image Qwen graph (the fake calls it "marked").
    redone = [kind for kind, _ in state["jobs"][jobs:]]
    assert redone == ["marked"], "only the variation shown, with no reference or analysis"
    assert edited in strings(state["jobs"][jobs][1])
    assert len(reply["files"]) == 1
    assert "Variação 1 refeita: Qwen inteira" in reply["content"]


# --- SPEC-317: regenerate with the same seed --------------------------------------------------


def numbers(job: dict) -> list:
    return [v for node in job.values() for v in node["inputs"].values() if type(v) is int]


def test_the_same_seed_is_armed_with_its_own_message_and_the_recipe_view_reports_it(native):  # noqa: F811
    publish(native)
    record(native)
    shown = native.client.get(
        "/api/v1/pepito/images/recipe/1", params={"chat_id": "chat-a"}, headers=ALICE
    ).json()
    assert shown["seed"] == 7
    armed = regenerate(native, mode="seed", prompt=None)
    assert armed.status_code == 200
    assert armed.json()["text"] == "Regerar a imagem 1 com a mesma semente"
    found = consume(native, armed.json()["text"])
    assert found["mode"] == "seed" and found["prompt"] is None


@pytest.mark.parametrize(
    "recipe",
    [
        {**RECIPE, "seed": None, "patch": {"width": 832, "height": 1216}},
        {**RECIPE, "patch": {"width": 832, "height": 1216}},
        {**RECIPE, "kind": "generate", "seed": None},
        {**RECIPE, "kind": "swap", "seed": None, "method": "scene", "prompts": {"scene": "x"}},
    ],
)
def test_a_recipe_without_a_seed_cannot_be_armed_for_the_same_seed(native, recipe):  # noqa: F811
    publish(native)
    record(native, recipe=recipe)
    shown = native.client.get(
        "/api/v1/pepito/images/recipe/1", params={"chat_id": "chat-a"}, headers=ALICE
    ).json()
    assert shown["seed"] is None
    refused = regenerate(native, mode="seed", prompt=None)
    assert refused.status_code == 409
    assert "não guardou a semente" in refused.json()["detail"]
    assert consume(native, "Regerar a imagem 1 com a mesma semente") is None, "nothing armed"


def test_the_seed_of_an_edit_is_the_patchs_and_a_swaps_is_the_turns(native):  # noqa: F811
    publish(native)
    edit_recipe = {**RECIPE, "seed": 99, "patch": {"seed": 7}}
    record(native, recipe=edit_recipe)
    swap_recipe = {**RECIPE, "kind": "swap", "seed": 11, "method": "scene", "patch": {}}
    swap_recipe["prompts"] = {"scene": "x"}
    publish(native, number=2, file_id="alice-extra")
    record(native, number=2, recipe=swap_recipe)
    seeds = [
        native.client.get(
            f"/api/v1/pepito/images/recipe/{n}", params={"chat_id": "chat-a"}, headers=ALICE
        ).json()["seed"]
        for n in (1, 2)
    ]
    assert seeds == [7, 11]


def test_a_swap_variation_gets_the_recorded_seed_for_every_stage(swapping, monkeypatch):  # noqa: F811
    turn, _files, _ollama, _ns, state, sources, _scene, _ = swapping
    store = install(monkeypatch)
    turn.turn(SWAP_SAID, attach=sources)
    first = state["jobs"][:]
    qwen = next(n for n, recipe in store.recipes.items() if recipe["method"] == "whole")
    marked = next(n for n, recipe in store.recipes.items() if recipe["method"] == "marked")
    seed = store.recipes[qwen]["seed"]
    assert type(seed) is int and store.recipes[marked]["seed"] == seed, "one seed names the turn"

    def seeds(jobs):
        return [[n for n in numbers(graph) if abs(n - seed) < 10] for _kind, graph in jobs]

    full = seeds(first)  # reference, the whole picture, the marked pass, then the analysis
    before = len(state["jobs"])
    store.arm(marked, mode="seed")
    turn.turn(f"Regerar a imagem {marked} com a mesma semente")
    redone = seeds(state["jobs"][before:])
    assert store.recipes[max(store.recipes)]["seed"] == seed
    assert redone[:2] == [full[0], full[2]], "the reference and the pass keep the seeds they had"
    before = len(state["jobs"])
    store.arm(qwen, mode="seed")
    turn.turn(f"Regerar a imagem {qwen} com a mesma semente")
    assert seeds(state["jobs"][before:]) == [full[1]], "the whole picture keeps its seed"
    before = len(state["jobs"])
    store.arm(qwen, mode="same")
    turn.turn(f"Regerar a imagem {qwen} igual")
    assert store.recipes[max(store.recipes)]["seed"] != seed
    assert state["jobs"][before:], "a new seed still redoes the variation"


# --- SPEC-318: see the model's raw output, and redo on the whole picture --------------------

AREA_RECIPE = {**RECIPE, "mask_png": "AAAA"}


def raw_path(number=1):
    return f"/api/v1/pepito/images/raw/{number}"


def recipe_of(hub, number=1):
    return hub.client.get(
        f"/api/v1/pepito/images/recipe/{number}", params={"chat_id": "chat-a"}, headers=ALICE
    ).json()


def test_the_raw_output_is_kept_beside_the_recipe_and_read_only_by_its_owner(native):  # noqa: F811
    publish(native)
    raw = solid((10, 200, 30))
    kept = asyncio.run(
        native.module.record_recipes("chat-a", "alice", {1: AREA_RECIPE}, {1: (raw, False)})
    )
    assert kept == 1
    params = {"chat_id": "chat-a"}
    assert native.client.get(raw_path(), params=params).status_code == 401
    assert native.client.get(raw_path(), params=params, headers=BOB).status_code == 404
    other = native.client.get(raw_path(), params={"chat_id": "chat-a2"}, headers=ALICE)
    assert other.status_code == 404
    shown = native.client.get(raw_path(), params=params, headers=ALICE)
    assert shown.status_code == 200 and shown.content == raw, "the bytes the model returned"
    assert shown.headers["x-pepito-raw-crop"] == "0"
    assert recipe_of(native)["raw_available"] is True and recipe_of(native)["raw_crop"] is False
    asyncio.run(native.module.record_recipes("chat-a", "alice", {1: AREA_RECIPE}, {1: (raw, True)}))
    cropped = native.client.get(raw_path(), params=params, headers=ALICE)
    assert cropped.headers["x-pepito-raw-crop"] == "1"
    assert recipe_of(native)["raw_crop"] is True


def test_nothing_is_kept_without_an_area_and_a_raw_over_the_ceiling_is_not_kept(
    native,  # noqa: F811
    monkeypatch,
):
    publish(native)
    record(native)
    shown = recipe_of(native)
    assert shown["raw_available"] is False and shown["raw_crop"] is False
    params = {"chat_id": "chat-a"}
    missing = native.client.get(raw_path(), params=params, headers=ALICE)
    assert missing.status_code == 404 and "não foi guardada" in missing.json()["detail"]
    monkeypatch.setattr(native.module, "MAX_SOURCE_BYTES", 10)
    asyncio.run(
        native.module.record_recipes("chat-a", "alice", {1: AREA_RECIPE}, {1: (b"x" * 11, False)})
    )
    assert recipe_of(native)["raw_available"] is False
    assert native.client.get(raw_path(), params=params, headers=ALICE).status_code == 404


def test_the_whole_picture_is_armed_only_for_an_edit_with_an_area(native):  # noqa: F811
    publish(native)
    record(native, recipe=AREA_RECIPE)
    armed = regenerate(native, mode="whole", prompt=None)
    assert armed.status_code == 200
    assert armed.json()["text"] == "Regerar a imagem 1 na imagem inteira"
    found = consume(native, armed.json()["text"])
    assert found["mode"] == "whole" and found["prompt"] is None
    record(native, recipe=RECIPE)
    assert regenerate(native, mode="whole", prompt=None).status_code == 409
    record(native, recipe={**AREA_RECIPE, "kind": "generate"})
    assert regenerate(native, mode="whole", prompt=None).status_code == 409
