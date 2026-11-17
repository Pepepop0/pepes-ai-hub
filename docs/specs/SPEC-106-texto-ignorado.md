# SPEC-106 — Texto ignorado

| Campo | Valor |
|---|---|
| Onda | 1, raia D |
| Cobre | CHAT-07, DEC-02 |
| Prioridade / esforço | P1 / P (meio dia) |
| Depende de | SPEC-002 |
| Desbloqueia | SPEC-112 |
| Possui | Função nova `strip_ignored`; o começo do `_inlet` (`pepito_image_chat.py:6590`); `start_trace` (`:465`, campo novo) |
| Toca | `last_user_text` (`:2540`) e `neutral_history` (`:2344`) só se for preciso; preferência é limpar `body["messages"]` uma vez no começo |
| Pod | Validar |
| Status | aberto (premissa DEC-02) |

## PRD

**Problema.** Você escreve notas para o desenvolvedor no meio da sessão (`[ Ignorar: para dev, voltamos ao problema… ] Sessão encerrada`, 09/11 01:51:18; `[Ignorar: para o dev, essa porra voltou ]  Já estava ligado…`, 01:36:04). Elas vão para o roteador e para o modelo, que podem reagir a elas.

**Objetivo.** Texto marcado como ignorado não chega ao roteador nem ao modelo, mas fica no trace para você ler depois.

**Gramática** (premissa DEC-02):

- `['qualquer coisa']` — colchete, aspas simples retas ou curvas (`'`, `‘`, `’`), conteúdo, aspas, colchete. Espaços opcionais por dentro.
- `[Ignorar: qualquer coisa]` — sem diferenciar maiúsculas nem acentos; espaços opcionais.
- Sem colchetes aninhados. Colchete sem fechar não é ignorado.

**Critérios de aceite.**

| Entrada | Vai ao roteador e ao modelo |
|---|---|
| `[ Ignorar: para dev, voltamos ao problema de ficar repetindo a mesma imagem ] Sessão encerrada` | `Sessão encerrada` |
| `[Ignorar: para o dev, essa porra voltou ]  Já estava ligado, mas reiniciei, mete bala` | `Já estava ligado, mas reiniciei, mete bala` |
| `['anotação'] deixa o fundo azul` | `deixa o fundo azul` |
| `deixa o fundo ['nota no meio'] azul` | `deixa o fundo azul` |
| `['só isto']` | nada: resposta fixa "Anotado.", sem roteador nem modelo |
| `deixa a camiseta [azul]` | inalterado |
| `[Ignorar: sem fechar deixa azul` | inalterado |

Além disso: as mensagens **anteriores** do usuário também chegam limpas ao roteador e ao modelo; o trace do turno tem `ignored_text` com os trechos removidos.

## Spec técnica

```python
def strip_ignored(text: str) -> tuple[str, list[str]]:
    """(texto limpo, trechos ignorados). Colapsa os espaços deixados pela remoção."""
```

- No começo do `_inlet`, antes da aprovação digitada e do caminho Image desligado: limpar o conteúdo de toda mensagem `user` de `body["messages"]` (texto simples ou partes `type: text`).
- Se a última mensagem do usuário ficar vazia e não houver imagem anexada: `fixed_reply(body, metadata, "Anotado.", event="reply")`.
- `start_trace(..., ignored_text=[...])` quando houver.
- Vale com Image ligado ou desligado.

## Testes

A tabela acima, como teste parametrizado de `strip_ignored`, mais um teste do `_inlet` com o caso 01:36:04 da SPEC-002 verificando o texto que o roteador recebe e o `ignored_text` no trace.

## Tarefas

1. `strip_ignored` e testes.
2. Integração no `_inlet` e no trace.
3. Linha no `docs/testing/image-tool-chat.md` explicando o recurso (ou na SPEC-113).

## Não regredir

Texto entre colchetes comum (sem aspas, sem "Ignorar:") continua passando.
