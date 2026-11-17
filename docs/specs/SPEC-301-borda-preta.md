# SPEC-301 — Borda preta nas edições do Qwen

| Campo | Valor |
|---|---|
| Onda | 3 |
| Cobre | IMG-08 (P1 do pod de 12/11) |
| Prioridade / esforço | P0 / P |
| Depende de | — |
| Possui | `source_for_canvas` e `bounded_canvas` em `assets/open-webui/pepito_image_preservation.py`; o cálculo de canvas das edições Qwen em `pepito_image_chat.py` (perto de `bounded_canvas(...)` e `canvas = tuple(max(32, value // 32 * 32) ...)`, ~4940 e ~4984) |
| Pod | Validar |

## PRD
**Problema** (material do pacote privado, P1). Cena 736×1072; o Qwen exige múltiplos de 32 → canvas 704×1088 (proporção 5,8% diferente). Acima de 3%, `source_for_canvas(reframe=True)` preenche com transparente, que sai **preto puro** (faixas de 24–30 px no topo e na base). A faixa passa para as edições seguintes.

**Objetivo.** Nenhuma faixa preta causada por arredondamento; a imagem entregue mantém a proporção da origem.

**Critérios de aceite.**
1. Quando a diferença de proporção vem só do arredondamento para múltiplo de 32 (o canvas é o mais próximo possível da proporção da origem), a fonte é **cortada** no centro (no máximo alguns pixels por lado) ou escalada, nunca preenchida.
2. O preenchimento transparente fica só para o reenquadramento pedido de verdade (mudança de proporção que o usuário pediu).
3. O resultado volta à proporção e ao tamanho da origem (a composição/preservação já redimensiona; conferir que não estica).
4. Teste com 736×1072 (o caso real), 1097×1817 e 2438×2183: nenhuma linha ou coluna de borda com média < 5 quando a origem não tem preto na borda.
5. Edições já com faixa (imagens de 12/11) não pioram.

## Testes
Unitários em `tests/test_image_quality_preservation.py` (ou arquivo novo) com imagens sintéticas sem preto na borda.

## Notas da implementação
- **Causa real de 12/11** (trace, turno `d5c8efd8`): o roteador respondeu `aspect: "portrait"` para a cena 736×1072, que já é retrato. O filtro tratou isso como formato pedido: canvas 2:3 (720×1088 → 704×1088 no piso de 32), `scope` virou `geometry` e a fonte foi preenchida. O canvas agora segue a proporção da fonte quando ela já tem o formato indicado; o roteamento anterior de imagem inteira, sem área, permanece.
- `bounded_canvas` (preservação) agora devolve múltiplos de 32 na proporção mais próxima (um lado num passo de 32, o outro no múltiplo mais próximo; parâmetro `ratio` para manter a proporção da fonte). 736×1072 → 736×1088 (1,5%); 1097×1817 → 1088×1792; 2438×2183 → 1472×1312. As linhas `// 32 * 32` foram trocadas por ele (edição Qwen e canvas do patch para `qwen21`/`comparison_seed`).
- `rounding_only(fonte, canvas)`: a fonte escalada por um fator, cada lado a no máximo 16 px. Nesse caso `source_for_canvas` **escala** (opção "ou escalada" do critério 1), nunca preenche; o preenchimento fica para a mudança de proporção pedida.
- Critério 3: com área, a composição já desfaz a escala. Sem área, a saída do Qwen não voltava à origem; `source_proportion` + `restore_proportion` (filtro) reescalam cada resultado Qwen sem área para a proporção da fonte (tamanho da fonte quando o canvas é o da própria fonte; senão mesma área). A métrica de preservação deixa de dar `geometry_changed` nesse caso.
- **Correção da integração:** preservar o caminho de imagem inteira (sem área) das edições de tamanho de 10/11 (01:54:42, 01:55:54) e 12/11 (23:29:10, 23:29:25). Foram as melhores avaliações do usuário (10/10, "ótimas"); as edições com área automática tiveram "corte seco". A classificação de reenquadramento é calculada com o canvas anterior apenas para manter esse roteamento, separada do canvas efetivamente enviado ao Qwen. Não se troca uma edição bem avaliada por área automática para corrigir a faixa. Os dois `expected` da SPEC-112 voltaram a `mask: none`, sem os localizadores supostos acrescentados; tabela regenerada e `router_raw` intacto.
- Fora do escopo (não alterado): a nota "O canvas desta edição global foi limitado a 2 MP" aparece para fontes < 2 MP quando o canvas arredondado tem menos pixels que a fonte (apareceu no turno de 12/11, fonte de 0,79 MP).
- Pod: repetir os pedidos de tamanho na cena ~736×1072 com seleção automática ligada; devem continuar na imagem inteira, sem área, sem faixa e com entrega 736×1072. O teste usa o `router_raw` real do turno de 12/11 23:29:10 e falha se o localizador for chamado.
