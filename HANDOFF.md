# HANDOFF — LEIA PRIMEIRO

## Escopo atual — Hub 1.17

O desktop continua Windows/NVIDIA. A pedido do usuário, Capella também tem uma
bancada Linux x86-64 headless: o CLI original instala/opera a stack, e o acesso
ao Open WebUI ocorre por túnel SSH. Veja `docs/testing/runbook-linux-1.17.md`.
O restante deste documento registra o objetivo histórico da v0.1.

## Objetivo imediato

Transformar o setup manual validado na máquina **Pepe (RTX 5070 12 GB / Windows 10)** em um gerenciador de stack local reproduzível.

A v0.1 é deliberadamente limitada a:
- Windows 10;
- NVIDIA;
- Python 3.11+;
- profiles declarativos;
- Pepe, Capella e Test.

## Próxima entrega esperada

Uma TUI que:
1. detecta sistema/GPU/VRAM;
2. carrega profiles;
3. valida compatibilidade;
4. exibe plano de instalação;
5. executa provisioning em etapas;
6. possui Doctor;
7. cria launcher/atalho depois.

## Divisão recomendada

- Codex: arquitetura, TUI, schema, integração e merge.
- Claude 1: provisioning Windows.
- Claude 2: runtime, health, shortcut e integração Open WebUI/ComfyUI.

Prompts prontos em `prompts/`.

## Fora do escopo

Desktop Linux/macOS, AMD, multi-GPU, auto-update complexo, uninstall perfeito,
hacks no SQLite do Open WebUI e otimizações prematuras. Docker Linux sem GPU é
usado apenas para validar a preparação; não prova inferência na RTX 5090.

## Primeira ação

Abra `local-ai-stack.code-workspace` no VS Code e rode `bootstrap-dev.bat`.
