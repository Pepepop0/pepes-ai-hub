# SPEC-210 — Integração, decisões e entrega da Onda 2

| Campo | Valor |
|---|---|
| Onda | 2, fases A (integrar antes do pod) e C (fechar depois) |
| Cobre | REL-02, REL-03, DOC-02, DOC-04, DOC-05; preparo da Onda 3 |
| Prioridade / esforço | P0 / P |
| Depende de | Fase A: 200, 202A, 203A, 204A, 205A, 207A, 208, 209. Fase C: 201 e os experimentos |
| Possui | Versão do filtro; `docs/ARCHITECTURE.md`; `docs/testing/swap-lab.md`; `docs/testing/capella-5090-checklist.md`; `docs/PLANO.md` |
| Status | aberto |

## Fase A (antes do pod)

1. Integrar na ordem da seção 14.4 do `PLANO.md`; testes dos arquivos em comum a cada merge.
2. Suíte completa e compositor; `ruff` limpo.
3. Versão do filtro 0.22.0; commit nos dois ramos; hash conferido; TAR e ZIP com `.sha256`.
4. Todas as escolhas novas atrás de Valves com o padrão **igual ao comportamento de hoje**, para o pod comparar e para nada mudar sem decisão.

## Fase C (depois do pod)

1. Para cada experimento, registrar no `PLANO.md` a tabela de resultados e a decisão (regra da spec aplicada).
2. Fixar os padrões vencedores; remover variantes perdedoras que não tenham uso (sem código morto).
3. Corrigir os defeitos da SPEC-201 (cada um como spec curta ou commit com teste).
4. `swap-lab.md` com o teste final de 09/11 e os resultados da SPEC-205/206 (DOC-04).
5. Checklist da Capella com o comportamento novo de "nada mudou" (DOC-02) e uma seção "filter 0.22".
6. Suíte, commit nos dois ramos, hash, TAR e ZIP. Status no `PLANO.md`. Som.

## Critérios de aceite

- Fase A: o TAR do pod contém todas as specs da fase A, com os padrões inalterados.
- Fase C: cada pendência da Onda 2 em `validado`, `descartado` (com o motivo) ou de volta a `aberto` com o defeito descrito.
