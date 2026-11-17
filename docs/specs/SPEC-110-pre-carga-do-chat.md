# SPEC-110 — Pré-carga e permanência do modelo de chat

| Campo | Valor |
|---|---|
| Onda | 1, raia G |
| Cobre | PERF-03, DEC-08 |
| Prioridade / esforço | P1 / P (1 dia) |
| Depende de | SPEC-001 |
| Desbloqueia | SPEC-113 |
| Possui | Chaves novas `runtime.preload_chat_model` e `runtime.chat_keep_alive` em `schemas/profile.schema.json` e `profiles/capella.yaml`; a pré-carga no lado do Hub (`src/local_ai_stack/stack_application.py`, depois da prontidão dos serviços em `start_stack`, `:945`, / `_start_services`, `:1406`); `OLLAMA_KEEP_ALIVE` no ambiente do Ollama (`adapters.py:96`) |
| Pod | Validar (medir) |
| Status | aberto (premissa DEC-08) |

## PRD

**Problema.** O primeiro turno de cada sessão esperou 41 a 57 s pelo roteador, porque o modelo de 20 GB carregava do disco. Depois de 18 min parado, o modelo tinha saído da GPU (padrão do Ollama: 5 min) e o turno levou 10 s.

**Objetivo.** O primeiro turno depois de "Iniciar" e os turnos depois de uma pausa respondem no tempo de modelo residente (mediana 6,3 s).

**Não-objetivos.** Mudar a alternância chat ↔ imagem: o filtro continua descarregando o Ollama antes de cada imagem.

**Critérios de aceite.**

1. Dado um perfil com `preload_chat_model: true`, quando "Iniciar" termina, então o Hub envia ao Ollama uma requisição vazia de carga com o modelo de chat primário e o `num_ctx` dos parâmetros do perfil (16384 na Capella).
2. A pré-carga roda em segundo plano: "Iniciar" não espera por ela; o resultado e o tempo vão para `logs/hub-events.log` (`chat preload elapsed=...s` ou a falha).
3. Falha na pré-carga não falha o "Iniciar".
4. Dado `chat_keep_alive: "60m"`, o processo do Ollama recebe `OLLAMA_KEEP_ALIVE=60m`.
5. Perfil sem as chaves: nada muda.
6. No pod: primeiro turno com o roteador abaixo de 12 s.

## Spec técnica

- Requisição: `POST {ollama}/api/generate` com `{"model": <primário>, "keep_alive": <chat_keep_alive ou omitido>, "options": {"num_ctx": <perfil>}}`, sem `prompt` (forma documentada na [FAQ do Ollama](https://docs.ollama.com/faq)).
- O nome do modelo vem do mesmo lugar que o Hub usa para o registro do Open WebUI (perfil → modelo Ollama).
- Schema: `preload_chat_model` boolean; `chat_keep_alive` string no formato do Ollama (`"60m"`, `"-1"`).
- Capella: `preload_chat_model: true`, `chat_keep_alive: "60m"`.
- Linux: conferir se `scripts/capella-linux.sh start` passa por `start_stack`; se não, aplicar no caminho equivalente de `posix_runtime.py`.

## Testes

- Ollama falso (padrão dos testes de serviço): critérios 1, 3 e 5; o corpo da requisição tem `num_ctx` 16384.
- Ambiente do processo: critério 4 (teste do adaptador).
- `tests/test_profiles.py`: o schema aceita e recusa valores inválidos.

## Tarefas

1. Schema e perfil.
2. [P] `OLLAMA_KEEP_ALIVE` no adaptador.
3. [P] Pré-carga em segundo plano depois da prontidão, com o evento.
4. Testes; conferir o caminho Linux.

## Riscos

20 GB de VRAM ocupados com o Hub parado. Na máquina do cliente é o uso esperado da placa; num computador compartilhado, desligar no perfil.
