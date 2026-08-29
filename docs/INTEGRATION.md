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

**Árvore de processos.** `stop` encerra o processo que registramos, não seus
filhos. `ollama serve` pode ter um `ollama runner` filho que sobrevive. Não foi
resolvido nesta rodada de propósito: matar árvore inteira (`taskkill /T` ou
equivalente) exige uma política de ownership por processo filho que ainda não
existe, e sem ela o risco é encerrar algo que não é nosso. Fica documentado
como limitação.

**Ids de serviço.** Os alvos do plano vêm das chaves do profile (`open_webui`),
enquanto specs de serviço usam o id do serviço (`open-webui`). Enquanto só o
Ollama tem provisioner, os dois coincidem. Um provisioner de Open WebUI precisa
resolver isso antes de ser registrado.

**Ainda não existe:** Open WebUI e ComfyUI provisioners, aquisição de modelos,
VRAM orchestration, repair/update/uninstall.
