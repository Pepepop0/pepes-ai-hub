# SPEC-316 — Integração e entrega da Onda 3

| Campo | Valor |
|---|---|
| Onda | 3 |
| Depende de | 301 a 315 |
| Possui | Versão do filtro (0.23.0), `docs/ARCHITECTURE.md`, `docs/testing/capella-5090-checklist.md` (seção filtro 0.23), roteiro histórico da Onda 3 (pacote privado) (roteiro do próximo pod: validação + lab de prompt), `docs/PLANO.md` |

## Critérios
1. Integração na ordem do PLANO §15.3, testes dos arquivos em comum a cada merge.
2. Suíte completa, compositor, emenda no swap-venv; `ruff` limpo.
3. Commit nos dois ramos, hash conferido; TAR e ZIP com `.sha256`.
4. PLANO: status das pendências; registro de mudanças. Som.

## Evidências da integração

- Filtro 0.23.0 integrado. Os testes de área/canvas, receitas históricas e recorte condicional passaram juntos: 295 passaram, 2 pulados.
- Rotas nativas e seletor de prompt: 73 passaram no ambiente descartável `native-test-venv`, sem instalar pacotes na `.venv` do Hub.
- Compositor, preservação e geometria: 68 passaram no `swap-venv`, com os pares reais de 09/11.
- Chromium, JS/CSS reais sobre superfície sintética: visualizador das enviadas, pan livre, autocomplete Tab/Enter sem envio, destaque das notas, painel Prompt e envio da mensagem de regeneração. O backend de geração não foi executado nessa prova.
- Avaliador no Chromium: escala relativa e culpa do prompt funcionaram. Capturas e logs em `Downloads/pod-onda-3/verificacao-local`.
- Lab de prompts: 12 casos, 96 registros `dry_run`, 48 grafos A/D, 24 requisições B/C, nenhuma fonte ausente. Os 96 jobs de GPU ainda não foram executados.

Onda 3 continua sem validação GPU: roteiro em roteiro histórico da Onda 3 (pacote privado). IMG-11 (referência contaminada por outras pessoas) e IMG-13 (corte estrutural) permanecem abertos.

## Verificação final (13/11)

- Suíte completa: **4.653 passaram, 112 pulados, zero falhas**, 753,32 s. Quatro avisos de depreciação de `Image.getdata`, já conhecidos.
- `ruff format --check` e `ruff check` em `src tests scripts assets`: aprovados.
- Rotas nativas/Prompt: 73 aprovados; matemática do compositor/preservação/geometria: 68 aprovados.
- TAR conferido por SHA-256 e pela presença do filtro 0.23 e dos subcomandos do lab de prompts. Build Windows concluído: smoke com `problems: []` e `--version` 1.19.0; ZIP e SHA-256 gerados. Smoke não é teste de GPU.
- Logs e capturas locais em `Downloads/pod-onda-3/verificacao-local`. Próximo trabalho depende do pod, conforme roteiro, não de outro lote de refatorações.

**Entrega local concluída.** Os dois ramos conservam a diferença esperada; nenhum push/tag/release. Artefatos em `dist/`, próximos testes no roteiro do pod da Onda 3.
