# SPEC-003 — Registrar as decisões e preparar os ramos

| Campo | Valor |
|---|---|
| Onda | 0 |
| Cobre | DEC-02, DEC-04, DEC-05, DEC-06, DEC-08, DEC-14 |
| Prioridade / esforço | P0 / P (15 min seus) |
| Depende de | — |
| Desbloqueia | SPEC-001, e a criação dos ramos da Onda 1 |
| Possui | `docs/PLANO.md` seção 5 |
| Status | aberto |

## PRD

**Problema.** As specs da Onda 1 foram escritas sobre seis premissas que são decisões suas. Se uma mudar depois, a spec correspondente é refeita.

**Objetivo.** Cada premissa confirmada ou trocada, registrada no `PLANO.md`, antes de abrir os ramos.

| Decisão | Premissa | Spec afetada se mudar |
|---|---|---|
| DEC-02 | Ignorar `['...']` e `[Ignorar: ...]` | SPEC-106 |
| DEC-04 | Descartar `GLOBAL_EDIT_QUESTION` | SPEC-001, 104 |
| DEC-05 | Commitar `OVERLAY_REWRITTEN` | SPEC-001 |
| DEC-06 | Roda do mouse/touchpad = zoom | SPEC-001 |
| DEC-14 | Zoom e fundo entram sem conferência visual prévia | SPEC-001, 107 |
| DEC-08 | Pré-carregar o modelo de chat na Capella (20 GB ocupados com o Hub ocioso) | SPEC-110 |

**Critério de aceite.** Na seção 5 do `PLANO.md`, as seis linhas estão `aprovado` ou trocadas, com data.

## Preparo dos ramos (depois da SPEC-001)

- Base: o commit da SPEC-001 em `test/all-in-one`.
- Um ramo e um worktree por spec: `spec/101-contexto-honesto`, `spec/102-reconciliar-decisao`, etc.
- Os ramos integram em `test/all-in-one` na ordem da seção 13.4 do `PLANO.md`. O cherry-pick para `desktop-ui-shadcn` acontece **uma vez**, na SPEC-113, enquanto a DEC-12 (unificar os ramos) não for decidida.
- Quem implementa uma spec não roda a suíte completa; roda os testes dos arquivos que tocou.
