# SPEC-313 — Composição da troca em resolução reduzida

| Campo | Valor |
|---|---|
| Onda | 3 |
| Cobre | SWAP-04 (P10 do pod) |
| Prioridade / esforço | P1 / M |
| Possui | `compose_swap`/`compose` em `assets/open-webui/pepito_swap_compose.py` e a chamada em `swap_chat_image` |
| Pod | Validar (tempo e qualidade) |

## PRD
**Problema.** Na cena de 5,3 MP (2438×2183) a composição levou 83,8 e 82,2 s por método (166 s de um turno de 265 s); geração+SAM+profundidade ≈ 16 s. Na cena de 0,8 MP, 2,2 s.

**Critérios de aceite.**
1. A análise e a montagem das máscaras rodam em resolução reduzida (≤ 2 MP); a composição final aplica a máscara ampliada sobre os pixels de tamanho cheio (os pixels finais vêm do original e do resultado em tamanho cheio).
2. Nos 18 casos do `swap_lab` (com as saídas brutas de `Downloads\swap-lab\results-pod-r3-20261108-1956`), a diferença de pixels entre a composição nova e a atual é pequena (limiar a definir medindo; reportar média e máximo por caso) e o tempo cai (medir no PC com numpy/OpenCV).
3. Os 20 testes do compositor passam (`--noconftest tests/test_swap_composite.py` no swap-venv).
4. Meta no pod: T1 ≤ 125 s.

## Notas da implementação
- `compose_swap` reduz cena e resultado a ≤ `ANALYSIS_PIXELS` (2 MP, INTER_AREA) e faz toda a análise em `compose` nesse tamanho: máscaras, alinhamento, deriva, emenda e recorte das pessoas da frente. `full_size` amplia o alinhamento (afim escalada), o campo de deriva, o alfa da região e o recorte da frente, e mistura os pixels da cena e do resultado em tamanho cheio. Cena ≤ 2 MP: mesmo caminho de antes, com saída idêntica bit a bit.
- Medição no PC (swap-venv, `%TEMP%/spec313/measure*.py`, descartável). Nos 18 casos originais (≤ 1,6 MP), diferença 0 e tempo igual. Como nenhum passa de 2 MP, também medi com cena e resultado ampliados 2× (Lanczos, 1,6 a 6,2 MP). Formato: antes → depois; diferença média/máx por pixel (0-255); % de pixels com diferença > 16:
  - T1 (6,2 MP): Q 51,6 → 5,8 s, 0,35/133, 0,04%; K 51,8 → 5,9 s, 0,93/157, 0,62%.
  - T3 (5,4 MP): Q 35,7 → 5,8 s, 0,08/84, 0,00%; K 35,2 → 5,8 s, 0,12/120, 0,02%.
  - T8 (6,2 MP): Q 49,8 → 5,9 s, 5,61/254, 4,90%; K 49,2 → 5,6 s, 2,43/239, 2,92%.
  - T2/T4/T6 (2,8–3,1 MP): ≈ 9 → 4,6–4,9 s, média ≤ 0,18, ≤ 0,13% acima de 16.
  - T5/T7/T9 (1,6–2,2 MP): tempo e resultado praticamente iguais.
- Limiar proposto: média ≤ 1 e ≤ 1% dos pixels acima de 16. Só o T8 passa disso. Ali os balões vêm da forma (`overlay_mask`, arte granulada), e essa detecção muda com a escala. Com a máscara de balão do SAM (como no chat com `detect_overlay`), a detecção não depende da escala. Conferir o T8 no pod.
- Teste novo: cena analisada em ¼ da área mantém o detalhe de 1 px do resultado e fica próxima da composição inteira. Total: 21 testes.
