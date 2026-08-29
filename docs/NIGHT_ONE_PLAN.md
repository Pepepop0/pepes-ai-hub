# Plano da primeira noite

## Meta

Base integrável, não produto final.

### 0:00–0:45 — freeze
Revisar PRD/SPEC/profiles/ownership e commit inicial.

### 0:45–3:00 — paralelo
- Codex: schema, TUI, compatibility, interfaces.
- Claude Provisioning: detector, runner, provisioning.
- Claude Runtime: health, process manager, shortcut, integration research.

### 3:00–4:00 — integração
Commits pequenos, merge por módulo, resolver interfaces.

### 4:00–5:30 — smoke test Pepe
Clone limpo, bootstrap, TUI, Doctor, profile Pepe e subset seguro.

### 5:30–6:30 — estabilização
README, TODOs, known issues, tag opcional.

## Stop conditions

Documentar em vez de improvisar se Open WebUI exigir hack de DB, privilégios inesperados, origem duvidosa de download, reescrita arquitetural ou bug consumir >45 min sem reduzir incerteza.

## Vitória mínima

`clone -> bootstrap -> TUI -> detect -> select Pepe -> plan -> doctor` funciona.
