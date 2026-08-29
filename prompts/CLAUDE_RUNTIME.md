# Prompt — Claude 2 / Runtime & Integration

Trabalhe APENAS no branch/worktree de runtime. Leia PRD, SPEC, Architecture, Current State e Agent Workflow.

## Ownership

- health checks;
- process manager;
- start/stop/status;
- launcher;
- browser open;
- desktop shortcut;
- adapters de runtime;
- pesquisa da configuração suportada do Open WebUI.

Não altere TUI, downloader, install logic ou schema sem aprovação.

## Health

Defaults: Ollama 11434, Open WebUI 8080, ComfyUI 8188. Quando houver endpoint adequado, prefira health real a apenas “porta aberta”.

## Open WebUI

Descubra forma suportada/documentada de configurar image generation, ComfyUI URL, workflow, node mappings e image edit. Se não houver forma segura, NÃO hackeie SQLite: documente o gap e proponha etapa manual.

## VRAM

Crie apenas interfaces/ganchos; não implemente orchestration agressiva sem alinhamento.

Entregue também `docs/OPENWEBUI_AUTOMATION_FINDINGS.md`.
