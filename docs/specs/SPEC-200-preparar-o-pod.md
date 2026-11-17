# SPEC-200 — Preparar o pod da Onda 2

| Campo | Valor |
|---|---|
| Onda | 2, fase A (local) |
| Cobre | OBS-02; infraestrutura de todos os experimentos da onda |
| Prioridade / esforço | P0 / P (meio dia) |
| Depende de | Onda 1 integrada (`74957bc`) |
| Desbloqueia | SPEC-201 e todos os experimentos (fase B) |
| Possui | `scripts/pod_session.py` (novo); seção 9 do `docs/PLANO.md` (receita); roteiro histórico da Onda 2 (pacote privado) (novo, roteiro do dia) |
| Pod | Não (prepara) |
| Status | aberto |

## PRD

**Problema.** Cada sessão de pod até agora custou tempo em passos manuais (copiar arquivos, achar caminhos, lembrar o que trazer) e perdeu dado importante: o `webui.db` nunca veio, então o texto das respostas do modelo não pôde ser lido. A Onda 2 tem uma sessão longa com seis experimentos; sem preparo, metade da janela de GPU vai para logística.

**Objetivo.** Um pod que sai do zero até o primeiro teste em um comando, e uma coleta que traz tudo o que os experimentos precisam, em um comando.

**Não-objetivos.** Automatizar a criação do pod no provedor (Lium); automatizar o uso do chat.

**Critérios de aceite.**

1. `scripts/pod_session.py up --host <h> --port <p> --alias <id>` faz os passos 1 a 5 da receita (seção 9): envia TAR e `.sha256`, confere, extrai, `deps/prepare/check`, instala com os tokens lidos do armazenamento local e enviados só por stdin, `start`. Retoma do ponto em que parou se rodado de novo.
2. `scripts/pod_session.py tunnel ...` mantém o túnel com reconexão (o laço de hoje), imprimindo o endereço local.
3. `scripts/pod_session.py sync-filter ...` faz o passo 6 (atualizar filtro e JS sem novo TAR) e reinicia os serviços.
4. `scripts/pod_session.py collect --out <pasta>` traz trace, personagens, uploads, entrada/saída do ComfyUI, logs **e o banco do Open WebUI** (caminho descoberto no pod, não suposto), e grava um `manifest.json` com tamanhos e SHA-256.
5. Nenhum token aparece em argumentos de processo, log ou saída.
6. roteiro histórico da Onda 2 (pacote privado): o roteiro do dia na ordem das SPEC-201 a 207, com tempos estimados e o que anotar.

## Spec técnica

- Usa `ssh`/`scp` do sistema, com as opções da receita (`HostKeyAlias`, `StrictHostKeyChecking=accept-new`, `ServerAliveInterval=30`). Sem dependência nova.
- Os tokens vêm de `local_ai_stack.civitai_auth.load_token()` e `huggingface_auth.load_token()`, escritos no stdin do `ssh`, como hoje.
- Estado local em `<pasta de saída>/pod-state.json` (passo concluído), para a retomada.
- O caminho do banco: procurar `webui.db` sob `$ROOT/data/open-webui` no pod; se não existir, `find $ROOT -name webui.db`.

## Testes

- Unitários com um `run` falso: a sequência de comandos de `up`, a retomada, que o token só aparece no stdin, a lista de caminhos do `collect`.
- Manual no próprio pod da SPEC-201.

## Tarefas

1. `pod_session.py` com os quatro subcomandos.
2. [P] Testes unitários.
3. [P] roteiro histórico da Onda 2 (pacote privado).
4. Atualizar a seção 9 do plano para apontar para o script.

## Não regredir

`scripts/capella-linux.sh` continua sendo o que roda no pod; o script novo só o chama.

## Notas da implementação

- Quinto subcomando `start`: o passo 5 da receita (segundo `start` depois de criar a conta) não cabia em `up`/`sync-filter`.
- `host`, `port` e `alias` ficam em `<out>/pod-state.json` e podem ser omitidos depois do `up`; o `collect` e o `sync-filter` também leem o release de lá.
- `sync-filter` envia todos os `.py/.js/.css` de `assets/open-webui` e copia cada um para todos os caminhos achados com `find` sob `$ROOT/apps/open-webui`.
- `collect` baixa o `-wal` e o `-shm` do banco, se existirem, e lista em `manifest.json` o que faltou no pod (`missing`). Inclui também `logs/hub-events.log` (SPEC-201, PERF-03).
- A tarefa 4 (seção 9 do plano) fica com o integrador.
