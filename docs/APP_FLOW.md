# App flow — Wave A (first-run / lifecycle / Open WebUI)

Camada de aplicação atrás do botão **Instalar**. Compõe os domínios existentes;
não decide nada que já tenha dono.

| Módulo | Papel |
|---|---|
| `operations.py` | `OperationStream`: produtor validado pelo reducer congelado (`advance_operation`) antes de qualquer observer; `safe_text` (uma linha, sem caminhos absolutos, ≤240) |
| `first_run.py` | `FirstRunCoordinator`: perfil → preflight → plano → instalar → subir Ollama → modelos → configuração → subir o resto → pronto |
| `stack_application.py` | `StackApplication`: `first_run`, `start_stack`, `stop_stack`, `restart_stack`, `open_webui`, `status`; implementa `ApplicationReadModel` |
| `hub_application.py` | Composição do `HubApplication` da TUI com `StackApplication`; worker, seleção persistida e bridge de eventos |

## Integração do produto

`run_tui()` cria o adapter real e injeta o `HubApplication` em `LocalAIStackApp`.
O fallback `UnwiredHubApplication` permanece disponível para testes isolados;
o entry point do produto usa a composição real. As telas continuam consumindo o
Protocol. Os contratos congelados não foram alterados.

O Wizard chama `install(selection)`. O adapter usa `InstallLocation` e
`resolve_installation`, seleciona o backend para aquela raiz e executa
`StackApplication.first_run` em uma thread de operação. O backend instancia o
`FirstRunCoordinator` real. A TUI acompanha os `OperationEvents` por mensagens
Textual; nenhuma instalação é simulada pela composição. Comandos simultâneos
são recusados antes de executar trabalho.

O bridge guarda o snapshot produzido pelo backend antes de entregar cada evento.
Replay da última observação, entrega e fechamento da subscription são
serializados pelo mesmo lock. Exceções de um consumer não interrompem os outros.
Fechar o adapter impede novas operações e aguarda a operação aceita terminar;
não foi acrescentado cancelamento de instalação.

`services_snapshot`, `vram_snapshot` e prontidão vêm do backend. Iniciar, parar e
reiniciar delegam a `StackApplication`. O botão Open WebUI solicita um target ao
backend em um worker da TUI: o controller garante readiness e o domínio de
endpoints fornece a URL. Somente a UI chama o browser injetável. Atualizações
de status preservam o bloqueio do botão durante o lançamento e seu cooldown.

## Seleção e reabertura

- DEFAULT continua usando a raiz de dados resolvida pelo domínio.
- PORTABLE continua resolvendo `repo/stack` e persistindo `root: null`.
- CUSTOM passa o caminho absoluto escolhido a `InstallLocation.custom`/resolver;
  nenhum caminho específico de máquina foi codificado na aplicação.

A seleção usa o schema existente `InstalledConfig`, em `state/config.yaml` da
instalação. `state/hub-selection.yaml` na raiz de dados padrão permite localizar
também uma instalação CUSTOM ao reabrir. Um config portable no checkout tem
precedência sobre esse registro de usuário. Todos os registros mantêm
`root: null` para DEFAULT/PORTABLE; a raiz é resolvida novamente ao abrir.

A intenção é salva antes de iniciar o worker, inclusive para retomar uma
instalação incompleta. Isso não declara sucesso: `installation.ready` depende
do dry-run do domínio. Escrita atômica só ocorre se os valores mudaram; um
config existente inválido não é sobrescrito. No segundo first-run, testes
conferem bytes **e mtime** do config e do ambiente Open WebUI, além de downloads,
pulls e `FirstRunReport.did_nothing`.

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

- Capella agora declara `chat.primary: ollama/qwen3_5_9b`, ID canônico já presente
  no catálogo. O perfil enviado é usado sem substituição nos testes; só os
  providers e bytes dos modelos são controlados. Pepe/Test continuam inalterados.
- Capella mantém o requisito de 32 GiB de VRAM. Em uma RTX 5070 de 12 GiB, o
  resultado esperado ao selecioná-la é uma recusa útil no preflight. Para smoke
  de instalação bem-sucedida nessa placa, usar um perfil compatível, como Pepe.
  O caminho CUSTOM `C:\Pepito-Test-5070` é aceito pelo resolver e não foi criado
  nesta rodada. Validar caminho não implica executar o smoke local-real.
- Nos testes, git/venv/pip são respondidos pelo `ControlledToolchain` na seam
  do `CommandRunner`; os provisioners reais dirigem e verificam. O commit do
  ComfyUI vem do próprio pin. Instalação real com rede segue fora da suíte.

## Provas da integração

`tests/test_delivery_wave_a.py` cobre a TUI real com o backend real e providers
controlados, sem `FakeHubApplication` na prova principal: Capella sugerida,
DEFAULT, Instalar, progresso responsivo, Concluído e Dashboard pronto. Cobre
também CUSTOM pelo Wizard, os três modos após reabertura, segundo run sem
aquisição/escrita desnecessária, modelo removido que exige reparo, falhas de
preflight/provisioning/readiness, lifecycle, external preservado, target numa
porta não padrão, readiness antes de abrir o browser e clique duplo.

`tests/first_run_support.py` concentra o setup controlado reutilizado pelos
testes do backend e da composição. Fixtures encerram workers e liberam somente
raízes registradas como test-owned. A suíte de integração proíbe conexões reais
de socket. O browser é injetado e nenhum teste usa GPU ou baixa pesos grandes.

Packaging, VRAM orchestration, image edit, maintenance, model cache e local-real
continuam fora desta entrega. O checkpoint não representa prova de Job Object
Win32 real nem uma instalação real na 5070.

## Gate final — 2026-09-11

- Backend `e0dbcdd` integrado com histórico pelo merge `8b6719c`.
- TUI `76b7c44` integrada com histórico pelo merge `bca82df`.
- Pytest: **1951 passed**, em 131.19 s; **zero warnings**.
- Composição do produto: **15 testes**; regressão integrada da test infra: **19/19 P0**.
- Raízes temporárias do Harness: **0 → 0**; ledger vazio; nenhum worker
  `hub-operation` restante. Nenhuma raiz preexistente removida.
- `ruff check .`, `ruff format --check .` e `git diff --check`: aprovados.
- Contratos congelados, catálogo, Pepe/Test e discovery global do pytest intactos.
- Entrega completa para `checkpoint-delivery-wave-a`; sem push e sem local-real.
