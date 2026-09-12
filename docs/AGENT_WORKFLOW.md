# Workflow para múltiplos agentes

Nunca colocar Codex + dois Claudes na mesma working tree.

## Wave A — operações e TUI

A divisão abaixo registra o ownership do MVP. Para a próxima Wave A, seguir os
caminhos e contratos congelados em [Operation Contract Foundation](OPERATION_CONTRACT.md#wave-a-ownership):
Claude 1 assume geração/recursos; Claude 2 assume TUI e sua composição em `app.py`;
Codex mantém contratos e integração. Cada frente parte do commit da foundation
em sua própria worktree.

Para a rodada de conflitos externos do first-run, o contrato, os limites de
autoridade e o ownership por arquivo estão congelados em
[External Conflict Policy Foundation](EXTERNAL_CONFLICT_POLICY.md#ownership-da-próxima-rodada).

## Worktrees

```powershell
git init
git add .
git commit -m "chore: initial project skeleton"

git branch agent-provisioning
git branch agent-runtime

git worktree add ..\local-ai-provisioning agent-provisioning
git worktree add ..\local-ai-runtime agent-runtime
```

- repo principal: Codex / lead;
- provisioning worktree: Claude 1;
- runtime worktree: Claude 2.

## Ownership

### Codex
TUI, profiles/schema, compatibility, contracts, integração e testes de contrato.

### Claude 1
Detector, command runner, downloads, git, installers, filesystem layout.

### Claude 2
Process manager, health, launcher, shortcut, status e adapter/pesquisa Open WebUI/ComfyUI.

## Disciplina

Commits pequenos; uma responsabilidade por commit; testes antes do merge; lead revisa diff; sem reset/force-push em branch alheia; interface-first.
