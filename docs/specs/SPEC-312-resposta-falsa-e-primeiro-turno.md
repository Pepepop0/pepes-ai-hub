# SPEC-312 — Resposta que finge imagem; primeiro turno lento

| Campo | Valor |
|---|---|
| Onda | 3 |
| Cobre | CHAT-13 (P13), PERF-07 (P12) |
| Prioridade / esforço | P2 / P |
| Possui | A guarda de promessa/afirmação no `_stream`/`outlet` (`_PROMISE`, `promises_picture`, `claimed_success` e vizinhos) em `pepito_image_chat.py`; `preload_ollama_model` em `src/local_ai_stack/ollama_models.py` |

## PRD
1. **CHAT-13:** quando a resposta do modelo afirma que uma imagem foi feita/anexada ("aqui está ela… [imagem anexada]") e nenhuma imagem existe no turno, a resposta inteira é **trocada** por um aviso curto (não só acrescentar a nota). Caso real: 2026-11-12T23:36:11 (texto em `answer_text` do trace).
2. **PERF-07:** a pré-carga gera 1 token (`num_predict: 1`, prompt mínimo) em vez de só carregar, para aquecer a primeira inferência (12/11: primeiro turno 32,4 s com o modelo residente; os seguintes 5–7 s). Medir no pod.

## Notas da implementação
- O `outlet` já trocava a resposta inteira: no `webui.db` do pod de 12/11 a mensagem das 23:36:11 ficou salva como `NO_PICTURE_NOTICE`, com o texto do modelo em `originalContent`. O `answer_text` do trace é gravado antes dessa troca. O que o usuário via era o stream: o texto falso inteiro e a nota no fim, até o `outlet` trocar.
- Stream (`_stream`, Image desligado): `_CLAIMED_PICTURE` passa a pegar "aqui está/estão ela(s)/ele(s)", "segue a/sua imagem|foto|versão", "here she/he is", as notas do próprio filtro desde as primeiras palavras ("[imagem editada…", "[2 imagens geradas…", "[imagem 3 anexada de novo…") e "anexada de novo pelo sistema]". O caso real cai na abertura retida (`CLAIM_PREFIX_CHARS`) e o stream mostra só o aviso.
- Afirmação depois da abertura: o stream para ali (nada mais do texto aparece) e termina com `\n\n` + `NO_PICTURE_NOTICE` (`claim_replacement`), em vez de acrescentar `CLAIM_NOTICE`; o `outlet` troca a resposta inteira pelo aviso (`state["claimed"]`). Um `[` aberto é retido até `BRACKET_HOLD_CHARS` (48) para a nota "[imagem … pelo sistema]" não aparecer pela metade. Promessas (`promises_picture`) continuam ganhando a nota no fim.
- Pré-carga: `preload_ollama_model` manda `prompt: "Oi"` (`PRELOAD_PROMPT`), `stream: false` e `options.num_predict: 1` (mais o `num_ctx` do perfil).
- Pod: medir o primeiro turno depois de "Iniciar" (`turn_end.total_seconds`, `answer_usage`) contra os 32,4 s de 12/11 e conferir o tempo em `chat preload elapsed=` no `hub-events.log`; pedir uma imagem com Image desligado e ver se o texto falso aparece durante o stream.
