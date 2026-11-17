# SPEC-202 — Redesenho: "refaça", "mais realista", "harmonizar"

> Registro técnico SFW. As referências históricas a `edit_lab.py`, seus testes e fontes experimentais pertencem ao pacote privado e não são comandos ou fixtures disponíveis no público. A implementação pública de métricas e avaliação compartilhadas é `scripts/lab_common.py` / `scripts/lab_review.html`.

| Campo | Valor |
|---|---|
| Onda | 2, fases A (implementar com escolha configurável), B (experimento) e C (fixar o padrão) |
| Cobre | IMG-03; DEC-03 |
| Prioridade / esforço | P0 / M (2 a 3 dias + 40 min de pod) |
| Depende de | Onda 1 (trace com semente e `image_change`, SPEC-109) |
| Desbloqueia | SPEC-203 |
| Possui | `EDIT_MODES` e as regras do roteador para `edit_mode` (`ROUTER_MODE_RULES`); a escolha de parte no `_routed` para o modo novo; `intent_prompt` para o modo novo; uma Valve nova `redraw_part` |
| Pod | Sim |
| Status | fases A, B e C feitas: padrão `qwen` (SPEC-306); critério 5 no pod |

## PRD

**Problema** (Rel. 1, item 3; fixtures `2026-11-10T01:47:08` a `01:50:22`, `02:08:06`, `02:09:11`). Pedidos de refazer ou mudar o estilo da imagem inteira vão para o adaptador de edição **local** (General Edit), com um prompt que manda manter quase tudo. O resultado difere 2,7 a 3,6 níveis da origem: a mesma imagem. Baixar a fidelidade até o piso não muda isso. A tabela de decisão marca esses seis turnos como `pendente IMG-03`.

**Objetivo.** Esses pedidos produzem uma imagem visivelmente nova já na primeira tentativa, mantendo quem é a personagem.

**Não-objetivos.** Mudar edições locais ("deixa o casaco azul"); a escada de "nada mudou" (SPEC-203).

**Histórias.**

- "Faz uma versão fotorrealista dessa personagem" → uma foto da mesma personagem, mesma roupa e pose aproximada.
- "A imagem está toda cortada, pode harmonizar?" → a mesma cena redesenhada sem os cortes, partindo da imagem **atual** (DEC-03).

**Critérios de aceite.**

1. O roteador tem `edit_mode: "redraw"`, com regra e exemplos: refazer, "do zero", outro estilo ou meio ("fotorrealista", "anime"), "harmonizar", "redesenhar", "mais realista", sem pose ou cena nova (isso continua `restage`).
2. Com `redraw`, a parte é a da Valve `redraw_part` (padrão definido pelo experimento). Opções na fase A: `restage_current` (Identity Edit com a imagem atual como fonte), `edit_free` (General Edit com `ref_boost` 1,0 e `change` strong), `qwen` (edição do Qwen também na variante Krea, se o experimento indicar), `t2i_described` (geração nova a partir de uma descrição da imagem atual feita pelo modelo de visão).
3. Com `redraw`, o prompt não leva a lista "Keep unchanged" inteira: só identidade e o que o usuário pediu para manter ("mantendo esse fundo").
4. Fixtures: os seis turnos IMG-03 passam a esperar `edit_mode: redraw` com a parte padrão, e a marca `pendente IMG-03` sai.
5. No pod: as quatro mensagens do chat `09cd5d` dão `image_change` ≥ 15 na primeira tentativa e você confirma que virou foto; "harmonizar" na imagem remendada fica sem os cortes, segundo você.
6. Edição local simples continua com a mesma parte e o mesmo `ref_boost` de hoje (tabela de decisão).

## Experimento (fase B)

- **Entradas:** as fontes das fixtures IMG-03 (imagens 1 e 14 da sessão final, já no pod pela coleta antiga ou reenviadas) e duas imagens novas suas (uma ilustração, uma foto).
- **Variantes:** as quatro opções da Valve, mesma semente por variante, 2 sementes cada → 4 pedidos × 4 variantes × 2 = 32 jobs (cerca de 10 min de GPU).
- **Medidas:** `image_change`, tempo do job, e sua nota 1 a 5 para "mudou o que pedi" e "é a mesma personagem".
- **Regra de decisão:** padrão = a variante com maior média das duas notas entre as que têm `image_change` ≥ 15 em todos os casos; empate → a mais rápida.
- **Ferramenta:** um caso novo em `scripts/edit_lab.py` (que já roda variantes por caso e gera página de avaliação), não o chat.

