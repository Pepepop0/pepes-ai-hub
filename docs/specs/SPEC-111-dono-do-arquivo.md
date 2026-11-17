# SPEC-111 — Leitura de arquivo só do dono

| Campo | Valor |
|---|---|
| Onda | 1, raia H |
| Cobre | SEC-01 (N4) |
| Prioridade / esforço | P2 / P (meio dia) |
| Depende de | SPEC-001 |
| Desbloqueia | SPEC-113 |
| Possui | `picture_bytes`, `replace_picture` (`pepito_image_chat.py:4649-4700`); uma variável de contexto nova com o usuário do turno |
| Toca | Uma linha no começo do `_inlet` (`:6593`, onde `pepito_user_id` já é definido), combinada com a SPEC-106 |
| Pod | Não |
| Status | aberto |

## PRD

**Problema.** `picture_bytes` e `replace_picture` buscam o arquivo do Open WebUI só pelo id (`Files.get_file_by_id`, `:4667`, `:4686`). O caminho nativo do Hub já exige o dono (`pepito_native.py:181-182`). Com o link de convidado há mais de um usuário: quem souber o id de um upload alheio poderia usá-lo como fonte de edição, e `replace_picture` grava por id. Os ids são UUID aleatórios, então a exploração é improvável; a correção é barata.

**Objetivo.** O filtro só lê e grava arquivos do usuário do turno.

**Critérios de aceite.**

1. Dado um turno do usuário A e o id de um arquivo do usuário B, `picture_bytes` devolve `None` e `replace_picture` devolve `False`.
2. Arquivos do próprio usuário (uploads e imagens geradas para ele) funcionam como hoje.
3. Imagens em `data:` continuam funcionando.
4. Fora de um turno (sem usuário definido), comportamento de hoje, com um aviso no log: nenhuma chamada atual deve cair aqui.

## Spec técnica

- `_TURN_USER: contextvars.ContextVar[str | None]`, definida no `_inlet` com `metadata["pepito_user_id"]`. Variável de contexto evita mudar a assinatura das cerca de 15 chamadas de `picture_bytes`.
- Com usuário: `Files.get_file_by_id_and_user_id(id, user)` e conferência de `file.user_id == user`, como em `pepito_native.py`.
- **Conferir antes:** as imagens geradas pelo Open WebUI no caminho de edição pertencem ao usuário que pediu (o `image_edits` recebe o usuário). Se alguma for gravada sem dono, o critério 2 falha e é preciso tratar esse caso.
- Os `asyncio.to_thread` copiam o contexto; as tarefas criadas com `asyncio.create_task` também. Conferir se algum caminho roda fora do contexto do turno (por exemplo o gancho `request`).

## Testes

Fakes de `Files` com dois usuários: os critérios 1 a 4.

## Tarefas

1. Variável de contexto e definição no `_inlet`.
2. Consulta com dono em `picture_bytes` e `replace_picture`.
3. Testes; conferir que nenhum teste existente quebrou por usuário ausente.

## Não regredir

Edição, troca e "nada mudou" com uploads do próprio usuário.

## Notas da implementação

- `_TURN_USER` (ContextVar) é definida no `_inlet`, logo após `metadata["pepito_user_id"]`. `picture_bytes` e `replace_picture` usam `_stored_file`: com usuário, `Files.get_file_by_id_and_user_id` + `file.user_id == user`; sem usuário, busca por id com `log.warning`.
- Verificado no Open WebUI 0.12.0 (`routers/images.py`): todo `upload_image` (geração e edição) chama `upload_file_handler(..., user=user)`, e o filtro chama `image_edits(..., user=user)`; as imagens geradas ficam com o `user_id` de quem pediu. O critério 2 vale.
- Chamadores de `picture_bytes`/`replace_picture` (`measure_edits`, `keep_outside_mask`, leitura de dimensões, planos de edição/troca) rodam dentro do `_inlet` (mesma tarefa ou `to_thread`, que copia o contexto). Nenhum está no `stream`/`outlet`, que não os chamam; se algum for criado lá, cairá no caso sem usuário (aviso no log).
- Correção: o gancho `request` (que chama `_generate`, e dali `measure_edits`/`keep_outside_mask`) é invocado pelo Open WebUI à parte do `inlet`, sem herdar o `ContextVar`. Por isso `request` também define `_TURN_USER` no começo, a partir de `__user__` (ou `metadata["pepito_user_id"]`). A afirmação anterior de que todos os chamadores rodam dentro do `_inlet` estava errada; `stream` e `outlet` continuam sem chamá-los.
