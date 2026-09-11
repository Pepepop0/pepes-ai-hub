# E2E harness

Implementa o `TestHarness` de `test_contracts.py`. Orquestra os domínios
existentes; não reimplementa nenhum deles.

Integrado à Test Control API por `test_application.py`: uma factory cria um
world controlado e um `StackTestHarness` novo por run. HTTP conhece apenas o
protocolo. Veja [modo de start e SSE](test-control-api.md) e
[evidências de integração](integration.md).

## Módulos

| Arquivo | Papel |
|---|---|
| `e2e/harness.py` | `StackTestHarness`: fases, cleanup em `finally`, montagem do relatório |
| `e2e/world.py` | `ControlledWorld`: todas as dependências substituíveis, amarradas uma vez |
| `e2e/providers.py` | Os fakes: runner, transporte HTTP, store Ollama, probes, inventário, launcher, relógio |
| `e2e/seams.py` | `Injection` e `InjectionPlan` — falhas nomeadas, determinísticas |
| `e2e/roots.py` | Root efêmero (removido) vs configurado (nunca apagado) |
| `e2e/metrics.py` | Medição monotônica; amostras cruas + count/mediana/máximo |
| `e2e/fixtures.py` | Fixtures: repositório, catálogo de pesos pequenos, instalação external real |
| `e2e/scenarios.py` | Matriz P0 e o runner que verifica invariantes de fora |

## O que o harness não faz

Ownership, verificação, classificação external e autoridade de path continuam
nos domínios. O harness compõe `preflight_managed_stack`, `build_install_plan`,
`stage_managed_plan`, `plan_executor`, `StackModelResolver`,
`detect_integration` e `build_service_controller`, e **lê** as respostas.

Um teste garante isso: `harness.py` não contém `taskkill`, nem cálculo de
checksum, nem decisão própria de "é external".

## Spec, world e injections: uma fonte de verdade cada

O world é a autoridade sobre root e providers. Uma spec que peça
`root_mode=CONFIGURED` contra um world efêmero, ou `provider_mode=LOCAL_REAL`
contra providers controlados, é **recusada antes de qualquer fase** — todas as
fases pedidas viram `blocked` e o run é `failed`. Nada é normalizado em
silêncio: um relatório que nomeia um modo em que ninguém rodou é pior que
nenhum relatório.

`local-real` não existe ainda. `real_world()` **recusa** mesmo com a capability
de rede, porque antes ela devolvia facts reais e cliente Ollama real enquanto
mantinha inventário, probes e launcher controlados — um scan de processos
controlado seria reportado como evidência sobre a máquina real.

Existe **um** `InjectionPlan` por run, o do world; o harness não aceita outro.

E **nenhuma injection decorativa**: cada membro de `Injection` é consumido por
uma seam, e um teste parametrizado exercita essa seam com e sem o plano e
exige observações diferentes. Porta ocupada e máquina sem CUDA deixaram de ser
injections — são o que o mundo *é*, e o cenário passa a construir esse mundo
(`controlled_world(cuda=False)`, listener no inventário) em vez de declarar um
nome que nenhuma seam lia.

## Cleanup cobre o run inteiro

O root existe desde a construção do world, então **tudo** a partir da validação
da spec roda dentro do `try` cujo `finally` o libera. Um run que nem consegue
carregar o profile ainda limpa. O contexto é construído em dois passos: um sem
I/O, que dá ao cleanup o que ele libera, e o bind do profile, que pode falhar.

## Regras de fase

- Falha para o run inteiro.
- **Bloqueio para só as fases mutantes.** É o que o contrato pede em
  "GPU phase is blocked before workload while CPU/read-only phases stay
  available": sem CUDA, `plan` continua disponível.
- Uma fase bloqueada **carrega seus diagnósticos**. Sem isso, um cenário não
  consegue provar *por quê*, que é a maior parte do valor.
- `cleanup` roda de um `finally`, sempre, e reporta independente. Uma falha de
  cleanup nunca transforma uma falha anterior em passe.

## Veredito de cenário

Um verificador que só junta problemas passa sempre que não achar nenhum. Cada
linha da matriz portanto **declara** o que espera: o estado do run, a fase de
que ela realmente trata, o outcome dessa fase, e o outcome do cleanup. Um run
`failed` só é cenário `passed` onde a linha diz isso (readiness, download
interrompido, config adulterada, workflow inválido, cleanup após falha). Uma
fase que nunca rodou nunca é prova.

Além disso, uma fase `passed` não pode esconder um resultado estruturado com
`ok=False`: o veredito de toda linha varre `ok`, ações e resultados de
componente de cada fase aprovada. Há testes que adulteram cada um e exigem que
a linha caia.

Todas as linhas P0 exigem cleanup aprovado. Em `cleanup-after-failure`, a
injection `STOP_FAILURE` usa `stop_failure_once=True`: recusa o stop da fase,
que falha, e permite o retry no cleanup. A falha do run permanece registrada;
nenhum processo, membership ou registro managed pode sobreviver. A recusa
persistente continua disponível em testes negativos de diagnóstico de cleanup,
mas não conta como cleanup P0 aprovado.

## O que cada linha realmente exercita

- **chat_smoke** manda uma geração para o endpoint que o profile resolveu,
  nomeando o modelo que o resolver disse estar disponível. O store controlado
  recusa uma referência que não tem. `/api/version` provava porta aberta, que o
  start já provava.
- **image_smoke** renderiza o workflow declarado — manifest real, grafo real,
  `resolve_workflow`/`verify_workflow_references`/`render_workflow` reais — e o
  submete. A prova é o contrato de submissão: binding de modelo com o
  `runtime_name` resolvido, parâmetros nos endereços que o manifest declarou,
  resultado de volta. Nada executa: sem GPU, sem imagem.
