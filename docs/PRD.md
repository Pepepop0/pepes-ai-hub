# PRD — Local AI Stack Manager

## 1. Visão

Provisionador local de IA para Windows com TUI. O usuário escolhe um profile e o sistema instala, configura, valida e inicia uma stack local com LLM, visão, tool calling, geração e edição de imagem.

## 2. Problema

A montagem manual exige múltiplas instalações, paths, downloads grandes, workflows ComfyUI, node mappings, URLs/portas, configuração de modelos, controle de processos e cuidados de VRAM. O produto converte esse conhecimento implícito em um processo reproduzível e auditável.

## 3. Profiles

### Pepe
- Windows 10
- RTX 5070
- 12 GB VRAM
- chat local, visão e tools;
- FLUX.2 Klein para geração/edição;
- política agressiva de unload/reload.

### Capella
- Windows 10
- RTX 5090
- 32 GB VRAM
- single GPU na v0.1;
- modelos maiores e menos pressão de VRAM.

### Test
- deve caber nas restrições de Pepe ou abaixo quando possível;
- prioriza instalação rápida e smoke tests;
- deve validar chat, visão, tools, geração e edição;
- escolha de modelos de baixa restrição/alinhamento deve ser declarativa no manifest, não acoplada ao core.

## 4. Jornada

1. Clonar repo.
2. Executar bootstrap.
3. TUI abre.
4. Detectar OS/GPU/VRAM/Python/Git.
5. Escolher profile.
6. Mostrar compatibilidade e plano.
7. Confirmar.
8. Instalar/verificar componentes.
9. Baixar/validar modelos.
10. Aplicar workflows/configs.
11. Executar health checks.
12. Criar atalho.
13. Abrir stack por um launcher.

## 5. Requisitos v0.1

- TUI Python;
- profiles YAML;
- detector Windows/NVIDIA;
- provisioning idempotente;
- Ollama, ComfyUI e Open WebUI;
- downloads resumíveis quando possível;
- checksums em artefatos declarados;
- workflows API versionados;
- mappings versionados;
- health checks 11434/8080/8188;
- Doctor;
- launcher e atalho;
- logs legíveis.

## 6. Não funcionais

Idempotência, reprodutibilidade, operações explícitas, versões pinadas, erros recuperáveis, logs, zero mutação destrutiva sem confirmação e profiles independentes da lógica de hardware.

## 7. Fora de escopo v0.1

Linux, macOS, AMD, Intel Arc, multi-GPU, cluster, update irrestrito, rollback total e edição não documentada do DB interno do Open WebUI.

## 8. Sucesso da v0.1

Na Pepe, a partir de clone limpo: TUI inicia, GPU é detectada, profile é compatível, componentes são provisionados, modelos são localizados/baixados, serviços sobem, Doctor passa, Open WebUI abre, text-to-image funciona e o caminho de image-edit está pronto.

## 9. MVP da primeira noite

Arquitetura congelada, profiles validados, TUI funcional, detector/Doctor funcional, provisioning básico implementado ou mockado, launcher/status base, testes e documentação suficiente para continuar sem reconstruir contexto.
