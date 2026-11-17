# SPEC-306 — Qwen como padrão do redesenho

| Campo | Valor |
|---|---|
| Onda | 3 |
| Cobre | IMG-10; decisão do lab 202 |
| Prioridade / esforço | P0 / P |
| Depende de | SPEC-304 (canvas) — branda |
| Possui | `REDRAW_DEFAULT`, o caminho da parte `qwen` do redesenho, a Valve `redraw_part` e suas Notas; `docs/specs/SPEC-202-redesenho.md` (fase C) |
| Pod | Validar |

## PRD
**Decisão** (notas do usuário em `Downloads\notas-redesenho.json`, resumo no material do pacote privado, §8): Qwen 4,1 "mudou o que pedi" / 4,7 "mesma personagem" (média 4,4); General Edit solto 3,95; Identity Edit (padrão de hoje) 3,4; geração descrita 3,05. A regra de mudança medida ≥ 15 é abandonada para a escolha (mede pixels, não o pedido); fica só para a nova tentativa automática.

**Critérios de aceite.**
1. `REDRAW_DEFAULT = "qwen"`. As outras opções continuam na Valve (o usuário pode trocar); remover `t2i_described` só se nenhum uso restar (perdeu em identidade, 2,2) — registrar a decisão.
2. A parte Qwen do redesenho respeita o limite de 2 MP (no lab, sem limite, levou 79–90 s em 5,3 MP).
3. Funciona na variante Krea (pacote privado) e na Capella (Qwen).
4. Fase C da SPEC-202 registrada na própria spec (tabela de notas e decisão).
5. Testes de despacho do redesenho com o novo padrão; tabela de decisão atualizada se algum `expected` mudar (sem reescrever `router_raw` gravado).

## Notas da implementação

- **Padrão.** `REDRAW_DEFAULT = "qwen"`; a Valve `redraw_part` mantém as quatro opções e a descrição põe `qwen` primeiro como padrão. `t2i_described` **fica**: além de opção da Valve, é o degrau `new` da escada do "nada mudou" (SPEC-203, `retry_ladder`/`ladder_redo`); removê-lo quebraria a escada.
- **Oferta (`redraw_available`).** Com `qwen`, o modo `redraw` só é oferecido quando a stack do próprio modelo tem `edit` **e** a família Qwen está instalada (`method_stack("qwen21", ...)`). Antes, com `qwen` na Valve, um modelo sem stack de imagem (`{}`/`None`) via o modo só porque havia um Qwen no arquivo de stacks; com o novo padrão isso apareceria para todo modelo de chat. Capella (Qwen) usa a própria stack; a variante Krea do pacote privado usa a stack Qwen que o perfil Capella instala ao lado. Sem Qwen instalado, o padrão não oferece `redraw` (a resposta `redraw` vira `local`); o usuário pode escolher outra parte.
- **2 MP.** Não precisou de código novo: a parte `qwen` passa por `edit_canvas` (`canvas_dimensions`, teto `MAX_CANVAS_PIXELS` = 2 MP, piso de 1 MP da SPEC-304, passo 32 na família Qwen) e `model_edit_sources` redimensiona a primeira referência para esse canvas (o Qwen desenha no tamanho dela). O lab rodou sem esse caminho (5,3 MP). Teste com a fonte do lab (2432×2176): canvas e referência enviada ≤ 2 MP, múltiplos de 32, mesma proporção.
- **Testes.** `tests/test_image_redraw.py`: padrão `qwen` sem Valve nas duas Capellas (o roteador vê `redraw`; job Qwen; trace `edit_method`/`redraw_part` = `qwen`); oferta por stack (Qwen própria, Krea com Qwen instalado, sem Qwen, sem stack de imagem); 2 MP; as seis frases IMG-03 (decisões sintéticas) agora rodam a parte `qwen` sobre a imagem atual; os testes de `restage_current` passam a Valve explícita. O `lab` devolve PNGs reais (o Qwen lê a imagem). `tests/test_retry_ladder.py`: o degrau `redraw` da escada é o Qwen. `tests/test_image_edit_dispatch.py`: a Capella (Qwen) passa a oferecer `redraw`; o teste de oferta grava as duas stacks da Capella, como o Hub instala.
- **Tabela de decisão.** Nenhum `expected` mudou: os seis turnos IMG-03 gravaram `edit_mode: local` (`router_raw` intocado) e seguem `pending: "IMG-03"`.
- **Para o pod.** Validar o tempo do Qwen no redesenho com o teto de 2 MP (no lab, 79–90 s a 5,3 MP) e na variante Krea (troca de stack Krea → Qwen na mesma placa); gravar o que o roteador real responde às frases IMG-03 e então atualizar `expected`. O critério 5 da SPEC-202 (`image_change` ≥ 15) não serve para o Qwen (12,5–14,4 com notas 3–5): julgar pelas notas.