## Spec técnica

- `EDIT_MODES = ("restage", "remove", "swap", "redraw")`; esquema e `router_capabilities` oferecem `redraw` quando a stack tem a parte escolhida.
- `restage_current`: a mesma parte `restage`, mas a fonte é a imagem atual (não a primeira da personagem). O resto do caminho de restage não muda.
- `t2i_described`: uma chamada de visão ao modelo de chat ("descreva esta imagem para regerá-la") antes da descarga, depois o caminho de geração nova com essa descrição + o pedido.
- Trace: `image_job.params.edit_mode = "redraw"` e `redraw_part`.

## Testes

- Fixtures IMG-03 na tabela de decisão (critério 4).
- `intent_prompt` em modo redraw (critério 3).
- Cada opção da Valve monta o job esperado (unitário).

## Tarefas

1. Fase A: modo `redraw`, Valve, as quatro opções, prompt, trace, testes. [P] Caso de lab no `edit_lab.py`.
2. Fase B: experimento no pod.
3. Fase C: fixar o padrão, atualizar fixtures, remover opções que perderam por larga margem (sem deixar código morto).

## Riscos

- O roteador classificar como `redraw` um pedido local. Mitigação: regra só para "imagem inteira"; casos negativos nos exemplos; tabela de decisão.
- `t2i_described` pode perder a identidade; o experimento decide.

## Notas da implementação (fase A)

- **Padrão provisório.** A opção que reproduz o comportamento de hoje (edição local que mantém quase tudo) não existe na Valve. `redraw_part` vale `restage_current` até o experimento: **o pod decide** o padrão (`REDRAW_DEFAULT` no filtro; `edit_lab.py redraw-decide` aplica a regra de decisão). Fase C troca o padrão, atualiza as fixtures se a parte mudar e remove opções derrotadas por larga margem.
- **Oferta ao roteador.** `router_capabilities(stack, redraw_part=...)` oferece `redraw` quando a stack tem a parte escolhida: `restage_current` pede `restage`; `edit_free` pede o `ref_boost` (`edit.strength`); `t2i_described` pede `generate`; `qwen` pede a família Qwen instalada. Sem a parte, o roteador não vê o modo e uma resposta `redraw` vira `local`. Por isso a lista de modos do preset da variante privada na suíte de despacho ganhou `redraw` no fim.
- **Fonte = imagem atual (DEC-03).** Um redesenho novo parte da imagem atual, qualquer que seja a base que o plano escolheu, a menos que o usuário nomeie outra. Um retry (`request_mode: retry`) ou queixa sobre o último resultado (`correction` diferente de `none`, como "nada mudou") mantém a fonte que o plano escolheu; a escada de "nada mudou" é da SPEC-203. Por isso o turno 02:09:11 segue na imagem 14.
- **Prompt.** Em `redraw`, `intent_prompt` apaga a frase `Keep ... unchanged.` que o modelo escreve e troca a lista por `Keep unchanged: the character's identity (face, hair, body)` mais o que o usuário nomeou nas próprias palavras. A lista habitual do modelo (pose, roupa, enquadramento, luz, fundo) só entra se o texto do usuário a pede ("mantendo esse fundo", "mantém a roupa"); o resto é descartado, inclusive itens específicos que o usuário não nomeou (limite conhecido: um item específico em português, como "o chapéu vermelho", só volta se o usuário o escreveu em inglês). A fonte leva um único propósito (`REDRAW_PURPOSE`): os propósitos do roteador reintroduziriam a lista.
- **Partes.** `restage_current` = parte `restage` com a fonte atual. `edit_free` = parte `edit`, `ref_boost` 1,0, nível `strong`, ignorando `ref_boost_manual` e a fidelidade aprendida (não é edição local). `qwen` = método `qwen` (ignora a preferência de método de edição do usuário). `t2i_described` = uma chamada de visão ao modelo de chat antes da descarga (`describe_picture`), depois geração nova com `described_prompt`; se a descrição falha, o turno responde que não conseguiu e nada é enfileirado; uma área pintada recusa o modo, como qualquer troca de cena.
- **Trace.** `image_job.params.edit_mode = "redraw"` e `redraw_part` (só em redesenhos); `image_change` é medido também em `restage_current` e `qwen` (antes só na edição local sintonizada). O redesenho `t2i_described` não tem origem para medir no filtro: o laboratório mede.
- **Fixtures.** `router_raw` fica como o roteador real respondeu (`edit_mode: local`); não é reescrito. Os seis turnos IMG-03 mantêm `expected` do comportamento com essa resposta (`part: edit`) e `pending: "IMG-03"`, com `pending_note`: o roteador real ainda não foi testado com o modo redraw (pod). O comportamento com `redraw` é provado em `tests/test_image_redraw.py` com decisões sintéticas (as frases reais dos seis turnos como texto do usuário). Quando o pod mostrar o que o roteador real responde, a fase C grava essas respostas e atualiza `expected` para `edit_mode: redraw`.
- **Laboratório.** `scripts/edit_lab.py redraw-prepare | redraw-run | redraw-decide` (ver `docs/testing/edit-lab.md`): 4 pedidos × 4 variantes × 2 sementes, mesmo conjunto de sementes por variante, `image_change` igual ao do trace, página de notas 1 a 5 e a regra de decisão da spec.
- **Para o pod (B).** Criterio 5 (quatro mensagens do chat 09cd5d com `image_change` ≥ 15 na primeira tentativa; "harmonizar" sem os cortes) e a escolha do padrão. A hipótese de que o `qwen` aceita o pedido na variante Krea também sai do experimento.

