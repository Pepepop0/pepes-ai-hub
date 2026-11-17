"""SPEC-105: a no-picture reply that promises a picture, or hands over an image prompt to copy,
ends with a note that fits the Image button's state."""

import pytest

from tests.test_image_gpu_and_native_flow import load_module, run

PROMPT = (
    "A photo of a red apple on a rustic wooden table, soft morning light from the left, "
    "shallow depth of field, dewdrops on the skin, muted green background, 85mm lens"
)


def stream_reply(module, text: str, image_on: bool) -> str:
    filter_ = module["Filter"]()
    state = {"text": "", "no_image": True}
    if image_on:
        state["image_on"] = True
    metadata = {module["STATE_KEY"]: state}
    shown = []
    for piece in (text[: len(text) // 2], text[len(text) // 2 :]):
        event = {"choices": [{"delta": {"content": piece}, "finish_reason": None}]}
        shown.append(run(filter_.stream(event, metadata))["choices"][0]["delta"]["content"])
    event = {"choices": [{"delta": {}, "finish_reason": "stop"}]}
    shown.append(run(filter_.stream(event, metadata))["choices"][0]["delta"]["content"])
    return "".join(shown)


PROMISES = [
    "Ótima ideia! Vou gerar essa cena agora mesmo, com luz suave.",
    "Vou criar a imagem da maçã. Me avise quando estiver pronto!",
    "Let me know when you want me to continue with the next scene.",
    "Here is the prompt you can use for the apple scene.",
    "Aqui está o prompt para a cena da maçã, em inglês.",
    f"Use isto:\n```\n{PROMPT}\n```\nFica ótimo.",
    f"Sugestão:\n> {PROMPT}\n\nCopie e cole.",
    f"Prompt:\n```text\nPrompt: {PROMPT}\n```",
]

NORMAL = [
    "A imagem 3 mostra uma maçã sobre a mesa; a luz vem da esquerda.",
    "Essa foto tem um bom enquadramento, mas o fundo está escuro.",
    "A fotografia de rua nasceu no século XX e o prompt de comando é outra coisa.",
    "```\nA photo of an apple.\n```",
]


@pytest.mark.parametrize("text", PROMISES)
def test_a_promise_gets_the_state_matching_notice(text):
    module = load_module()
    on = stream_reply(module, text, image_on=True)
    off = stream_reply(module, text, image_on=False)
    assert on.startswith(text[:20]) and on.endswith(module["CLAIM_NOTICE_ON"])
    assert "Image" not in module["CLAIM_NOTICE_ON"]
    assert off.endswith(module["CLAIM_NOTICE"]) and "**Image**" in off


@pytest.mark.parametrize("text", NORMAL)
def test_a_normal_reply_gets_no_notice(text):
    module = load_module()
    assert stream_reply(module, text, image_on=True) == text
    assert stream_reply(module, text, image_on=False) == text


@pytest.mark.parametrize("text", PROMISES)
@pytest.mark.parametrize("image_on", [True, False])
def test_the_outlet_adds_the_notice_once(text, image_on):
    module = load_module()
    state = {"text": "", "no_image": True, **({"image_on": True} if image_on else {})}
    notice = module["CLAIM_NOTICE_ON" if image_on else "CLAIM_NOTICE"]
    for content in (text, text + notice):
        body = {"messages": [{"role": "assistant", "content": content}]}
        run(module["Filter"]().outlet(body, {module["STATE_KEY"]: state}))
        assert body["messages"][0]["content"] == text + notice


@pytest.mark.parametrize("text", NORMAL)
def test_the_outlet_leaves_a_normal_reply_alone(text):
    module = load_module()
    body = {"messages": [{"role": "assistant", "content": text}]}
    state = {"text": "", "no_image": True, "image_on": True}
    run(module["Filter"]().outlet(body, {module["STATE_KEY"]: state}))
    assert body["messages"][0]["content"] == text
