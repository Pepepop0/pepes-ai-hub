# SPEC-105 — Guarda contra promessa de imagem

| Campo | Valor |
|---|---|
| Onda | 1, raia C |
| Cobre | CHAT-05 |
| Prioridade / esforço | P1 / P (meio dia) |
| Depende de | SPEC-101 (mesma redação) |
| Desbloqueia | SPEC-112 |
| Possui | `CLAIM_NOTICE`, `NO_PICTURE_NOTICE` (`pepito_image_chat.py:2301-2310`) e a detecção de afirmação de imagem usada no `outlet` (`:7871`) |
| Pod | Validar |
| Status | aberto |

## PRD

**Problema.** Na sessão de 09/11, logo antes do teste da maçã, o modelo de chat escreveu um prompt em inglês e disse "Me avise quando estiver pronto!" em vez de a imagem ser gerada. A guarda atual só detecta quando o modelo **afirma** ter feito uma imagem; não detecta promessa nem prompt escrito para o usuário copiar. E o aviso atual (`CLAIM_NOTICE`) manda "deixe **Image** ligado", o que repete o problema da SPEC-101 quando ele já está ligado.

**Objetivo.** Em turnos sem imagem, uma resposta que promete gerar ou entrega um prompt de imagem recebe um aviso claro e coerente com o estado do botão.

**Não-objetivos.** Gerar a imagem a partir da promessa (seria decisão do modelo de chat, não do roteador).

**Critérios de aceite.**

1. Dado um turno com Image ligado e sem imagem, quando a resposta contém promessa ("vou gerar", "vou criar a imagem", "me avise quando", "let me know when", "here is the prompt", "aqui está o prompt") ou um bloco que parece prompt de imagem (bloco de código ou citação com mais de 20 palavras em inglês começando por "A photo of"/"An illustration of"/"Prompt:"), então o aviso é acrescentado.
2. Com Image ligado, o aviso diz: "_(Nenhuma imagem foi criada nesta mensagem. Para gerar, diga o que desenhar ou mudar.)_" — sem mencionar o botão.
3. Com Image desligado, o aviso atual continua.
4. Respostas normais (sem promessa) não recebem aviso.

## Spec técnica

- Lista de padrões nova, `_PROMISE`, ao lado da detecção existente; mesma normalização (`_fold`).
- Duas constantes: `CLAIM_NOTICE_ON` (Image ligado) e a atual para desligado. O estado vem de `metadata[STATE_KEY]` (`no_image`) e de `features`.

## Testes

Unitários com respostas de exemplo em português e inglês (promessa, prompt em bloco, resposta normal sobre uma imagem existente, resposta que cita "foto" sem prometer).

## Tarefas

1. `_PROMISE` e detecção do prompt em bloco.
2. Aviso dependente do estado.
3. Testes.

## Riscos

Falso positivo em conversa sobre fotografia. O aviso é uma nota em itálico ao fim; não apaga a resposta.
