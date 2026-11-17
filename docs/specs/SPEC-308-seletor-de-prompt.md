# SPEC-308 — Ver o prompt de uma imagem e regerar com ele editado

| Campo | Valor |
|---|---|
| Onda | 3 |
| Cobre | Pedido do usuário (12/11, ponto 3) |
| Prioridade / esforço | P1 / M |
| Depende de | — |
| Possui | Item novo no menu da imagem (`imageActions`) e painel no visualizador em `pepito_image_ui.js`/`.css`; rota nova no `pepito_native.py` (ler a receita de uma imagem; armar uma receita editada, no padrão do draft da área); o caminho no filtro que consome a receita armada |
| Pod | Validar |

## PRD
**Problema.** Muitas edições falham por prompt ruim do roteador, não pelo método. Corrigir pelo chat custa 2–3 mensagens.

**Histórias.**
- Clico em "Prompt" numa imagem e vejo o texto que a gerou, a parte, a origem, a área e a semente.
- Edito o texto e clico "Regerar com este prompt": sai uma imagem nova com a mesma receita e o meu texto, sem passar pelo roteador.
- "Regerar igual" repete a receita com semente nova.

**Critérios de aceite.**
1. A receita de cada imagem entregue fica legível por número da imagem (o registro por chat já guarda `last_edit`; generalizar para toda imagem entregue, com prompt final, parte, fontes, área, canvas, semente, `edit_mode`).
2. Rota nativa só do dono do chat (padrão de `_owned_chat`/`_owned_file`).
3. "Regerar com este prompt": o JS arma a receita editada (como o draft da área, com expiração) e envia uma mensagem fixa ("Regerar a imagem N com o prompt editado"); o filtro consome a receita e despacha direto, sem roteador.
4. **A salvaguarda de idade roda no prompt editado** (`minor_risk_rules`), sempre.
5. Trace: `prompt_override` com a imagem de origem e o tamanho do texto (o texto vai no `image_job.params.prompt`, como sempre).
6. Funciona para geração, edição, restage, redesenho e troca (na troca, o prompt editável é o da etapa que o usuário escolher ou o da variação mostrada).
7. Testes: receita lida só pelo dono; regerar sem roteador; salvaguarda bloqueia prompt editado com menor; receita expirada recusa com mensagem clara.

## Notas da implementação
- **Receita por imagem.** O filtro grava, depois de publicar a galeria (`_attach` → `record_native_recipes`), a receita de cada imagem entregue no `pepito_native` (tabela `recipe` do SQLite do chat, chave = número; `record_recipes`, sem rota). Campos: `kind` (generate/edit/swap), `part`, `prompt` final, `edit_mode`, `source_urls` + `sources` (números), `mask_png` (área final usada), `patch` (canvas + semente da variação), `seed`, `strength`, `fidelity`, `change`, `style`, `aspect`, `model`, `user_text` (palavras do pedido que fez a imagem) e `recipe` (a receita de edição, sem `region_png`). Troca: `method`, `prompts` {`reference`, método}, `swap_plan`, `swap_medium`. O `last_edit` do registro por chat não mudou (continua servindo ao "nada mudou"). Imagens anteriores a esta versão não têm receita: o painel diz "Esta imagem não tem receita registrada.".
- **Rotas** (só o dono, via `_source` = `_owned_chat` + `_owned_file`; outro usuário recebe o mesmo 404): `GET /recipe/{n}?chat_id=`, `POST /regenerate` {chat_id, number, mode `edited`|`same`, prompt?, step?} e `DELETE /regenerate`. O armar segue o draft da área: estado `regenerate` com o hash da mensagem fixa, expira em 300 s, usado uma vez (`consume_regeneration`: issues `text` mantém, `expired`/`invalid` removem).
- **Mensagens fixas**: "Regerar a imagem N com o prompt editado" / "Regerar a imagem N igual". O filtro as reconhece antes do roteador, com Image ligado ou desligado (o botão pede exatamente isso), e despacha o job direto (`Filter._regenerate`). Sem receita armada, expirada ou de outro pedido: resposta fixa clara, nada é feito.
- **Salvaguarda**: `minor_risk_rules` sempre, sobre todos os prompts que o job envia (o editado e, na troca, o da outra etapa), com as 2 mensagens anteriores do usuário e o `user_text` da imagem de origem. Bloqueio = `_refuse` (nada descarregado nem chamado). O consentimento de referências também vale quando o prompt editado fica explícito numa edição de upload.
- **Semente**: com prompt editado a semente gravada é mantida (só o texto muda; `generate_new_image` ganhou `seed`); "Regerar igual" sorteia nova. Na troca, a semente é sempre nova (a `SwapComfyUI` sorteia por turno) e só a variação mostrada (o método dela) é refeita; a etapa escolhida (referência limpa ou geração da variação) leva o texto editado.
- **Calibragem**: a imagem regerada não registra tentativa (sem `calibration` no estado), então "Aprovar resultado" fica oculto nela. Deliberado: um prompt manual não deve ensinar a fidelidade.
- **Trace**: evento `prompt_override` {source_number, mode, step, kind, prompt_chars, edited, minor_risk} (ou {source_number, issue}); o texto vai em `image_job.params.prompt` (troca: `params.prompts`).
- **JS**: item "Prompt" no menu ⋯ (galeria e visualizador) abre o painel lateral do visualizador (parte, origem, área em miniatura, canvas, semente, seletor de etapa na troca, texto editável, "Regerar com este prompt", "Regerar igual", "Restaurar texto"). Regerar arma no servidor, fecha o visualizador, cola a mensagem fixa no compositor e clica Enviar. Recusa com área anexada ou texto no compositor; se o envio automático falhar, avisa para enviar a mensagem em até 5 min.
- **Roteiro manual (pod)**: (1) gerar uma imagem; ⋯ → Prompt: conferir parte, origem, canvas e semente; (2) editar o texto → "Regerar com este prompt": a mensagem fixa sai, nenhum status do roteador, imagem nova com a mesma semente (painel da nova); (3) "Regerar igual": semente diferente; (4) edição com área pintada: miniatura da área no painel, regerar mantém o fora da área; (5) restage e redesenho: regerar mantém a parte; (6) troca: seletor Etapa, só a variação mostrada é refeita; (7) armar e digitar a mensagem à mão depois de 5 min: mensagem de expiração; (8) prompt editado com menor em contexto de risco (salvaguarda de idade): bloqueio; (9) texto no compositor ou área anexada: recusa sem enviar.
- **Integração Onda 3 (`e1b08fc`).** O painel convive com `openAlbum`/`galleryAlbum` e com o arrastar por `translate` da SPEC-310. Imagens enviadas (`uploadAlbum`) continuam só com Download: não recebem Prompt. A troca segue a entrega da SPEC-311: Krea = variação 1, Qwen com marcação = 2; a regeneração filtra pelo método da receita e a nota de uma única variação preserva seu número (testado para Qwen = 2).
