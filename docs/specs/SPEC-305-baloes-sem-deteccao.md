# SPEC-305 — Detecção de balões em cena sem balão

| Campo | Valor |
|---|---|
| Onda | 3 |
| Cobre | SWAP-05 (lab 205) |
| Prioridade / esforço | P0 (para ligar a Valve) / P |
| Depende de | — |
| Possui | `workflows/sam3-people-api.json` (ramos de balão e texto), a leitura das saídas no `SwapComfyUI` e o `overlay-detect` de `scripts/swap_lab.py` |
| Pod | Validar |

## PRD
**Problema.** No lab de 12/11, T2, T4, T5 e T6 (cenas sem balão) deram `IndexError: index 0 is out of bounds for dimension 0 with size 0` no `SaveImage`: o grafo tenta salvar uma máscara vazia. Com a Valve `swap_overlay_detection` ligada, a troca quebraria em qualquer foto sem balão.

**Critérios de aceite.**
1. Detecção vazia nunca falha o job: o grafo salva uma máscara vazia válida (ou o filtro trata a ausência de saída) e a troca segue com "nenhuma sobreposição detectada".
2. Teste com o ComfyUI falso devolvendo zero saídas de balão/texto.
3. `swap_lab.py overlay-detect` não quebra nos casos sem balão.
4. Depois desta spec, a Valve pode ser ligada por padrão (decisão registrada no PLANO, com os números do lab: detecção preserva mais texto em todos os casos; T7/T9 de 45–81% para 7–16% de letras alteradas no Qwen).

## Notas da implementação
- Causa, no ComfyUI v0.39.0 fixado (`comfy_extras/nodes_sam3.py`, commit `b0b7435`): `SAM3_Detect` com `individual_masks=true` e nada detectado devolve `torch.zeros(0, H, W)`; `MaskToImage` mantém o lote vazio; `SaveImage` (`nodes.py`) lê `images[0].shape` para o nome do arquivo e dá o `IndexError`. Com `individual_masks=false` o nó devolve a união, `torch.zeros(1, H, W)` quando nada é achado: uma máscara preta válida.
- Grafo: os ramos de balão e de texto passaram a `individual_masks=false` (uma máscara de união por tipo). O compositor já unia todas as máscaras de sobreposição (`compose_swap` faz `detected |= ...`) e separa as peças por componentes conexos, então nada se perde. O ramo de pessoas não muda.
- Filtro: o `SwapComfyUI` segue lendo as saídas; `swap_chat_image` descarta máscara de sobreposição em branco (nenhum pixel acima de 127, o `MASK_THRESHOLD` do compositor). Sem nenhuma máscara útil o compositor recebe lista vazia e usa a regra por forma, como com a Valve desligada (`overlay_source: "shape"`). O evento `swap_overlay_detection` ganhou `blank` (máscaras em branco descartadas). Saída de balão/texto ausente no histórico também segue sem erro.
- `swap_lab.py overlay-detect`: não grava máscara em branco (contagem 0 no `overlay_detect.json`) e apaga `overlay-*.png` antigos do caso antes de gravar.
- Testes: `tests/comfy_sam_fake.py` reproduz o que o ComfyUI salva para um grafo SAM (incluindo o `IndexError` do lote vazio); usado pelo ComfyUI falso de `test_image_swap.py` (zero balões e texto, em máscaras pretas e sem saída), por `test_workflows.py` (o grafo real não falha sem detecção) e por `test_swap_lab.py` (`overlay-detect` com caso sem balão).
- Critério 4: a Valve `swap_overlay_detection` continua desligada por padrão; ligar e registrar a decisão no PLANO fica com a integração (números do lab: detecção preserva mais texto em todos os casos; T7/T9 de 45–81% para 7–16% de letras alteradas no Qwen).
- Pod: rodar `overlay-detect` em T1–T9 (T2, T4, T5, T6 devem sair com contagem 0, sem erro) e uma troca no chat com a Valve ligada numa cena sem balão.
