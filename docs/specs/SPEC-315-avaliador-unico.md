# SPEC-315 — Avaliador único com base e escala `--` a `++`

> Registro técnico SFW. As referências históricas a `edit_lab.py`, seus testes e fontes experimentais pertencem ao pacote privado e não são comandos ou fixtures disponíveis no público. A implementação pública de métricas e avaliação compartilhadas é `scripts/lab_common.py` / `scripts/lab_review.html`.

| Campo | Valor |
|---|---|
| Onda | 3 |
| Cobre | Pedidos do usuário (12/11, pontos 1 e 2) |
| Prioridade / esforço | P1 / M |
| Possui | `scripts/lab_review.html` e os geradores de página em `scripts/edit_lab.py` (incluindo o do redesenho), `scripts/swap_lab.py` (balões), `scripts/swap_composite.py`, `scripts/blend_lab.py` |

## PRD
**Problema.** O avaliador compartilhado (`lab_review.html`) já tem a escala relativa `--` a `++` quando uma variante é marcada como `base`, mas: o lab de área não marcou base (caiu no S/P/N); a página do redesenho é outra, com listas de 1 a 5; o lab de balões não tem página; o lab de área mostrou variantes antigas "not run" e a exportação misturou notas de labs anteriores (mesma chave de armazenamento).

**Critérios de aceite.**
1. Todo lab gera a página pelo componente único, com uma variante `base` (o comportamento de hoje) e escala relativa; sem base, S/P/N.
2. Só aparecem as variantes que o lab rodou; a chave de armazenamento é por lab e por rodada (nada de notas de outros labs na exportação).
3. Embaixo de cada imagem, o **prompt** que a gerou (quando houver), recolhível.
4. Botão **"culpa do prompt"** por variante: sai da conta do método e entra numa lista "prompts a corrigir" na exportação.
5. Redesenho e balões migram para o componente (o redesenho mantém as duas perguntas como dois eixos ou vira relativo à base `restage_current`; documentar a escolha).
6. Corrigir o "Sem saída real: not run" sobreposto à imagem quando a variante tem saída.
7. Testes: geração das páginas em pasta temporária com dados falsos (já existem padrões em `tests/test_edit_lab.py`, `tests/test_swap_lab.py`, `tests/test_blend_lab.py`).

## Notas da implementação

- **Componente único.** `lab_common.review_page(out, página, dados, lab=, title=)` preenche `scripts/lab_review.html` (marcadores de DATA, chave e PAGE) e é chamado por todos os geradores: `edit_lab.write_review` (edição/área), `edit_lab.write_redraw_review` (redesenho; a página própria com listas 1–5 foi apagada), `swap_lab.write_review` (troca), `swap_lab.overlay_review` (balões, nova: `overlay-review` e no fim de `overlay-compose`), `blend_lab.write_review`, `swap_composite.write_review`. A SPEC-309 deve usar o mesmo `review_page`.
- **Base = hoje (critério 1).** Edição: o primeiro de `V0_current`/`R0_box` com saída; redesenho: `restage_current`; troca: `S2_krea` (a variação que o chat mostra primeiro, SPEC-311); balões: composição por forma; blending: variante `B0_none` (a troca sem blending); composição: `F2` (rodada anterior), como já era. A escala é por caso: caso com base → −− a ++; sem base com saída (ex.: a base falhou) → S/P/N.
- **Redesenho (critério 5): relativo à base `restage_current`**, um caso por pedido e semente (a base na mesma semente). As duas perguntas viraram uma nota relativa; o comentário diz se falhou o pedido ou a personagem. `redraw-decide` lê a exportação nova (`--`…`++` = −2…2, base 0, culpa do prompt fora) e continua aceitando o `notas-redesenho.json` de 12/11 (1–5). Depois da SPEC-306 (Qwen padrão), a próxima rodada de redesenho deve trocar `REDRAW_BASE` para `qwen`.
- **Chave por lab e rodada (critério 2).** `pepito-review:<lab>:<rodada>`; a rodada nasce na primeira geração da página na pasta (`review-rounds.json`, por lab) e se mantém ao regenerar. As chaves antigas e a importação automática delas (`LEGACY_KEYS`) saíram: era isso que punha G1–N2 na exportação do lab de área. Exportar e Importar só tratam os casos e variantes da página. Exportação: `avaliacao-<lab>-<rodada>.md` com tabela, conta por método, "Prompts a corrigir" e o JSON (mesmo formato de antes por variante, mais `pb: true`), lido por `review_marks.py`, `--skip-good`/`--good`.
- **Só o que rodou.** Uma variante aparece se o caso tem registro dela no `metrics.json` (ok ou falha); na composição, se o arquivo existe.
- **Critério 6.** Causa: `.missing{display:grid}` vencia o atributo `hidden`, então o "Sem saída real" ficava sempre por cima. Regra `.missing[hidden]{display:none}`.
- **Testes.** `tests/test_edit_lab.py`, `test_edit_lab_redraw.py`, `test_swap_lab.py`, `test_blend_lab.py`, `test_swap_composite.py` (swap-venv). O JS foi exercitado no Node com um DOM falso (avaliar, culpa do prompt, importar de outro lab, exportar), além de `node --check`.

