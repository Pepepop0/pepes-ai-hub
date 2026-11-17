# SPEC-103 — Pergunta pendente e respostas curtas

| Campo | Valor |
|---|---|
| Onda | 1, raia A |
| Cobre | CHAT-03 |
| Prioridade / esforço | P0 / M (2 dias) |
| Depende de | SPEC-102 |
| Desbloqueia | SPEC-112 |
| Possui | `fixed_reply` (`pepito_image_chat.py:1711`), `PENDING_KEY` (`:1732`, uso em `:6848`), `confirmed_picture_request` (`:2206`), o `PLANNING CONTEXT` de `router_messages` (`:1293`), a regra correspondente no texto do roteador (`ROUTER_PLAN_RULES`, `:1188`) |
| Toca | O registro por chat (`_picture_record`), com uma chave nova |
| Pod | Validar |
| Status | aberto |

## PRD

**Problema.** Quando o sistema faz uma pergunta ("Qual personagem devo trocar?", "…ou o contrário?", "Qual imagem devo usar?"), a resposta curta do usuário chega ao roteador sem a pergunta e sem o pedido original. O roteador classifica como `chat`. Hoje só existe recuperação para "sim" depois de uma oferta **do modelo** (`confirmed_picture_request`) e para "tente de novo" depois de recusa (`PENDING_KEY`).

**Objetivo.** Toda pergunta do sistema cria um estado pendente. A próxima mensagem é lida primeiro como resposta a ela e, sendo resposta, executa o pedido original com a informação dada.

**Não-objetivos.** O consentimento de referências ("Confirmo"), que já tem fluxo próprio; perguntas feitas pelo modelo de chat em texto livre.

**Histórias.**

- Pedi uma troca, o sistema perguntou qual pessoa trocar, respondi "a da esquerda": a troca acontece.
- O sistema perguntou algo, mas eu mudei de assunto ("oi, tudo bem?"): a conversa segue normal e a pergunta é esquecida.

**Critérios de aceite.**

1. Dada uma resposta fixa que é pergunta (`fixed_reply(..., question=<tipo>)`), quando o turno termina, então o registro do chat guarda `pending_question = {kind, text, request, asked_at}`, onde `request` é a decisão do roteador daquele turno (a mesma que `PENDING_KEY` já guarda).
2. Dado um `pending_question` com menos de 30 min, quando chega a próxima mensagem, então o roteador recebe no `PLANNING CONTEXT` `{"pending_question": text, "original_request": <resumo>}` e a regra: "se a última mensagem responde à pergunta pendente, combine-as num só pedido".
3. Dado que o roteador ainda devolve `chat` sem plano e a mensagem é uma resposta curta (`short_answer(text)`: até 8 palavras, sem saudação isolada, sem pergunta), quando o filtro processa, então executa `request` com a resposta anexada às mudanças do plano (`plan.changes += ["user answer: <texto>"]`) e registra `pending_answered` no trace.
4. Dada qualquer imagem feita, ou uma mensagem que não é resposta, ou 30 min depois, então `pending_question` é apagado.
5. Os casos reais: "Esta personagem" depois de "Qual personagem devo trocar?" (08/11 00:38:23 → 00:38:53) executa a troca; "a imagem toda" depois de uma pergunta de escopo executa a edição global.

## Spec técnica

- `fixed_reply(body, metadata, reply, *, event="refusal", question: str | None = None)`. Com `question`, grava o pendente. Tipos: `clarification` (SPEC-102), `which_person`, `which_source`, `region` ("Não consegui isolar uma região local…").
- Pontos que passam `question`: a resposta de `reconcile_decision` (SPEC-102), as respostas de `ImagePlanError` que terminam em "?" no `_routed` (`:7018-7020` e as da troca), e as recusas de região que ficarem depois da SPEC-104.
- Armazenamento: no registro por chat já usado para `failed_edit` (`_picture_record` / gravação atômica em `:4769`). Unifica `failed_edit` + `PENDING_KEY` com `pending_question` quando possível: o "tente de novo" é um caso de pendente do tipo `refused`.
- `short_answer(text)`: função pura, com os exemplos reais como teste.
- O texto do roteador ganha uma linha de regra e um exemplo (a mensagem "a imagem toda" com pergunta pendente de escopo).

