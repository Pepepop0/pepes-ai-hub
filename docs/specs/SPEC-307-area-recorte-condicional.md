# SPEC-307 — Área: recorte só em área pequena, silhueta fora, ambíguo com uma pessoa

> Registro técnico SFW. As referências históricas a `edit_lab.py`, seus testes e fontes experimentais pertencem ao pacote privado e não são comandos ou fixtures disponíveis no público. A implementação pública de métricas e avaliação compartilhadas é `scripts/lab_common.py` / `scripts/lab_review.html`.

| Campo | Valor |
|---|---|
| Onda | 3 |
| Cobre | IMG-12, decisão do lab 204, CHAT-12 (P8 do pod) |
| Prioridade / esforço | P1 / M |
| Depende de | — |
| Possui | `prepare_local_preservation` e o despacho da edição com área (`region_crop`, `region_shape`), `workflows/sam3-region-api.json` e sua entrada em `manifests/workflows.yaml` / `service_config.py` / `profiles/capella.yaml` / `schemas/profile.schema.json` (se a silhueta sair), as variantes R do `scripts/edit_lab.py` |
| Pod | Validar |

## PRD
**Decisão** (avaliação `Downloads\avaliacao-edit-lab (1).md`): caixa+recorte (R2) foi a melhor nas 5 ilustrações (A1–A5); nas fotos de 09/11 (B1, B2) o recorte cobria quase a imagem toda e quebrou — ali a caixa sem recorte (R0) venceu. Silhueta (R1, R3) perdeu nos dois tipos.

**Critérios de aceite.**
1. Recorte automático quando o recorte (área + contexto) cobre menos de metade da imagem; senão, imagem inteira com a caixa (R0). Limiar em constante, registrado no trace (`crop_used`, `crop_share`).
2. Silhueta removida (Valve `region_shape`, grafo `sam3-region`, a parte `region` do `character_swap` e as variantes R1/R3 do lab), sem código morto; o SAM continua para a troca.
3. Localizador `ambiguous` com uma pessoa só na imagem (ou confiança ≥ 0,8 com uma caixa cobrindo a pessoa) → segue na imagem inteira, `region_skipped reason=ambiguous_single`. Caso real 2026-11-12T23:15:18 ("Confirmo").
4. "Corte seco" continua aberto (estrutura): registrar como pendência nova no PLANO, sem tentar resolver aqui.

## Notas da implementação

- **Recorte automático, sem Valve.** `crop_plan` passa a devolver também `share` (fração exata da imagem coberta pelo recorte, já com contexto). `REGION_CROP_MAX_SHARE = 0.5`: só usa o recorte quando `share < 0.5`; exatamente metade usa imagem inteira + caixa. As margens continuam 35%, mínimo 128 px; tamanhos nativos 1 MP/Qwen e 1,5 MP/Krea. Valem as restrições anteriores: uma fonte, `edit`/`remove`, área automática ou pintada, sem deformação nem mudança de formato. Sem área/família suportada, não recorta. `image_job.params` registra `crop_used`, `crop_share`, `crop_box`, `crop_scale`; o evento `region_crop` registra a decisão e o limite. R2 do lab segue a mesma regra; R0 continua sempre na imagem inteira.
- **Silhueta removida.** Saem `region_shape`, `region_crop` (a escolha agora é automática), `region_options`, detecção/refino da área pelo SAM, recorte de grafo só para a união, `silhouette_within`, workflow `sam3-region`, manifesto e parte `character_swap.region`. A constante das partes fica em `profiles.py`, não em `service_config.py`; este já consome as partes do perfil e não precisou mudar. Os cinco passes da troca, incluindo `sam3_people`, permanecem. R1/R3 e seus ajudantes exclusivos saem do laboratório; a geração das páginas não foi alterada.
- **Como reconhecer uma pessoa só.** O filtro não tinha contagem de pessoas. `description` são instruções de geração truncadas, não observações visuais; uploads não as têm. O plano textual do roteador não conta pessoas; `swap_plan` só existe na troca e seus `keep_boxes` são sobreposições/texto. Portanto `ground_regions` agora pede `people_count` na **mesma chamada/schema de visão**, sem novo detector ou inferência. `-1` significa desconhecido, `0` nenhuma pessoa; nunca se assume 1 por padrão. O campo é observação do modelo, não prova geométrica.
- **Escolha operacional para `ambiguous_single`.** Exige `status=ambiguous`, `people_count` inteiro exatamente 1 e confiança numérica ≥ 0,8. Ausente/desconhecido, duas pessoas (inclusive sobrepostas), zero pessoas e baixa confiança continuam pedindo seleção. Caixas aninhadas não provam pessoa única e não promovem a edição. A área anterior continua prioritária numa correção, e área pintada não passa pelo localizador. `region_skipped reason=ambiguous_single` registra confiança e contagem; `region_refused.answer` conserva a observação para diagnóstico.
- **Fixture real.** `2026-11-12T23:15:18` (“Confirmo”) entrou em `router_cases.json` a partir do trace, com `router_raw` e `grounding_answer` históricos inalterados. O trace antigo não tem contagem: sua expectativa permanece recusa de região, sem inventar `people_count=1`. Um teste sintético explícito repete o mesmo turno com nova resposta visual `people_count=1` e prova o job na fonte 4, sem máscara, com `ambiguous_single`. Tabela da SPEC-112 regenerada. Restrição acordada na implementação: a correção desse caso real depende da observação do modelo no próximo pod, não se infere contagem das quatro caixas aninhadas.
- **Pendência nova para integração no PLANO:** “Corte seco estrutural na borda da edição local” (A2/A5/B1 do lab 204; manter aberta, sem corrigir aqui). Por contrato deste worktree, `docs/PLANO.md` não foi editado; integrar esta pendência na SPEC-316. A emenda de tom permanece como estava.

### Provas e validação no pod

- `test_region_crop.py`: seleção automática, contexto/resolução, trace, imagem inteira quando grande, fronteira exata de 50%, ausência das Valves/SAM de região.
- `test_region_geometry.py`: contexto, fração do recorte, costura e pixels externos; os testes de costura requerem swap-venv (numpy/OpenCV).
- `test_region_ambiguous_single.py`: contagem na mesma inferência, limiar 0,8, desconhecido/duas pessoas sobrepostas/baixa confiança, prioridade da área anterior, fixture histórica imutável e replay sintético com contagem explícita.
- `test_edit_lab.py`: apenas R0/R2, nenhum passe SAM, R2 condicional e preservação externa; testes de perfil/workflows/empacotamento cobrem a retirada do grafo sem retirar o SAM da troca.
- Pod: repetir A1–A5/B1–B2, confirmar o pedido real das 23:15:18 e testar duas pessoas sobrepostas. Julgar aderência e continuidade estrutural, que testes de pixels não provam.
