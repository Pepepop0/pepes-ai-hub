# SPEC-319 — Duas versões lado a lado (E e C), preferência do usuário e entrega sem colagem

> Registro técnico SFW. As referências históricas a `edit_lab.py`, seus testes e fontes experimentais pertencem ao pacote privado e não são comandos ou fixtures disponíveis no público. A implementação pública de métricas e avaliação compartilhadas é `scripts/lab_common.py` / `scripts/lab_review.html`.

| Campo | Valor |
|---|---|
| Onda | 3 (pedido do usuário em 14/11, após LAB-03) |
| Cobre | CHAT-14, IMG-13 (parcial), UI-10 (novo) |
| Prioridade / esforço | P0 / M |
| Depende de | LAB-03 (material do pacote privado), SPEC-308/317/318 |
| Possui | Backend: `assets/open-webui/pepito_image_chat.py`, `assets/open-webui/pepito_native.py`, `scripts/edit_lab.py` (só a troca do construtor do C). UI: `assets/open-webui/pepito_image_ui.js`/`.css` |

## Pedido

1. Toda edição gera **duas versões**: uma com o prompt do escritor **E** (revisão sem raciocínio, ~1,3 s) e outra com o do **C** (revisão com raciocínio, ~11 s). As duas aparecem lado a lado; o usuário clica "Prefiro esta". A escolhida vai para a frente e vira a imagem usada daqui em diante; a outra continua acessível no histórico/galeria, como hoje.
2. O lado a lado com "Prefiro esta" vale para **toda galeria com exatamente duas imagens** (edição com E/C, troca de personagem Krea/Qwen, pedido de 2 imagens).
3. Nas edições com área, entregar a **saída inteira do modelo**, sem colar só a área (o LAB-03 mostrou a saída bruta preferida à colagem em A2, A3, B2 e P1).

## Contrato — backend

### Escritores (só o código do filtro; o lab passa a importar daqui)
- **C** = o pedido que o lab usou como `vision_think` em `scripts/edit_lab.py::prompt_request`: `router_messages` do histórico, a imagem atual anexada, a linha que congela a decisão ("action, mode and sources are frozen…"), `router_schema`, `think: true`, `temperature 0.2`. Mover esse construtor para o filtro como `frozen_route_payload(model, history, number, image_bytes, decision, style, capabilities, num_ctx)` e fazer o lab chamá-lo (uma fonte só). A resposta é o JSON do roteador; usa-se só `prompt`; `minor_risk` verdadeiro bloqueia.
- **E** = `prompt_revision_payload(..., think=False)` (já no filtro), com a área contornada quando houver máscara. Antes de usar, corrigir `PROMPT_REVISION_SYSTEM` com os três achados do LAB-03:
  1. grau e cor: forte e explícito ("much paler", "very pale"); comparações relativas só como complemento, nunca sozinhas;
  2. nunca listar em "Keep … unchanged" algo que a mudança precisa tocar (ex.: "the top" quando o top estica);
  3. não nomear peças ou partes do corpo dentro da área para remover/alterar se o usuário não as citou; nunca remover braços, mãos ou partes do corpo.

### Fluxo de uma edição
- Valve de usuário `prompt_pair: bool = True` ("Duas versões por edição (E e C)"). Vale para `ACTION_EDIT` com uma imagem-fonte nas partes `edit`, `restage`, `remove` e redesenho. **Não** vale para troca (`swap`), deformação (`edit_method` warp*), regeração da SPEC-308/317/318, geração nova (`ACTION_NEW`), nem quando o usuário pediu explicitamente N imagens (`requested_count` > 1: segue como hoje).
- Os dois prompts são escritos **depois do roteador e antes de descarregar o Ollama** (o modelo de chat ainda está na placa): E primeiro, depois C. A salvaguarda de idade (`minor_risk_rules`) roda nos dois.
- Falhas: C falhou (timeout, JSON inválido, sem prompt) → entrega só E e registra; E falhou → usa o prompt do roteador na versão 1. Nunca inventar prompt.
- `edit_chat_image` ganha `prompts: list[str] | None`. Com `prompts`, `variations = len(prompts)`, cada variação usa o seu prompt, **a mesma semente** (offset 0 nas duas) e a mesma força; variação 1 = E, variação 2 = C. O auto-retry de "quase nada mudou" não roda em par.
- Cada receita gravada (SPEC-308) leva o seu prompt e `writer` (`revise`/`vision_think`/`router`).
- Trace: evento `prompt_pair` com `writers`, `seconds` por escritor, `fallback` e os tamanhos dos prompts.
- Texto fixo da resposta: "2 versões da edição: escolha a sua favorita com «Prefiro esta»." (sem "usa a 2").

### Entrega sem colagem
- Valve `area_paste: bool = False` ("Colar só a área pintada"). Desligada (padrão): edições com área que não sejam deformação **não usam recorte** (`crop_area`) e **não colam** (`keep_outside_mask`): entrega a saída inteira; a área continua indo ao modelo e à receita; `quality_measurements` continua no trace. Ligada: comportamento de hoje.
- Deformação (warp) e troca continuam colando sempre.

### Preferência (nativo)
- `POST /preference {chat_id, number}`: só o dono; a imagem precisa estar numa galeria (grupo) com 2+ imagens; grava `preferred_number` do grupo e chama a seleção existente (`select_image`), então a escolhida vira a base das próximas edições. Responde `{group_id, preferred_number, selected_number}`.
- `GET /gallery/{group}` passa a devolver `preferred_number` (ou `null`).
- Trocar a preferência depois é permitido (último clique vale).

