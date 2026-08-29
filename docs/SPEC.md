# SPEC — Contratos técnicos

## Plataforma v0.1

- Windows 10
- NVIDIA
- Python 3.11+
- PowerShell/cmd via subprocess

## Portas padrão

| Serviço | Porta |
|---|---:|
| Ollama | 11434 |
| Open WebUI | 8080 |
| ComfyUI | 8188 |

## Layout alvo

```text
%LOCALAPPDATA%\LocalAIStack\
  apps\
  models\
  data\
  logs\
  runtime\
```

O clone contém código/config; o runtime contém instalações e dados.

## Separação obrigatória

### Detector
Descobre fatos: OS, Python, Git, GPU, VRAM, `nvidia-smi`, serviços. Não decide profile.

### Profile loader
Carrega e valida YAML. Não consulta hardware.

### Compatibility engine
Recebe facts + profile e retorna compatible/warnings/blockers.

### Provisioners
Interfaces conceituais: `detect()`, `plan()`, `install()`, `configure()`, `verify()`. Devem ser idempotentes.

### Runtime manager
Start/stop/status/health/launcher. Não instala componentes.

## Downloads

Artefatos grandes devem declarar id, URL, filename, destination, SHA256 e size hint quando disponíveis. Arquivo válido = skip; checksum inválido = erro claro.

## Git

Repos externos ficam em manifest e devem ser pinados por tag/commit quando a v0.1 estabilizar. Evitar `main/master` em produção.

## Open WebUI

Usar configuração documentada/suportada. Preferir env/config/API. Evitar mutação direta do DB. Se algo só puder ser feito pela UI, documentar como etapa manual em vez de hackear internals.

## ComfyUI

Preferir instalação controlada pelo projeto, workflows API versionados, paths conhecidos e API em `127.0.0.1:8188` por default.

## Logging

`timestamp | level | component | message`. Nunca logar tokens/chaves.
