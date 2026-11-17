# SPEC-107 — Esc não desliga os botões

| Campo | Valor |
|---|---|
| Onda | 1, raia E |
| Cobre | UI-01 |
| Prioridade / esforço | P1 / P (meio dia, com navegador) |
| Depende de | SPEC-001 (zoom commitado no mesmo arquivo) |
| Desbloqueia | SPEC-113 |
| Possui | Novo ouvinte de teclado em `assets/open-webui/pepito_image_ui.js` (perto de `guardSend`, `:1305-1325`) |
| Pod | Não (basta um Open WebUI 0.12.0 local, mesmo sem modelo) |
| Status | aberto |

## PRD

**Problema.** No Open WebUI 0.12.0, o `keydown` do campo de mensagem faz, ao apertar Esc ([`MessageInput.svelte`](https://github.com/open-webui/open-webui/blob/v0.12.0/src/lib/components/chat/MessageInput.svelte)):

- linha 2163: `stopResponse()`;
- linhas 2217–2225: zera `selectedToolIds`, `selectedFilterIds`, `webSearchEnabled`, `imageGenerationEnabled`, `codeInterpreterEnabled` e o modelo marcado com `@`.

Ou seja, Esc desliga Imagem, Fotorrealista, Arte digital, Alta qualidade e Buscar referências.

**Objetivo.** Esc no campo de mensagem nunca mexe nos botões. Continua parando a resposta em andamento e fechando listas abertas.

**Critérios de aceite** (no navegador, Open WebUI 0.12.0):

1. Com Imagem e Fotorrealista ligados, foco no campo, Esc: os dois continuam ligados.
2. Durante uma resposta em streaming, Esc: a resposta para.
3. Com a lista de sugestões (`#suggestions-container`) ou o menu de `@`/`/` aberto, Esc: a lista fecha.
4. Esc fora do campo de mensagem: comportamento do Open WebUI inalterado (o visualizador e o editor de máscara continuam fechando com Esc).

## Spec técnica

```js
document.addEventListener('keydown', event => {
  if (event.key !== 'Escape' || event.isComposing) return;
  if (!event.target?.closest?.('#chat-input')) return;
  if (document.getElementById('suggestions-container')) return; // critério 3: deixa passar
  event.stopImmediatePropagation();
  event.preventDefault();
  stopButton()?.click(); // critério 2
}, true);
```

- `stopButton()`: o botão de parar que o Open WebUI mostra no lugar do de enviar durante uma resposta (`MessageInput.svelte:2652-2661`: um `<button aria-label={$i18n.t('Stop')}>` dentro do bloco `isActive`). O `aria-label` é traduzido; localizar pelo contêiner do campo e pela ausência de `#send-message-button`, e confirmar no navegador em português.
- Fase de captura, no `document`, como `guardSend`: roda antes do handler do Svelte.
- Conferir no navegador se o menu de menções/comandos usa outro id além de `#suggestions-container`; se usar, entra na condição do critério 3.

## Testes

- Manual no navegador (os 4 critérios); registrar em `docs/testing/image-tool-chat.md`.
- Se a QA-04 (testes de navegador) já existir, vira teste automático; não é pré-requisito.

## Tarefas

1. Ler os trechos citados do `MessageInput.svelte` 0.12.0 (cópia em `%TEMP%\owui\`).
2. Implementar o ouvinte e `stopButton()`.
3. Conferir no navegador.

## Riscos

Atualização do Open WebUI trocar `#chat-input`: o ouvinte deixa de agir e volta o comportamento original, sem quebrar nada.

## Notas da implementação

- Ouvinte de captura e `stopButton(field)` em `pepito_image_ui.js`, logo após `guardSend`. `stopButton` não usa o `aria-label`: dentro do `<form>` do campo, sem `#send-message-button` presente, escolhe o último botão habilitado e visível fora de `#chat-input-container` e sem `aria-haspopup` (no 0.12.0 o Stop é o último elemento do grupo à direita, depois do seletor de modelo).
- Verificado só por leitura do `MessageInput.svelte` 0.12.0; sem navegador.

### Roteiro manual (Open WebUI 0.12.0, em português)

1. Ligue Imagem e Fotorrealista, clique no campo de mensagem, aperte Esc: ambos continuam ligados (e Alta qualidade/Buscar referências, se ligados).
2. Envie um prompt de chat comum para o modelo gerar texto em streaming; com o campo vazio e foco nele, aperte Esc: a resposta para (o botão Stop é clicado). Confira também que Imagem continua ligado.
3. Abra as sugestões na tela inicial (`#suggestions-container`) ou digite `@` / `/` para abrir o menu: Esc fecha a lista. Se o menu de `@`/`/` usar outro id além de `#suggestions-container` e Esc não fechar ou ainda desligar botões, anote o id e inclua-o na condição.
4. Foco fora do campo (visualizador de imagem aberto, editor de máscara aberto): Esc fecha ambos como antes.
