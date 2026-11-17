# SPEC-317 — Regerar com a mesma semente

| Campo | Valor |
|---|---|
| Onda | 3 (complemento, pedido do usuário em 14/11) |
| Cobre | UI-08 |
| Prioridade / esforço | P1 / P |
| Depende de | SPEC-308 (receita por imagem, `POST /regenerate`) |
| Possui | Backend: `pepito_native.py` (modo novo), `pepito_image_chat.py` (`_regenerate`, mensagem fixa, semente da troca). UI: `pepito_image_ui.js`/`.css` |

**Pedido.** Em toda imagem gerada no Open WebUI, um botão "Regerar (mesma semente)" que repete a geração com a receita e a semente gravadas.

**Comportamento.**
- Com a mesma receita, o mesmo modelo e a mesma GPU, a saída do ComfyUI é normalmente idêntica. Serve para reproduzir uma imagem depois de mudar modelo, filtro, Valves ou workflow, e para conferir se algo mudou.
- "Regerar igual" (SPEC-308, semente nova) passa a se chamar "Regerar (nova semente)" na UI. A mensagem fixa e o modo `same` não mudam.
- "Regerar com este prompt" continua com a semente gravada.

## Contrato

- `POST /api/v1/pepito/.../regenerate` (a rota existente da SPEC-308) aceita `mode: "seed"`, com `step` opcional e sem `prompt`.
- Mensagem fixa: `REGENERATE_TEXT["seed"] = "Regerar a imagem {number} com a mesma semente"`. O filtro a reconhece como as outras duas, antes do roteador.
- No `_regenerate`, `mode == "seed"` usa o prompt gravado (como `same`) e mantém a semente gravada (como `edited`), em geração, edição, restage, redesenho e troca.
- **Troca:** a receita grava a semente do turno (`SwapComfyUI.seed`). `SwapComfyUI` aceita uma semente inicial, e a regeração com `seed` reusa a gravada; com `same`/`edited`, segue sorteando, como hoje.
- **Receita sem semente** (gravada antes desta spec, ou workflow sem nó de semente): `arm_regeneration` responde 409 com "Esta imagem não guardou a semente; use Regerar (nova semente).". A UI mostra a mensagem e não envia nada.
- `GET /recipe/{n}` informa `seed` (já existe). Quando a receita não tem semente, a UI desabilita o botão.
- Salvaguarda de idade, consentimento de referências, trace (`prompt_override`, com `mode: "seed"`) e calibragem seguem iguais às da SPEC-308.

## Critérios de aceite

1. Geração, edição com área, restage e troca: regerar com `seed` envia ao ComfyUI os mesmos nós de semente gravados na receita (testes no filtro com fakes).
2. `same` continua sorteando uma semente nova; `edited` continua mantendo a gravada.
3. Receita sem semente: 409 com a mensagem clara; nenhum job.
4. UI: item "Regerar (mesma semente)" no menu ⋯ de toda imagem gerada (galeria e visualizador) e no painel Prompt; imagens enviadas pelo usuário não o recebem. Desabilitado sem semente; a mesma recusa de envio da SPEC-308 (área anexada ou texto no compositor).
5. Filtro na versão 0.24.0; suíte completa verde; nativos no ambiente descartável.

## Notas da implementação (backend)

- `pepito_native.py`: `REGENERATE_TEXT["seed"]`, `RegenerateForm.mode` aceita `"seed"`. `recipe_seed` define a "semente utilizável": geração e troca, `recipe.seed`; edição/restage/redesenho (`kind: edit`), `recipe.patch.seed`. Só inteiro de 0 a 2^63-1 vale. `GET /recipe/{n}` informa essa mesma semente (`null` = sem semente, coerente com o 409) e `arm_regeneration(mode="seed")` recusa com 409 e a mensagem da spec, sem armar nada.
- `pepito_image_chat.py`: `_REGENERATE` reconhece a frase nova; `_regenerate` mantém a semente (`keep_seed`) em `edited` e `seed`, e `same` segue sorteando.
- **Troca:** a receita já gravava `client.seed` (semente do turno). `SwapComfyUI(seed=...)` aceita a semente inicial; só `mode == "seed"` a passa. A semente de cada etapa é `seed + SWAP_JOBS[etapa]` (referência 0, Qwen 1, Krea 2, análise da cena 3, segmentação Qwen 4, segmentação Krea 5, profundidade 6), a ordem de uma execução completa de hoje, então os números de execuções completas não mudaram. Antes era um contador de jobs, que numeraria diferente ao refazer só uma variação; agora cada etapa tem número fixo e a variação refeita sozinha reproduz a semente que a etapa teve.
- Desvio: `SwapComfyUI.run`/`run_outputs` passam a exigir `job=` (o contador `jobs` saiu). Se uma etapa falhava antes, os números seguintes deslocavam; agora não.
- Testes: `tests/test_prompt_selector.py` (geração, edição com e sem área, restage, troca por etapa, `same` nova, `edited` gravada, 409 sem semente, reconhecimento antes do roteador, `GET /recipe`).

## Notas da implementação (UI)

- `pepito_image_ui.js`: item "Regerar (mesma semente)" no menu ⋯ de toda imagem numerada (galeria e visualizador; imagens enviadas continuam só com Download). Ao clicar, lê `GET /recipe/{n}`: sem semente ou receita não refazível, mostra a mensagem no próprio menu e não envia nada; caso contrário, arma `mode: "seed"` e envia a mensagem fixa pelo compositor (`sendRegeneration`, com as mesmas recusas da SPEC-308). No painel Prompt, três ações: "Regerar com este prompt", "Regerar (mesma semente)" (desabilitada quando `seed` é `null`, com o motivo) e "Regerar (nova semente)" (o antigo "Regerar igual").
- Desvio: no menu o botão não fica desabilitado antes do clique, porque o menu não carrega a receita ao abrir; a recusa aparece no clique.
- Prova local (Chromium, superfície sintética com a API falsa): galeria → "Regerar (mesma semente)" na imagem 1 enviou `{mode: "seed", step}` e a mensagem "Regerar a imagem 1 com a mesma semente"; na imagem 2, sem semente, mostrou a recusa e não enviou; no painel Prompt da imagem 2 o botão ficou desabilitado com o motivo; na imagem 1 regerou e fechou o visualizador. Falta conferir no Open WebUI real com GPU.
