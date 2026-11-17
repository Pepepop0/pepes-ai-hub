# SPEC-205 — Balões e texto preservados na troca de personagem

| Campo | Valor |
|---|---|
| Onda | 2, fases A, B e C |
| Cobre | SWAP-01 |
| Prioridade / esforço | P2 / M (2 dias + 20 min de pod) |
| Depende de | — (independente da SPEC-204, mas usa o mesmo tipo de grafo do SAM) |
| Desbloqueia | SPEC-210 |
| Possui | `overlay_mask` e a regra `OVERLAY_REWRITTEN` em `pepito_swap_compose.py`; a análise da cena em `prepare_swap_job`/`SwapComfyUI` (um prompt a mais); `scripts/swap_lab.py` para o experimento |
| Pod | Sim |
| Status | aberto |

## PRD

**Problema.** Na troca, o modelo reescreve letras de balões ("rather", "Potter"). O compositor acha o "conteúdo sobreposto" pela forma (pixels longe de um desfoque da cena) e, desde 10/11, mantém um pedaço inteiro se menos de 35% dele mudou. Mudou pouco: 13,7% → 12,9% dos pixels do balão alterados.

**Objetivo.** Texto e balões da cena original voltam intactos, exceto onde a personagem nova realmente os cobre.

**Critérios de aceite.**

1. A análise da cena roda o SAM 3.1 com os prompts `speech bubble` e `text` (além de `person`) uma vez por troca; as máscaras entram no compositor como "sobreposição detectada".
2. Regra: pixel de sobreposição detectada é sempre da cena original, **a não ser** que esteja dentro da silhueta nova **e** a profundidade diga que a personagem está na frente (a mesma ordem `others_order` já usada para outras pessoas).
3. Sem detecção → comportamento de hoje (`overlay_mask` por forma).
4. Nos casos de laboratório com balão (T1, T8): 0 palavras alteradas fora da área coberta pela personagem nova (contagem visual sua, apoiada na diferença de pixels por componente).
5. O caso do chapéu sobre o balão (T6) continua com o chapéu na frente.
6. Nenhuma piora nos 16 casos aprovados (`casos-bons.json`), pela sua avaliação no `swap_lab`.
7. Trace: `overlay_detected_share`, `overlay_kept_share`.

## Experimento (fase B)

Rodar no `swap_lab.py` os 18 casos (16 bons + Q3/Q4) com e sem a detecção, sementes fixas das rodadas anteriores. Medir o tempo extra do SAM e a contagem de palavras alteradas; você avalia lado a lado.

## Testes

Unitários do compositor: máscara de sobreposição + personagem atrás (mantém) / na frente (cede); sem detecção = hoje.

## Tarefas

1. Prompts e saída no grafo do SAM; leitura no `SwapComfyUI`.
2. [P] Regra no compositor e testes.
3. Variante no `swap_lab.py`.
4. Fase C: se aprovado, vira padrão e a regra `OVERLAY_REWRITTEN` é removida se ficar sem efeito.

## Riscos

O SAM pode detectar texto em roupa ou fundo como "texto": a regra só protege, não apaga nada; o pior caso é uma estampa da personagem antiga sobreviver. O experimento mostra.

## Notas da implementação (fase A)

- Chave: Valve de usuário `swap_overlay_detection` (padrão desligada). O inlet copia o valor para o job (`overlay_detection`), porque o gancho `request` não recebe a Valve.
- Grafo: `sam3-people-api.json` ganhou dois ramos (prompts `speech bubble:1000` e `text:1000`, saídas `PepitoSwap/overlay_bubble` e `overlay_text`). O `SwapComfyUI` remove esses ramos do grafo em todo job que não é a análise da cena com a Valve ligada; assim as saídas dos geradores seguem com só `person:1000`.
- "Profundidade diz que está na frente": o mapa é da cena original, então a personagem nova é representada pela pessoa trocada (mesma pose). A peça fica atrás se a profundidade mediana da pessoa trocada junto dela não passa de `FRONT_MARGIN` vezes a da peça. A decisão por peça aparece em `overlay_order` (`front`/`behind`/`clear`).
- Trace: `overlay_source`, `overlay_detected_share`, `overlay_kept_share`, `overlay_order` em `compositor` de cada `swap_method`, e o evento `swap_overlay_detection` (`masks`, `seconds`).
- Laboratório: `swap_lab.py overlay-detect` / `overlay-compose` (ver `docs/testing/swap-lab.md`). Os critérios 4 a 6 e o tempo extra do SAM ficam para o pod.
