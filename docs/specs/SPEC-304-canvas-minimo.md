# SPEC-304 — Canvas mínimo de 1 MP no restage e no redesenho

| Campo | Valor |
|---|---|
| Onda | 3 |
| Cobre | IMG-09 (P6b do pod) |
| Prioridade / esforço | P0 / P |
| Depende de | — |
| Possui | O cálculo do canvas (`width`/`height` do patch) das partes `restage` e do caminho `redraw` em `pepito_image_chat.py` |
| Pod | Validar |

## PRD
**Problema.** O restage usa a mesma quantidade de pixels da fonte: fonte 736×645 → canvas 560×848 ≈ 0,47 MP ("cintura pra cima" com a cabeça cortada, pouco detalhe). O Krea trabalha bem perto de 1–1,5 MP.

**Critérios de aceite.**
1. Restage e redraw: canvas com no mínimo 1 MP (e no máximo o limite atual de 2 MP), proporção pedida pelo roteador (`aspect`) ou da fonte, múltiplos de 16/32 conforme a parte.
2. Fonte grande continua limitada (nenhum canvas acima de 2 MP).
3. Trace `image_job.params.size` reflete o canvas.
4. Testes: fonte 736×645 retrato → canvas ≥ 1 MP; fonte 2438×2183 → ≤ 2 MP.

## Notas da implementação
- `canvas_dimensions` ganhou `minimum` e `step`; `MIN_REDRAW_PIXELS = 1_000_000` vale quando `part == "restage"` ou `mode == "redraw"` (inclui `edit_free`/`qwen`, que passam por `edit_canvas`; `t2i_described` usa o tamanho de geração).
- O múltiplo de 32 (Qwen/comparação) agora é aplicado dentro do cálculo (arredonda para cima quando o mínimo manda, para manter a proporção dentro dos 3% que o Qwen exige) em vez de truncar depois, o que podia derrubar o canvas abaixo de 1 MP.
- Retry puro continua repetindo o canvas gravado da receita anterior.
- Pod: conferir `image_job.params.size` ≥ 1 MP num restage de fonte pequena e se a cabeça deixa de ser cortada.
