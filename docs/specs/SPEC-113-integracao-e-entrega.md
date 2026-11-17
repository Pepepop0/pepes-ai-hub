# SPEC-113 — Integração, documentação e entrega da Onda 1

| Campo | Valor |
|---|---|
| Onda | 1 (fecha a onda) |
| Cobre | REL-02, REL-03, DOC-05; preparo da Onda 2 |
| Prioridade / esforço | P0 / P (meio dia) |
| Depende de | Todas as specs 101–112 |
| Possui | Versão do filtro (`pepito_image_chat.py:4`, hoje `0.20.0`); `docs/ARCHITECTURE.md`; `docs/testing/image-tool-chat.md`; `docs/testing/capella-5090-checklist.md`; `docs/PLANO.md` |
| Pod | Não (prepara o pod da Onda 2) |
| Status | aberto |

## PRD

**Objetivo.** Os ramos da Onda 1 integrados num estado que passa na suíte completa, documentado, commitado nos dois ramos e empacotado para o pod.

**Critérios de aceite.**

1. Ramos integrados em `test/all-in-one` na ordem do `PLANO.md` §13.4: 001 → 002 → 107 → 110 → 108 → 111 → 104 → 106 → 101 → 102 → 103 → 105 → 109 → 112.
2. `ruff format --check` e `ruff check` limpos em `src tests scripts assets`.
3. Suíte completa: zero falhas (referência de 10/11: 4.189 passaram, 85 pulados, 11 min 44 s). Compositor no `swap-venv`: 15 ou mais passando.
4. Versão do filtro `0.21.0`; Hub continua `1.19.0` (não lançado).
5. Documentos:
   - `ARCHITECTURE.md`: o parágrafo do prompt inicial (SPEC-101), a reconciliação e a pergunta pendente (102/103), o texto ignorado (106), a emenda (108), a pré-carga (110); versão do perfil corrigida para 5.2.0 (parte de DOC-01).
   - `image-tool-chat.md`: como usar o texto ignorado e o comportamento do Esc.
   - `capella-5090-checklist.md`: seção "Hub 1.19.0 (filtro 0.21)" com os blocos do roteiro do pod (`PLANO.md` §8); corrigir as linhas 184–186 (DOC-02).
6. Commit nos dois ramos; `git diff test/all-in-one desktop-ui-shadcn | git hash-object --stdin` = `2549eab47a26d1fcd303ef26f749828ba7bc2b6e`.
7. TAR novo (`scripts/build_linux.py`) com `.sha256`; ZIP do Windows se houver mudança no Hub (SPEC-110 muda).
8. `PLANO.md`: status de cada pendência coberta para `feito sem pod`; linha nova no registro de mudanças.
9. Som de marco tocado.

## Tarefas

1. Integração na ordem, com os testes dos arquivos em comum a cada merge.
2. Suíte completa uma vez.
3. Documentos e versão do filtro.
4. Commit, cherry-pick, hash.
5. TAR e ZIP.
6. `PLANO.md` e som.
