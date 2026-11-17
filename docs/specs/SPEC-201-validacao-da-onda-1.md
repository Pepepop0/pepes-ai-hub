# SPEC-201 — Validação da Onda 1 no pod

| Campo | Valor |
|---|---|
| Onda | 2, fase B (pod) |
| Cobre | Validação de CHAT-01..07, IMG-01, IMG-05, UI-01, UI-02, PERF-03, OBS-01, SEC-01 |
| Prioridade / esforço | P0 / P (cerca de 1 h de pod) |
| Depende de | SPEC-200; fase A da Onda 2 integrada (para não precisar de dois pods) |
| Desbloqueia | SPEC-210 (o que falhar volta como correção) |
| Possui | Seção "Hub 1.19.0 (filter 0.21)" de `docs/testing/capella-5090-checklist.md`; o relatório da sessão |
| Pod | Sim |
| Status | aberto |

## PRD

**Problema.** Tudo da Onda 1 foi provado com fakes e com os turnos gravados. Nenhum item rodou com o modelo de 27B, o ComfyUI e o navegador de verdade.

**Objetivo.** Cada pendência marcada "feito sem pod" passa a "validado" ou volta como defeito com evidência (trace, log, banco).

**Critérios de aceite** (um por bloco; o roteiro detalhado está no checklist):

| Bloco | Pendências | Passa quando |
|---|---|---|
| Subida | PERF-03 | Primeiro turno depois de "Iniciar" com roteador < 12 s; turno após 10 min parado também < 12 s; `chat preload elapsed` no `hub-events.log` |
| Chat com Image ligado | CHAT-01, 05, 06 | 30 turnos de conversa e perguntas sobre imagens: 0 respostas pedindo para ligar o botão (conferido no banco); promessas recebem a nota |
| Pergunta do roteador e resposta curta | CHAT-02, 03 | Pelo menos 3 perguntas reais respondidas em poucas palavras viram a imagem pedida (`pending_answered` ou `router_reconciled`) |
| Pedido global | CHAT-04 | As três mensagens de 09/11 geram imagem no mesmo turno |
| Texto ignorado | CHAT-07 | Os dois formatos funcionam; mensagem só com nota responde "Anotado." |
| Esc | UI-01 | Os 4 critérios da SPEC-107 no navegador |
| Visualizador | UI-02 | Sua avaliação do zoom e do fundo (depois da SPEC-209) |
| Emenda | IMG-01 | 6 edições com área no Krea: você não aponta corte reto; `band_step_final ≤ 1,25 × band_step_source` em todas |
| Trace | OBS-01, IMG-05 | Todos os campos novos presentes em todos os turnos da sessão (script de conferência da SPEC-200 ou um `jq`) |
| Convidado | SEC-01 | Um segundo usuário (link de convidado) faz uma edição; nenhum aviso "picture read outside a turn" no log |

**Saída:** `relatorio-validacao.md` na pasta da sessão, com uma linha por bloco (passou / falhou + evidência) e a lista de defeitos com o id da pendência.

## Tarefas

1. Rodar os blocos na ordem do roteiro histórico da Onda 2 (pacote privado).
2. Coletar com `pod_session.py collect`.
3. Escrever o relatório e atualizar os status no `PLANO.md`.

## Não regredir (conferir na mesma sessão)

Troca de personagem T1 e T3; edição de forma corporal (a body-shape edit) com área automática no Qwen; restage; geração nova; "tente de novo" após recusa.
