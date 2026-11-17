# SPEC-318 — Ver sem colagem e refazer na imagem inteira

| Campo | Valor |
|---|---|
| Onda | 3 (complemento, pedido do usuário em 14/11) |
| Cobre | UI-09 |
| Prioridade / esforço | P1 / M |
| Depende de | SPEC-308 (receita, `POST /regenerate`), SPEC-317 (modos e mensagens fixas) |
| Possui | `pepito_image_chat.py` (guardar a saída bruta; regeração sem área), `pepito_native.py` (rota da saída bruta; modo novo), `pepito_image_ui.js`/`.css` |

**Problema.** Nas edições com área, o modelo desenha a imagem inteira e o filtro cola só a parte de dentro da área sobre a original (`keep_outside_mask` → `masked_composite` → `replace_picture`, `pepito_image_chat.py` ~5818). Quando a área está errada ou pequena, a imagem parece recortada e colada. A saída inteira do modelo é descartada: `replace_picture` sobrescreve o arquivo.

## Comportamento

1. **"Ver sem colagem"** (sem GPU). Antes de `replace_picture`, o filtro guarda a saída bruta da edição (os bytes que o ComfyUI devolveu) junto com a receita da imagem, no armazenamento nativo do chat (o mesmo SQLite da receita, por número de imagem; só o dono lê). No menu ⋯ e no visualizador, um item "Ver sem colagem" abre a saída bruta no visualizador, com rótulo claro ("Saída do modelo, antes da colagem"), sem criar mensagem nem imagem nova no chat. Com recorte (SPEC-307, `crop`), a saída bruta é só o recorte: o rótulo diz "Saída do modelo (só o recorte)".
2. **"Refazer na imagem inteira"** (GPU). Novo modo de regeração `whole`: mesma receita, mesmo prompt e mesma semente (`recipe_seed`), **sem área e sem recorte**, na imagem inteira. Mensagem fixa `"Regerar a imagem {number} na imagem inteira"`, reconhecida antes do roteador como as outras. Entrega uma imagem nova no chat. Sem semente gravada: usa uma nova e diz isso na resposta.
3. Os dois itens só aparecem em imagens que tiveram área (`mask_png` na receita). Imagens sem área, uploads e trocas não os mostram.

## Limites

- A saída bruta guardada tem um teto (o mesmo `MAX_SOURCE_BYTES` das fontes); acima dele, não é guardada, e o item explica "A saída do modelo não foi guardada".
- A saída bruta é apagada junto com o chat, pelo mesmo caminho da receita.
- Salvaguarda de idade, consentimento, trace (`prompt_override` com `mode: "whole"`) e calibragem: como na SPEC-308.

## Critérios de aceite

1. Edição com área: a receita passa a ter a saída bruta; `GET` da saída bruta só para o dono (outro usuário: 404 igual ao das fontes); bytes idênticos ao que o ComfyUI devolveu antes da colagem.
2. Edição sem área, troca e geração: nada é guardado; o item não aparece.
3. `whole`: o job vai ao ComfyUI sem máscara, sem recorte, com o prompt e a semente gravados; o canvas é o da imagem inteira (como uma edição sem área faria).
4. UI: "Ver sem colagem" mostra a imagem com o rótulo, sem enviar mensagem; "Refazer na imagem inteira" usa `sendRegeneration` com as recusas da SPEC-308.
5. Filtro 0.25.0; testes do filtro, nativos (ambiente descartável) e do compositor (swap-venv) verdes.

## Notas da implementação

- Filtro: `keep_outside_mask` devolve a saída bruta (`data`, lida antes de `replace_picture`) em `seams[i]["raw_output"]` (assinatura intacta); `edit_chat_image` a tira de lá (nunca vai ao trace) e a põe na receita como `raw_output`/`raw_crop` (`crop is not None`); `record_native_recipes` separa os bytes do JSON e os passa a `record_recipes(..., raws=)` (só quando há). Edição sem área, troca e geração não guardam nada.
- Nativo: tabela `raw(number, data, crop)` no mesmo SQLite do chat (some com ele); teto `MAX_SOURCE_BYTES` (acima, não é guardada). `GET /raw/{n}?chat_id=` só do dono (404 igual ao das fontes; 404 "A saída do modelo não foi guardada." sem saída), com `X-Pepito-Raw-Crop`. `recipe_view` ganhou `raw_available` e `raw_crop`.
- Modo `whole` ("Regerar a imagem {n} na imagem inteira"): reconhecido em `_REGENERATE`; `arm_regeneration` só aceita edição com área (409 para outras e para edição por deformação, que precisa da área). `Filter._regenerate`: prompt e semente gravados, `mask=None` (portanto sem recorte; o canvas é o `patch` gravado, o da imagem inteira). Sem semente no `patch`: semente nova e a nota "A semente original não foi guardada; usei uma semente nova.". Trace `prompt_override` com `mode: "whole"`.
- UI: no menu ⋯ (galeria e visualizador), "Ver sem colagem" e "Refazer na imagem inteira" ficam escondidos e só aparecem se `GET /recipe` traz `area_png` (o segundo também exige `regenerable`). "Ver sem colagem" baixa a saída bruta, fecha o visualizador aberto e abre outro (só Download) com o rótulo "Saída do modelo, antes da colagem" ou "Saída do modelo (só o recorte)"; sem saída guardada, a mensagem aparece no menu. Nada é enviado ao chat. "Refazer" usa `sendRegeneration` com `mode: 'whole'`.
- Testes: `tests/test_prompt_selector.py` (rota, dono, teto, armar, bytes idênticos ao do ComfyUI, `whole` sem máscara e com prompt/semente/canvas). Prova de UI no navegador (superfície sintética, API falsa, já apagada): item escondido na imagem sem área; rótulo normal e de recorte; mensagem de saída não guardada; `{mode: "whole"}` enviado e mensagem fixa digitada; a partir do menu do visualizador também.
- Para o pod: conferir com GPU que a saída bruta guardada é a do ComfyUI e que "Refazer na imagem inteira" entrega a imagem sem colagem no Open WebUI real. Versão do filtro: o integrador sobe (0.25.0).
