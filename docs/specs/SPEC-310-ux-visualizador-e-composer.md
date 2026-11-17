# SPEC-310 — Visualizador nas imagens enviadas, arrastar livre, `[` com autocomplete

| Campo | Valor |
|---|---|
| Onda | 3 |
| Cobre | UI-03, UI-04, UI-06 (pedidos U1, U2, U4) |
| Prioridade / esforço | P1 / M |
| Depende de | — |
| Possui | `openViewer`/`setZoom`/pan em `pepito_image_ui.js`; o tratamento de clique nas imagens enviadas pelo usuário; o campo `#chat-input` (sugestão de `[` e realce do trecho ignorado); `pepito_image_ui.css` |
| Pod | Não (Open WebUI aberto) |

## PRD
1. **Imagens enviadas (U1):** o clique numa imagem que o usuário anexou abre o **nosso** visualizador (zoom, fundo, arrastar, menu), não o do Open WebUI. Mesmo comportamento das imagens geradas.
2. **Arrastar livre (U4):** a imagem se move com o mouse em qualquer zoom, inclusive menor que a tela (hoje só move quando o zoom passa da área). Duplo clique ou "Ajustar" recentraliza.
3. **`[` com autocomplete (U2):** ao digitar `[` no campo, aparece uma sugestão discreta (estilo IDE) com `['…']` e `[Ignorar: …]`; Tab/Enter completa e põe o cursor dentro; Esc fecha (sem conflito com a SPEC-107).
4. **Trecho ignorado mais apagado (U2):** enquanto digita, o trecho `['…']`/`[Ignorar: …]` aparece com opacidade menor no campo, sem mudar o texto enviado.
5. Roteiro manual com os critérios nas Notas da spec (sem navegador no ambiente do agente).

## Notas da implementação

- **Enviadas (U1):** ouvinte de clique em captura no `document` para `button > img[data-cy="image"]` (o componente `Image.svelte` do 0.12.0) dentro de `.user-message` ou de `#message-input-container` (anexos ainda no campo também abrem). Para antes do handler do botão, então o `ImagePreview` do Open WebUI não abre. Abre o mesmo visualizador das geradas (`openAlbum`): zoom, fundo, arrastar, setas entre as imagens da mesma mensagem e menu `⋯` só com Download (uma enviada não tem número: Usar esta/Aprovar/Editar não se aplicam). Se a mesma imagem já estiver numerada numa galeria montada da conversa, abre por essa galeria, com o menu completo.
- **Arrastar livre (U4):** o visualizador não rola mais; a imagem fica centrada no palco e se move por `translate` em qualquer zoom. Uma faixa de 48 px sempre fica visível (não some da tela). Roda do mouse continua dando zoom em torno do ponteiro (DEC-06). Duplo clique, "Ajustar" ou `0` recentralizam. Arrastar desliga o reajuste automático ao redimensionar a janela.
- **`[` (U2):** ao digitar `[` no `#chat-input`, aparece uma lista acima do cursor com `['…']` e `[Ignorar: …]`. Setas escolhem; Tab/Enter inserem `['']` (cursor entre as aspas) ou `[Ignorar: ]` (cursor antes do `]`); Esc ou qualquer outra tecla fecha. A inserção é uma transação de texto do Tiptap (`#chat-input.editor`), sem as aspas curvas do Typography. O teclado da lista roda em captura no `window`, antes do `guardSend` e do ouvinte de Esc da SPEC-107: com a lista aberta, Esc só fecha a lista (não para a resposta nem desliga botões); fechada, a SPEC-107 segue igual.
- **Trecho apagado:** CSS Custom Highlight API (`::highlight(pepito-ignored)`, Chromium 105+) sobre os trechos que casam com a mesma gramática de `strip_ignored` (só notas completas). Não mexe no DOM nem no texto enviado. Navegador sem a API: sem realce, o resto funciona.
- Verificado por leitura do `Image.svelte`, `UserMessage.svelte`, `MessageInput.svelte` e `RichTextInput.svelte` do 0.12.0 e `node --check`; sem navegador.

### Roteiro manual (Open WebUI 0.12.0)

1. Envie uma mensagem com duas imagens anexadas. Clique na primeira: abre o visualizador escuro do Pepito (não o preto do Open WebUI), com zoom, `‹ ›` e contador `1 / 2`, `⋯` → Download baixa o arquivo com a extensão certa. Esc fecha e o foco volta à miniatura.
2. Anexe uma imagem no campo (sem enviar) e clique na miniatura: abre o mesmo visualizador. O `×` da miniatura continua removendo o anexo.
3. Numa imagem gerada (galeria), abra o visualizador: com o zoom em "Ajustar" (imagem menor que a tela), arraste: a imagem acompanha o mouse em qualquer direção. Dê zoom com a roda e arraste de novo. Arraste para fora: uma borda sempre fica visível. Duplo clique e "Ajustar": volta ao centro.
4. No campo, digite `[`: aparece a lista. Enter → `['']` com o cursor entre as aspas; digite `nota`: o trecho `['nota']` fica mais apagado que o resto. Repita com seta para baixo + Tab → `[Ignorar: ]`.
5. Digite `[` e aperte Esc com Imagem e Fotorrealista ligados (e, se possível, durante uma resposta em streaming): a lista fecha, os botões continuam ligados, a resposta não para. Esc de novo (lista fechada): comportamento da SPEC-107.
6. Digite `[azul]` normalmente: a lista some ao digitar `a`, o texto não fica apagado. Envie `['nota'] deixa o fundo azul`: o trace mostra `ignored_text` igual ao que estava apagado.
