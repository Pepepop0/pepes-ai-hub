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

## Ollama — estratégia e limitação

A distribuição oficial do Ollama para Windows é um **instalador executável**,
que escreve no registry e no PATH do usuário. Rodar isso sem supervisão
violaria a promessa de não mutar estado da máquina, então **não é usado**.

O caminho gerenciado usa o **arquivo portable**: download → verificação
SHA-256 → extração em `<layout.apps>/ollama` → marcador. PATH, registry e
qualquer Ollama externo ficam intactos.

**Limitação atual, deliberada:** este módulo não traz URL nem checksum
embutidos, porque ambos mudam a cada release upstream e não podem ser
verificados aqui. `apply()` exige um `Artifact` fornecido pelo chamador e, sem
ele, falha com `PRECONDITION` e uma mensagem explicando o porquê — em vez de
adivinhar uma URL. O pinning fica, portanto, explícito e auditável: quem
fornece o artifact fornece a versão e o hash.

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

## O que ainda não existe

- provisioners de Open WebUI e ComfyUI (só o contrato base);
- aquisição de modelos;
- update/repair/uninstall;
- VRAM orchestration;
- resume de download parcial (o `.part` é descartado, não retomado);
- artifact manifest de produção com URLs e hashes reais.
