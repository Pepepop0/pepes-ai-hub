# Post-MVP test infrastructure integration

## Estado inicial e proveniência

- Master inicial: `590b38fbb091554eb3406f0f3172f2da5ae40654`.
- Harness aprovado: `agent-provisioning`, `a43d7f1d975b38523c533f6059c0db254c6c739f`.
- API aprovada: `agent-runtime`, `fc99bea17cecaa9807483574bd7a64d20b59d973`.
- `checkpoint-mvp`: `5475810e08274726731bc48cd5bd0dfbdc2ad3f8`, preservado.
- Únicos arquivos locais iniciais: `.serena/.gitignore` e `.serena/project.yml`;
  preservados, sem staging ou commit.
- Merge Harness sem squash: `df3db3841a2bf50e068f3ea3dc1afa316590e044`.
  Após ele: 168 testes focados aprovados, Ruff e diff-check verdes;
  `src/local_ai_stack/test_api` ainda inexistente.
- Merge API sem squash: `99d52a371d1df82b29a58356722ac4b765348d12`.
  Ambos concluíram sem conflitos.

## Composição

`python -m local_ai_stack.test_api` chama `create_test_api` em
`test_application.py`. A aplicação implementa `TestHarness`, cria um
`StackTestHarness` com world novo por run e devolve seu report ao scheduler.
O scheduler e o HTTP permanecem dependentes do contrato congelado.

A factory é composição Python confiável, não um campo de request. Ela permite
que testes escolham fixtures e injections sem dar ao cliente HTTP autoridade
sobre shell, executable, path, PID, URL, processo external ou provider real.
Nenhum contrato congelado foi alterado.

O start é opt-in. CONTROLLED/EPHEMERAL são os defaults; LOCAL_REAL permanece
fail-closed. A source de status copia observações do último run concluído e
identifica quando foram coletadas. Ela não chama domínios durante `GET /status`.
Instruções de token, loopback, requests e SSE estão em
[test-control-api.md](test-control-api.md).

## Defeitos concretos encontrados na composição

1. O contexto do report incluía `_plan_object` e `_plan_phases`, objetos de
   domínio que impediam serialização JSON em `GET /runs/{id}`. O harness mantém
   esses objetos no contexto de execução e publica apenas suas projeções.
2. A linha `cleanup-after-failure` aceitava cleanup falho com dois PIDs managed
   residuais. Agora a recusa de stop é transitória nessa fixture; a fase falha,
   o cleanup tenta novamente e precisa liberar árvore, membership e registry.
3. Um teste do entry point abandonava servidor/thread daemon. Agora o teste
   executa o caminho normal de stop e comprova o Harness real pela CLI.

## Evidências executáveis

`tests/test_api/test_integration.py` exercita HTTP loopback e o
`StackTestHarness` real. Nenhuma prova principal usa `ScriptedHarness`.

| Obrigação P0 | Evidência integrada |
|---|---|
| Harness 15/15 | Todos os `P0_SCENARIOS` passam por POST, scheduler, Harness, RunStore, GET e SSE; os verifiers originais e o inventário independente são conferidos |
| API: auth/locality | Credenciais ausentes/incorretas, auth antes de routing, token em query e bind não-loopback recusados antes da factory |
| API: authority validation | Campos shell, command, executable, path/root, PID, URL, kill e adoption recusados; nenhum world alocado |
| API: equivalência | One-phase e run completo comparados com execução direta; aquisição, configuração, chat/image smoke e segundo plano reais em providers controlados |
| API: eventos | Sequência monotônica, phase-state running/completed, diagnostics de falha, terminal único, leitores concorrentes, Last-Event-ID, reconnect e EOF após terminal |

As 19 obrigações P0 não são aprovadas por soma: a matriz inteira é executada
pela composição e complementada pelos casos de lifecycle e segurança.

| Lifecycle cruzado | Prova |
|---|---|
| A: run termina, API stop | Porta liberada, worker/handler encerrados, root removido, zero managed residual, registry/membership vazios |
| B: stop durante run | `ShutdownTimeout`, árvore ainda sob responsabilidade do harness, cleanup ao concluir e segundo stop liberando o listener |
| C: exceção em fase | FAILED no report e no GET, cleanup aprovado, diagnóstico por SSE e exatamente um terminal |
| D: leitor desconecta | Run continua; reconnect retoma após o cursor; cleanup não depende do leitor |

Cada teste de integração verifica também que nenhum recurso de outra execução
ou instalação external foi removido. As fixtures contabilizam roots, sockets,
workers e handlers; a suite E2E mantém seu ledger de diretórios criados.

## Validação e limites

Resultado final em Windows/Python 3.11.9:

- `pytest`: **1.781 passed**, 83,75 s, **zero warnings**.
- Harness P0: **15/15**; API P0: **4/4**; obrigações integradas: **19/19**.
- `tests/test_api/test_integration.py`: **31 testes aprovados**, incluindo
  os 15 cenários enviados por HTTP e os controles adicionais de composição.
- Ruff check e format-check: aprovados, **176 arquivos** formatados.
- Diretórios temporários do harness: **0 antes / 0 depois / delta 0**;
  ledger vazio e nenhum diretório preexistente removido.
- **164 starts de API** observados na suíte; zero portas ainda ocupadas,
  objetos API ativos, workers/handlers residuais, listeners novos ou processos
  filhos novos ao término.
- Matriz integrada: registry e membership vazios, handles lógicos liberados,
  zero managed residual e external/unrelated preservados após cada run.
- `git diff --check`: aprovado; contratos congelados e discovery global intactos.

Gates: `pytest`, `ruff check .`, `ruff format --check .`, `git diff --check` e
`git status`. A validação final mede delta dos diretórios `pepito-e2e-*`, dos
listeners e workers da API e dos processos filhos do runner. Nenhum teste
automático habilita providers locais reais ou workloads reais de GPU/rede.
HTTP dos testes fica em loopback.

O total P0 é 19 obrigações: 15 do Harness e 4 da API, com testes cruzados
adicionais. Job Object Win32 real, providers local-real, status da máquina real,
configured roots via API, cancelamento, persistência de runs e acesso remoto
continuam fora desta integração. Nenhuma feature de produto foi implementada.

`checkpoint-test-infra` é criado somente após gates e invariantes aprovados,
com as alterações de integração commitadas e `.serena/` preexistente excluída
do commit. Nenhum push faz parte desta rodada.
