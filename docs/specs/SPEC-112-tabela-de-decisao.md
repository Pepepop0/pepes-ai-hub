# SPEC-112 — Tabela de decisão completa

| Campo | Valor |
|---|---|
| Onda | 1, raia A (fecha a raia) |
| Cobre | ARQ-01 |
| Prioridade / esforço | P1 / M (1 a 2 dias) |
| Depende de | SPEC-103, 104, 105, 106 integradas |
| Desbloqueia | SPEC-113; e, na Onda 4, a divisão do `_routed` (ARQ-02) com segurança |
| Possui | `tests/test_router_decision_table.py` (novo); a coluna `expected` de `tests/fixtures/router_cases.json` |
| Pod | Não |
| Status | integrado (2 defeitos em xfail) |

## PRD

**Problema.** `_routed` tem 377 instruções e complexidade 110. Três dos defeitos de 09/11 nasceram de combinações que nenhum teste cobria. Antes de dividir a função (Onda 4), o comportamento precisa estar fixado de ponta a ponta com casos reais.

**Objetivo.** Uma tabela, verificada por teste, que diz para cada turno real o que o filtro faz.

**Critérios de aceite.**

1. Todos os casos das quatro sessões (61 turnos, menos os que a SPEC-002 descartou por privacidade) têm `expected`:
   - `action`: `chat`, `reply` (resposta fixa), `new_image`, `edit_image`, `show_image`, `search`, `refused`;
   - para imagem: `part` (`edit`, `restage`, `remove`, `pose`, `swap`), `mask` (`none`, `auto`, `painted`), `source` (número da imagem);
   - para resposta fixa: o tipo (`clarification`, `which_person`, `refused`, `ignored`…).
2. Um teste parametrizado roda cada caso pelo `inlet` (SPEC-002, `run_case`) e compara com `expected`. Diferença = falha com o id do caso.
3. Os casos que motivaram a Onda 1 têm o `expected` corrigido e passam: 01:35:14 (`reply/clarification`), 01:58:25 e 02:00:31 (`edit_image`, `mask: none`), 01:58:10, 02:00:09, 02:02:01 (imagem no mesmo turno), 01:51:18 e 01:36:04 (texto ignorado), 08/11 00:38:53 (troca).
4. Os casos que hoje funcionam bem (troca 00:59:31, 01:15:34, 01:52:45; edição de forma corporal (a body-shape edit) 01:54:42 com máscara automática; restage 02:04:09) mantêm o resultado observado.
5. A tabela legível (id, texto curto, esperado) é gerada a partir do JSON e anexada a esta spec, para revisão humana.

## Spec técnica

- `expected` mora no mesmo JSON dos casos; quem muda o comportamento muda o `expected` no mesmo commit, com o motivo na mensagem.
- Casos de IMG-03 (01:47–01:50, 02:08, 02:09) ficam marcados `"pending": "IMG-03"`: o teste confere o comportamento atual até a Onda 2 mudá-lo.

## Tarefas

1. Preencher `expected` para todos os casos (revisão sua para os casos ambíguos).
2. Teste parametrizado.
3. Gerar e anexar a tabela legível.

## Riscos

Casos com histórico incompleto (as respostas livres do modelo não estão no trace) podem decidir diferente do que decidiram na sessão. Marcar `"history_incomplete": true` e aceitar a diferença quando ela vier só daí.

## Notas da implementação

