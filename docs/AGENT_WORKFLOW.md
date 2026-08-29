# Workflow para múltiplos agentes

Nunca colocar Codex + dois Claudes na mesma working tree.

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
