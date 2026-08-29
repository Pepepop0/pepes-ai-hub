# Arquitetura

```text
                    +---------------------+
                    |        TUI          |
                    +----------+----------+
                               |
               +---------------+----------------+
               |               |                |
               v               v                v
        Hardware Detector  Profile Loader     Doctor
               |               |                |
               +-------+-------+                |
                       v                        |
              Compatibility Engine             |
                       |                        |
                       v                        |
                 Install Plan                   |
                       |                        |
          +------------+-------------+          |
          v            v             v          |
       Ollama      Open WebUI      ComfyUI      |
      Provisioner   Provisioner   Provisioner   |
          |            |             |          |
          +------------+-------------+          |
                       v                        |
                 Runtime Manager <--------------+
```

## Runtime

```text
User -> Open WebUI :8080 -> Ollama :11434
                     \-> ComfyUI :8188 -> FLUX.2 Klein
```

## Pepe / VRAM

A 5070 12 GB não deve assumir LLM + FLUX residentes juntos. Direção futura: LLM atende -> pedido de imagem -> unload LLM -> FLUX gera -> free image model -> LLM recarrega na próxima mensagem.

## Capella

Single RTX 5090 32 GB, mesma arquitetura e APIs, porém com modelos maiores e política de VRAM relaxada.

## Fronteiras entre camadas

Como planning, provisioning e runtime se ligam — e o que managed vs external
permite ou proíbe — está em [INTEGRATION.md](INTEGRATION.md).

## Princípios

Declarative over imperative; profiles não contêm código; provisioner não decide hardware; runtime não instala; pin de versões; idempotência; falhar cedo; logs claros; nenhuma mutação silenciosa.
