# Integração: planning -> provisioning -> runtime

Três camadas, nenhuma conhece as outras duas:

```text
InstallPlan  ->  PlanExecutor  ->  Provisioner  ->  software managed
                                                          |
                                                          v
                                    ServiceController -> ProcessLauncher -> Health
                                                          |
                                                          v
                                                       Dashboard
```

`integration.py` é o único lugar que liga as três. Ele compõe; não
reimplementa. Path continua em `StackLayout` e no provisioner, mecânica de
spawn continua no launcher, health continua na camada de services.

## Managed vs external

Duas perguntas diferentes, nunca fundidas numa só:

| Pergunta | Quem responde | Valores |
| --- | --- | --- |
| Quem **instalou**? | provisioning | `Ownership`: NONE / MANAGED / EXTERNAL |
| Quem **iniciou o processo**? | runtime | `ProcessOrigin`: MANAGED / EXTERNAL / UNKNOWN |
| Está **servindo**? | health | `ServiceState`: UP / DOWN / DEGRADED / UNKNOWN |
| Está **rodando**? | runtime | `RuntimeProcessState`: STOPPED / STARTING / RUNNING / ... |

MANAGED é o único caso em que este hub pode agir. Um Ollama instalado pelo
usuário é INSTALLED + EXTERNAL: é observado, exibido e nunca adotado, parado,
substituído ou desinstalado.

## A fronteira provisioning -> runtime

O runtime enxerga exatamente um objeto do lado de provisioning:
`RuntimeHandoff`. Ele responde uma pergunta só — *isto pode ser iniciado por
nós, e com qual executável?* — e não carrega archive, marcador, download nem
install root.

| `LaunchAvailability` | `executable` | Consequência |
| --- | --- | --- |
| `AVAILABLE` | o binário managed | único caso com launch spec; start/stop/restart existem |
| `EXTERNAL` | `None` | observação apenas; start conflita, stop recusa |
| `NOT_INSTALLED` | `None` | nada a iniciar; nenhum executável é inventado |
| `UNAVAILABLE` | `None` | managed porém quebrado (ex.: marcador sem binário) |

O invariante está no próprio tipo: `RuntimeHandoff` recusa ser construído com
executável fora de `AVAILABLE`. Não existe caminho pelo qual o runtime receba o
path de um binário que não é nosso.

`detected_path` é separado e apenas observacional: é onde o componente foi
encontrado, inclusive quando pertence a outra pessoa. A TUI mascara `%USERPROFILE%`
antes de exibir.

## PlanExecutor

O planner descreve; o executor executa. `PlanExecutor.resolve()` mapeia cada
passo do plano para suportado/não suportado e é puro — não lê disco, então a
TUI pode chamá-lo ao montar a view.

Executáveis hoje:

- `PREPARE_LAYOUT` -> `StackLayout.ensure()`
- `INSTALL` -> provisioner registrado para Ollama, Open WebUI ou ComfyUI
- `ACQUIRE_MODEL` -> `ModelInstaller`
- `APPLY_WORKFLOW` -> resolução/materialização declarativa do workflow
- `CONFIGURE` -> geração, escrita e verificação da configuração managed

`CHECK system` continua explícito como UNSUPPORTED **com motivo**; a
compatibilidade é validada pelo preflight antes da execução.

Dry-run é determinístico e não muta nada: `ensure(dry_run=True)` só lê e todo
provisioner recebe `dry_run=True`.

## Coexistência managed + external

O default continua conservador: se uma instalação externa for detectada, o
provisioner retorna `SKIPPED`. Coexistência só existe quando o caller cria um
`IntegrationOptions` que habilita `alongside_external` por component id. O
mesmo objeto carrega overrides determinísticos de porta e é propagado pelo
composition root para provisioners, model client, configuração e runtime.

Antes de qualquer mutação, `preflight_managed_stack()` verifica hardware,
espaço, separação de paths, opt-in efetivo dos provisioners e disponibilidade
dos endpoints managed. Falha no inventário de listeners ou porta ocupada é
`PRECONDITION`; o layout ainda não foi criado. A instalação externa permanece
apenas evidência observacional e nunca fornece executable ao runtime.

O store do Ollama managed é `ManagedModelPaths.provider_root(OLLAMA)`, sob
`<root>/models/ollama`. O launch spec recebe esse path em `OLLAMA_MODELS`, e o
`HttpOllamaClient` recebe o endpoint managed vindo do mesmo
`IntegrationOptions`. Assim, um modelo presente apenas em um Ollama externo
não pode satisfazer o resolver managed.

Pull via API exige servidor pronto. `stage_managed_plan()` divide o plano sem
ensinar start/stop ao `PlanExecutor`:

```text
preflight -> provisioning -> ServiceController.start(ollama) -> readiness
          -> ACQUIRE_MODEL -> APPLY_WORKFLOW -> CONFIGURE -> restante da stack
```

O caller continua usando `ServiceController` para iniciar o Ollama; o executor
não ganhou uma segunda implementação de runtime.

