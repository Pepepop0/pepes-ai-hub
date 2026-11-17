# SPEC-209 — Ajustes do visualizador (zoom com a roda)

| Campo | Valor |
|---|---|
| Onda | 2, fase A (local) |
| Cobre | UI-02 |
| Prioridade / esforço | P2 / P (2 a 3 h) |
| Depende de | — |
| Desbloqueia | SPEC-201 (bloco do visualizador) |
| Possui | `openViewer` / `setZoom` e o ouvinte `wheel` em `assets/open-webui/pepito_image_ui.js` |
| Pod | Não (Open WebUI aberto; conferência sua no pod) |
| Status | aberto |

## PRD

**Problema** (revisão de 10/11, Rel. 1 item 8). O zoom ancorado no cursor está certo para imagem maior que a área, com três falhas pequenas:

1. Com a imagem menor que a área (centralizada, sem rolagem), o ponto sob o cursor anda até a imagem passar do tamanho da área.
2. No Firefox e em mouses que informam a rolagem em linhas (`deltaMode = 1`), cada clique da roda dá 0,45% de zoom.
3. O texto de ajuda da área (`aria-label`) não menciona a roda.

**Critérios de aceite.**

1. Imagem pequena: o ponto sob o cursor fica parado (tolerância 2 px) ao longo de 10 cliques de roda.
2. `deltaMode` 1 e 2 normalizados para pixels (1 linha ≈ 33 px; 1 página ≈ altura da área).
3. O texto de ajuda diz "roda do mouse para zoom".
4. Touchpad: mantém rolar = zoom (DEC-06), sem mudança.

## Spec técnica

A conta do `setZoom` passa a usar a posição da imagem dentro da área (`image.offsetLeft/Top` mais a rolagem) em vez de só `scrollLeft/Top`.

## Testes

Se a QA-04 (testes de navegador) não existir ainda: roteiro manual com os 4 critérios no roteiro histórico da Onda 2 (pacote privado).

## Tarefas

1. As três correções.
2. Roteiro manual.

## Notas da implementação

- `setZoom` mede a imagem com `getBoundingClientRect` (posição real dentro da área, centrada ou rolada) em vez de `scrollLeft/Top`. Imagem pequena não rola, então o que a rolagem não absorve vira `translate` na imagem (zerado a cada `setZoom`, inclusive `fit`); quando a imagem passa a rolar, o `translate` volta a 0 sozinho.
- Roda: `deltaMode` 1 = 33 px por linha, 2 = altura da área; depois o mesmo limite de ±240 e a mesma curva de antes (touchpad inalterado).
- `aria-label` da área: "…use a roda do mouse para zoom, ou +, − e Ajustar."
- roteiro histórico da Onda 2 (pacote privado) não existe neste ramo; o roteiro fica aqui para evitar conflito entre ramos.

### Roteiro manual (Open WebUI, qualquer navegador)

1. Imagem menor que a área (ex.: 512 px): abra o visualizador, ponha o cursor num detalhe fora do centro, role a roda 10 cliques para cima. O detalhe fica sob o cursor (±2 px) o tempo todo, mesmo antes de a imagem encher a área. Repita para baixo.
2. Firefox (ou mouse com rolagem em linhas; `about:config` → `mousewheel.default.delta_multiplier_y` não muda `deltaMode`): 1 clique deve mudar o zoom ~5% (33 px × 0,15%), não 0,45%. Console: `$0.addEventListener('wheel', e => console.log(e.deltaMode, e.deltaY))` no `.pepito-viewer-stage` confirma `deltaMode` 1.
3. Inspecione a área: o `aria-label` contém "roda do mouse para zoom".
4. Touchpad: dois dedos rolando = zoom ancorado no cursor, como antes (DEC-06). Imagem maior que a área continua com zoom ancorado e arrastar para deslocar; "Ajustar" recentra a imagem.