- **crash-after-start** começa de verdade, fica ready de verdade, e morre na
  **próxima** observação. É o caso que um relatório de start sozinho não pega.
  O stop lê `controller.statuses()` antes de tocar em nada.
- **external coexistence** cria uma instalação real onde os *hints do próprio
  provisioner* olham, fora do root managed. Cada componente tem prova própria:
  classificado external, coexistência não recusada, endpoint managed separado
  do external, nunca adotado, nunca `stoppable`, nunca iniciado ou parado,
  bytes em disco inalterados, processo vivo depois.
- **second-run** exige primeiro passe limpo (provisioning e modelos `ok`,
  nenhuma ação `failed`) e segundo passe `ok` sem `failed`; "failed" não é
  forma válida de "nada a fazer".
- **stop-cleanup** para uma árvore (root + worker) com membership provada, ao
  lado de um processo não relacionado e de um external que sobrevivem.
- **crash-after-start** lê o estado duas vezes; a segunda leitura é a que tem
  de mostrar processo parado e serviço fora de UP.
- **corrupted-model** instala e verifica pesos de verdade e só então muda os
  bytes; quem pega é o installer re-hasheando um arquivo que já tinha aceito.
- **invalid-workflow** roda com os três modelos genuinamente AVAILABLE. A seam
  `workflow-source` remove do grafo o nó que o binding de modelo endereça, então
  o defeito é estrutural, não ausência de modelo. O mesmo mundo com a injection
  desligada passa até o image smoke — esse é o controle da linha.
- **interrupted-download** age no URL de *modelo*, não no binário do componente.

## Pesos de fixture

O profile `pepe` exige 12,45 GB de pesos ComfyUI. O catálogo-fixture soma ao
catálogo real três pesos de algumas dezenas de bytes, fixados no SHA-256
exatamente dos bytes que o transporte controlado serve. Download, verificação
de checksum e colocação via `ManagedModelPaths` são o caminho real sobre
arquivos reais: o que é fixture é o **tamanho** dos pesos, não o tratamento
deles.

## Modo controlado

Substitui só os seams com efeito colateral. O provisioner do Ollama recebe um
transporte controlado e um artifact fixture cujo SHA-256 é genuinamente o dos
bytes servidos — então o download, a verificação, a extração e a escrita do
marcador são o código real rodando sem alteração.

ComfyUI e Open WebUI precisam de `git` e `pip`. Um fake disso não provaria nada
sobre a instalação real, então um run controlado **não os instala**. A fase de
provisioning nomeia isso: `components.installed`, `components.declared` e
`components.not_attempted` vão no relatório, e a linha se chama *controlled
partial provisioning (Ollama only)*, não "clean-root install". A idempotência
que a matriz prova é a do que esse modo instala de fato — *partial controlled
stack idempotence* — medida em downloads e pulls antes e depois, não inferida
de um outcome que um dry-run nunca produz.

## Configuração gerada, adulterada e recusada

Dois cenários P0 precisam de um run que **chegue** à configuração gerada. O
profile `pepe` exige 12,45 GB de pesos ComfyUI, então num run controlado esses
roles não estão disponíveis e a configuração é bloqueada bem antes de existir
arquivo — resposta correta, mas não é sobre isso que esses dois cenários falam.

Por isso eles apontam `repo_root` para um repositório-fixture: `manifests/` e
`workflows/` são cópias literais dos reais; só o profile é escrito ali, e
profile é dado que o loader valida como qualquer outro. O fixture vive em
diretório próprio, fora do root managed, e some no teardown do cenário — nunca
pode ser confundido com algo que o run instalou.

- **`corrupted-config`** — `ControlledConfigWriter` é o seam `config-write`:
  herda `ManagedConfigWriter` e, só sob `CONFIG_TAMPERED`, edita o **corpo** do
  arquivo recém-escrito e deixa o cabeçalho intacto. O arquivo continua
  nomeando o manager e continua reivindicando o digest que geramos, então
  ownership sozinho não vê nada; quem vê é `verify()` recalculando o digest dos
  bytes em disco. O seam não calcula digest nenhum — há um teste por AST que
  falha se `sha256`/`content_digest` aparecer nessa classe.
- **`invalid-workflow`** — o profile-fixture habilita image generation e declara
  `text_to_image`, com os três modelos fixture AVAILABLE. `INVALID_WORKFLOW`
  remove o nó `75:70` do template e preserva o binding que o referencia.
  Materialização e verificação reais recusam o grafo estruturalmente inválido.
  Sem a injection, o mesmo profile chega ao image smoke. A ausência de roles é
  coberta separadamente e não representa esta linha P0.

Em ambos: fase falha fechada, nenhuma fase posterior roda (o smoke fica
`skipped`), e o cleanup continua correto.

## Providers reais

`real_world()` existe e **recusa** sem a capability de rede explícita. Nenhum
teste automático o chama — há um teste que varre `tests/e2e/` para garantir.
Nada na suíte depende da internet, da RTX 5070, do Ollama externo ou do
ComfyUI Desktop.

## Cleanup

Depois de todo cenário: nenhum processo managed residual, nenhum PID
pré-existente destruído, root efêmero removido. O runner verifica isso **de
fora** do run — o relatório dizer "passed" nunca é a prova sozinho.

Root configurado nunca é apagado. A assimetria é deliberada: esquecer de
limpar um efêmero deixa gigabytes; "limpar" um configurado destrói uma
instalação real.

## Performance

Mede, não otimiza. Nenhum cache — há um teste por AST que falha se
`lru_cache`/`cache` aparecer em `metrics.py`. O baseline conhecido de re-hash
(~26 s para 12,45 GB) é gravado como contexto, nunca como threshold.
