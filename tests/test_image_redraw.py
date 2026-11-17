"""SPEC-202: "refaça", "mais realista", "harmonizar" redraw the whole picture, from the current
one, on the part the ``redraw_part`` valve names, with a prompt that does not hold everything.
SPEC-306: the default part is the Qwen edit, on both Capella presets, within 2 MP."""

from __future__ import annotations

import io
import json

import pytest
from test_image_edit_dispatch import edit
from test_image_gpu_serialization_and_trace import trace_records
from test_open_webui_image_filter import load_module

PLAN = {
    "scope": "global",
    "changes": ["photorealistic style"],
    "preserve": ["character identity", "pose", "clothing", "background", "framing"],
}
DESCRIBE = "Describe this picture to draw it again."


def redraw(prompt: str, **plan) -> dict:
    return {**edit(prompt), "edit_mode": "redraw", "plan": {**PLAN, **plan}}


def prompts(job: dict) -> list[str]:
    return [
        value
        for node in job.values()
        for value in node["inputs"].values()
        if isinstance(value, str) and len(value) > 20
    ]


def readable(turn, files, monkeypatch) -> None:
    """The filter reads the pictures of the chat from the fake Open WebUI storage."""

    async def picture_bytes(url):
        return next((data for fid, (data, _) in files.store.items() if fid in url), None)

    monkeypatch.setitem(turn.filter.inlet.__globals__, "picture_bytes", picture_bytes)


def png_of_size(width: int, height: int, color: str = "red") -> bytes:
    pil = pytest.importorskip("PIL.Image")
    buffer = io.BytesIO()
    pil.new("RGB", (width, height), color).save(buffer, "PNG")
    return buffer.getvalue()


def png_size(data: bytes) -> tuple[int, int]:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        return image.size


# --- the router ----------------------------------------------------------------------


def test_the_router_offers_redraw_only_where_the_chosen_part_exists():
    module = load_module()
    caps = module["router_capabilities"]
    krea = {"restage": {}, "edit": {"strength": {"key": "ref_boost"}}, "generate": {}}
    for part, needs in (
        ("restage_current", "restage"),
        ("edit_free", "edit"),
        ("t2i_described", "generate"),
    ):
        assert "redraw" in caps(krea, redraw_part=part).modes
        without = {key: value for key, value in krea.items() if key != needs}
        assert "redraw" not in caps(without, redraw_part=part).modes, part
    assert "redraw" not in caps({"edit": {}}, redraw_part="edit_free").modes, "no ref_boost input"
    assert "redraw" not in caps(krea).modes, "the default part is qwen: none installed here"


def test_a_redraw_the_stack_cannot_make_is_a_local_edit(monkeypatch):
    module = load_module()
    answer = json.dumps(
        {
            "action": "edit_image",
            "prompt": "x",
            "edit_mode": "redraw",
            "references": [],
            "plan": None,
        }
    )

    class Reply:
        def __init__(self, data):
            self.data = data

        def read(self):
            return json.dumps({"message": {"content": self.data}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: Reply(answer))
    payload = {"model": "m", "messages": []}
    plain = module["router_capabilities"]({"edit": {}})
    offered = module["router_capabilities"]({"family": "qwen21", "edit": {"workflow": "{}"}})
    assert module["_route_sync"]("http://x", payload, plain)["edit_mode"] == "local"
    assert module["_route_sync"]("http://x", payload, offered)["edit_mode"] == "redraw"


# --- the prompt ----------------------------------------------------------------------


def compiled(prompt: str, preserve: list, text: str, mode: str) -> str:
    intent = {"changes": ["photorealistic style"], "preserve": preserve}
    return load_module()["intent_prompt"](prompt, intent, text, mode)


HABITS = ["character identity", "pose", "clothing", "background", "framing"]
MODEL_PROMPT = (
    "A realistic photograph of the woman, natural skin. "
    "Keep the character's face, hair, outfit, pose, background, and framing unchanged."
)


def test_a_redraw_prompt_keeps_the_identity_and_what_the_user_asked_to_keep_only():
    got = compiled(MODEL_PROMPT, HABITS, "faz mais realista, mantendo esse fundo", "redraw")
    assert "A realistic photograph of the woman, natural skin." in got
    assert "Keep unchanged: the character's identity (face, hair, body); background." in got
    for habit in ("pose", "outfit", "clothing", "framing"):
        assert habit not in got, habit
    assert got.count("Keep") == 1, "the model's own Keep sentence is gone"


def test_a_redraw_without_a_keep_request_holds_only_the_identity():
    got = compiled(MODEL_PROMPT, HABITS, "refaça ela do zero", "redraw")
    assert got.endswith("Keep unchanged: the character's identity (face, hair, body).")


def test_a_redraw_keeps_what_the_user_names_in_portuguese_or_english():
    got = compiled(MODEL_PROMPT, HABITS, "refaça, mantém a roupa e o enquadramento", "redraw")
    assert "clothing; framing." in got and "pose" not in got and "background" not in got
    got = compiled(MODEL_PROMPT, HABITS, "redraw it keeping the same pose", "redraw")
    assert got.endswith("(face, hair, body); pose.")


def test_a_local_edit_keeps_the_whole_list_as_before():
    got = compiled("Make the dress blue. Keep her face unchanged.", HABITS, "veste azul", "local")
    assert got.endswith("Keep unchanged: " + "; ".join(HABITS) + ".")
    assert got.startswith("Make the dress blue. Keep her face unchanged.")


# --- the job each part builds ---------------------------------------------------------


def last_edit_trace(tmp_path) -> dict:
    jobs = [r for r in trace_records(tmp_path) if r["event"] == "image_job"]
    return jobs[-1]


QWEN = "qwen3-8-27b-q5"  # the Capella preset of the Qwen family


def redraw_offered(ollama) -> bool:
    return any(
        "redraw" in r["format"]["properties"].get("edit_mode", {}).get("enum", [])
        for r in ollama.router_requests
        if isinstance(r.get("format"), dict) and "properties" in r["format"]
    )


# --- the six real turns of IMG-03, with SYNTHETIC router decisions ----------------------
# The recorded answers of the real router are the old ones (``edit_mode: local``, see the
# decision table); what the real router says to the new rule is for the pod. Here the router
# is scripted: each decision below is synthetic, made to say ``redraw`` for the user's own
# words of those turns.

IMG_03_WORDS = [
    "Faz uma versão fotorrealista dessa personagem",
    (
        "Faça mais realista, pode realmente fazer uma foto de uma pessoa na vida real, "
        "mantendo esse fundo, pode fazer isso"
    ),
    "Você só repetiu a imagem, refaça ela do zero",
    "Literalmente nada mudou, só refaça ela e ponto",
    (
        "A imagem está boa, mas está toda \u2018cortada'e com erros de continuidade, pode "
        "\u2018harmonizar\u2019 ela? redesenhar ela bem similar mas corrigir os erros?"
    ),
    "Nada mudou",
]