- **51 casos** (a SPEC-002 deixou 51 dos 61 turnos), todos com `expected`: `action` (`chat`, `reply`, `refused`, `new_image`, `edit_image`), `kind` (`reply`/`refused`: `clarification`, `consent`, `region`, `plan`…), `part`, `mask` (`none`/`auto`), `source` (primeira imagem do trabalho; na troca, a cena) e `ignored_text: true` nos dois turnos com `[Ignorar: …]`. `show_image` e `search` não aparecem em nenhum turno real; o observador os conhece, sem caso.
- **O marcador de IMG-03 mora em `expected.pending`**, não no caso: `pending` já é a receita do "tente de novo" que o `run_case` põe em `failed_edit`. O teste ignora essa chave e confere o comportamento atual (01:47:08, 01:48:47, 01:49:37, 01:50:22, 02:08:06, 02:09:11).
- **Pilha do pod com `restage`**: o teste da tabela acrescenta `restage` ao `STACK` compartilhado (como a SPEC-104 já fazia para 01:58:10); sem isso nove turnos que a sessão roteou como reposicionamento viram `edit`.
- **`prepare_swap_job` do carregador** passou a devolver `part: "swap"` e `images` (as URLs dos slots), como o trabalho real, para a tabela dizer "troca" e a imagem de origem.
- **Defeitos que o caso real revelou (`xfail(strict=True)`):** 01:58:25 e 00:38:53 (o roteador responde `chat` com plano completo e `references: []`; a SPEC-102 promove a edição, mas a promoção também fica sem referências e a validação do plano recusa com "O plano não incluiu a imagem que seria editada"). `expected` é o comportamento desejado: 01:58:25 `edit_image`, `restage`, sem máscara, imagem 7; 00:38:53 `edit_image`, `edit`, máscara automática, imagem 3.
- **00:38:53 reconstruído**: `answers` (turno 00:38:14, `which_person`, "Qual personagem devo trocar?") remonta a pergunta pendente; o localizador não chegou a rodar na sessão, então o caso traz um `grounding_answer` `local` suposto (`"grounding_assumed": true`).
- **01:54:42 (edição de forma corporal ampla)**: o critério 4 diz máscara automática, mas o trace da sessão tem `mask: false`, `automatic_region: false` (escopo geometria); a tabela mantém o observado (`edit`, sem máscara). O pedido de forma corporal com máscara automática é 01:40:21 (e 01:05:04).
- **Casos ambíguos escolhidos**: 02:00:09 e 02:02:01 ficam `edit` sem máscara (localizador "global"), não `restage`; 00:37:53 e 00:38:14 são recusa `region` (o localizador não respondeu na gravação); 01:36:04 é troca com texto ignorado.
- Nenhum caso precisou de `history_incomplete`.
- `scripts/router_cases.py` regenera o JSON inteiro e apagaria `expected`/`answers`; ao regerar, preserve essas chaves.

## Tabela de decisão

Gerada de `tests/fixtures/router_cases.json` (`python tests/test_router_decision_table.py`); um teste a mantém igual ao JSON.

<!-- decision-table -->

