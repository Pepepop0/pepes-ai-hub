# SPEC-208 — Limpeza das pastas de entrada e saída do ComfyUI

| Campo | Valor |
|---|---|
| Onda | 2, fase A (local) |
| Cobre | ROB-01 |
| Prioridade / esforço | P2 / P (meio dia) |
| Depende de | — |
| Desbloqueia | SPEC-210 |
| Possui | Uma função nova de retenção no lado do Hub (módulo do ComfyUI, `src/local_ai_stack/comfyui.py` ou vizinho), chamada no início e na parada dos serviços; chave de perfil `runtime.comfyui_retention_hours` |
| Pod | Validar |
| Status | aberto |

## PRD

**Problema.** Em uma hora de sessão, o ComfyUI acumulou 50 entradas (77 MB) e 36 saídas (42 MB). Nada as apaga. As imagens entregues já ficam no armazenamento do Open WebUI.

**Objetivo.** As pastas não crescem sem limite e nada que o usuário vê é perdido.

**Critérios de aceite.**

1. Ao iniciar e ao parar os serviços, o Hub apaga de `apps/comfyui/input` e `apps/comfyui/output` os arquivos mais antigos que `comfyui_retention_hours` (Capella: 24).
2. Só arquivos com os prefixos que o Hub e o filtro geram (levantar a lista no código: os `filename_prefix` dos workflows em `workflows/*.json` e os nomes de upload do filtro). Subpastas e arquivos de outros donos ficam.
3. Nunca apaga com o ComfyUI processando um job (consultar `/queue` antes; se ocupado, adia).
4. Evento no `hub-events.log` com quantidade e bytes liberados.
5. Perfil sem a chave: nada é apagado.

## Testes

Pasta temporária com arquivos novos, velhos, de outro prefixo e em subpasta; fila ocupada falsa.

## Tarefas

1. Lista de prefixos; função de retenção; chamadas no início e na parada; schema e perfil.
2. Testes.

## Riscos

Apagar uma entrada de um job em andamento: o critério 3 e a idade mínima de 24 h evitam.

## Notas da implementação

- Código em `src/local_ai_stack/comfyui_retention.py`; chamado em `_start_services` (ComfyUI iniciado) e `_stop_services` (parada total ou do ComfyUI) de `stack_application.py`; evento `comfyui cleanup removed=N freed_bytes=B older_than_h=H` (ou `deferred`) no `hub-events.log`.
- Prefixos de saída: os `filename_prefix` sem pasta dos workflows. `PepitoSwap/depth` e `PepitoSwap/people` caem em subpasta e, pela regra do critério 2, ficam (crescimento a tratar na fase B se importar). Nomes de entrada: `pepito_swap_<hex32>.png` (filtro) e `<uuid>.png` (upload do Open WebUI, "uuid novo" segundo `open_webui_image_edit.py`; não confirmado contra o código do Open WebUI instalado: pod valida).
- `/queue` inacessível = ocioso; resposta ilegível ou erro HTTP = ocupado (adia).
- Revisão: `output/PepitoSwap/` (máscaras `people_*` e `depth_*` da troca) é nossa e também é limpa, com a mesma regra de idade e fila; a pasta fica, outras subpastas e subpastas dentro dela não são tocadas. Esta nota substitui a anterior sobre `PepitoSwap/*` ficar de fora. Nomes de entrada e de saída conferidos pelo integrador numa sessão do pod.
