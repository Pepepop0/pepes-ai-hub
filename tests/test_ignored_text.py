"""SPEC-106: text the user marks as ignored never reaches the router or the model."""

from __future__ import annotations

import json

import pytest
import router_cases
from router_cases import load_cases, run_case
from test_open_webui_image_filter import load_module, run, ui_metadata

CASES = [
    (
        (
            "[ Ignorar: para dev, voltamos ao problema de ficar repetindo a mesma imagem ]"
            " Sessão encerrada"
        ),
        "Sessão encerrada",
    ),
    (
        "[Ignorar: para o dev, essa coisa voltou ]  Já estava ligado, mas reiniciei, mete bala",
        "Já estava ligado, mas reiniciei, mete bala",
    ),
    ("['anotação'] deixa o fundo azul", "deixa o fundo azul"),
    ("deixa o fundo ['nota no meio'] azul", "deixa o fundo azul"),
    ("[‘curvas’] deixa o fundo azul", "deixa o fundo azul"),
    ("[ignórar : sem acento e caixa ] deixa azul", "deixa azul"),
    ("['só isto']", ""),
    ("deixa a camiseta [azul]", "deixa a camiseta [azul]"),
    ("[Ignorar: sem fechar deixa azul", "[Ignorar: sem fechar deixa azul"),
    ("[Ignorar: com [aninhado] dentro] deixa azul", "[Ignorar: com [aninhado] dentro] deixa azul"),
]


@pytest.mark.parametrize(("text", "clean"), CASES)
def test_strip_ignored_table(text, clean):
    got, removed = load_module()["strip_ignored"](text)
    assert got == clean
    assert (got != text) == bool(removed)


def test_the_real_ignored_turn_reaches_the_router_clean(monkeypatch):
    case = load_cases("2026-11-10T01:36:04")[0]
    sent: list[str] = []
    real_call = router_cases.Recording.__call__

    def spy(self, request, timeout=None):
        data = getattr(request, "data", None)
        if data:
            sent.append(json.dumps(json.loads(data), ensure_ascii=False))
        return real_call(self, request, timeout)

    monkeypatch.setattr(router_cases.Recording, "__call__", spy)
    body, _metadata, events = run_case(case, monkeypatch)
    router = [payload for payload in sent if '"action"' in payload]
    assert router, "the router was asked"
    for payload in router:
        assert "Ignorar" not in payload
        assert "essa coisa voltou" not in payload
        assert "Já estava ligado, mas reiniciei, mete bala" in payload
    for message in body["messages"]:
        assert "Ignorar" not in json.dumps(message, ensure_ascii=False)
    start = events[0]
    assert start["user_text"] == "Já estava ligado, mas reiniciei, mete bala"
    assert start["ignored_text"] == ["[Ignorar: para o dev, essa coisa voltou ]"]


def test_only_ignored_text_is_noted_without_router_or_model(monkeypatch):
    module = load_module()
    called = []

    async def routed(*_args, **_kwargs):
        called.append("router")
        raise AssertionError("the router must not run")

    metadata = ui_metadata()
    metadata["chat_id"] = "temporary:note"
    body = {
        "model": "capella",
        "messages": [
            {"role": "user", "content": "oi"},
            {"role": "assistant", "content": "olá"},
            {"role": "user", "content": "['só isto']"},
        ],
    }
    monkeypatch.setattr(module["Filter"], "_routed", routed)
    result = run(module["Filter"]().inlet(body, metadata, {"id": "u"}))
    assert not called
    assert module["REPLY_CONTEXT"].format(reply="Anotado.") == result["messages"][0]["content"]
    assert metadata[module["STATE_KEY"]]["reply"] == "Anotado."


def test_ignored_text_with_a_picture_still_goes_on():
    module = load_module()
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "['nota']"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
            ],
        }
    ]
    removed = module["strip_ignored_messages"](messages)
    assert removed == ["['nota']"]
    assert module["last_user_has_image"](messages)
    assert messages[0]["content"] == [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}
    ]
