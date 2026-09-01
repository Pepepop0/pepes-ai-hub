# Provisioning — M2 foundation

Como a stack é instalada. Camada de backend: sem TUI, sem prompt interativo.

```
Profile / InstalledConfig  ->  InstallPlan  ->  Provisioning  ->  StackLayout
```

O planner descreve; o provisioning executa. `PlanExecutor` é a única ponte
entre os dois, e o planner continua sem método de execução.

## Ownership: managed vs external

Duas perguntas independentes, nunca misturadas:

| `ComponentState` | significado |
|---|---|
| `NOT_INSTALLED` | não encontrado |
| `INSTALLED` | presente e utilizável |
| `INVALID` | presente mas quebrado (ex.: instalação interrompida) |
| `UNKNOWN` | não foi possível determinar |

| `Ownership` | significado |
|---|---|
| `MANAGED` | nós instalamos, sob `<layout.apps>/<componente>`, com marcador |
| `EXTERNAL` | já existia na máquina; **só é lido e reportado** |
| `NONE` | nada encontrado |

Regras que valem para todo provisioner:

- instalação externa **nunca** é adotada, substituída, desinstalada ou movida;
- `apply()` sobre um componente externo retorna `SKIPPED`, não `INSTALLED`;
- PATH global, registry e atalhos não são tocados;
- `detect()` é read-only e **não cria diretórios**.

O marcador (`.pepito-managed.json`) é o que prova posse. Ele é escrito **por
último** e de forma atômica: uma instalação interrompida deixa arquivos sem
marcador e é reportada como `INVALID`, nunca como pronta.

## Downloads

`Downloader` é genérico — não conhece nenhum componente.

1. bytes vão para um `.part` ao lado do destino (mesmo volume, senão o rename
   atômico não vale); com `StackLayout` disponível, o parcial fica em
   `layout.downloads` quando estiver no mesmo drive;
2. SHA-256 é conferido antes de qualquer promoção;
3. `os.replace()` publica o arquivo final — o destino nunca existe pela metade;
4. checksum errado ⇒ o `.part` é apagado e nada é promovido.

Idempotência: destino já existente com checksum válido ⇒ `SKIPPED`, sem
requisição HTTP. Sem checksum no manifest, um arquivo existente é aceito mas o
resultado vem marcado `verified=False` — é o limite honesto do que dá pra
afirmar.

Retry é limitado e explícito: só erros transitórios (transporte, 5xx, 429).
`4xx` não é repetido. Dry-run não abre socket e não escreve nada.

URLs passam por `runner.sanitize()` em todo log, evento e mensagem de erro:
token em query string ou credencial embutida não vaza.

## Git

`GitClient` roda sobre o `CommandRunner` (argv em lista, sem shell, timeout,
log sanitizado). Limites deliberados:

- config global nunca é tocada; nada emite `--global`;
- `reset --hard` nunca é emitido, implícita ou explicitamente;
- `checkout` e `fetch` **recusam** working tree suja, salvo `allow_dirty=True`;
- `clone` recusa destino não vazio;
- pinning é explícito: `clone(..., revision=...)` e `checkout(path, revision)`
  usam `--detach`. Nada se auto-atualiza.

## IDs canônicos

Um id por componente, hifenizado, usado em **todas** as camadas: stack
definition, chaves de `services` no profile, target de step do plano,
provisioner, `ServiceSpec`, `RuntimeProcessSpec` e integração.

    ollama   open-webui   comfyui

Fonte única em `components.py`. Nomes de identificador Python não aceitam
hífen, então `ComponentId.attr` deriva a forma com underscore
(`open_webui_app`) — é o único ponto onde as duas grafias se encontram.
Profiles escritos com a grafia antiga (`open_webui`) são canonicalizados no
carregamento; declarar as duas no mesmo profile é erro.

Não existe tabela de tradução entre component id e service id: são a mesma
string.

## Pinning por componente

| componente | distribuição | pin |
|---|---|---|
| ollama | zip portable | `v0.33.2` + SHA-256 (`artifacts.yaml`) |
| open-webui | pacote PyPI | `open-webui==0.11.3` (`packages.yaml`) |
| comfyui | checkout Git | tag `v0.34.0` + commit `12d5279…` (`repositories.yaml`) |

Nada resolve `latest`. Subir versão é editar manifest, revisado como qualquer
mudança. `catalog.py` lê os manifests e não toca a rede.

## Ollama — estratégia e limitação

A distribuição oficial do Ollama para Windows é um **instalador executável**,
que escreve no registry e no PATH do usuário. Rodar isso sem supervisão
violaria a promessa de não mutar estado da máquina, então **não é usado**.

O caminho gerenciado usa o **arquivo portable**: download → verificação
SHA-256 → extração em `<layout.apps>/ollama` → marcador. PATH, registry e
qualquer Ollama externo ficam intactos.

O artifact vem de `manifests/artifacts.yaml`, pinado em `v0.33.2` com o
SHA-256 publicado pelo release. Um componente sem pin ainda faz detect, plan e
dry-run; só o install real recusa, com `PRECONDITION`, em vez de chutar URL.

Extração tem guarda de path traversal: entrada de ZIP que escape do destino
aborta a instalação.

## Estados de resultado

`ProvisionOutcome`: `INSTALLED`, `SKIPPED`, `FAILED`, `DRY_RUN`.

Falha nunca é uma string solta — vem em `ProvisionFailure(reason, message,
detail)` com `FailureReason` tipado (`UNSUPPORTED`, `PRECONDITION`,
`EXTERNAL_PRESENT`, `COMMAND_FAILED`, `NETWORK`, `CHECKSUM`, `FILESYSTEM`,
`CANCELLED`, `UNKNOWN`), pronto pra TUI renderizar sem parsear texto.

Progresso sai por `ProvisionEvent` via callback opcional — o backend não sabe
que existe terminal.

## Open WebUI — estratégia

Pacote publicado, então não há archive nem clone: o install gerenciado é um
virtualenv em `<apps>/open-webui/.venv` com um requirement pinado.

Virtualenv em vez de `uvx` ou user-site porque é autocontido: nada entra no
PATH, nenhum site-packages do sistema é tocado, e apagar o diretório desinstala.

`pip` recebe sempre `open-webui==<versão>`. Interpretador é escolhido dentro da
faixa que o upstream declara (`>=3.11,<3.13`), sem mexer no PATH; se não houver
um compatível, recusa em vez de montar ambiente não suportado.

## ComfyUI — estratégia e o que falta

Distribuído como fonte: clone Git pinado por tag **e** commit, mais um
virtualenv ao lado. Depois do checkout o commit é conferido contra o pin, então
tag movida upstream é reportada em vez de seguida silenciosamente.

**Limitação real, deliberada:** o `requirements.txt` do ComfyUI no revision
pinado deixa `torch`, `torchvision` e `torchaudio` **sem versão**. Instalar
aquilo verbatim resolveria versões flutuantes — e, num alvo NVIDIA, *qual* build
de torch instalar é uma decisão, não um default.

Por isso o passo de dependências exige um `DependencyPin` explícito
(requirements + índice, quando as wheels não vêm do PyPI). Sem ele, `apply()`
recusa com `PRECONDITION` **antes de clonar qualquer coisa** — nada é criado e
não sobra checkout meio-instalado se dizendo utilizável.

Decisão pendente para destravar: qual versão de torch e de qual índice.

## O que ainda não existe

- aquisição de modelos;
- update/repair/uninstall;
- VRAM orchestration;
- resume de download parcial (o `.part` é descartado, não retomado);
- pin de dependências do ComfyUI (torch e índice).
