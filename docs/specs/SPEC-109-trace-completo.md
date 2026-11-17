# SPEC-109 — Trace completo e semente registrada

| Campo | Valor |
|---|---|
| Onda | 1, raia I |
| Cobre | OBS-01, IMG-05; a medição do SWAP-02 |
| Prioridade / esforço | P1 / P (1 dia) |
| Depende de | Brandas: SPEC-102, 104 e 108 integradas antes (mesmo arquivo) |
| Desbloqueia | A Onda 2: sem estes campos o pod não mede IMG-03, SWAP-02 nem PERF-01 |
| Possui | Eventos `image_job` (`pepito_image_chat.py:5716`, `:6135`, `:6249`), `swap_method`, `turn_end` (`:481`), `answer_usage`; a escolha de semente (`:7514-7515`); evento novo `image_change`; evento novo `answer_text` no `outlet` |
| Pod | Validar |
| Status | implementada |

## PRD

**Problema.** Na pesquisa de 10/11, quatro perguntas exigiram engenharia reversa porque o trace não tinha o dado: a semente efetiva (`seed: null`), o texto da resposta do modelo (itens 1 e 5), o tempo de cada etapa da troca (70 a 252 s sem decomposição) e a mudança medida (só no log do Open WebUI).

**Objetivo.** Cada turno de imagem registra o suficiente para explicar o resultado sem abrir código nem imagens.

**Critérios de aceite.**

1. Todo `image_job` tem `params.seed` numérico. O filtro escolhe a semente para toda parte cujo grafo tem a ligação `seed` (hoje só escolhe com `edit_method` selecionado, `:7514`).
2. Todo `image_job` tem `unload_s` (descarga do Ollama), `job_s` (o atual `seconds`) e `post_s` (composição, preservação, gravação).
3. Todo `swap_method` tem `gen_s`, `segment_s`, `depth_s`, `compose_s`.
4. Evento `image_change` com o valor de `measured_change` de cada imagem entregue, no lugar (ou além) da linha de log de `:6275`.
5. Evento `answer_text` no `outlet` com a resposta do modelo, cortada em `TRACE_TEXT_CHARS` e passando pela mesma sanitização (`_trace_value`).
6. Nos jobs feitos pelo cliente próprio da troca (`SwapComfyUI`), o `prompt_id` do ComfyUI e o nome do arquivo de saída.
7. Nada disso aparece no log do Open WebUI: só no trace (privado).

## Spec técnica

- Semente: `patch["seed"] = uuid.uuid4().int % (2**63)` sempre que `"seed" not in patch` e a parte tem a ligação. As variações continuam somando o número da variação (`:6225-6226`). O valor de `preferences["comparison_seed"]` continua tendo prioridade.
- Tempos: `time.monotonic()` em volta de cada etapa; arredondar em 3 casas, como `turn_end`.
- `answer_text`: no `outlet`, junto de `answer_usage`.

## Testes

Em `tests/test_image_gpu_serialization_and_trace.py`: um turno de edição e um de troca com fakes; conferir presença e tipo de cada campo; conferir que o log não recebe o texto da resposta (o teste `test_the_trace_never_holds_credentials_or_picture_bytes` continua passando).

## Tarefas

1. Semente.
2. [P] Tempos do `image_job`.
3. [P] Tempos do `swap_method` e `prompt_id`.
4. [P] `image_change`, `answer_text`.
5. Testes.

## Riscos

O trace cresce (uma resposta por turno, até 8.000 caracteres). A rotação em 20 MB já existe. Privacidade num pod com convidado: SEC-03 (Onda 3).

## Notas da implementação

- `unload_s` soma o tempo de `unload_ollama(metadata)` no contexto do trace; `image_job` leva `unload_s`, `job_s` (substitui `seconds`) e `post_s`, emitido ao fim do pós-processamento. Na troca, `job_s` vai até o início da composição e `post_s` cobre composição, gravação e anexo.
- Semente: a escolha é por ligação `seed` do grafo da parte (edição e geração; geração soma o número da chamada). Uma repetição pura não reaproveita mais a semente da receita (repetiria a imagem); `comparison_seed` segue com prioridade.
- Troca: `swap_method` sempre tem `gen_s`, `segment_s`, `depth_s`, `compose_s` (`null` se a etapa não foi alcançada). `SwapComfyUI` usa uma semente por turno (`params.seed` do `image_job`; cada job soma seu número) e escreve o evento `swap_comfy_job` (`step`, `prompt_id`, `seed`, `output_files`, `seconds`, `status`).
- `image_change` (`measured`: lista) substitui a linha de log; `answer_text` no `outlet`, só no trace.
