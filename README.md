# Local AI Stack Manager

Provisionador local de IA com TUI, orientado a **Windows 10 + NVIDIA**, para instalar, configurar, validar e iniciar uma stack com Ollama, Open WebUI e ComfyUI.

## Perfis iniciais

- **Pepe** — RTX 5070 12 GB. Modelos compactos e política agressiva de VRAM.
- **Pepe 8 GB** — GPUs NVIDIA de 8 GB. A mesma stack completa (chat, geração e edição de imagem) com o modelo de chat menor e imagens mais modestas.
- **Capella** — RTX 5090 32 GB, single GPU. Modelos maiores e menos unload.
- **Test** — perfil leve para validar instalação, APIs, visão, tool calling e imagem.

## O que este starter contém

- PRD, SPEC, arquitetura e estado atual do setup manual;
- roadmap e plano da primeira noite;
- profiles YAML e manifests;
- prompts separados para Codex e dois Claudes;
- skeleton Python executável;
- TUI inicial e `--doctor`;
- testes e configuração do VS Code.

## Quick start

```bat
bootstrap-dev.bat
run.bat
```

Diagnóstico:

```bat
doctor.bat
```

Testes:

```powershell
pytest
ruff check .
```

## Leia nesta ordem

1. `HANDOFF.md`
2. `docs/CURRENT_STATE.md`
3. `docs/PRD.md`
4. `docs/SPEC.md`
5. `docs/ARCHITECTURE.md`
6. `docs/NIGHT_ONE_PLAN.md`
7. `docs/AGENT_WORKFLOW.md`

## Regra de ouro

O projeto deve ser **reproduzível, idempotente e profile-driven**. O detector detecta; o profile declara; o compatibility engine decide.
