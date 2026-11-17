# SPEC-104 — Pedido que muda a imagem toda segue direto

| Campo | Valor |
|---|---|
| Onda | 1, raia B |
| Cobre | CHAT-04 |
| Prioridade / esforço | P0 / P (meio dia) |
| Depende de | SPEC-002 (e SPEC-001, que remove `GLOBAL_EDIT_QUESTION`) |
| Desbloqueia | SPEC-112; integra antes da SPEC-109 |
| Possui | `prepare_local_preservation` (`pepito_image_chat.py:4428-4485`) |
| Pod | Validar |
| Status | aberto |

## PRD

**Problema.** Três pedidos de 09/11 que mudavam a imagem inteira foram parados e custaram um turno cada:

| UTC | Pedido | Decisão do roteador | O que aconteceu |
|---|---|---|---|
| 01:58:10 | "Agora faça só a palhaça, com a roupa começando a rasgar…" | `edit_mode: restage`, `scope: local` | Recusa: "Esta operação não permite preservar uma região local…" (`:4446-4449`) |
| 02:00:09 | "Tire o prato da mão dela… centralise ela na imagem" | `local`, `scope: local` | Localizador respondeu `status: global` (confiança 0,98) → mensagem de erro de região (hoje `:4483`) |
| 02:02:01 | "Deixa ela nas proporções massivas de antes" | `local`, `scope: local` | Igual ao anterior |

**Objetivo.** Quando o pedido não cabe numa região, a edição roda na imagem inteira, no mesmo turno.

**Não-objetivos.** Escolher uma parte de redesenho melhor (IMG-03, Onda 2). Mudar a máscara pintada pelo usuário.

**Critérios de aceite.**

1. Dada uma parte que não aceita máscara (`restage`, `pose`), quando a preparação de região roda, então ela não recusa: retorna sem máscara e registra `region_skipped` com `reason=part_not_maskable`.
2. Dado o localizador com `status: global`, quando a preparação roda, então retorna sem máscara e registra `region_skipped` com `reason=vision_global` e a confiança.
3. Dado o localizador com `status: ambiguous`, ou confiança abaixo de 0,9, ou coordenadas inválidas, o comportamento continua o de hoje (pede para marcar a área), porque não é "imagem toda".
4. Dada uma área **pintada** pelo usuário e um pedido de pose ou cena, continua a recusa de hoje (`:7135-7149`).
5. Os três casos da tabela geram um job de imagem no mesmo turno.

## Spec técnica

- `:4446-4449`: o `raise ImagePlanError(...)` vira `trace_event(metadata, "region_skipped", reason="part_not_maskable", part=job["part"])` + `return`.
- `:4466-4485`: antes de qualquer `raise`, se `getattr(exc, "answer", None)` for dict com `status == "global"`, registrar `region_skipped` (`reason="vision_global"`, `confidence`) e `return`. O caminho de `prior.fallback` (reusar a área anterior) continua tendo prioridade sobre isso.
- Nenhuma mudança em `region_mask` nem no localizador.

## Testes

- Unitários com o localizador falso: `global` → sem máscara; `ambiguous` → `ImagePlanError`; `local` → máscara (não regressão).
- Parte `restage` com `scope: local` → sem recusa.
- Os três casos reais via `tests/router_cases.py` (com `grounding_answer` gravado).
- Não regressão: a recusa com área pintada (o teste existente dela).

## Tarefas

1. As duas mudanças em `prepare_local_preservation`.
2. Testes unitários e dos casos reais.

## Riscos

Um pedido local julgado global pelo localizador passa a mudar a imagem inteira. É a troca pedida. A lista "manter" do plano continua no prompt.

## Notas da implementação

- O `STACK` de `tests/router_cases.py` não tem `restage`; o caso 01:58:10 (roteado como `restage` na sessão) o ganha por `monkeypatch` no teste, sem mudar o carregador compartilhado.
- O trace `region_refused` continua sendo gravado antes de `region_skipped` quando o localizador diz `global`.
- A recusa com área pintada (`A área anexada…`) segue coberta por `test_image_gpu_and_native_flow.py`; esse teste já falhava antes desta spec.