## Testes

- Unitários: `short_answer` (respostas reais e conversas), gravação, expiração e limpeza.
- Com `tests/router_cases.py`: o par 08/11 00:38:23 → 00:38:53 e o par 09/11 02:00:09 → 02:00:31 ("toda a imagem"), com o pendente montado a partir do primeiro turno.
- Não regressão: `test_short_confirmation_recovers_the_pending_picture_even_if_model_says_chat` e os testes de "tente de novo" depois de recusa.

## Tarefas

1. Estrutura `pending_question` e `fixed_reply(question=…)`.
2. [P] `short_answer` com testes.
3. Contexto e regra do roteador.
4. Reserva determinística (critério 3) e limpeza (critério 4).
5. Passar `question=` nos pontos listados.
6. Testes com os casos reais.

## Riscos

- Texto do roteador maior: medir a latência no pod (mediana atual 6,3 s).
- Uma resposta que era conversa ser tomada como resposta: o limite de 8 palavras e a exigência de pendente recente reduzem; a mensagem só vira pedido se o roteador concordar ou se a regra determinística valer.

## Notas da implementação

- **Os números de linha da spec estavam defasados.** Os pontos reais são `fixed_reply`, `PENDING_KEY`, `confirmed_picture_request`, `ROUTER_PLAN_RULES` e o `_routed`, achados por nome.
- **Registro.** `pending_question = {kind, text, request, original, asked_at}` (`original` é a mensagem do usuário que gerou o pedido, usada no resumo do `PLANNING CONTEXT`). `request` é a decisão do turno depois da reconciliação (SPEC-102) e da recuperação do "tente de novo", guardada em `metadata[TURN_REQUEST_KEY]` por `_routed`; vale para a pergunta do roteador (`clarification`, que nunca passa por `PENDING_KEY`, pois a ação é `chat`).
- **Limpeza.** Todo turno roteado lê e apaga o pendente no começo (`take_pending_question`): o turno o responde, e uma nova pergunta grava outro, ou o abandona. Passados 30 min ele é descartado sem uso. Uma imagem feita também o apaga.
- **`failed_edit` não foi unificado.** O "tente de novo" segue em `failed_edit` (nada do fluxo mudou); só os pendentes de pergunta usam `pending_question`.
- **Respostas.** `short_answer` vale até 8 palavras, sem `?`, sem saudação isolada. A reserva só age quando o roteador devolve `chat` (ou falha) sem a reconciliação ter agido; um turno em que o roteador responde sozinho (ação de imagem, pergunta nova, promoção) não é sobrescrito. A decisão guardada ganha `user answer: <texto>` (no máximo 6 mudanças; as 5 primeiras ficam) e, se a resposta nomeia a imagem toda ("toda a imagem", "a imagem toda"), `scope: global`; sem isso o pedido voltaria a recusar a região. Ação `chat` vira `edit_image` (ou `new_image` sem imagem no chat), com a base entrando nas referências.
- **Tipos.** `question_kind` deduz o tipo do texto: `region` ("isolar uma região"), `which_person`, `which_source`, `clarification`. As respostas de `ImagePlanError` que terminam em "?" e as recusas de região passam por `ask_reply`; a pergunta do roteador grava sempre.
- **Casos reais.** Depois da SPEC-104, 02:00:09 e 01:58:10 não perguntam mais, e "Esta personagem" e "a imagem toda" já eram promovidos pela SPEC-102 quando o roteador mandava o plano. Os testes usam o roteador respondendo `chat` sem plano e montam o pendente: a partir do trace do 00:38:14 (`which_person`), do 02:00:09 com localizador "ambíguo" (`region`) e do 01:35:14 (pergunta real do roteador).
- `tests/router_cases.py` ganhou `pending_question` (entra no registro do chat) e `LAST_RECORD` (o registro depois do turno), sem mudar o resto do carregador.
