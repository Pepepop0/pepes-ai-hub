# E2E harness

Implementa o `TestHarness` de `test_contracts.py`. Orquestra os domínios
existentes; não reimplementa nenhum deles.

## Módulos

| Arquivo | Papel |
|---|---|
| `e2e/harness.py` | `StackTestHarness`: fases, cleanup em `finally`, montagem do relatório |
| `e2e/world.py` | `ControlledWorld`: todas as dependências substituíveis, amarradas uma vez |
| `e2e/providers.py` | Os fakes: runner, transporte HTTP, store Ollama, probes, inventário, launcher, relógio |
| `e2e/seams.py` | `Injection` e `InjectionPlan` — falhas nomeadas, determinísticas |
| `e2e/roots.py` | Root efêmero (removido) vs configurado (nunca apagado) |
| `e2e/metrics.py` | Medição monotônica; amostras cruas + count/mediana/máximo |
| `e2e/scenarios.py` | Matriz P0 e o runner que verifica invariantes de fora |

## O que o harness não faz

Ownership, verificação, classificação external e autoridade de path continuam
nos domínios. O harness compõe `preflight_managed_stack`, `build_install_plan`,
`stage_managed_plan`, `plan_executor`, `StackModelResolver`,
`detect_integration` e `build_service_controller`, e **lê** as respostas.

Um teste garante isso: `harness.py` não contém `taskkill`, nem cálculo de
checksum, nem decisão própria de "é external".

## Regras de fase

- Falha para o run inteiro.
- **Bloqueio para só as fases mutantes.** É o que o contrato pede em
  "GPU phase is blocked before workload while CPU/read-only phases stay
  available": sem CUDA, `plan` continua disponível.
- Uma fase bloqueada **carrega seus diagnósticos**. Sem isso, um cenário não
  consegue provar *por quê*, que é a maior parte do valor.
- `cleanup` roda de um `finally`, sempre, e reporta independente. Uma falha de
  cleanup nunca transforma uma falha anterior em passe.

## Modo controlado

Substitui só os seams com efeito colateral. O provisioner do Ollama recebe um
transporte controlado e um artifact fixture cujo SHA-256 é genuinamente o dos
bytes servidos — então o download, a verificação, a extração e a escrita do
marcador são o código real rodando sem alteração.

ComfyUI e Open WebUI precisam de `git` e `pip`. Um fake disso não provaria nada
sobre a instalação real, então um run controlado **não os instala** e diz isso
no relatório (`installable_components`), em vez de fingir.

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
