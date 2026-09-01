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
- `INSTALL ollama` -> `OllamaProvisioner`

Todo o resto (modelos, workflows, Open WebUI, ComfyUI) sai como UNSUPPORTED
**com motivo**, nunca omitido e nunca exibido como concluído.

Dry-run é determinístico e não muta nada: `ensure(dry_run=True)` só lê e todo
provisioner recebe `dry_run=True`.

## Pendências conhecidas

**Artifact de produção do Ollama.** Não há URL nem SHA-256 embutidos, de
propósito: ambos mudam a cada release upstream. `apply()` exige um `Artifact`
explícito e, sem ele, falha com `PRECONDITION`. A instalação real fica
bloqueada até existir um manifest pinado. A TUI não oferece controle de
instalação real por esse motivo.

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

Dois limites medidos nesta máquina antes de escrever o código:

1. **A prova morre com a sessão.** O job é destruído quando o último *handle*
   fecha, mesmo com membros vivos — os membros sobrevivem, mas o nome deixa de
   resolver (`OpenJobObject` → erro 2). Um Hub reiniciado não consegue reabrir o
   job e, portanto, não pode provar a árvore que iniciou antes. Nesse caso o
   `stop` age só no processo registrado, cuja identidade ainda é verificável, e
   diz isso no `detail`. Não usamos `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` porque
   ele mataria todos os serviços quando o Hub fechasse.

2. **Janela de adoção.** O filho já está executando quando `Popen` retorna, e a
   atribuição ao job acontece logo depois. Um neto criado nesses primeiros
   instantes nasce fora do job. A falha é segura por construção: esse neto não é
   provável, então é **deixado vivo**, nunca morto por suposição.

### NEEDS_DECISION

Fechar a janela do item 2 exige criar o processo com `CREATE_SUSPENDED`,
atribuir o job e só então `ResumeThread`. `subprocess.Popen` não expõe o handle
da thread primária, então isso significa uma implementação própria de
`CreateProcessW` via ctypes — incluindo herança de handles para os arquivos de
log. É risco real em cima do caminho de spawn que hoje está estável e testado.

Fazer a prova sobreviver a reinício do Hub (item 1) exige um processo
supervisor que segure os handles, ou aceitar matar os serviços ao fechar o Hub.
As duas são decisões de produto, não detalhes de implementação.

Ambas ficam **BLOCKED até decisão**. O que está implementado é a parte segura:
prova por job dentro da sessão, com fallback honesto fora dela.

**Ids de serviço.** Os alvos do plano vêm das chaves do profile (`open_webui`),
enquanto specs de serviço usam o id do serviço (`open-webui`). Enquanto só o
Ollama tem provisioner, os dois coincidem. Um provisioner de Open WebUI precisa
resolver isso antes de ser registrado.

**Ainda não existe:** Open WebUI e ComfyUI provisioners, aquisição de modelos,
VRAM orchestration, repair/update/uninstall.
