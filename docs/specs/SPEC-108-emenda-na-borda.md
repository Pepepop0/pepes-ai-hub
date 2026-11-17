# SPEC-108 — Emenda na borda das edições com máscara (fase 1)

| Campo | Valor |
|---|---|
| Onda | 1, raia F |
| Cobre | IMG-01 |
| Prioridade / esforço | P0 / M (1 a 2 dias) |
| Depende de | SPEC-001 |
| Desbloqueia | SPEC-109 (integra antes), SPEC-113 |
| Possui | `assets/open-webui/pepito_image_preservation.py` (`preserve_pixels`, `_boundary_illumination`); `masked_composite` (`pepito_image_chat.py:2529-2537`); a chamada de `keep_outside_mask` e o evento `preservation` (`:6277-6293`) |
| Toca | `pepito_swap_compose.py` só se for preciso expor uma função (sem mudar comportamento) |
| Pod | Validar |
| Status | aberto |

## PRD

**Problema** (medido; Rel. 1, item 2). O Krea redesenha a imagem inteira mesmo numa edição local: fora da máscara a saída dele muda 21 a 28 níveis em relação à origem, com até 45 níveis no vermelho. Colamos o miolo dessa imagem sobre a original e a borda vira um degrau:

| Edição | Degrau natural (origem) | Degrau entregue | Saída crua do modelo |
|---|---|---|---|
| 02:05 automática (Krea) | 12,9 | 33,9 | 11,1 |
| 02:06 pincel (Krea) | 11,5 | 21,7 | 12,2 |
| 01:57 automática (Qwen) | 23,3 | 23,1 | 23,1 |

A correção atual (`_boundary_illumination`) só age em desvio pequeno, cinza e uniforme; nunca dispara no Krea.

**Objetivo.** Emenda sem degrau de tom, com o lado de fora da máscara exatamente igual à origem e o miolo (o que você pediu) sem mudança de cor.

**Não-objetivos.** Continuidade de estrutura (linhas fora do lugar): fase 2 (IMG-02, Onda 2). Silhueta em vez de retângulo: fase 2.

**Critérios de aceite** (`band_step` = diferença de luminância entre a média logo dentro e logo fora, ao longo da borda, gaussiana de 4 px; a mesma métrica do G2):

1. Imagem sintética (fundo em gradiente; "resultado" = origem com desvio global de +20 em R e +10 em G, e um retângulo de conteúdo novo dentro da máscara), máscara retangular e máscara irregular: `band_step_final ≤ 1,25 × band_step_source`.
2. Em todos os casos: pixels fora da máscara idênticos à origem (`outside_exact=true`).
3. No miolo (máscara erodida em 15 px): diferença média para o resultado do modelo ≤ 2 níveis.
4. Desvio pequeno (+3, caso Qwen): saída a no máximo 1 nível da saída atual.
5. Com os pares reais (quando `PEPITO_SESSION_DIR` aponta para `sessao-final-20261109-2312`; senão o teste é pulado): 02:05 ≤ 16,1 e 02:06 ≤ 14,4 (1,25 × origem). Na medição de 10/11 a técnica deu 12,2 e 12,3.
6. O evento `preservation` do trace ganha `band_step_source`, `band_step_raw`, `band_step_final`, `outer_ring_mad`, `seam_mode`.

## Spec técnica

```python
def seam_composite(original: Image.Image, result: Image.Image, mask: Image.Image) -> tuple[Image.Image, dict]:
    ...
def preserve_pixels(source: bytes, edited: bytes, mask_png: bytes) -> tuple[bytes, dict]:
```

Algoritmo (`h, w` = tamanho; `s = min(h, w)`):

1. `hard` = máscara > 0. `ring` = `dilatar(hard, s/40) − hard`, excluindo pixels transparentes.
2. Desvio de baixa frequência: `drift = spread(original − result medido em ring, sigma = s/20)`. Reaproveitar `drift_field` ou `spread` de `pepito_swap_compose.py` (`:58`, `:253`), importados como `open_webui.pepito_swap_compose`, com o mesmo tratamento de outliers (`DRIFT_OUTLIER`).
3. `dist` = transformada de distância para dentro da máscara. `corrected = result + drift · exp(−dist / (3 · sigma))`.
4. Rampa interna: `ramp = clip(dist / (2 · feather), 0, 1)`, com `feather = max(2, s/100)`; multiplicada pela máscara suave atual (o pincel já vem com borda suave).
5. `out = corrected · ramp + original · (1 − ramp)`; alfa da origem.
6. `seam_mode = "drift"` se o desvio médio no anel passar de 2 níveis; senão `"plain"` (só a rampa).

- `_boundary_illumination` é removida (substituída).
- `masked_composite` passa a devolver `(bytes, facts)`; `keep_outside_mask` junta os `facts` de cada imagem às métricas do evento `preservation`.
- O Open WebUI 0.12.0 tem numpy e OpenCV (decisão de 08/11). Os testes deste módulo passam a rodar no `swap-venv`, como os do compositor, e a `.venv` do Hub pula com `importorskip` se não tiver cv2. **Conferir** se a suíte do Hub hoje carrega `pepito_image_preservation` sem numpy; se carrega, manter um caminho que importe numpy/cv2 só dentro da função.

## Testes

- `tests/test_image_quality_preservation.py`: critérios 1 a 4 (sintéticos) e 5 (real, opcional).
- O script de medição de 10/11 (`%TEMP%\degrau_medir.py`) serve de referência para a métrica; a métrica oficial fica num helper de teste.

## Tarefas

1. `band_step` como helper de teste (mesma fórmula do G2).
2. `seam_composite` e a nova assinatura de `preserve_pixels`.
3. `masked_composite`/`keep_outside_mask` e os campos do trace.
4. Testes sintéticos; teste real opcional.
5. Remover `_boundary_illumination` e os testes que fixavam a implementação dela.

## Não regredir

- Edição por região automática no Qwen ("uma edição de forma corporal (a body-shape edit)"): critério 4.
- `outside_exact=true` em toda edição com máscara.
- "Nada mudou" com crescimento de área (usa a mesma colagem).

## Notas da implementação

- O desvio vem de `spread` (sigma = `s/20`) **sem** o filtro `DRIFT_OUTLIER` de `drift_field`: no par real 02:05 o anel tem desvio de até 45 níveis que varia no espaço, o filtro o descartava e o degrau final ficava em 20,8 (limite 16,1). Sem o filtro: 11,7 (02:05) e 12,2 (02:06).
- O decaimento do desvio para dentro é gaussiano, `exp(-(dist/sigma)²)`, e não `exp(-dist/(3·sigma))`: com este o miolo mudava mais de 2 níveis (critério 3), com aquele fica abaixo de 1 nos sintéticos.
- `seam_mode = "drift"` quando o desvio absoluto médio do campo suavizado no anel passa de 2 níveis em algum canal.
- `keep_outside_mask` devolve `(quantas, [facts por imagem])`; `band_step` também existe no módulo (o trace precisa dele); o teste usa uma cópia independente.
- A `.venv` do Hub não tem numpy/cv2: o módulo importa ambos dentro de `seam_composite`, os testes numéricos rodam no `swap-venv` (`--noconftest`) e pulam na `.venv`. Os testes que fixavam `_boundary_illumination` foram removidos.