## Contrato — UI
- Galeria com exatamente 2 imagens e sem `preferred_number`: modo comparação, as duas lado a lado (empilhadas em tela estreita), cada uma com "Prefiro esta"; clicar na imagem abre o visualizador como hoje.
- Ao clicar "Prefiro esta": `POST /preference`; a galeria volta ao modo normal (uma imagem por vez) com a escolhida na posição 1 e a outra na 2 (‹ › e contador continuam), selo "Preferida" na escolhida e o rótulo de seleção atualizado ("Usar esta · selecionada"). Recarregar a página mantém o estado (vem de `preferred_number`).
- No menu ⋯ da galeria em modo normal, "Comparar lado a lado" volta ao modo comparação sem desfazer a preferência.
- Galerias com 1 ou 3+ imagens: sem mudança.

## Critérios de aceite
1. Edição com uma fonte: dois jobs, mesma semente, prompts de E e C; receitas com `writer`; trace `prompt_pair`. C falhando → 1 imagem (E) e registro.
2. Troca, deformação, geração nova, regeração e pedido de N imagens: como hoje.
3. `area_paste` desligado: edição com área entrega a saída do modelo (bytes iguais ao que o ComfyUI devolveu), sem recorte; ligado: igual a hoje.
4. `POST /preference` só do dono, grava e seleciona; `GET /gallery` devolve `preferred_number`.
5. UI: comparação lado a lado, "Prefiro esta", reordenação, estado após recarregar, "Comparar lado a lado".
6. O lab continua gerando o mesmo pedido de C (teste do lab).

## Notas da implementação (UI)
- `upgrade` lê `preferred_number` do `GET /gallery`. Só vale em galeria com exatamente 2 imagens; a escolhida vai para a posição 1 (`arrangeEntries`). Com 2 imagens e sem preferência, `mount.compare = true`.
- Modo comparação: `buildGallery` monta `.pepito-compare` (duas células: imagem que abre o visualizador, rótulo "Imagem N" e botão "Prefiro esta" com `aria-label` "Prefiro esta: imagem N"). `mount.show` carrega as duas fontes (`mount.resource` e `mount.extra`) e as libera juntas em `pause`, troca de modo e `destroyMount`. Lado a lado até 700 px de largura; abaixo disso empilha. Setas do teclado e overlay (Editar/⋯) ficam desligados nesse modo.
- "Prefiro esta" → `POST /preference {chat_id, number}` (mesmas guardas de `select`: envio em andamento e área anexada de outra imagem). A resposta atualiza `mount.preferred`, a ordem e a seleção (`setSelection`, rótulo "Usar esta · selecionada"); a galeria volta ao modo normal com o foco na imagem. Erro aparece no status da galeria e não muda o modo.
- Selo "Preferida" (`.pepito-badge`) na imagem preferida no modo normal.
- Menu ⋯: "Comparar lado a lado" (`imageActions`, 7º parâmetro `compare`) só aparece em galeria de 2 imagens no modo normal; volta à comparação sem mexer na preferência. O visualizador não o mostra.
- Galerias de 1 ou 3+ imagens: caminho antigo, sem selo nem item de menu.
- Provado no Chromium numa superfície sintética com API falsa: comparação → "Prefiro esta" na 2 (POST `{chat_id, number: 2}`) → vira 1/2 com selo e "Usar esta · selecionada" → recarregar mantém → "Comparar lado a lado" volta; 1 e 3 imagens inalteradas; a 420 px empilha.

## Notas da implementação (backend)

- `frozen_route_payload` mora no filtro; `scripts/edit_lab.py::prompt_request` o chama (teste do lab inalterado).
- Os dois prompts são escritos em `_routed`, depois de `prepare_local_preservation` (a área já existe, para o E) e imediatamente antes de `unload_ollama`. O rascunho dos dois é o prompt do roteador (`decision`), como no lab; o prompt de fallback da versão 1 é o do filtro (`job["prompt"]`).
- Elegibilidade (`pair`): valve ligada, `ACTION_EDIT`, uma fonte, partes `edit`/`restage`/`remove` (o redesenho `edit_free`/`qwen` roda na parte `edit`), sem `warp*`, sem referência automática e `variations <= 1`, isto é, sem N pedido pelo usuário nem N gravado numa receita. O par grava `requested_count = 1` na receita (a contagem não foi pedida), então "refaça" volta a ser um par.
- Mesma semente = `offset` 0; mesma força = `strengths[0]` repetida (calculada como para uma imagem). `edit_chat_image` ganhou `prompts`, `writers` e `area_paste`; o auto-retry não roda em par.
- C com `minor_risk` verdadeiro, ou qualquer dos prompts reprovado por `minor_risk_rules`, recusa o turno inteiro (como a salvaguarda de hoje), em vez de entregar só um lado.
- O texto fixo chega ao modelo como instrução (`PAIR_TELL`), no mesmo canal de `NEW_PICTURE_TELL`.
- `area_paste` desligado: sem `crop_area`, sem `keep_outside_mask`, sem `raw_output` (não há colagem a guardar); `restore_proportion` do Qwen roda também com área. A regeração (SPEC-308/317) lê a mesma valve. Deformação (`warp*`) cola sempre.
- Nativo: `preferred:<grupo>` na tabela `state` do SQLite do chat; o grupo é o mais recente com 2+ imagens que contém o número.
- Testes: `tests/test_prompt_pair.py` (pares, quedas, bloqueios, exclusões, saída inteira, preferência). Testes antigos sobre a colagem ligam `area_paste` (`EditTurn.valves`); os do auto-retry e de `router_requests[-1]` desligam `prompt_pair`.
- Para o pod: medir o tempo real do par (E ~1,3 s + C ~11 s antes do descarregamento) e o limite `PAIR_THINK_TIMEOUT` (180 s) com o modelo frio.
