# Laboratório público de troca de personagem

O laboratório público usa somente Qwen-Image-2.1. Compara `S1_qwen_marked` (alvo marcado e referência limpa) com `S4_prompt` (pedido direto, quando o caso declara `free_prompt`). Os experimentos Krea e seus registros ficam no pacote privado.

## Preparar e executar

Use imagens SFW de adultos com consentimento ou personagens fictícios. O arquivo `cases.json` segue o schema `swap-lab-1`; consulte `python scripts/swap_lab.py prepare --help` para os argumentos de preparo e `run --help` para os de execução. Não use fontes privadas dos estudos históricos como fixtures públicas.

Com o ComfyUI instalado, modelos Qwen disponíveis e serviços iniciados:

```bash
bash scripts/capella-linux.sh swap-lab /absolute/dedicated/root /absolute/lab
```

A pasta do laboratório deve conter `cases.json` e as entradas referenciadas por ele. O comando grava resultados em `results/`; não inicia serviços. Para conferir os grafos sem inferência, use `python scripts/swap_lab.py run --cases /absolute/lab/cases.json --out /absolute/lab/dry-run --dry-run`.

## Avaliar e compor

Abra `results/avaliar-troca.html`. O revisor compartilhado é `scripts/lab_review.html`, preenchido por `scripts/lab_common.py`. As notas são isoladas por laboratório e rodada; a exportação contém apenas os casos da página.

`overlay-detect`, `overlay-compose` e `overlay-review` preservam balões e textos. Consulte `python scripts/swap_lab.py <comando> --help`. O compositor `scripts/swap_composite.py`, a análise `scripts/swap_analysis_compose.py` e o refinamento `scripts/blend_lab.py` processam a base pública `S1_qwen_marked`; as entradas terminadas em `_private.py` pertencem exclusivamente ao pacote privado.

Os testes públicos usam imagens sintéticas e pedidos neutros: `tests/test_swap_lab.py`, `tests/test_swap_composite.py` e `tests/test_blend_lab.py`.
