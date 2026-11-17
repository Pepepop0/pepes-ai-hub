# SPEC-303 — Loop de recusas do plano

| Campo | Valor |
|---|---|
| Onda | 3 |
| Cobre | CHAT-09, CHAT-10, CHAT-11 (P3, P4, P5 do pod) |
| Prioridade / esforço | P0 / P |
| Depende de | — |
| Possui | `validate_intent` (~3089), `previous_edit` / receita de retry (purposes, ~3595 e ~8697), o registro de pergunta pendente para recusas internas (`fixed_reply(question=...)`) |
| Pod | Validar |

## PRD
**Problema.** No chat do retrato, "O plano não explicou o que usar de cada referência" 4 vezes (12/11 23:42–23:45); responder não destravava; o modelo de chat ficou confuso ("tive um probleminha técnico"). Causas: (a) retry/"nada mudou" remonta a receita juntando os propósitos dos slots (239–246 caracteres no restage) e `validate_intent` recusa acima de 160; (b) a pergunta pendente (SPEC-103) repete a mesma receita; (c) no redraw, `base` fora das referências → "O plano não incluiu a imagem que seria editada" (23:41).

**Critérios de aceite.**
1. O limite de tamanho do propósito vale só para o texto escrito pelo roteador; a receita montada pelo filtro não é recusada por tamanho (ou é encurtada, nunca recusada).
2. No modo `redraw` (e `restage`), base ausente das referências com uma referência única → a referência dada vira a base.
3. Uma recusa que o usuário não pode resolver respondendo (erro interno do plano) não vira pergunta pendente.
4. Duas recusas iguais seguidas no mesmo chat → a terceira tentativa roda com padrões seguros (referência única como base, propósito genérico) ou responde dizendo claramente o que falhou, sem repetir a pergunta.
5. Os casos reais 2026-11-12T23:41:03, 23:42:17, 23:42:41, 23:43:02, 23:45:25 (acrescentar à fixture da SPEC-002 a partir de `Downloads\pod-onda-2\data\open-webui\pepito-trace\turns.jsonl`) geram imagem ou uma resposta clara, nunca a mesma recusa duas vezes.

## Notas da implementação
- Critério 1: `validate_intent(..., trusted=...)` só aplica `PURPOSE_LIMIT` (160) aos propósitos que o roteador escreveu no turno; os montados pelo filtro (receita gravada, pedido pendente, recusado) passam. `previous_edit` monta o propósito por `recipe_purpose`: sem a frase de identidade que o filtro acrescenta ao restage (`IDENTITY_PURPOSE`, que voltava duplicada) e encurtado a 160 numa palavra, para o roteador não ecoar um texto longo.
- Critério 2: `reference_as_base` (modos `redraw`/`restage`, uma imagem nas referências, base fora delas). O `router_result` do trace continua mostrando a base que o roteador escreveu.
- Critério 3: `PlanFault` (subclasse de `ImagePlanError`) marca recusas que nenhuma resposta resolve: `request_mode` inválido, alterar e preservar a mesma coisa, correção inválida, referências que não são lista, propósito vazio/longo. Não viram pergunta pendente e o texto deixou de terminar em pergunta ("Peça de novo dizendo..."). "Imagem não disponível", "base fora das referências", região etc. continuam perguntas (a resposta resolve).
- Critério 4: a sequência de recusas iguais fica no registro do chat (`plan_refusals`), lida e limpa no começo de cada turno roteado; só `ask_reply` a regrava. Na 3ª igual: `safe_decision` (a única imagem das referências, ou a atual se não houver referências, como base; propósito `SAFE_PURPOSE`); se não houver fonte única ou o plano seguro também falhar, `STUCK_REPLY` diz o motivo sem a pergunta. A recusa por menor (`BLOCKED_REPLY`) não entra nessa contagem.
- Fixture: os cinco turnos entram com o estado real do chat; `tests/router_cases.py` passou a semear `last_edit` (números de imagem → URLs), `pending_question` (`asked_at` = agora quando ausente) e `refusals`. 23:42:41 e 23:43:02 carregam a pergunta pendente que o filtro antigo deixou (propósito de 194 caracteres).
- Pod: confirmar que o retrato de 12/11 sai sem recusas e que a 3ª recusa igual responde com o motivo.
