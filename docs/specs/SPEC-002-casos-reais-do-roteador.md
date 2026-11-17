# SPEC-002 — Casos reais do roteador como fixtures de teste

| Campo | Valor |
|---|---|
| Onda | 0 |
| Cobre | ARQ-01 (base) |
| Prioridade / esforço | P0 / P (meio dia) |
| Depende de | SPEC-001 |
| Desbloqueia | SPEC-102, 103, 104, 105, 106, 112 |
| Possui | `scripts/router_cases.py` (novo), `tests/fixtures/router_cases.json` (novo), `tests/router_cases.py` (novo) |
| Pod | Não |
| Status | aberto |

## PRD

**Problema.** As correções do roteador precisam ser provadas com as respostas reais que deram errado. Hoje cada teste monta uma decisão à mão; três dos defeitos vieram de combinações que ninguém montou.

**Objetivo.** Um arquivo de casos extraído dos traces das sessões (texto do usuário, contexto mínimo, resposta crua do roteador, resposta do localizador quando houve), e um carregador que as specs usam nos testes.

**Não-objetivos.** Fixar o resultado esperado de cada caso: isso é de cada spec (e, ao final, da SPEC-112).

**Histórias.**

- Como quem implementa a SPEC-102, quero rodar o filtro com a resposta exata do roteador das 01:35:14 sem GPU nem Ollama.
- Como revisor, quero ver num só arquivo todos os turnos reais que o filtro já recebeu.

**Critérios de aceite.**

1. Dado um `turns.jsonl`, quando rodo `scripts/router_cases.py <turns.jsonl> --out <json>`, então cada turno com `router_result` vira um caso.
2. As fixtures cobrem, no mínimo, os turnos: 09/11 01:35:14, 01:47:08, 01:48:47, 01:49:37, 01:50:22, 01:51:18, 01:58:10, 01:58:25, 02:00:09, 02:02:01, 02:08:06, 02:09:11; 08/11 00:38:23 e 00:38:53; 09/11 01:36:04 (com `[Ignorar: …]`).
3. Nenhum caso contém bytes de imagem, URL de upload real ou id de usuário.
4. `tests/router_cases.py` oferece `load_cases(*ids)` e `run_case(case, monkeypatch)`, que executa o `inlet` do filtro com o Ollama, o localizador e o ComfyUI substituídos pelas respostas gravadas, e devolve `(body, metadata, trace_events)`.

## Spec técnica

**Formato de um caso** (`tests/fixtures/router_cases.json`):

```json
{
  "id": "2026-11-10T01:58:25",
  "session": "sessao-final-20261109-2312",
  "user_text": "a imagem toda",
  "history": [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}],
  "images": [{"number": 7, "by": "system", "current": true}],
  "model": "capella",
  "toggles": [],
  "router_raw": "{...resposta crua do roteador...}",
  "grounding_answer": null,
  "pending": null,
  "observed": {"action": "chat", "refusal": null}
}
```

- `history`: as mensagens anteriores do mesmo chat que estão no trace (`user_text` dos turnos anteriores e as respostas fixas registradas em `refusal`). As respostas livres do modelo não estão no trace; ficam como `"(resposta do modelo)"`.
- `grounding_answer`: o campo `answer` do evento `region_refused`, quando houver.
- `observed`: o que aconteceu na sessão (para comparação), nunca o esperado.
- **Texto explícito:** o arquivo vai para o repositório. Palavras explícitas que não mudam a decisão podem ser trocadas por equivalentes neutros, marcando `"edited": true`. Os casos acima quase não precisam.

**Carregador:** reutiliza o que já existe em `tests/test_open_webui_image_filter.py` (`load_module`, `routed_env`, `run`, `ui_metadata`, `with_plan`). Substitui `route()` (ou `_route_sync`) pela `router_raw` do caso, `ground_regions` pela `grounding_answer`, e falha o teste se algo tentar chamar o ComfyUI.

## Testes

- Um teste de fumaça: todos os casos carregam e rodam o `inlet` sem exceção.
- Um teste de higiene: nenhum caso tem `data:`, `/files/` ou um UUID.

## Tarefas

1. `scripts/router_cases.py`: leitura do JSONL, agrupamento por turno, montagem do histórico, remoção de URLs e ids.
2. [P] `tests/router_cases.py`: `load_cases`, `run_case`.
3. Gerar o JSON das quatro sessões; revisar à mão os textos (critério 3 e a regra do texto explícito).
4. Os dois testes acima.

## Não regredir

Nenhum código do produto muda.

## Notas da implementação

- **Ids em UTC, com a data do trace.** O id é o `ts` do `turn_start` (UTC) até o segundo. Os turnos citados no critério 2 como "09/11" e "08/11" estão no trace em `2026-11-10` e `2026-11-09` (as datas da spec eram as locais); o exemplo acima passou a `2026-11-10T01:58:25`. O turno "08/11 00:38:23" é o de `2026-11-09T00:38:14` ("Tente denovo"), cuja recusa foi registrada às 00:38:23.
- **Sessões sobrepostas.** `sessao-chat-pod-20261109-2220` está contida em `sessao-final-20261109-2312`; um id aparece uma vez, com a sessão passada primeiro ao script. São 51 casos.
- **O trace não guarda a lista de imagens do chat.** `images` é inferida: o maior número que o roteador cita (`references[].image`, `plan.base`, "imagem N" no texto) vira imagens enviadas pelo usuário; cada `image_job` acrescenta `outputs` imagens do assistente. Por isso o caso traz `"images_inferred": true`.
- **Campos além dos da spec**, todos para o `inlet` reproduzir o turno: `user_images` (números das imagens anexadas à mensagem atual), `history[].images` (as anexadas a cada mensagem anterior), `images[].generated` (imagem que o filtro criou do zero ou editou só a partir de imagens suas; decide o pedido de consentimento) e `images[].size` (do `image_job`/`swap_canvas`; sem ele, 1024×1536), `consent` (o usuário já respondeu "Confirmo") e `edited`.
- **`grounding_answer`** é o `answer` do `region_refused`; quando o localizador aceitou, é reconstruído do evento `region` (`status: "local"`, `confidence`, `boxes`). Sem nenhum dos dois, o localizador falha como um modelo visual inacessível.
- **`pending`** é a decisão do turno anterior do mesmo chat que terminou em recusa (a receita que "tente de novo" repete).
- **Texto explícito.** Os termos explícitos que mudavam o texto foram trocados por equivalentes neutros (`"edited": true`: 00:11:23 e 02:04:09 em diante, porque o histórico os repete); as demais palavras que não mudam a decisão ficaram.
- **`run_case`** executa o `inlet` real: o `urlopen` devolve `router_raw` para a chamada do roteador e falha para qualquer outra (visão do swap, medium); `ground_regions` usa o `region_mask` real sobre `grounding_answer`; `prepare_swap_job` é substituído por um trabalho mínimo; qualquer endereço fora do Ollama (ComfyUI) levanta `AssertionError`. `observed` é só comparação: o código atual do worktree já difere da sessão em alguns turnos (ex.: 02:00:09 recusa por "Não consegui isolar uma região local", a sessão por "muda a imagem inteira").
- **Testes:** `tests/test_router_cases.py` (cobertura dos 15 turnos, fumaça de todos os casos, higiene, geração a partir de um `turns.jsonl` sintético). Regerar: `python scripts/router_cases.py <turns.jsonl>... --out tests/fixtures/router_cases.json` (passe `sessao-final` primeiro).
