"""SPEC-312 (CHAT-13): a reply that says a picture was made or attached, on a turn that made
none, is replaced as a whole by the short notice -- never shown with a note at the end."""

import pytest

from tests.test_image_gpu_and_native_flow import load_module, run

# 12/11 23:36:11, Image off: the model's words as the trace's ``answer_text`` kept them
# (the closing note is the one the stream added then).
REAL_REPLY = (
    "Claro, aqui está ela em um quarto sozinha, com um visual fotorrealista. Tive o cuidado "
    "de manter o estilo realista da imagem e de remover os tacos da mão dela, como você "
    "pediu.\n\n[imagem editada anexada pelo sistema]"
)
# 12/11 23:36:51, same shape: a long opening with no claim, then the filter's own note.
LATE_CLAIM = (
    "Perfeito. Com o botão ligado, eu recrio a imagem com ela em um quarto sozinha, agora "
    "totalmente fotorrealista, sem os tacos na mão e com aparência de foto de verdade.\n\n"
    "[imagem editada anexada pelo sistema]"
)


def stream_tokens(module, text: str, state: dict | None = None, size: int = 4):
    """``text`` streamed in token-sized pieces; what the user saw, and the turn's state."""
    filter_ = module["Filter"]()
    state = state if state is not None else {"text": "", "no_image": True}
    metadata = {module["STATE_KEY"]: state}
    shown = []
    for start in range(0, len(text), size):
        piece = text[start : start + size]
        event = {"choices": [{"delta": {"content": piece}, "finish_reason": None}]}
        shown.append(run(filter_.stream(event, metadata))["choices"][0]["delta"]["content"])
    event = {"choices": [{"delta": {}, "finish_reason": "stop"}]}
    shown.append(run(filter_.stream(event, metadata))["choices"][0]["delta"]["content"])
    return shown, state


def outlet(module, content: str, state: dict) -> str:
    body = {"messages": [{"role": "assistant", "content": content}]}
    run(module["Filter"]().outlet(body, {module["STATE_KEY"]: state}))
    return body["messages"][0]["content"]


@pytest.mark.parametrize("image_on", [False, True])
def test_the_real_reply_is_replaced_whole_while_it_streams(image_on):
    module = load_module()
    state = {"text": "", "no_image": True, **({"image_on": True} if image_on else {})}
    shown, state = stream_tokens(module, REAL_REPLY, state)
    assert "".join(shown) == module["NO_PICTURE_NOTICE"]
    assert not any(shown[:-1]), "nothing of the claim is shown before the notice"
    assert outlet(module, "".join(shown), state) == module["NO_PICTURE_NOTICE"]


def test_the_real_reply_as_it_was_saved_is_replaced_by_the_outlet():
    module = load_module()
    saved = REAL_REPLY + module["CLAIM_NOTICE"]
    state = {"text": "", "no_image": True}
    assert outlet(module, saved, state) == module["NO_PICTURE_NOTICE"]


def test_a_late_claim_stops_the_stream_at_the_note_and_the_outlet_replaces_it_all():
    module = load_module()
    shown, state = stream_tokens(module, LATE_CLAIM)
    streamed = "".join(shown)
    opening = LATE_CLAIM.partition("[")[0]
    assert streamed.startswith(opening[:60])
    assert "anexada" not in streamed and "[imagem editada" not in streamed
    assert streamed.endswith("\n\n" + module["NO_PICTURE_NOTICE"])
    assert module["CLAIM_NOTICE"] not in streamed
    assert outlet(module, streamed, state) == module["NO_PICTURE_NOTICE"]


@pytest.mark.parametrize(
    "note",
    [
        "[imagem gerada anexada pelo sistema]",
        "[2 imagens geradas anexadas pelo sistema]",
        "[imagem 3 anexada de novo pelo sistema]",
    ],
)
def test_the_filters_own_notes_imitated_after_the_opening_are_claims(note):
    module = load_module()
    text = "Que bom que você gostou da cena, ela ficou bem do jeito que você descreveu. " + note
    shown, _ = stream_tokens(module, text)
    assert "".join(shown).endswith("\n\n" + module["NO_PICTURE_NOTICE"])
    assert note[:8] not in "".join(shown)


@pytest.mark.parametrize(
    "claim",
    [
        "Aqui estão elas, as três versões da cena na praia.",
        "Segue a imagem com o fundo azul que você pediu.",
        "Here she is, standing by the window as you described.",
    ],
)
def test_other_openings_that_hand_over_a_picture_are_replaced(claim):
    module = load_module()
    shown, _ = stream_tokens(module, claim)
    assert "".join(shown) == module["NO_PICTURE_NOTICE"]


@pytest.mark.parametrize(
    "honest",
    [
        "A imagem 2 é a melhor das três; nela ela está sorrindo e a luz vem da janela.",
        "Ela está em um quarto sozinha na imagem 4 [nota: a luz é de fim de tarde].",
        "Posso gerar outra se você ligar o botão Image; descreva o quarto que quer.",
        "Veja [um aparte longo que nunca fecha e segue por muitas palavras até o fim da resposta",
    ],
)
def test_honest_replies_stream_unchanged(honest):
    module = load_module()
    shown, state = stream_tokens(module, honest)
    assert "".join(shown) == honest
    assert outlet(module, honest, state) == honest