### Roteiro manual (navegador)

1. Gere uma página e abra: à esquerda fica **Original — sem edições**; à direita, todos os resultados, inclusive o método atual (a referência das notas), selecionáveis pela lista. Uma comparação ainda não iniciada abre mostrando o resultado de referência à direita. As demais variantes mantêm a escala −− a ++ contra esse resultado.
2. Uma variante com saída não mostra "Sem saída real" por cima da imagem; uma que falhou mostra.
3. Abra "Prompt desta variante" embaixo da imagem; troque de variante (←/→): continua aberto e o texto muda. A imagem original permanece à esquerda. "Resultado de referência" é uma comparação direta opcional à esquerda, não o padrão.
4. Marque "culpa do prompt" (ou `P`) numa variante: o caso conta como avaliado; Exportar → a célula diz `prompt`, a conta por método diz "fora da conta" e a seção "Prompts a corrigir" traz o prompt.
5. Importe `avaliacao-edit-lab (1).md` de 12/11 numa página de outro lab: "nenhuma avaliação desta página". Regenere a página na mesma pasta: as notas continuam; numa pasta nova, começa vazia.
6. Redesenho e balões: `avaliar-redesenho.html` (casos `<pedido>_s<semente>`, base `restage_current`) e `swap_lab.py overlay-review` → `avaliar-baloes.html` (casos `T1_S1`, `T1_S2`…, base por forma).

### Correção solicitada pelo usuário

O avaliador abria com o resultado-base à esquerda, embora o usuário esperasse a imagem de entrada sem edições. O padrão foi corrigido no componente compartilhado: original à esquerda e A/B/C/D selecionáveis à direita, incluindo A como primeira saída em um caso sem notas. A referência de pontuação não mudou, nem IDs, sementes, imagens, prompts, chave de armazenamento ou notas existentes. Página real do lab de prompts regenerada na mesma URL com a chave anterior preservada. Verificação: 34 testes passaram; Chromium confirmou origem fixa ao selecionar A e B e a escala relativa intacta.

### Correções pedidas pelo usuário em 15/11 (LAB-04)

- **Quadro comum.** O avaliador usava o tamanho da imagem da esquerda para o zoom, a posição e as marcas das duas. Quando a saída tem outro tamanho que a fonte (as do LAB-04 saem em ~1 MP, a fonte em 736×645, por exemplo), os painéis não alinhavam e as marcas pintadas na direita eram guardadas com a escala errada (até valores acima de 1). Agora as duas imagens e as marcas ficam no quadro da variante (o tamanho da saída); a fonte é ajustada a ele (`object-fit: contain`). Verificado no Chromium em R6, R3 e R1: mesmo tamanho na tela nos dois painéis, canvas de marcas do tamanho da saída. **Marcas pintadas antes desta correção, em páginas cujas imagens tinham tamanhos diferentes, estão deslocadas e devem ser refeitas**; notas e comentários não são afetados.
- **"Copiar da outra semente"** (botão no alto e atalho `S`): copia, de outra semente do mesmo pedido (`R1_qwen21_s320101` ↔ `…102`), a nota, o comentário, a nota da saída bruta e o comentário da saída bruta, **só para campos ainda vazios**; nunca sobrescreve o que já foi escrito. Marcas não são copiadas (dependem de cada imagem).
- **"Culpa do prompt" removido** (botão, atalho `P`, contagem e a seção "Prompts a corrigir" da exportação). Exportações antigas com `pb` continuam sendo lidas pelo `redraw-decide` e ignoradas na importação.