| id | texto | esperado |
|---|---|---|
| 2026-11-09T00:10:23 | Oi querida~ | chat |
| 2026-11-09T00:11:23 | hmmm Ah sla, me mostra um retrato bem bonito? | new_image, part: new |
| 2026-11-09T00:12:24 | Bom dia! vamos fazer alguns testes de imagem? | chat |
| 2026-11-09T00:13:11 | troque esta personagem por esta | edit_image, part: swap, mask: none, source: 2 |
| 2026-11-09T00:14:21 | Claro, quero que troque esta personagem por est… | edit_image, part: swap, mask: none, source: 4 |
| 2026-11-09T00:18:45 | troque esta personagem por esta ( A loira pela … | edit_image, part: swap, mask: none, source: 2 |
| 2026-11-09T00:21:36 | Isso são refs ou a imagem pronta? | chat |
| 2026-11-09T00:27:42 | troque esta personagem por esta ( A loira pela … | edit_image, part: swap, mask: none, source: 2 |
| 2026-11-09T00:37:41 | Aumenta os braços dela, bastante, quero eles be… | refused, kind: region |
| 2026-11-09T00:37:53 | Confirmo | refused, kind: region |
| 2026-11-09T00:38:14 | Tente denovo | refused, kind: region |
| 2026-11-09T00:38:53 | Esta personagem | edit_image, part: edit, mask: auto, source: 3 |
| 2026-11-09T01:27:46 | troque esta personagem por esta (a loira pela m… | edit_image, part: swap, mask: none, source: 2 |
| 2026-11-10T00:59:31 | troque esta personagem por esta (a loira pela m… | edit_image, part: swap, mask: none, source: 2 |
| 2026-11-10T01:04:46 | aumenta os braços dela, bastante | refused, kind: consent |
| 2026-11-10T01:05:04 | Confirmo | edit_image, part: edit, mask: auto, source: 4 |
| 2026-11-10T01:09:45 | Faça ela de lado agora, sorrindo e deixe o bato… | edit_image, part: edit, mask: none, source: 5 |
| 2026-11-10T01:10:26 | ta ae | chat |
| 2026-11-10T01:10:48 | Você não enviou ou gerou as imagens, refaça | edit_image, part: edit, mask: none, source: 5 |
| 2026-11-10T01:11:34 | Toda a imagem | edit_image, part: restage, mask: none, source: 5 |
| 2026-11-10T01:13:16 | MARAVILHOSA! | chat |
| 2026-11-10T01:14:27 | troque esta personagem por esta | edit_image, part: swap, mask: none, source: 2 |
| 2026-11-10T01:15:34 | coloca a personagem no lugar do homem de terno … | edit_image, part: swap, mask: none, source: 2 |
| 2026-11-10T01:35:14 | Bom dia! troque esta personagem por esta (a loi… | reply, kind: clarification |
| 2026-11-10T01:36:04 | Já estava ligado, mas reiniciei, mete bala | edit_image, part: swap, mask: none, source: 2, texto ignorado |
| 2026-11-10T01:40:21 | Aumente os braços dela, bastante | edit_image, part: edit, mask: auto, source: 4 |
| 2026-11-10T01:41:44 | faça ela de lado agora, sorrindo e deixe o bato… | edit_image, part: restage, mask: none, source: 5 |
| 2026-11-10T01:42:50 | Faz ela olhando para a camera com um olhar pene… | edit_image, part: restage, mask: none, source: 6 |
| 2026-11-10T01:44:11 | Faz ela de lado, mas com a cabeça virada/jogada… | edit_image, part: edit, mask: none, source: 7 |
| 2026-11-10T01:47:08 | Faz uma versão fotorrealista dessa personagem | edit_image, part: edit, mask: none, source: 1, pendente IMG-03 |
| 2026-11-10T01:48:47 | Faça mais realista, pode realmente fazer uma fo… | edit_image, part: edit, mask: none, source: 2, pendente IMG-03 |
| 2026-11-10T01:49:37 | Você só repetiu a imagem, refaça ela do zero | edit_image, part: edit, mask: none, source: 3, pendente IMG-03 |
| 2026-11-10T01:50:22 | Literalmente nada mudou, só refaça ela e ponto | edit_image, part: edit, mask: none, source: 4, pendente IMG-03 |
| 2026-11-10T01:51:18 | Sessão encerrada | chat, texto ignorado |
| 2026-11-10T01:52:45 | Já estava ligado, mas pode continuar | edit_image, part: swap, mask: none, source: 2 |
| 2026-11-10T01:54:42 | Aumente a perna e os braços dela BASTANTE, tipo… | edit_image, part: edit, mask: none, source: 4 |
| 2026-11-10T01:55:54 | Aumente ainda mais e faça a camiseta cobrir os … | edit_image, part: edit, mask: none, source: 5 |
| 2026-11-10T01:56:47 | Ainda maiores, com o short começando a rasgar e… | edit_image, part: edit, mask: auto, source: 6 |
| 2026-11-10T01:58:10 | Agora faça só a palhaça, com a roupa começando … | edit_image, part: restage, mask: none, source: 7 |
| 2026-11-10T01:58:25 | a imagem toda | edit_image, part: edit, mask: none, source: 7 |
| 2026-11-10T01:58:40 | gere | edit_image, part: restage, mask: none, source: 7 |
| 2026-11-10T02:00:09 | Tire o prato da mão dela e fala com que ela ras… | edit_image, part: edit, mask: none, source: 8 |
| 2026-11-10T02:00:31 | toda a imagem | edit_image, part: restage, mask: none, source: 8 |
| 2026-11-10T02:01:21 | Faz ela deitada na cama, com pijama e chapéu, f… | edit_image, part: restage, mask: none, source: 9 |
| 2026-11-10T02:02:01 | Deixa ela nas proporções massivas de antes | edit_image, part: edit, mask: none, source: 10 |
| 2026-11-10T02:02:58 | Toda a imagem, quando eu estava falando de prop… | edit_image, part: edit, mask: none, source: 10 |
| 2026-11-10T02:04:09 | Agora faz ela ajoelhada com uma caixa enorme ao… | edit_image, part: restage, mask: none, source: 11 |
| 2026-11-10T02:05:27 | Quase, tem alguém segurando a caixa, faça com q… | edit_image, part: edit, mask: auto, source: 12 |
| 2026-11-10T02:06:35 | Faltou o resto das pernas dele | refused, kind: region |
| 2026-11-10T02:08:06 | A imagem está boa, mas está toda ‘cortada'e com… | edit_image, part: edit, mask: none, source: 14, pendente IMG-03 |
| 2026-11-10T02:09:11 | Nada mudou | edit_image, part: edit, mask: none, source: 14, pendente IMG-03 |
| 2026-11-12T23:41:03 | Não, quero uma versão IRL e de pessoa real dela… | edit_image, part: restage, mask: none, source: 1 |
| 2026-11-12T23:42:17 | Não, assim não, refaça a imagem do zero, quero … | edit_image, part: restage, mask: none, source: 1 |
| 2026-11-12T23:42:41 | A mesga | edit_image, part: restage, mask: none, source: 1 |
| 2026-11-12T23:43:02 | Essa | edit_image, part: restage, mask: none, source: 1 |
| 2026-11-12T23:45:25 | Não mudou nada, refaça | edit_image, part: restage, mask: none, source: 4 |
| 2026-11-12T23:15:18 | Confirmo | refused, kind: region |
