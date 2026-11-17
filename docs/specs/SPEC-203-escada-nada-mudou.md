# SPEC-203 — Escada de "nada mudou" e nova tentativa automática

| Campo | Valor |
|---|---|
| Onda | 2, fases A e C |
| Cobre | IMG-04; N2 (checklist) |
| Prioridade / esforço | P1 / M (1 a 2 dias) |
| Depende de | SPEC-202 (a parte de redesenho existe) |
| Desbloqueia | SPEC-210 |
| Possui | `retry_decision`, `edit_strengths` (escada e piso), a decisão sobre `measure_edits` / evento `image_change` no despacho da edição |
| Pod | Sim (medir o limiar) |
| Status | fase A implementada (Valves desligadas por padrão); B e C no pod |

## PRD

**Problema.** Depois de "nada mudou", o filtro repete a mesma parte com `ref_boost` um ponto menor. Quando já está no piso (1,0), repete o mesmo. A nova tentativa automática que existia ("Quase nada mudou; refazendo com mais liberdade") não existe mais: a medição só vai para o trace.

**Objetivo.** "Nada mudou" nunca devolve a mesma imagem duas vezes, e um pedido forte que sai quase igual é refeito antes de chegar ao usuário.

**Critérios de aceite.**

1. Escada por tentativa, a partir da parte usada: (a) mesma parte com `ref_boost` − 1 até o piso; (b) no piso, a parte de redesenho (SPEC-202); (c) depois, geração nova descrita (`t2i_described`), com a frase "Fiz uma imagem nova a partir desta, porque a edição não mudava".
2. Cada degrau aparece no trace (`retry_step`: `boost`, `redraw`, `new`).
3. Nova tentativa automática: se `change` era `moderate` ou `strong` e `image_change` < limiar, o filtro refaz **uma vez** no degrau seguinte antes de entregar, com o status "Quase nada mudou; refazendo com mais liberdade". As duas imagens são entregues (a fraca primeiro), como antes.
4. Limiar inicial 6 (casos ruins: 2,7 a 3,6; o comentário de 21/10 registra 5,7 a 8,8 para mudanças grandes que não apareceram). O experimento ajusta.
5. "Nada mudou" duas vezes seguidas no pod: partes diferentes no trace.
6. Edição local que funcionou na primeira não faz nova tentativa (medida acima do limiar).

## Experimento (fase B)

Junto com o da SPEC-202: registrar `image_change` de 20 edições locais que você considera "mudou certo" e de todas que você considera "não mudou". Limiar = o valor que separa melhor os dois grupos (anotar a matriz de acertos).

## Testes

- Unitários da escada (cada degrau a partir de cada estado).
- Nova tentativa com `measured_change` falso abaixo e acima do limiar.
- Fixtures `02:09:11` e `01:50:22` passam a esperar o degrau certo.

## Tarefas

1. Fase A: escada, evento, nova tentativa com limiar em constante; testes.
2. Fase C: limiar medido; checklist (DOC-02) atualizado com o comportamento novo.

## Riscos

Uma tentativa a mais custa um job (12 a 15 s). Só acontece abaixo do limiar.

## Notas da implementação (fase A)

- **Duas Valves de usuário, ambas desligadas** (= comportamento de antes, a tabela de decisão não muda): `retry_ladder` (escada) e `auto_retry` (nova tentativa automática). O pod decide ligá-las; o limiar é a constante `NO_CHANGE_THRESHOLD = 6.0`.
- **Escada** (`ladder_step`, `ladder_redo`, `retry_decision(..., ladder)`; `retry_ladder()` monta o que a stack oferece). Parte da receita anterior: edição local de uma imagem, `ref_boost` numérico → `boost` (a mesma parte; `edit_strengths` tira 1 ponto até `SHAPE_RETRY_FLOOR`) enquanto `ref_boost` > 1,0; no piso → `redraw` (a parte da Valve `redraw_part`, sobre a imagem atual, escopo global, sem área nem deformação); depois de um redesenho → `new` (`t2i_described`, com `NEW_PICTURE_NOTE` dita pelo modelo). Se a Valve `redraw_part` já é `t2i_described`, o degrau `redraw` some (ele seria o `new`). `ref_boost_manual` ou fidelidade fixa nas Valves pulam `boost` (não mudaria nada). Degrau indisponível na stack é pulado; sem nenhum degrau, o retry segue como antes. Troca, remoção, deformação/Qwen e edição de várias imagens não têm escada (cada uma tem a sua).
- **Trace.** Evento `retry_step` (`step`: `boost`/`redraw`/`new`, `part`, `from_ref_boost`); na nova tentativa automática traz também `automatic`, `measured`, `ref_boost`, `outputs`.
- **Nova tentativa automática** (`edit_chat_image`): edição de uma imagem com `ref_boost` sintonizado, `change` `moderate`/`strong`, `image_change` (todas as imagens) abaixo do limiar e `ref_boost` acima do piso → um job a mais com `ref_boost` − 1 (nova semente), status "Quase nada mudou; refazendo com mais liberdade"; as duas imagens são entregues, a fraca primeiro, e a receita guardada é a da nova tentativa (o próximo "nada mudou" parte dela).
- **Desvios da fase A.** (1) A nova tentativa automática só cobre o degrau `boost`; no piso (ou com `ref_boost` fixo) não há degrau `boost` e o filtro entrega sem refazer: refazer num `redraw`/`new` dentro do job exige montar outro job (outra parte, descrição por visão) e fica para depois do experimento. (2) Como a medição que já existia, só vale sem área (`not mask`): numa edição com área (a maioria das locais automáticas) a diferença global é dominada pelo que fica fora da área; medir dentro da área é decisão da fase B. (3) As fixtures `02:09:11` e `01:50:22` continuam `pending: "IMG-03"` com o resultado de hoje (Valves desligadas); a escada delas é provada em `tests/test_retry_ladder.py` com receitas sintéticas.
- **Para o pod (B/C).** Ligar as Valves; limiar (matriz de acertos com 20 edições "mudou certo" + as "não mudou", partindo de 6); critério 5 (duas vezes "nada mudou" com partes diferentes no trace); decidir se a nova tentativa automática deve subir além do `boost` e se mede dentro da área; atualizar o checklist (DOC-02).
