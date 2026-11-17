# SPEC-204 — Área automática com silhueta (SAM) e edição recortada

> Registro técnico SFW. As referências históricas a `edit_lab.py`, seus testes e fontes experimentais pertencem ao pacote privado e não são comandos ou fixtures disponíveis no público. A implementação pública de métricas e avaliação compartilhadas é `scripts/lab_common.py` / `scripts/lab_review.html`.

| Campo | Valor |
|---|---|
| Onda | 2, fases A (implementar atrás de Valves), B (experimento) e C (padrão) |
| Cobre | IMG-02 (degrau, fase 2), N3 (área retangular) |
| Prioridade / esforço | P1 / M (2 a 3 dias + 30 min de pod) |
| Depende de | SPEC-108 (emenda), integrada |
| Desbloqueia | SPEC-210; decide se a IMG-06 (fase 3) é necessária |
| Possui | `prepare_local_preservation` (o ponto depois de `ground_regions`); um workflow novo `workflows/sam3-region-api.json` e sua entrada em `manifests/workflows.yaml`; o recorte e a costura no despacho da edição com máscara; Valves `region_shape` e `region_crop` |
| Pod | Sim |
| Status | aberto |

## PRD

**Problema.** A área automática é a união de retângulos do modelo de visão: na sessão de 09/11 a área das 02:05 preenche 99% da própria caixa. Com a emenda da SPEC-108 o tom fica certo, mas uma borda reta atravessando o corpo ou o fundo continua visível quando a estrutura não bate (linhas fora do lugar dos dois lados). E o modelo redesenha a imagem inteira para mudar um pedaço dela.

**Objetivo.** A área segue o contorno do que muda, e o modelo trabalha só na região (com contexto), na resolução dele.

**Não-objetivos.** Amostragem com máscara dentro do grafo (IMG-06).

**Critérios de aceite.**

1. `region_shape = "silhouette"`: os `region_targets` do roteador viram prompts de texto do SAM 3.1 (mesmo modelo da troca), restritos à caixa do modelo de visão (interseção). Sem detecção do SAM → retângulo de hoje, com `region_shape_used: "box"` no trace.
2. A dilatação por tipo de mudança (`GEOMETRY_DILATION`, "nada mudou") continua aplicada sobre a silhueta.
3. `region_crop = true`: a fonte é recortada na caixa da área + margem de contexto (padrão 35%, mínimo 128 px), escalada para a resolução nativa do modelo (1 MP no Qwen, 1,5 MP no Krea), editada, reduzida de volta e costurada com a emenda da SPEC-108. Fora do recorte: original exato.
4. Trace: `region_shape_used`, `crop_box`, `crop_scale`, mais os `band_step_*` da SPEC-108.
5. No pod (experimento): você compara 4 variantes em 8 casos e a melhor vira padrão.

## Experimento (fase B)

- **Casos:** as três edições com área de 09/11 (01:57, 02:05, 02:06) e cinco novas (roupa, cabelo, fundo atrás da pessoa, objeto na mão, aumento de tamanho).
- **Variantes:** caixa sem recorte (hoje), silhueta sem recorte, caixa com recorte, silhueta com recorte. 2 sementes. 64 jobs, cerca de 15 min.
- **Medidas:** `band_step_final / band_step_source`, tempo, sua nota 1 a 5 de "emenda invisível" e "mudou o que pedi".
- **Regra:** padrão = maior nota média de emenda entre as variantes cuja nota de "mudou o que pedi" não cai mais de 0,5 em relação à atual.

## Spec técnica

- Workflow SAM: o mesmo grafo de `sam3-people-api.json` com o prompt de texto como entrada mapeada (`text`) e saída de máscara única (união) além das individuais.
- A chamada do SAM acontece **depois** de descarregar o Ollama (precisa da GPU), dentro do job, antes da edição; não no `inlet`.
- O recorte e a costura usam as funções da SPEC-108 sem alteração.

## Testes

- Unitários: interseção caixa × silhueta, queda para caixa, recorte com margem e limites da imagem, costura devolvendo o original exato fora.
- Job falso do SAM: a máscara chega à edição.

## Tarefas

1. Workflow e manifesto.
2. [P] Silhueta no preparo da área.
3. [P] Recorte e costura.
4. Caso de lab no `edit_lab.py` com as 4 variantes.
5. Fase C: padrão e remoção da variante perdedora, se perder por larga margem.

## Riscos