## Pendências conhecidas

**Árvore de processos.** Resolvido dentro de uma sessão, com fronteira
explícita. Ver `src/local_ai_stack/jobs.py`.

A regra continua: nunca matar um processo só porque ele descende de um PID que
foi nosso. Descendência não é prova — no Windows o PPID vira um número obsoleto
assim que o pai morre, e PIDs são reciclados. Por isso nada aqui usa
`taskkill /T` nem `children(recursive=True)`.

A prova real é um **Job Object**: um contêiner do kernel. Todo processo criado
por um membro entra no job automaticamente, ninguém sai, e o kernel enumera a
membresia sob demanda. "Esse processo faz parte da árvore que eu iniciei?" deixa
de ser inferência e vira consulta. `TerminateJobObject` age exatamente sobre
esse conjunto. Um processo que o usuário iniciou nunca aparece nele.

`TerminateJobObject` não é gracioso: é `TerminateProcess` em todos os membros
de uma vez. Por isso ele é **escalonamento**, nunca o caminho normal:

- `stop` normal pede terminate na **raiz** e espera. Descendentes provados
  nossos são reportados em `left_running` e não são tocados;
- `allow_force=True` é o que autoriza a escalada, exatamente como já
  autorizava o kill de um processo só. Com ele, uma raiz que sobrevive ao
  timeout — ou descendentes que sobrevivem a uma raiz que saiu limpa — são
  encerrados pelo job;
- sem prova, a escalada degrada para o processo único cuja identidade o
  registry verifica, e o `detail` diz isso.

Dois limites medidos nesta máquina antes de escrever o código:

1. **A prova normalmente morre com a sessão.** O job é destruído quando o último
   *handle* fecha, mesmo com membros vivos. Serviços comuns preservam esse limite.
   Ollama e ComfyUI são exceções explícitas porque ambos mantêm trabalho real em
   descendentes: runners `llama-server` no primeiro; o interpretador Python que
   atende `8188` no segundo. O root recebe uma cópia do handle e o job usa
   `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. O root mantém o job vivo quando o Hub
   fecha; outro Hub reabre o mesmo nome e recupera a prova. Quando o root termina,
   o handle dele fecha e descendentes ainda pertencentes ao job não podem virar
   órfãos.

2. **Janela de adoção.** O filho já está executando quando `Popen` retorna, e a
   atribuição ao job acontece logo depois. Um neto criado nesses primeiros
   instantes nasce fora do job. A falha é segura por construção: esse neto não é
   provável, então é **deixado vivo**, nunca morto por suposição.

**Reconhecer não é matar.** O processo que atende a porta quase nunca é a raiz
registrada: `open-webui.exe` inicia um `python.exe` de venv que inicia o
interpretador real; o `python.exe` de venv do ComfyUI roda `main.py` num filho.
Para *reconhecer* a stack como nossa (preflight, status, detecção de conflito)
existe uma segunda prova, mais fraca que o job e usada só para isso:
`registry.proven_descendants` sobe a cadeia de PPIDs dentro de **um** scan de
processos, exigindo em cada salto que o pai exista no scan e tenha sido criado
antes do filho — um PPID reciclado pertence a um processo nascido *depois* do
filho, e é recusado por isso. A caminhada é limitada (`MAX_DESCENT_DEPTH`) e
para em ciclo. A raiz continua sendo a entrada do registry com identidade
verificada por `create_time`; raiz reciclada não possui árvore nenhuma.
`StackApplication.managed_pids` junta as duas provas: membros do job quando a
sessão os tem, descendência quando não tem; `ServiceController.status` aplica
a mesma regra por serviço, para que um listener filho de uma raiz que o spec
de processo não reconhece pelo nome continue sendo nosso — e, portanto,
parável. O que nenhuma das duas prova é externo, seja qual for o nome ou a
porta. Essa prova **nunca** autoriza um
stop: encerrar continua agindo sobre a raiz verificada ou sobre o job.

3. **Shim de venv.** Um `python.exe` de venv no Windows é um lançador que
   inicia o interpretador real como outro processo e sai. Adotar o shim adota
   algo que já está saindo, e o processo que faz o trabalho nasce fora do job.
   Um `ProcessLaunchSpec` deve apontar para o executável real, não para um
   shim. Vale para Open WebUI instalado em venv.

### NEEDS_DECISION

Fechar a janela do item 2 exige criar o processo com `CREATE_SUSPENDED`,
atribuir o job e só então `ResumeThread`. `subprocess.Popen` não expõe o handle
da thread primária, então isso significa uma implementação própria de
`CreateProcessW` via ctypes — incluindo herança de handles para os arquivos de
log. É risco real em cima do caminho de spawn que hoje está estável e testado.

Para serviços sem essa opção, a prova continua limitada à sessão e o fallback
continua honesto. Ollama e ComfyUI optam pela retenção porque o cleanup de seus
processos de trabalho é parte do contrato de stop/restart.

**Ainda não existe:** VRAM orchestration e repair/update/uninstall.
