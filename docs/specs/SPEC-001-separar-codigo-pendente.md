# SPEC-001 — Separar e commitar o código pendente

| Campo | Valor |
|---|---|
| Onda | 0 |
| Cobre | REL-01; premissas DEC-04, DEC-05, DEC-06, DEC-14 |
| Prioridade / esforço | P0 / P (1 a 2 h) |
| Depende de | SPEC-003 (premissas confirmadas) |
| Desbloqueia | Todas as specs da Onda 1 (é o commit-base dos ramos) |
| Possui | Os 5 arquivos com mudanças sem commit |
| Pod | Não |
| Status | aberto |

## PRD

**Problema.** Cinco arquivos têm mudanças de 09/11 sem commit. Uma delas (`GLOBAL_EDIT_QUESTION`) contradiz a SPEC-104. Se os ramos da Onda 1 saírem de um estado sujo, cada agente herda um conjunto diferente de mudanças e o JS da SPEC-107 entra em conflito com o zoom.

**Objetivo.** Um commit-base limpo, nos dois ramos, com tudo o que vale manter e sem o que será substituído.

**Não-objetivos.** Corrigir qualquer problema da lista; conferir o zoom no navegador (fica para o pod, UI-02).

**Critérios de aceite.**

1. Dado o repositório depois do commit, quando rodo `git status --short`, então só aparece `?? .serena/` (e `docs/PLANO.md`, `docs/specs/`, `docs/research/2026-11-10-*` se ainda não foram commitados junto).
2. `GLOBAL_EDIT_QUESTION`, o ramo `status == "global"` que a usa e o teste `test_a_global_verdict_from_the_vision_model_asks_about_the_whole_picture` não existem mais; o comportamento volta ao do commit `049a8a2` nesse ponto.
3. `OVERLAY_REWRITTEN` e a regra por componente estão no commit; os 15 testes do compositor passam.
4. Zoom com a roda e fundo do visualizador estão no commit.
5. `git diff test/all-in-one desktop-ui-shadcn | git hash-object --stdin` = `2549eab47a26d1fcd303ef26f749828ba7bc2b6e`.

## Spec técnica

| Arquivo | Ação |
|---|---|
| `assets/open-webui/pepito_image_chat.py` | Desfazer a mudança sem commit (15 linhas: a constante `GLOBAL_EDIT_QUESTION` e o `if` em `prepare_local_preservation`). `git checkout -- <arquivo>` é suficiente: é a única mudança nele |
| `tests/test_image_gpu_serialization_and_trace.py` | Desfazer (só o teste novo) |
| `assets/open-webui/pepito_swap_compose.py` | Commitar |
| `assets/open-webui/pepito_image_ui.js`, `pepito_image_ui.css` | Commitar |
| `docs/PLANO.md`, `docs/specs/*`, `docs/research/2026-11-10-*.md` | Commitar no mesmo commit ou num segundo commit de documentação |

Mensagem sugerida: `Viewer wheel zoom anchored at the pointer and translucent backdrop; swap keeps a speech-bubble piece whole when little was rewritten; plan and Wave 0/1 specs`.

## Testes

- `tests/test_swap_composite.py` no `swap-venv`.
- `tests/test_image_gpu_serialization_and_trace.py`, `tests/test_open_webui_image_filter.py`.
- `ruff format` + `ruff check` em `src tests scripts assets`.

## Tarefas

1. Confirmar premissas na SPEC-003.
2. `git checkout -- assets/open-webui/pepito_image_chat.py tests/test_image_gpu_serialization_and_trace.py`.
3. Rodar os testes acima e o ruff.
4. `git add -- <arquivos>`; commit em `test/all-in-one`.
5. Cherry-pick para `desktop-ui-shadcn`; conferir o hash.
6. Marcar REL-01 como feito no `PLANO.md`.

## Não regredir

Composição G2 nos 16 casos aprovados (`casos-bons.json`); galeria e visualizador abrindo e fechando; Esc fecha o menu da imagem.
