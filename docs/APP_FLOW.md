# App flow — Wave A (first-run / lifecycle / Open WebUI)

Camada de aplicação atrás do botão **Instalar**. Compõe os domínios existentes;
não decide nada que já tenha dono.

| Módulo | Papel |
|---|---|
| `operations.py` | `OperationStream`: produtor validado pelo reducer congelado (`advance_operation`) antes de qualquer observer; `safe_text` (uma linha, sem caminhos absolutos, ≤240) |
| `first_run.py` | `FirstRunCoordinator`: perfil → preflight → plano → instalar → subir Ollama → modelos → configuração → subir o resto → pronto |
| `stack_application.py` | `StackApplication`: `first_run`, `start_stack`, `stop_stack`, `restart_stack`, `open_webui`, `status`; implementa `ApplicationReadModel` |

## Sequência e mensagens

`PREPARING/PREPARATION` "Verificando ambiente" → "Planejando instalação" →
`RUNNING/EXECUTION` "Preparando diretórios" → "Instalando <componente>" (a ordem
é a do plano) → `RUNNING/SERVICE_START` "Iniciando Ollama" →
`RUNNING/EXECUTION` "Baixando modelos" (progresso i/n) → "Aplicando
configurações" → `FINALIZING/SERVICE_START` "Inicializando serviços" →
`SUCCEEDED/FINALIZATION` "Tudo pronto". Falha: `FAILED` na fase onde parou, com
diagnósticos tipados. Exatamente um resultado; nada depois dele — o stream
recusa por construção.

## Decisões que ficaram nos domínios

- Compatibilidade, external, colisão de porta: `preflight_managed_stack`. Um
  porto managed ocupado por um processo que o **registry prova nosso** não é
  colisão (`managed_pids`), então rodar o first-run com a stack no ar não
  falha contra si mesmo.
- Instalação, verificação, marker: os provisioners. Modelos: `ModelInstaller`.
  Configuração: `install_actions`/`ManagedConfigWriter`. Readiness: o
  `ServiceController` (`start(wait=True)`), com timeout do próprio launch spec.
- Um workflow que o **manifest** declara não mapeado (`TBD`) é pulado e
  reportado como diagnóstico no resultado, não como falha — o first-run não
  pode mapeá-lo, e configuração depende do `text_to_image`.
- Endpoint do Open WebUI: `IntegrationOptions.endpoints_for(profile)`. O
  `OpenWebUILaunchTarget` recusa qualquer URL que não seja loopback http(s).
  A aplicação **não** abre browser; devolve o alvo para a UI.

## Idempotência

Segundo `first_run` numa instalação pronta: provisioners respondem
`skipped`, modelos `already available`, configuração sem mudança, serviços já
managed → "Tudo pronto (nada a fazer)". `status().installation_ready` usa a
mesma medida: um dry-run do plano inteiro que não propõe nada.

## Um launcher por aplicação

`StackApplication` mantém um `ManagedProcessRegistry` e um `ProcessLauncher`
pela vida toda. Um launcher por controller perderia a prova do que iniciou
(job handles) entre duas operações.

## Limitações honestas

- O `capella.yaml` **enviado** declara `chat.primary: TBD`. O domínio recusa em
  configuração ("no primary chat model"); o first-run termina `FAILED` com esse
  motivo. Escolher o modelo de chat da Capella é decisão de dados, não deste
  fluxo. Os testes do caminho feliz substituem só essa linha.
- Nos testes, git/venv/pip são respondidos pelo `ControlledToolchain` na seam
  do `CommandRunner`; os provisioners reais dirigem e verificam. O commit do
  ComfyUI vem do próprio pin. Instalação real com rede segue fora da suíte.
