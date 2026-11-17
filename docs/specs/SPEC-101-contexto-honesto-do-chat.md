# SPEC-101 — Contexto honesto do chat e prompt de sistema

| Campo | Valor |
|---|---|
| Onda | 1, raia C |
| Cobre | CHAT-01, CHAT-06 |
| Prioridade / esforço | P0 / P (meio dia a 1 dia) |
| Depende de | SPEC-001 |
| Desbloqueia | SPEC-105 |
| Possui | `NO_IMAGE_CONTEXT` (`pepito_image_chat.py:153-159`); `models.chat.system_prompt` em `profiles/capella.yaml`; a regra de semeadura em `src/local_ai_stack/open_webui_models.py` (`_carries_system`, `:145`; semeadura `:219`) |
| Toca | Nada mais |
| Pod | Validar |
| Status | aberto |

## PRD

**Problema.** Com o botão Imagem ligado, quando o roteador decide `chat`, o filtro injeta `NO_IMAGE_CONTEXT`, que termina com *"ask the user to explicitly request it with Image on"*. O modelo então pede para ligar um botão que já está ligado. O prompt de sistema da Capella (`capella.yaml:63-64`) manda o mesmo sem saber o estado do botão. Você respondeu "Já estava ligado" duas vezes na sessão de 09/11.

O prompt de sistema é semeado uma vez e nunca mais atualizado (`_carries_system` aceita qualquer texto não vazio), então mudar o YAML não chega às instalações existentes.

**Objetivo.** O modelo de chat nunca diz que o botão está desligado quando ele está ligado, e instalações existentes recebem o prompt novo, salvo se o administrador o editou.

**Não-objetivos.** Mudar o que o roteador decide (SPEC-102/103); mudar `IMAGE_OFF_CONTEXT`, que está correto para o botão desligado.

**Critérios de aceite.**

1. Dado um turno com Image ligado e roteador `chat`, quando o filtro monta o contexto, então o texto diz que o Image está ligado e proíbe pedir para ligá-lo.
2. Dado um registro do modelo no Open WebUI com o prompt semeado pela versão anterior (texto idêntico), quando o Hub sincroniza, então o prompt é trocado pelo novo.
3. Dado um registro com prompt editado pelo administrador, quando o Hub sincroniza, então nada muda.
4. Sincronizar duas vezes seguidas não gera escrita na segunda (`FilterSync.UNCHANGED`).

## Spec técnica

**Novo `NO_IMAGE_CONTEXT`** (inglês, como os demais):

> `<context>The Image switch is ON, but no image was created or edited on this turn. Answer as conversation. You cannot create images yourself: only the system's image tool can, and it did not run on this message. Never claim you made, are making, or will make a picture; never write image links, image markup, code, JSON or an image prompt for one. Never tell the user to switch Image on: it is already on. If the user seems to want a picture, ask in one short sentence what to draw or change.</context>`

**Nova frase do prompt de sistema** (substitui "Se o usuário quiser uma imagem e o botão Imagem estiver desligado, peça para ligá-lo."):

> "O sistema informa em cada mensagem se o botão Imagem está ligado. Só peça para ligá-lo quando o sistema disser que está desligado."

**Migração** em `open_webui_models.py`:

- Constante `SUPERSEDED_SYSTEM_PROMPTS`: SHA-256 dos textos semeados por versões anteriores (no mínimo o texto atual da 5.2.0; conferir no `git log` de `profiles/capella.yaml` se houve outros).
- `_carries_system(record, system_prompt)` passa a ser falso também quando o texto atual do registro, normalizado (espaços colapsados), tem hash em `SUPERSEDED_SYSTEM_PROMPTS` e difere do novo.
- Nesse caso a semeadura (`:219`) escreve o novo texto. Qualquer outro texto é do administrador e fica.
- Sem campo novo no registro: o hash é calculado do próprio texto.

## Testes

- `tests/test_open_webui_models.py` (ou o arquivo que hoje testa a semeadura): os casos 2, 3 e 4, com o cliente HTTP falso já usado ali.
- `tests/test_image_router_reliability.py`: um teste com o caso 01:35:14 da SPEC-002 verificando que o contexto injetado contém "is ON" e não contém "with Image on".
- `tests/test_profiles.py`: o perfil continua válido.

## Tarefas

1. Novo `NO_IMAGE_CONTEXT`; ajustar testes que fixavam o texto antigo (se fixam a redação, trocar por verificação de comportamento: "não pede para ligar").
2. [P] Nova frase no `capella.yaml`.
3. [P] `SUPERSEDED_SYSTEM_PROMPTS` e a regra em `_carries_system`, com os testes.
4. Atualizar `docs/ARCHITECTURE.md` (parágrafo do prompt inicial) — pode ficar para a SPEC-113.

## Não regredir

- Image desligado + pedido de imagem: continua pedindo para ligar (`IMAGE_OFF_CONTEXT` e a resposta fixa do `_inlet`, `:6629-6635`).
- Prompt editado pelo administrador continua intocado (checklist da Capella, "Repeat with … an administrator-edited preset").
