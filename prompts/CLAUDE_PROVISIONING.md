# Prompt — Claude 1 / Windows Provisioning

Trabalhe APENAS no branch/worktree de provisioning. Leia PRD, SPEC, Architecture e Agent Workflow.

## Ownership

- detector de ambiente;
- wrapper seguro de subprocess;
- filesystem layout;
- git clone/checkout;
- downloads;
- Ollama provisioner;
- base dos provisioners ComfyUI/Open WebUI.

Não altere TUI, runtime ou semântica dos profiles sem aprovação.

## Requisitos

Command runner com stdout/stderr, timeout, exit code, dry-run, sem `shell=True` salvo justificativa e logs sanitizados.

Detector: Windows, Python, Git, `nvidia-smi`, GPU e VRAM; comportamento previsível se comando não existir.

Provisioners conceituais: `detect()`, `plan()`, `install()`, `configure()`, `verify()` e idempotência.

Downloads: temp file, rename atômico, checksum quando disponível e nunca substituir arquivo válido.

Não faça instalação real em unit tests nem altere PATH global silenciosamente.
