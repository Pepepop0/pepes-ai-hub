# Como a indústria faz edição de imagem e o que isso significa para a Capella

Data: 02/11/2026. Complementa o diagnóstico do material do pacote privado. Fontes primárias consultadas nesta data; afirmações sem fonte marcadas como inferência.

## 1. Mudança de forma é o ponto fraco de todo mundo

- O Google publica no card do **Nano Banana 2.1**, entre as limitações: a pose da imagem de entrada tende a ser mantida no resultado, há confusão entre esquerda e direita e o seguimento de instruções em edição com máscara é parcial ([model card](https://deepmind.google/models/model-cards/nano-banana-2-1)).
- Taxa de sucesso por tipo de edição no Pico-Banana-400K (modelo fechado do Google): troca de estilo **0,934**, remoção de objeto **0,833**, mudança de tamanho/forma/orientação **0,663**, mover objeto **0,592** ([arXiv 2510.19808](https://arxiv.org/html/2510.19808)).
- KRIS-Bench: o Step1X-Edit tira **33,25** em atributos e **8,00** em edições espaciais; os autores relatam o mesmo padrão em modelos fechados ([arXiv 2505.16707](https://arxiv.org/html/2505.16707)).
- GEditBench v2 chama de "armadilha da sub-edição": modelos que mudam pouco ganham nota alta de consistência. GPT Image 1.5 tem 1260 em seguir instrução e 846 em consistência; Nano Banana Pro, 1126 e 1108 ([arXiv 2603.28547](https://arxiv.org/html/2603.28547v1)).

O nosso caso (troca de cor muda 19,3; aumentar volume muda 3,7–6,3) é a versão extrema desse padrão.

## 2. Como a OpenAI faz

- **Pipeline com planejador:** na Responses API, o modelo de chat reescreve o pedido (`revised_prompt`) e chama a ferramenta de imagem; a edição multi-turno usa a imagem anterior como contexto ([guia de imagem](https://developers.openai.com/api/docs/guides/image-generation)). É a mesma forma que a Capella usa: roteador → modelo de imagem com a fonte no contexto.
- **Máscara é sugestão, não recorte:** "Masking with GPT Image is entirely prompt-based. The model uses the mask as guidance, but may not follow its exact shape" (mesmo guia). O modelo regera o quadro inteiro.
- **Preservação exata é composição feita por quem usa:** o guia de prompting manda compor a edição aprovada sobre o original quando uma região precisa ficar idêntica ([image prompting](https://developers.openai.com/api/docs/guides/image-prompting)). É exatamente o que a Capella já faz.
- **Sem botão de força:** o `input_fidelity` existia no gpt-image-1.5 e foi removido do gpt-image-2 em diante; o modelo atual sempre trata a entrada em alta fidelidade.
- **Limitações declaradas:** consistência entre gerações, controle de composição, e "edições repetidas ainda podem mudar detalhes que você queria preservar". A ferramenta de seleção do ChatGPT avisa que a edição pode sair da área marcada ([ajuda](https://help.openai.com/en/articles/11084440-images-in-chatgpt)).

**Conclusão:** a arquitetura da Capella já é a da OpenAI. A diferença está no modelo: a OpenAI treina um editor de fronteira com volumes enormes de pares de edição; nós usamos um adaptador comunitário, treinado só com dados SFW, sobre uma mesclagem que o autor nunca validou, em modo destilado (CFG 1).

## 3. Como os outros fazem mudança de geometria

| Quem | Como especifica a mudança | Fonte |
|---|---|---|
| BFL **FLUX 3 Image / Klein** | Tabela de caixas: `src_bbox` → `tgt_bbox` move ou **redimensiona** o elemento; linhas *Keep* ancoram o resto | [docs BFL](https://docs.bfl.ml/flux_3/flux3_image_bounding_boxes) |
| ByteDance Seedream 5.0 Pro | Pontos, laço, caixa e rabisco viram instruções locais determinísticas | relatório ClosedLeaders |
| Ideogram Precise Edit | Máscara entra no modelo **e** os pixels fora dela são copiados exatamente | relatório ClosedLeaders |
| Fluxos profissionais locais | Deformar (warp/liquify) a região e depois harmonizar com difusão em baixa intensidade | relatório LocalPipelines |

O padrão comum: quem obedece a mudança de forma **diz ao modelo a geometria de destino** (caixa, contorno, deformação). Só a frase "deixe maior" é a forma mais fraca de pedir.

## 4. O que ainda não testamos no nosso próprio editor

O autor do adaptador documenta três ajustes que a Capella nunca usou para estes pedidos ([ComfyUI Wiki](https://comfyui-wiki.com/en/news/2026-10-16-krea2-edit-v2), [repo dos nós](https://github.com/lbouaraba/comfyui-krea2edit)):

1. **Raw, CFG 3, 20 passos, negativo ancorado na mesma imagem.** É a configuração dele para mudanças grandes. Em CFG 1 (Turbo) não existe orientação pelo texto; a única alavanca de "obedecer ao texto" fica desligada. O modelo Raw **já está instalado** na Capella (usado em remoções).
2. **`grounding_px` menor (384–512).** "Valores menores seguem a instrução com mais força." Usamos sempre 768.
3. **Descrever o resultado, não a operação.** O autor diz que o prompt pesa mais que os botões: descrever a aparência final concreta em vez de "aumente X".

`ref_position=beside` também existe, mas o autor treinou só com `aligned`.

## 5. Caminhos possíveis, do mais barato ao mais caro

| # | Caminho | Custo | Download | Evidência |
|---|---|---|---|---|
| A | Editor atual com Raw + CFG 3 + 20 passos + `grounding_px` menor | Baixo | Nenhum | Recomendação do autor para mudanças grandes; não medido em forma |
| B | Gerador repinta uma área que cobre **origem e destino**, intensidade total, composição depois | Baixo–médio | Nenhum | Mecanismo do sampler mascarado; sem benchmark |
| C | Deformar a região em Python e harmonizar com o gerador em baixa intensidade | Médio | Nenhum | Prática profissional; único caminho com magnitude e lado exatos |
| D | Qwen-Image 2.1 (Capella SFW) com máscara/círculo | Baixo | Nenhum | Card promete edição local; não medido em forma |
| E | FLUX 3 Image Klein com caixas `src_bbox → tgt_bbox` | Médio–alto | ~8 GB INT8 | Documentado para objetos; não para proporção do corpo; licença Kommunity exige filtro/revisão de conteúdo |

## 6. Recomendação

Não trocar de modelo nem reescrever o filtro às cegas. Primeiro um **laboratório de edição** que roda numa sessão de GPU (~30–40 min):

- Mesma imagem de origem e mesma semente para todas as variantes; casos neutros (casaco mais longo, bolsa maior, só a manga direita) e os casos reais.
- Variantes A, B, C e D (todas sem download). E fica para uma segunda rodada.
- Saída: PNG bruto de cada variante lado a lado, com a medida de mudança dentro e fora da área e o tempo.

O que obedecer de verdade vira o caminho de "mudança de forma" no roteador; o editor atual continua para cor, roupa, fundo, luz e remoções. Os 7 bugs do diagnóstico do material do pacote privado são corrigidos em qualquer cenário.

Relatórios completos desta pesquisa (sessão de trabalho): `research-closed-leaders.md`, `research-open-editors.md`, `research-local-pipelines.md`.
