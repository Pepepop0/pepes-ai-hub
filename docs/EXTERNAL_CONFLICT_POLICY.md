# External Conflict Policy Foundation

Este documento congela o contrato compartilhado para conflitos com serviços
externos durante o first-run. A implementação do fluxo, a parada de processos e
a tela de escolha pertencem à próxima rodada.

O contrato canônico está em
`src/local_ai_stack/external_conflict_contracts.py`. Ele separa três conceitos:

- `InstallLocationMode` (`DEFAULT`, `PORTABLE`, `CUSTOM`) escolhe apenas o layout
  no filesystem;
- uma instalação externa detectada continua sendo observação, sem conceder
  ownership ao produto;
- um conflito existe quando um serviço externo ocupa um endpoint ou recurso
  necessário à tentativa atual.

Assim, um root `CUSTOM` e um Ollama instalado em outro diretório formam um
cenário válido. A instalação só precisa de resolução quando existe conflito de
runtime.

## Políticas

`INSTALL_ALONGSIDE` é a opção recomendada. A aplicação habilita o suporte já
existente de `IntegrationOptions.alongside_external` somente para os componentes
envolvidos e usa seus `port_overrides` determinísticos. O serviço externo não é
parado nem passa a ser managed. O contrato não aloca portas. Se o endpoint
managed alternativo também estiver ocupado, o preflight produz bloqueio duro e
um diagnóstico específico.

`STOP_CONFLICTING_EXTERNAL` exige escolha explícita e confirmação na futura
TUI. A escolha pública carrega somente o identificador opaco do preflight e a
policy. Ela não aceita PID, executável, path, nome de processo, endpoint ou
comando shell.

`ABORT` encerra o fluxo sem mutação.

## Autoridade para parar um serviço externo

A application layer mantém, de forma privada e limitada à tentativa atual, a
evidência que originou o conflito. Antes de parar algo, a implementação deve:

1. rejeitar identificadores de preflight antigos, desconhecidos ou já
   consumidos;
2. confirmar que o registro foi observado no preflight atual, tem origem
   `EXTERNAL`, pertence a um `ComponentId` conhecido e ocupa o recurso descrito;
3. confirmar que o usuário escolheu `STOP_CONFLICTING_EXTERNAL`;
4. revalidar imediatamente a identidade privada do processo e o conflito;
5. executar somente a operação de parada limitada do adapter daquele serviço;
6. repetir o preflight antes de prosseguir.

Se a identidade não puder ser provada, `can_stop_safely` é falso e a opção de
parada fica indisponível. O resolver não recebe autoridade fornecida pela UI e
não pode procurar processos arbitrários por PID, nome, path ou endpoint. A
observação externa também não deve ser inserida no registry managed para obter
autoridade artificial.

## Resultado de preflight

`ConflictPreflightReport` possui canais distintos:

- `READY`: não há conflito nem bloqueio;
- `RESOLUTION_REQUIRED`: há conflitos externos com pelo menos uma política
  comum segura;
- `BLOCKED`: existe bloqueio duro; somente `ABORT` pode ser escolhido.

O preflight atual deve ser projetado para esse contrato da seguinte forma na
próxima implementação:

| Observação atual | Projeção compartilhada |
| --- | --- |
| instalação externa sem conflito de runtime | observação; não entra como conflito ou bloqueio |
| `ALONGSIDE_REQUIRED` com colisão atribuída ao mesmo serviço externo | conflito resolvível |
| incompatibilidade, falta de espaço, falha de listener scan, path overlap ou endpoint duplicado | bloqueio duro |
| endpoint managed alternativo ocupado | bloqueio duro `managed-endpoint-occupied` |
| listener sem identidade suficiente | `can_stop_safely=false`; bloqueio duro se alongside também não for possível |

`ALONGSIDE_REQUIRED` deixa de ser, por si só, uma falha terminal do wizard. A
aplicação pausa antes de qualquer mutação, apresenta as políticas permitidas,
valida a escolha contra o snapshot atual e repete o preflight depois da
resolução.

## Contrato da TUI

A futura tela lê apenas a projeção segura: `service_id`, `kind`, `origin`,
`endpoint`, `reason` e `can_stop_safely`. Ela recomenda “Instalar ao lado”.
“Parar conflitos e instalar” só aparece habilitado quando a policy estiver em
`available_policies` e sempre pede confirmação. Cancelar seleciona `ABORT`.

O heading terminal é congelado por `installation_outcome_heading`:

- `SUCCEEDED`: `Concluído`;
- `FAILED`: `Instalação não concluída`.

Estados ativos não recebem heading terminal. A TUI não pode combinar
“Concluído” com uma mensagem de falha.

## Ownership da próxima rodada

Cada agente deve trabalhar em uma worktree própria, partindo do commit desta
foundation.

### Claude 1 — backend

- criar o resolver e o armazenamento privado da evidência em
  `src/local_ai_stack/external_conflicts.py`;
- adaptar `src/local_ai_stack/integration.py` para projetar bloqueios e conflitos;
- integrar policy, novo preflight e repetição em
  `src/local_ai_stack/first_run.py` e `src/local_ai_stack/stack_application.py`;
- expor a bridge em `src/local_ai_stack/hub_application.py`;
- cobrir o backend em `tests/test_external_conflicts.py`,
  `tests/test_first_run.py` e `tests/test_managed_coexistence.py`.

### Claude 2 — TUI

- consumir os tipos congelados em `src/local_ai_stack/tui/application.py`;
- implementar escolha, recomendação e confirmação em
  `src/local_ai_stack/tui/wizard.py` e `src/local_ai_stack/tui/app.py`;
- aplicar headings de sucesso e falha em
  `src/local_ai_stack/tui/presenters.py`;
- cobrir a integração visual e de input nos testes da TUI.

### Codex — integração

- manter este contrato e seus testes;
- revisar os limites de autoridade e os diffs das duas frentes;
- integrar as branches e executar os gates completos.

Os paths atribuídos às duas frentes não se sobrepõem. Mudanças de assinatura no
contrato compartilhado voltam para Codex antes da integração.
