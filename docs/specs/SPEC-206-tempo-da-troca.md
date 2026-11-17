# SPEC-206 — Tempo da troca de personagem

| Campo | Valor |
|---|---|
| Onda | 2, fases B (medir) e C (otimizar) |
| Cobre | SWAP-02 |
| Prioridade / esforço | P1 / P (medir) + M (otimizar) |
| Depende de | SPEC-109 (tempos por etapa no trace), integrada |
| Desbloqueia | SPEC-210 |
| Possui | `swap_chat_image`, `SwapComfyUI`, a ordem dos passes e a resolução da composição |
| Pod | Sim |
| Status | aberto |

## PRD

**Problema.** A troca leva de 70 a 252 s. Com a cena grande (2438×2183), o passe do Qwen custa 119 a 129 s e o do Krea 62 s, contra 49 a 63 s e 2 a 5 s com a cena pequena. A geração já é reduzida; a suspeita é a análise (SAM, profundidade) e a composição no tamanho original, repetidas para os dois métodos. Até a Onda 1 o trace não separava as etapas.

**Objetivo.** Saber onde está o tempo e cortar pelo menos 40% do turno da cena grande sem piorar o resultado.

**Critérios de aceite.**

1. Fase B: tabela por etapa (`gen_s`, `segment_s`, `depth_s`, `compose_s`) para T1 (cena grande), T3 e uma cena pequena, 2 repetições cada, mais o tempo de troca de modelo entre os passes.
2. Fase C, aplicando só o que a tabela justificar, entre:
   - SAM e profundidade **uma vez por cena** (a cena é a mesma nos dois métodos), reaproveitados;
   - composição em resolução reduzida (≤ 2 MP) com upscale só da máscara e aplicação no tamanho original;
   - ordem dos passes para evitar uma recarga de modelo;
   - os dois métodos em paralelo na mesma carga, se a VRAM permitir (medir pico).
3. T1 cai de cerca de 210 s para ≤ 125 s; nenhuma piora visual nos casos aprovados (sua avaliação no `swap_lab`).
4. Trace com os tempos novos.

## Testes

Unitário: SAM e profundidade chamados uma vez por troca com dois métodos (contagem no `SwapComfyUI` falso).

## Tarefas

1. Fase B: medir (sem código novo; os campos já existem).
2. Fase C: otimizações escolhidas, cada uma num commit, com antes/depois.

## Riscos

Composição em resolução reduzida pode amolecer detalhes da cena original: só a máscara é escalada, os pixels finais vêm do original e do resultado no tamanho cheio.
