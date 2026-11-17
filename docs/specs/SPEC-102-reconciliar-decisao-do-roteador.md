# SPEC-102 — Reconciliar a decisão do roteador

| Campo | Valor |
|---|---|
| Onda | 1, raia A |
| Cobre | CHAT-02 |
| Prioridade / esforço | P0 / M (1 a 2 dias) |
| Depende de | SPEC-002 |
| Desbloqueia | SPEC-103; integra antes da SPEC-109 |
| Possui | Nova função `reconcile_decision` (junto de `decide`, `pepito_image_chat.py:1487`); o ramo `action == ACTION_CHAT` do `_routed` (`:7022-7031`) |
| Toca | O ponto do `_routed` onde a decisão do roteador é lida, antes de `decide` (uma chamada) |
| Pod | Validar |
| Status | aberto |

## PRD

**Problema.** Nos três turnos reais em que você recebeu resposta errada, o roteador tinha entendido o pedido, e o filtro descartou o que ele entendeu:

| Caso | Resposta do roteador | O que o filtro fez |
|---|---|---|
| 09/11 01:35:14, "Bom dia! troque esta personagem por esta…" | `action: chat` com `plan.clarification` ("…da imagem 1 pela da imagem 2, ou o contrário?") | Ignorou a pergunta; o modelo de chat respondeu sozinho |
| 09/11 01:58:25, "a imagem toda" | `action: chat` com plano completo: `scope: global`, `base: 7`, 4 mudanças | Ignorou o plano |
| 08/11 00:38:53, "Esta personagem" | `action: chat` com 1 mudança no plano | Ignorou o plano |

**Objetivo.** Quando a ação e o plano do roteador discordam, o filtro decide de forma previsível e registrada, sem descartar o entendimento do roteador.

**Não-objetivos.** Guardar a pergunta para o turno seguinte (SPEC-103); mudar o prompt do roteador.

**Critérios de aceite.**

1. Dado `action: chat` com `plan.clarification` não vazio, quando o filtro processa, então a resposta do turno é exatamente a pergunta (via `fixed_reply`, `event="reply"`) e o modelo de chat não é chamado.
2. Dado `action: chat` com `plan.changes` não vazio e (`plan.base > 0` ou `references` não vazio) e há imagem no chat, quando o filtro processa, então a ação vira `edit_image` e segue o fluxo normal de edição.
3. Idem sem imagem no chat: a ação vira `new_image`.
4. Dado `action: chat` sem pergunta e sem mudanças, nada muda.
5. Toda reconciliação gera o evento de trace `router_reconciled` com `reason` (`clarification` ou `promoted`) e a ação original.
6. Uma ação promovida passa pela salvaguarda de idade como qualquer pedido de imagem.

## Spec técnica

```python
def reconcile_decision(decision: dict | None, has_image: bool) -> tuple[dict | None, str | None]:
    """(decisão ajustada, motivo) — motivo None quando nada muda."""
```

- Pura (sem E/S), testável isoladamente.
- Ordem: pergunta primeiro (critério 1), depois promoção (critérios 2 e 3).
- Uma pergunta com `action` diferente de `chat` continua como hoje (o caminho de imagem já trata `clarification` em `validate_intent`).
- Chamada no `_routed` logo depois de obter a decisão do roteador e antes de `decide()`. Com motivo `clarification`, o `_routed` responde com `fixed_reply(body, metadata, clarification, event="reply")`. A SPEC-103 acrescenta o registro da pergunta pendente a essa chamada.
- `trace["source"]` = `"promoted"` quando promovida.
- A salvaguarda (`minor_risk_rules`, `:6989-6996`) é aplicada depois, sobre a ação já promovida: nenhuma mudança necessária, só um teste.

## Testes

- Unitários de `reconcile_decision`: os quatro critérios, mais `decision is None`.
- Com `tests/router_cases.py` (SPEC-002): 01:35:14 → resposta igual à pergunta; 08/11 00:38:53 → edição (ou troca, conforme `edit_mode` da resposta).
- O caso 01:58:25 promove a edição sozinho; o resultado completo (imagem toda, sem máscara) depende da SPEC-104 e entra na SPEC-112.
- Um caso de conversa ("oi, tudo bem?" com imagem no chat): continua `chat`.
- Um caso de salvaguarda: resposta `chat` com plano de mudança e texto que dispara `minor_risk_rules` → recusa.

## Tarefas

1. `reconcile_decision` e os testes unitários.
2. Chamada no `_routed` e o evento `router_reconciled`.
3. Testes com os casos reais.

## Riscos e não regredir

- Promoção indevida numa conversa sobre uma imagem ("o que tem na imagem 2?"): o roteador devolve `changes` vazio nesses casos; a exigência de `base > 0` ou referências reduz o risco. Medir com todos os casos da SPEC-002 na SPEC-112.
- `confirmed_picture_request` (o "sim" depois de uma oferta do modelo) continua funcionando: `test_short_confirmation_recovers_the_pending_picture_even_if_model_says_chat`.