- O SAM não reconhece partes do corpo (ex.: uma parte do corpo pedida pelo usuário) tão bem quanto pessoas: o experimento mostra; a queda para a caixa protege.
- Tempo: um passe de SAM (~1,8 s medidos em 08/11) por edição com área.

## Notas da implementação

Fase A (padrões iguais ao comportamento de hoje: `region_shape = "box"`, `region_crop = false`).

- **Workflow e perfil.** `workflows/sam3-region-api.json` tem o mesmo grafo do `sam3-people-api.json` com o prompt mapeado (nó `3`, chave `text`) e dois ramos: o das máscaras individuais (nó `6`) e o da união (nó `9`, `PepitoRegion/union`). O filtro roda só a união: `graph_for_output` tira do grafo o ramo que não vai ler, então um passe custa uma detecção do SAM. Entra como a sexta parte de `character_swap` (`region`, manifesto `sam3_region`, mesmos pesos `swap.segment`); `CHARACTER_SWAP_PARTS`, o schema do perfil e os testes de listas foram atualizados. Um perfil sem a parte nova deixa de carregar, como acontece com qualquer parte que falte.
- **Funções novas em `pepito_image_preservation.py`** (as da emenda, `preserve_pixels` e `seam_composite`, não mudaram): `silhouette_within` (interseção silhueta × caixa, borda suavizada; `None` sem detecção dentro da caixa), `crop_plan`, `crop_source` e `stitch_crop` (a costura chama `preserve_pixels` sobre o recorte e cola o resultado na origem, então fora da área e fora do recorte o original é exato). Elas ficam no módulo da emenda, e não no filtro, para o laboratório carregá-las pelo caminho, como faz com `pepito_edit_methods`.
- **Quando o SAM roda.** `prepare_local_preservation` continua no `inlet` e só guarda o limite (`job["region"]["limit"]`: a área como achada, antes do crescimento) e o que o crescimento precisa; o SAM roda em `edit_chat_image`, depois do Ollama descarregado e com a GPU do turno (`silhouette_area`). A dilatação do tipo de mudança (`GEOMETRY_DILATION`, "nada mudou") e a união com a área anterior da mesma parte do corpo valem sobre a silhueta; sem detecção (ou erro, ou nada dentro da caixa) a área é a caixa já calculada, com `region_shape_used: "box"` e o motivo no evento `region_silhouette`.
- **Um passe de SAM por alvo** do roteador (no máximo 4), cada um com `<alvo>:1000`; a união vai para a interseção com a caixa. Os `region_targets` vêm em inglês do roteador e o SAM os recebe como estão.
- **Recorte.** Só uma edição de uma imagem, com área (automática ou pintada), partes `edit`/`remove`, sem deformação (`warp*`) e sem mudança de formato. A margem é 35% da largura e da altura da área, mínimo 128 px, dentro da imagem; o tamanho é o de 1 MP (Qwen, `qwen21`) ou 1,5 MP (Krea, `krea2`) em múltiplos de 32. Um recorte que cobriria 90% da imagem ou mais é a imagem inteira: não recorta (`region_crop` com `status: skipped`). Família sem tamanho nativo, ou falha ao recortar, edita a imagem inteira.
- **Trace.** `image_job.params` ganhou `region_shape_used` (`box`/`silhouette` numa área automática, `null` sem ela), `crop_box` e `crop_scale`; `width`/`height` em `params.size` são os do que o modelo recebeu. Os `band_step_*` já vinham no evento `preservation`.
- **Valves** (`UserValves`): `region_shape` (lista `box`/`silhouette`) e `region_crop`.
- **Laboratório.** `scripts/edit_lab.py` ganhou as variantes `R0_box`, `R1_silhouette`, `R2_box_crop` e `R3_silhouette_crop` (receita de produção do Krea: Turbo, `ref_boost` 1,5, grounding 768). A área de caixa é a do filtro (`region_mask` das caixas do caso); a silhueta é rodada no ComfyUI (um passe por alvo) antes das variantes e a GPU é liberada depois. Os alvos vêm de `region_targets` em cada caso do `cases.json` (o `prepare` os copia do evento `region`; sem eles, edite à mão). Os oito casos do experimento (01:57, 02:05 e 02:06 de 09/11 e as cinco novas) dependem de fontes e caixas que não estão neste ramo: ficam para o pod. Sem numpy e OpenCV no ambiente do ComfyUI, `band_step_*` não sai no laboratório; os brutos são salvos para medir no PC.
