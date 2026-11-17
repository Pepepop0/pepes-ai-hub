# SPEC-302 — Área pintada com texto ignorado

| Campo | Valor |
|---|---|
| Onda | 3 |
| Cobre | CHAT-08 (P2 do pod) |
| Prioridade / esforço | P0 / P |
| Depende de | — |
| Possui | `consume_draft` / `_text_hash` em `assets/open-webui/pepito_native.py`; a chamada de `consume_draft` em `pepito_image_chat.py` (~2873); o armar do draft em `pepito_image_ui.js` (~1401) só se for preciso |
| Pod | Validar |

## PRD
**Problema.** Três recusas "Há uma área pintada marcada para outro texto de pedido" (12/11 23:23, 23:24, 23:24). O JS arma a área com o hash do texto inteiro (com `[‘...‘]`); o filtro tira o trecho ignorado (SPEC-106) antes de comparar → nunca bate. A terceira mensagem, sem trecho ignorado, também foi recusada: a área armada para outro texto continuou bloqueando.

**Critérios de aceite.**
1. O hash da área e o da mensagem são calculados sobre o texto **sem** os trechos ignorados, dos dois lados (uma função só de normalização, usada pelo native e pelo filtro).
2. Investigar e corrigir a terceira recusa: uma área nova marcada substitui a anterior; uma área presa a texto que não bate é avisada **uma vez** e descartada, nunca bloqueia duas mensagens seguidas.
3. Testes: os três turnos reais (com e sem `[‘...‘]`) usam a área; área marcada para "A" e mensagem "B" sem área nova → um aviso e, na mensagem seguinte, nenhum bloqueio.

## Notas da implementação
- **Causa da 1ª e 2ª recusa:** o JS arma a área com o texto inteiro (`[‘...‘]` incluído, ver `webui.db`: `"[‘Que?‘] Aqui, remarquei"`); o filtro entrega a `consume_draft` o texto já sem o trecho (SPEC-106). O `_text_hash` do native agora tira os trechos ignorados (mesmo padrão de `_IGNORED` do filtro) antes de juntar as palavras; é a única normalização, aplicada ao armar e ao consumir. O filtro não pode ser importado pelo native (o filtro mora no banco do Open WebUI), por isso o padrão é repetido no native com comentário apontando o par; o JS não mudou.
- **Causa da 3ª recusa:** um draft com texto diferente era mantido ("fica armado para a sua mensagem"). A 3ª mensagem (`"Faltou tirar essas peças"`, sem área nova) bateu no draft que restava da 2ª. Agora o desencontro de texto avisa uma vez e apaga o draft; a próxima mensagem segue sem bloqueio. Uma área nova já substituía a anterior (`_put_state` grava por cima), agora coberta por teste.
- O aviso "text" (`PAINTED_ISSUES` no filtro) mudou: não pede mais para reenviar com o texto antigo, porque a área foi descartada.
- Testes em `tests/test_open_webui_native.py` (três turnos reais, sequência de 12/11, A→B→C, substituição). Precisam de `fastapi`/`sqlalchemy`, ausentes na `.venv` e no swap-venv: ficam *skipped* localmente; a paridade de hash entre o `strip_ignored` do filtro e o `_text_hash` do native nos textos reais foi conferida por script avulso. **Pod:** rodar esses testes num ambiente com Open WebUI e validar: marcar área + mensagem com `[‘nota‘]` edita na área; mensagem sem área após um aviso não é recusada.