## Fase C — resultado do experimento e decisão (SPEC-306)

Laboratório de 12/11 (material do pacote privado, §5 e §8): 5 pedidos × 4 variantes × 2 sementes = 40 jobs, sem falha, 6,8 min. Notas do usuário (`Downloads\notas-redesenho.json`), 1 a 5, média das duas sementes nos 5 pedidos:

| Variante | Mudou o que pedi | Mesma personagem | Média | Mudança medida |
|---|---|---|---|---|
| **`qwen`** | **4,1** | **4,7** | **4,4** | 12–34 (12,5–14,4 em `mais_realista` e `do_zero`) |
| `edit_free` | 2,9 | 5,0 | 3,95 | no `metrics.json` de cada pedido |
| `restage_current` | 2,2 | 4,6 | 3,4 | no `metrics.json` de cada pedido |
| `t2i_described` | 3,9 | 2,2 | 3,05 | 53–69 |

- **Decisão: `REDRAW_DEFAULT = "qwen"`.** A regra escrita na fase B (maior média entre as variantes com mudança medida ≥ 15 em todos os casos) excluiria o Qwen, que fica abaixo de 15 em dois pedidos com notas humanas de 3 a 5: o limiar mede pixels, não o pedido (o Qwen troca o estilo mantendo a composição). O limiar deixa de valer para a escolha do padrão; fica só para a nova tentativa automática (SPEC-203). `edit_lab.py redraw-decide` continua aplicando a regra antiga; a decisão é a das notas.
- **Opções mantidas.** As quatro continuam na Valve `redraw_part` (o usuário pode trocar). `t2i_described` perdeu em identidade (2,2), mas não é código morto: é o degrau `new` da escada do "nada mudou" (SPEC-203). Nenhuma opção removida.
- **Oferta.** `qwen` exige que a stack do próprio modelo edite imagens (`edit`) e que a família Qwen esteja instalada: a Capella (Qwen) usa a própria stack; a variante Krea usa a stack Qwen instalada ao lado. Sem Qwen, o roteador não vê `redraw` com o padrão (o usuário pode escolher outra parte).
- **Fixtures.** Nenhum `expected` mudou: os seis turnos IMG-03 gravaram `edit_mode: local` e continuam `pending: "IMG-03"` até o pod gravar o que o roteador real responde à regra nova. As decisões sintéticas em `tests/test_image_redraw.py` agora esperam a parte `qwen`.
