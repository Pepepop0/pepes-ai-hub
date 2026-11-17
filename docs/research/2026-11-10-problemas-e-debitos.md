# Relatório 1 — Problemas e débitos técnicos (estudo + PRD por item)

Data: 10/11/2026. Base: ramo `test/all-in-one` em `049a8a2` mais as mudanças sem commit.
Nada foi alterado no repositório nesta pesquisa.

## Como ler

- **Fato** = li no código (cito `arquivo:linha`), no trace da sessão (`sessao-final-20261109-2312`, horário UTC) ou medi por script.
- **[INFERÊNCIA]** = conclusão minha, sem prova direta.
- **[NÃO VERIFICADO]** = referência externa citada de memória ou só pelo título/trecho da busca.
- Não abri nenhuma imagem das sessões. As medições de imagem são só números.
- Não havia GPU. Nenhuma correção proposta aqui foi testada gerando imagem.

## Resumo: o que mudou no diagnóstico

| # | Problema | Complexidade (revista) | Causa | Estado da causa |
|---|---|---|---|---|
| 2 | Colagem com degrau | Alta | O Krea redesenha a imagem inteira (fora da máscara muda 20–35 níveis) e nós colamos só o miolo sobre o original | **Confirmada por medição** |
| 3 | "Refaça" repete a imagem | Média-alta (era Alta) | Ferramenta errada: pedido de "refazer/mudar o estilo da imagem toda" vai para o adaptador de edição local, com prompt que manda manter tudo. Semente **não** é a causa | **Confirmada em parte** (semente descartada; falta teste no pod do caminho alternativo) |
| 1 | "Botão desligado" | Média (era Média-alta) | O nosso próprio texto de contexto manda o modelo pedir "com o Image ligado", e o filtro joga fora a pergunta de esclarecimento e o plano do roteador quando a ação é `chat` | **Confirmada no código e no trace** |
| 5 | Modelo escreve prompt em vez de gerar | Média | Mesmo mecanismo do item 1 | [INFERÊNCIA] forte; texto da resposta não está no trace |
| 4 | Pedido global recusado/perguntado | Baixa-média (era Média) | Dois `raise` em `prepare_local_preservation` | **Confirmada** |
| 6 | Esc desliga os botões | Baixa-média | Código do próprio Open WebUI 0.12.0 | **Confirmada no código upstream** |
| 9 | Palavras do balão trocadas | Média | Compositor não sabe onde há texto | Conhecida |
| 12 | Qwen-Edit 1 MP | Média (dívida) | Redimensionamento interno do nó de codificação | Conhecida |
| 11 | Primeira resposta 57 s | Baixa | Carga fria do modelo de 20 GB | **Confirmada** |
| 7 | Texto `['...']` ignorado | Baixa | Função nova | — |
| 8 | Zoom e fundo | Baixa | Feito; achei 3 pontos a ajustar | Revisado |
| 10 | Q3/Q4, texto duplicado | Sem correção | Conteúdo do modelo | — |
| 13, 14 | Validar 1.19 e commitar | Baixa | Processo | — |

Três achados novos que não estavam na lista:

- **N1.** Cada turno de imagem recarrega o modelo de chat só para ele dizer uma frase de 15 a 20 tokens: 6,1 s de carga em um turno de 27 s (22% do turno).
- **N2.** A correção automática "Quase nada mudou; refazendo com mais liberdade" não existe mais no código: a medição é só registrada no log (`pepito_image_chat.py:6275`). O checklist da Capella ainda descreve o comportamento antigo.
- **N3.** A região automática do localizador é um retângulo (o da 02:05 preenche 99% da sua caixa). Isso deixa a emenda reta e visível mesmo quando a cor está certa.

---

# Item 2 — Colagem com degrau nas edições com máscara

## Contexto e evidência

Você viu cortes retos com troca de tom nas imagens `erro-1..3.png`. No trace, as edições com máscara têm `outside_exact=true` e `inside_rgb_mad` de 10 a 37.

## Algoritmo atual (fato)

`preserve_pixels` em `pepito_image_preservation.py`:

1. Redimensiona o resultado do modelo para o tamanho da origem (Lanczos).
2. `_boundary_illumination` tenta corrigir **só** um desvio de brilho pequeno, acromático e uniforme. Ela desiste se o desvio médio for maior que 32, se os canais divergirem mais de 3 níveis, ou se o desvio-padrão no anel de fora passar de 3.
3. `Image.composite(result, original, mask)`: cola o resultado dentro da máscara, original fora.

A máscara automática (`region_mask`) é a união de retângulos do modelo de visão, com desfoque gaussiano de `min(lado)/250` px (cerca de 3 px numa imagem de 704 px) **só para dentro** (`ImageChops.multiply(blur, hard)`). A borda externa é seca de propósito, para garantir `outside_exact`.

## Medição (fato)

Script descartável em `%TEMP%\degrau_medir.py`, com numpy/OpenCV do `swap-venv`. Para cada edição com máscara achei a origem, o resultado entregue (uploads) e a saída crua do ComfyUI (`apps/comfyui/output`). A máscara foi reconstruída como "onde o entregue difere da origem".

Métrica "degrau": na linha da borda, diferença de luminância entre a média logo dentro e a média logo fora (gaussiana de 4 px). O valor da **origem** é o degrau natural da cena naquele lugar; é a referência.

| Edição (UTC) | Modelo | Degrau na origem | Degrau no entregue | Saída crua do modelo | O modelo mudou **fora** da máscara (MAD) | Desvio médio de cor fora (B, G, R) |
|---|---|---|---|---|---|---|
| 01:57 automática | Qwen | 23,3 | 23,1 | 23,1 | 3,8 | −2,9 / −2,5 / −2,4 |
| 02:05 automática | Krea General Edit | 12,9 | **33,9** (2,6×) | 11,1 | **27,7** | **+18,8 / +25,3 / +45,1** |
| 02:06 pincel | Krea General Edit | 11,5 | **21,7** (1,9×) | 12,2 | **21,5** | −7,9 / −7,3 / −5,2 |

Leitura:

- **No Qwen não há degrau.** O modelo preserva o resto da imagem (muda 3,8 níveis fora) e a colagem fica invisível.
- **No Krea o degrau é nosso.** A saída crua do modelo é contínua (11 a 12, igual à origem). O degrau aparece quando colamos o miolo dessa imagem sobre o original, porque o Krea redesenhou tudo: fora da máscara a imagem dele difere 21 a 28 níveis da original, com o vermelho 45 níveis acima em um caso.
- A correção de iluminação atual nunca dispara nesses casos: os limites dela (canais a no máximo 3 níveis um do outro, desvio-padrão até 3) foram feitos para o desvio pequeno do Qwen.
- Na 02:05 a máscara é um retângulo quase perfeito (preenche 99% da caixa): é o "bloco de outro tom com borda reta" que você descreveu.

Por que o Krea redesenha tudo (fato, `workflows/krea2-general-edit-v2-api.json`): o grafo parte de um latente vazio (`EmptySD3LatentImage`), com `denoise: 1`, 8 passos e `cfg: 1`. A imagem original entra só como referência (`Krea2EditModelPatch`, `ref_boost`). Não há máscara no grafo: o modelo não sabe que existe uma área a preservar.

## Candidatos medidos (mesmos três casos, sem GPU)

| Técnica | 02:05 (origem 12,9) | 02:06 (origem 11,5) | Fora da máscara continua exato? | Observação |
|---|---|---|---|---|
| Atual | 33,9 | 21,7 | Sim | — |
| B. Desfoque dos dois lados | 21,6 | 15,2 | Não (1,6–2,1% dos pixels de fora mudam) | Só borra o degrau |
| **C. Correção de desvio de baixa frequência + rampa só para dentro** | **12,2** | **12,3** | **Sim** | Mede o desvio (origem − cru) no anel de fora, difunde para dentro com decaimento, e faz a transição dentro da máscara |
| D. Poisson (`cv2.seamlessClone`) | 12,3 | 12,4 | Quase (borda de 1 px) | 0,1 s; mas desloca a cor **do miolo** em 13,7 a 21,1 níveis: muda o que você pediu |
| E. Pirâmide laplaciana | 14,1 | 12,4 | Não (5–6% mudam) | — |

A técnica C é a que o compositor G2 da troca já usa (`drift_rgb`, `band_step_raw` → `band_step_corrected` no trace da troca).

**Limite desta medição:** a métrica mede salto de **tom**. Não mede se uma linha (um braço, a borda de um móvel) continua no mesmo lugar dos dois lados. Com o Krea mudando 21 a 28 níveis fora da máscara, é provável que a **estrutura** também tenha saído do lugar [INFERÊNCIA]. A técnica C resolve o tom; não resolve a estrutura. Só os seus olhos ou um teste no pod dizem quanto sobra.

## Como projetos de referência resolvem

| Projeto / técnica | O que faz | Serve para modelo de edição por instrução? |
|---|---|---|
| ComfyUI-Inpaint-CropAndStitch ([nós suportados](https://comfy.org/cloud/supported-nodes/comfyui-inpaint-cropandstitch/), [Inpaint Stitch](https://comfy.icu/node/InpaintStitchImproved)) | Recorta a área da máscara com margem de contexto, edita só o recorte e cola de volta "sem tocar em mais nada", com máscara de mistura | Sim: é agnóstico de modelo. [NÃO VERIFICADO além da descrição] |
| `DifferentialDiffusion` ([nó do ComfyUI](https://comfy.icu/node/DifferentialDiffusion)) | Máscara em tons de cinza controla quanto cada pixel pode mudar durante a amostragem | Precisa partir do latente da origem; com `denoise 1` e 8 passos turbo exige experimento [NÃO VERIFICADO] |
| Stitch dedicado ao Qwen ([Qwen Image Stitch](https://comfy.icu/node/ArchAi3D_Qwen_Image_Stitch)) | Par "escala/recorte" + "costura" para Qwen-Image-Edit | Mostra que a comunidade trata o mesmo problema com recorte e costura [NÃO VERIFICADO] |
| Krita AI Diffusion, InvokeAI (canvas), Fooocus inpaint | Recorte com contexto, máscara com desfoque, passe de coerência na emenda, correção de cor | [NÃO VERIFICADO: de memória, não abri as fontes] |
| Poisson / pirâmide (clássicos) | Mistura no domínio do gradiente ou por frequência | Medidos acima |

## Objetivos

- Degrau na borda no máximo 1,25× o degrau natural da origem, nas edições com máscara do Krea.
- Fora da máscara continua byte a byte igual (`outside_exact=true`).
- O miolo da área (o que você pediu) não muda de cor por causa da emenda.
- Nenhuma regressão no Qwen, onde hoje não há degrau.

## Não-objetivos

- Corrigir erro de conteúdo do modelo (anatomia, objeto errado).
- Trocar o modelo de edição.

## Solução proposta, em três fases

**Fase 1 — emenda corrigida no pós-processo (sem GPU, entrega já):**

Em `pepito_image_preservation.py`, substituir a dupla `_boundary_illumination` + `Image.composite` por uma função `seam_composite(original, result, mask)`:

```
anel = dilatar(mask, r) − mask                    # r ≈ min(lado)/40
desvio = original − result, medido só no anel
campo = desfoque_normalizado(desvio, sigma ≈ min(lado)/20)   # baixa frequência
dist = distância até a borda, para dentro
corrigido = result + campo · exp(−dist / (3·sigma))          # some no miolo
rampa = clip(dist / (2·feather), 0, 1)                       # 0 na borda, 1 dentro
saida = corrigido·rampa + original·(1 − rampa)               # fora: original exato
```

- Reaproveitar a implementação do G2 (`pepito_swap_compose.py`) em vez de escrever outra. O Open WebUI 0.12.0 já tem numpy e OpenCV (decisão registrada).
- **Trava de segurança:** se o MAD do anel de fora passar de um limiar (sugestão inicial 12; os casos ruins medem 20 a 35, o Qwen 3 a 4), registrar `seam_mode=redrawn` no trace. Nesse caso a emenda de tom é aplicada, e a Fase 2 decide o resto.
- Novos campos no trace `preservation`: `band_step_source`, `band_step_raw`, `band_step_final`, `outer_ring_mad`. Assim o problema passa a ser mensurável em toda sessão.

**Fase 2 — máscara com silhueta e recorte com contexto (precisa de pod):**

- **Silhueta:** a área automática é um retângulo. O SAM 3.1 já está instalado para a troca (`workflows/sam3-people-api.json`, prompt textual). Usar os `region_targets` do roteador como prompt do SAM e a caixa do modelo de visão como limite. Se o SAM não achar nada, cai no retângulo de hoje.
- **Recorte e costura:** editar só o recorte (máscara + margem de contexto de 25 a 40%), no tamanho nativo do modelo, e costurar com a Fase 1. O modelo vê menos cena para redesenhar e a área ganha resolução.
- Medir no pod se `ref_boost` mais alto nas edições com máscara reduz o quanto o Krea mexe fora (na 02:05 foi 2,5).

**Fase 3 — amostragem com máscara de verdade (experimento):**

Testar no grafo do Krea: partir do latente da origem (`VAEEncode`, nó 73, já existe) com `SetLatentNoiseMask` e/ou `DifferentialDiffusion`. Só vale se a Fase 2 não bastar. Risco: o adaptador foi treinado partindo de latente vazio.

## Alternativas descartadas

- **Poisson:** resolve a borda mas muda a cor do miolo em 14 a 21 níveis.
- **Desfoque dos dois lados / pirâmide:** quebram a garantia de fora exato e só disfarçam.
- **Mandar tudo para o Qwen:** o Qwen preserva bem, mas o Krea é o modelo de geração da variante privada; seria trocar de modelo.

## Critérios de aceite

1. Nos três pares da sessão, `band_step_final ≤ 1,25 × band_step_source` (hoje 2,6× e 1,9× no Krea).
2. `outside_exact=true` em 100% das edições com máscara.
3. No miolo (erosão de 15 px da máscara), diferença para a saída crua ≤ 2 níveis.
4. Caso Qwen (01:57): resultado igual ao de hoje, com tolerância de 1 nível.
5. No pod: você olha 6 edições com máscara (3 automáticas, 3 com pincel) e não aponta corte reto.

## Plano de testes

- **Local:** teste sintético em `tests/test_image_quality_preservation.py` (fundo com gradiente, "resultado" com desvio de cor global de +20, máscara retangular e máscara irregular): exige os critérios 1 a 3. Teste de não-regressão com desvio pequeno (caso Qwen).
- **Regressão com dados reais:** guardar as métricas dos três pares (não as imagens) como valores esperados num teste que roda só se a pasta da sessão existir.
- **Pod:** repetir "Quase, tem alguém segurando…" e "Faltou o resto das pernas dele" da sessão; conferir os campos novos do trace.

## Riscos

- A correção de tom pode deixar a área com cor "puxada" se o desvio não for uniforme. O decaimento para o miolo limita isso; o critério 3 mede.
- Silhueta do SAM menor que a mudança pedida (ex.: aumentar algo). A dilatação por tipo de mudança que já existe (`GEOMETRY_DILATION`) continua valendo.
- O que funciona hoje e não pode quebrar: edição por região automática no Qwen ("uma edição de forma corporal (a body-shape edit)"), `outside_exact`, "nada mudou" com crescimento de área.

## Esforço

Fase 1: pequeno a médio (1 a 2 dias, sem pod). Fase 2: médio, com uma sessão de pod. Fase 3: só se necessário.

---

# Item 3 — "Refaça" e ajustes repetem a imagem

## Evidência (fato, trace)

| UTC | Texto | Parte | `ref_boost` | `change` | Fonte | Tempo do job | Diferença para a origem |
|---|---|---|---|---|---|---|---|
| 01:47:08 | "Faz uma versão fotorrealista dessa personagem" | edit | 2,5 | moderate | imagem 1 | 12,5 s | (tamanho mudou) |
| 01:48:47 | "Faça mais realista… foto de uma pessoa na vida real" | edit | 2,5 | moderate | imagem 2 | 12,7 s | 3,57 |
| 01:49:37 | "Você só repetiu a imagem, refaça ela do zero" | edit | 1,5 | moderate | imagem 3 | 12,5 s | 3,55 |
| 01:50:22 | "Literalmente nada mudou, só refaça ela e ponto" | edit | 1,0 | strong | imagem 4 | 12,5 s | 2,75 |
| 02:08:06 | "…pode 'harmonizar' ela? redesenhar bem similar mas corrigir os erros?" | edit | 4,0 | subtle | imagem 14 | 14,1 s | 9,31 |
| 02:09:11 | "Nada mudou" | edit | 3,0 | subtle | imagem 14 | 14,1 s | 9,98 |

Para comparar: os `restage` da mesma sessão diferem 39 a 75 da origem.

## Hipóteses, da mais forte para a mais fraca

**H1 — ferramenta errada (forte).** Todos os seis pedidos pedem para **refazer ou mudar o estilo da imagem inteira**. O roteador acertou o plano (`scope: global`), mas a parte escolhida foi `edit`: o adaptador General Edit V2 com a referência "alinhada", feito para mudança local preservando o resto. Ele preservou. A favor: baixar o `ref_boost` de 2,5 para 1,5 e para 1,0 (o mínimo, `SHAPE_RETRY_FLOOR`) não mudou nada (3,57 → 3,55 → 2,75). A alavanca está no fim do curso.

**H2 — o prompt se contradiz (forte, soma com H1).** O prompt enviado foi: "Regenerate the image from scratch… Keep the character's face, hair, pose, background, framing unchanged… Keep unchanged: character identity; pose; background; framing". `intent_prompt` (`pepito_image_chat.py:2803-2811`) sempre acrescenta a lista "Keep unchanged". Para um pedido de "refazer", o prompt manda manter quase tudo.

**H3 — a escada de correção só tem um degrau (confirmada no código).** Depois de "nada mudou", `retry_decision` (`:3670-3761`) repete a mesma parte com `ref_boost` um ponto menor (`edit_strengths`, `:3113-3116`). Não troca de ferramenta. No caso "harmonizar", a escada começou em 4,0 (`subtle`) e desceu para 3,0: ainda no lado "fiel".

**H4 — semente fixa ou cache do ComfyUI (descartada).**
- `seed=None` no trace quer dizer "o filtro não escolheu semente" (só escolhe quando o método é `qwen`/`warp`, `:7514`). O Open WebUI então sorteia: `seed = payload.seed if payload.seed else random.randint(0, 1125899906842624)` ([`utils/images/comfyui.py` v0.12.0, linha 182](https://github.com/open-webui/open-webui/blob/v0.12.0/backend/open_webui/utils/images/comfyui.py)), e o grafo tem o nó de semente mapeado (`manifests/workflows.yaml:171`).
- Os jobs levaram 12,5 s cada: foram executados, não vieram de cache.
- As diferenças variam (3,57, 3,55, 2,75): são imagens distintas, só que quase iguais.

**H5 — fonte errada (descartada para o caso 09cd5d).** Cada redo partiu do último resultado (imagens 2, 3, 4), como o código pretende após `no_change`.

## Como outros fazem

- ChatGPT e Gemini tratam "refaça" como nova geração com variação, não como reedição fiel. Midjourney separa "Vary (Subtle)" de "Vary (Strong)" e "Reroll". [NÃO VERIFICADO: de memória]
- O próprio Hub já teve uma nova tentativa automática quando a mudança medida era baixa ("Quase nada mudou; refazendo com mais liberdade", descrita em `docs/testing/capella-5090-checklist.md:184`). Hoje `measure_edits` só escreve no log (`:6275`).

## Objetivos

- "Refaça", "do zero", "mais realista", "harmonizar", "redesenhar" produzem uma imagem visivelmente diferente na primeira tentativa.
- "Nada mudou" nunca devolve a mesma imagem pela segunda vez.
- Edições locais que funcionam hoje não ficam mais soltas.

## Solução proposta

1. **Rotear pela intenção, não só pelo `edit_mode`.** Quando o plano vem com `scope: global` e as mudanças são de estilo, qualidade ou "refazer" (sem pose nova), usar uma parte de **redesenho**:
   - Variante Krea do pacote privado: `restage` (Identity Edit, referência "ao lado"), que na sessão mexeu 39 a 75.
   - Capella (Qwen): a edição do Qwen já aceita mudança global.
   - Acrescentar ao esquema do roteador um valor explícito, por exemplo `edit_mode: "redraw"`, com regra e exemplos ("versão fotorrealista", "refaça do zero", "harmoniza").
2. **Prompt sem contradição.** Em `intent_prompt`, quando o modo for redesenho, não anexar a lista "Keep unchanged" inteira; manter só identidade e o que o usuário citou ("mantendo esse fundo").
3. **Escada de escalada com troca de ferramenta.** Depois de `no_change`:
   - degrau 1: mesma parte, `ref_boost` menor (como hoje);
   - degrau 2: se já está no piso, ou se a diferença medida da tentativa anterior foi menor que o limiar, trocar para a parte de redesenho;
   - degrau 3: geração nova a partir da descrição da imagem (texto → imagem), avisando o usuário.
4. **Detecção automática de "saiu igual".** Reativar a medição como decisão: se `measured_change` < limiar e o nível pedido era `moderate` ou `strong`, refazer uma vez no degrau seguinte **antes** de entregar, com o aviso "Quase nada mudou; refazendo com mais liberdade". Limiar inicial: 6 (os casos ruins medem 2,7 a 3,6; o comentário do código registra "mudanças grandes que não apareceram: 5,7–8,8" em 21/10).
5. **Semente sempre nossa e sempre no trace.** Tirar a condição `selected_method and` de `:7514`, para o trace mostrar a semente efetiva. Não muda o comportamento; acaba com a dúvida.

## Alternativas

- **Só baixar mais o `ref_boost`:** descartado. O autor do adaptador documenta 1,0 como piso e a sessão 02 não mostrou ganho abaixo disso (comentário em `:2981-2984`).
- **Sempre gerar do zero em "refaça":** perde a identidade da personagem; fica como último degrau.

## Critérios de aceite

1. Repetindo no pod as quatro mensagens do chat `09cd5d`: diferença medida ≥ 15 já na primeira e você confirma que virou foto.
2. "Nada mudou" duas vezes seguidas: partes diferentes no trace (`edit` → redesenho → geração).
3. Edição local simples ("deixa o casaco azul"): mesma parte e mesmo `ref_boost` de hoje.
4. Todo `image_job` no trace tem `seed` numérico.

## Testes

- **Local:** testes de decisão em `tests/test_image_edit_dispatch.py` com as respostas reais do roteador desta sessão (estão no trace, campo `raw_json`): dado o plano das 01:47, a parte escolhida é a de redesenho; dado `no_change` com `boost` 1,0, a parte muda. Teste de `intent_prompt` sem "Keep unchanged" no redesenho.
- **Pod:** o roteiro do critério 1, mais o caso "harmonizar" na imagem remendada.

## Riscos

- Classificar como redesenho um pedido que era local. Mitigação: só quando `scope: global` e sem `region_targets`.
- Uma tentativa automática a mais custa cerca de 13 s.
- O `restage` hoje parte da **primeira** imagem da personagem (por desenho). Para "harmonizar a imagem atual" a fonte tem de ser a atual. É uma decisão de produto dentro deste item.

## Esforço

Médio (2 a 3 dias) mais uma sessão de pod.

---

# Itens 1 e 5 — "Ligue o botão Imagem" com o botão ligado; modelo escreve prompt em vez de gerar

## Evidência (fato)

Todos os turnos têm `enabled=yes`. Os dois turnos em que você recebeu a resposta errada:

| UTC | Texto | O que o roteador devolveu | O que o filtro fez |
|---|---|---|---|
| 01:35:14 | "Bom dia! troque esta personagem por esta (a loira pela mulher de cabelo preto)" | `action: chat` com `plan.clarification`: "Você quer trocar a loira da imagem 1 pela mulher de cabelo preto da imagem 2, ou o contrário?" | Ignorou a pergunta e passou o turno ao modelo de chat (263 tokens de resposta) |
| 01:58:25 | "a imagem toda" (resposta à nossa pergunta) | `action: chat`, mas com o plano completo: `scope: global`, `base: 7`, quatro mudanças, `change: strong` | Ignorou o plano e passou ao modelo de chat (151 tokens) |

Ou seja: nos dois casos o roteador **entendeu**. No primeiro ele queria perguntar; no segundo ele montou a edição inteira e errou só o campo `action`.

Há um terceiro caso igual na sessão de 08/11 (00:38:53): a mensagem "Esta personagem", resposta à nossa pergunta "Qual personagem devo trocar?", voltou do roteador como `chat` com uma mudança no plano. O padrão se repete: **resposta curta a uma pergunta do sistema → `chat`**.

## Causa raiz (fato, código)

1. **O nosso contexto manda o modelo falar do botão.** Quando a ação é `chat`, o filtro injeta `NO_IMAGE_CONTEXT` (`pepito_image_chat.py:153-159`), que termina com: *"If a picture was intended, ask the user to explicitly request it with Image on."* O modelo obedece e pede para ligar o Image. O prompt de sistema da Capella reforça ("Se o usuário quiser uma imagem e o botão Imagem estiver desligado, peça para ligá-lo", `profiles/capella.yaml:63-64`), sem que o modelo saiba o estado real do botão.
2. **A pergunta de esclarecimento é descartada.** No ramo `action == ACTION_CHAT` (`:7022-7031`) o filtro não olha `plan.clarification`. A pergunta só é usada quando a ação é de imagem.
3. **Um plano completo com `action: chat` é descartado.** Não há reconciliação entre `action` e `plan`.
4. **Resposta curta a uma pergunta nossa não tem estado.** `confirmed_picture_request` (`:2206-2229`) só reconhece "sim/pode fazer" depois de uma **oferta do modelo**. Depois de `GLOBAL_EDIT_QUESTION` ("responda 'toda a imagem'"), nada guarda que há uma pergunta pendente. O `PENDING_KEY` cobre só "tente de novo".
5. A saudação ("Bom dia!") não foi a causa direta no caso das 01:35: foi a pergunta descartada. As regras de reserva tratam saudação como conversa (`_CONVERSATION_START`, `:2081`), mas só entram quando o roteador falha.

**Item 5** (modelo escreve um prompt em inglês e diz "Me avise quando estiver pronto!"): é o que um modelo faz quando recebe `NO_IMAGE_CONTEXT` num turno em que o usuário queria imagem [INFERÊNCIA; o texto da resposta não está no trace porque o `webui.db` não veio].

## Como outros fazem

- ChatGPT e Gemini decidem a geração por chamada de ferramenta do próprio modelo; não existe um segundo classificador que possa discordar. [NÃO VERIFICADO]
- Em sistemas de diálogo, a pergunta de esclarecimento cria um estado ("slot pendente"), e a próxima mensagem é lida primeiro como resposta a ela. [NÃO VERIFICADO: prática geral]

## Solução proposta

1. **Reescrever `NO_IMAGE_CONTEXT`.** Dizer que o Image **está ligado**, que nada foi desenhado neste turno, e proibir de mencionar o botão. Última frase nova, em vez da atual: "The Image switch is ON. Never tell the user to switch it on. If the user wanted a picture, ask them in one short sentence to say what to draw or change."
2. **Honrar a pergunta do roteador.** Se `action == chat` e `plan.clarification` não está vazio: responder com a pergunta (`fixed_reply`) e gravar o pedido original como pendente.
3. **Promover plano completo.** Se `action == chat`, mas `plan.changes` não está vazio e (`plan.base > 0` ou há `references`): tratar como `edit_image` (ou `new_image` sem imagem). Registrar `source="promoted"` no trace.
4. **Estado de pergunta pendente.** Generalizar o `PENDING_KEY`: toda resposta fixa que é pergunta grava `{pedido original, tipo da pergunta}`. No turno seguinte o roteador recebe isso em `PLANNING CONTEXT` ("PENDING QUESTION: … ORIGINAL REQUEST: …"), e as regras tratam as respostas curtas conhecidas ("toda a imagem", "a imagem toda", "sim", "gere").
5. **Prompt de sistema.** Trocar a frase do botão por: "O sistema avisa em cada mensagem se o botão Imagem está ligado ou desligado; só peça para ligá-lo quando o sistema disser que está desligado."
   - **Migração:** o prompt é semeado uma vez e a edição do administrador é preservada. Atualizar o registro só se o texto salvo for **idêntico** ao texto semeado pela versão anterior (guardar o hash do texto semeado); se o administrador editou, não tocar.
6. **Guarda para o item 5.** O filtro já acrescenta "Nenhuma imagem foi criada nesta mensagem" quando o modelo afirma ter feito imagem. Estender a detecção para promessa de geração futura ("me avise quando", "vou gerar", bloco de prompt em inglês) nos turnos `chat` com Image ligado. É rede de proteção; a correção de fato são os passos 1 a 4.

## Critérios de aceite e casos de teste (mensagens reais)

| Entrada | Resposta do roteador (do trace) | Esperado |
|---|---|---|
| "Bom dia! troque esta personagem por esta (a loira pela mulher de cabelo preto)" | `chat` + `clarification` | O chat mostra a pergunta do roteador; nenhuma menção ao botão |
| "a imagem toda", após `GLOBAL_EDIT_QUESTION` | `chat` + plano completo | Edição da imagem inteira, sem outro turno |
| "gere", após recusa | — | Repete o pedido pendente |
| "oi, tudo bem?" com Image ligado | `chat` | Conversa normal, sem falar do botão |
| Image desligado + "gera uma maçã" | — | Continua pedindo para ligar (comportamento atual, correto) |

No pod: em 30 turnos com Image ligado, zero respostas mencionando o botão.

## Riscos

- Promoção de plano: o roteador pode preencher `changes` por engano numa conversa. A exigência de `base > 0` ou referências reduz; medir com os testes de `tests/test_image_router_reliability.py`.
- Migração do prompt em instalações com texto editado: não tocar é o comportamento seguro.

## Esforço

Médio-baixo (1 a 2 dias). Não precisa de GPU para os testes de decisão; confirmar no pod.

---

# Item 4 — Pedido que muda a imagem toda deve seguir direto

## Causa (fato)

Em `prepare_local_preservation` (`pepito_image_chat.py:4428`):

- `:4446-4449` — se a parte escolhida não aceita máscara (`restage`, `pose`), **recusa** com "Esta operação não permite preservar uma região local…". Foi o caso das 01:58:10: o roteador escolheu `restage` e marcou `scope: local`.
- `:4475-4482` — se o modelo de visão responde `status: global`, vira a pergunta `GLOBAL_EDIT_QUESTION` (casos das 02:00:09 e 02:02:01). Essa pergunta é a mudança sem commit de ontem.

Cada ocorrência custou um turno extra; uma delas ainda caiu no item 1.

## Solução

1. `:4446` — trocar o `raise` por `return`: a parte não aceita área, então a edição roda na imagem toda. Registrar `region_skipped reason=part_not_maskable`.
2. `:4475-4482` — `status: global` deixa de ser erro: seguir sem máscara. Se a parte atual for `edit` e o pedido for de pose ou enquadramento, usar a parte de redesenho do item 3.
3. Remover `GLOBAL_EDIT_QUESTION` e o teste que a fixa (`test_a_global_verdict_from_the_vision_model_asks_about_the_whole_picture`), trocando por "um veredito global segue na imagem toda".
4. **Manter a recusa** quando o usuário **pintou** uma área e pede pose/cena (`:7135-7149`): ali há conflito real entre o que ele marcou e o que pediu.
5. `status: ambiguous` continua pedindo seleção: ambiguidade não é "imagem toda".

## Critérios de aceite

- As três mensagens da sessão (01:58:10, 02:00:09, 02:02:01) geram imagem no mesmo turno.
- Com área pintada + pedido de pose: mesma recusa de hoje.

## Riscos

Uma edição que deveria ser local e que o modelo de visão julga global passa a mudar a imagem toda. Antes disso o usuário era perguntado. É exatamente a troca que você pediu; o `preserve` do plano continua no prompt.

## Esforço

Pequeno (meio dia), junto com o item 1.

---

# Item 6 — Esc desliga os botões de geração

## Causa (fato, código do Open WebUI v0.12.0)

[`src/lib/components/chat/MessageInput.svelte`](https://github.com/open-webui/open-webui/blob/v0.12.0/src/lib/components/chat/MessageInput.svelte), no `keydown` do campo de texto:

```
2163  if (e.key === 'Escape') { stopResponse(); }
...
2217  if (e.key === 'Escape') {
2219      atSelectedModel = undefined;
2220      selectedToolIds = [];
2221      selectedFilterIds = [];
2223      webSearchEnabled = false;
2224      imageGenerationEnabled = false;
2225      codeInterpreterEnabled = false;
2226  }
```

É comportamento intencional do Open WebUI: Esc no campo de mensagem para a resposta e **zera** ferramentas, filtros (Fotorrealista, Alta qualidade, Buscar referências), busca web e Imagem.

## Solução (sem fork)

Em `pepito_image_ui.js` já existe um ouvinte na fase de captura (`guardSend`, `:1305-1325`). Acrescentar outro:

```
document.addEventListener('keydown', event => {
  if (event.key !== 'Escape' || !event.target?.closest?.('#chat-input')) return;
  if (document.getElementById('suggestions-container')) return;   // menu de sugestões: deixa fechar
  event.stopImmediatePropagation();                               // o reset não roda
  document.querySelector('<botão de parar resposta>')?.click();   // preserva "Esc para parar"
}, true);
```

Confirmar no navegador o seletor do botão de parar e se o editor de texto usa Esc para mais alguma coisa (menções com `@`, comandos com `/`).

## Critérios de aceite

- Esc no campo com Image e Fotorrealista ligados: continuam ligados.
- Esc durante uma resposta: a resposta para.
- Esc com o menu de sugestões aberto: o menu fecha.

## Riscos

Atualização do Open WebUI muda o id `#chat-input` ou o fluxo do evento. Se o seletor não existir, o ouvinte não faz nada (volta ao comportamento de hoje, sem quebrar).

## Esforço

Pequeno (2 a 3 horas com teste no navegador). Não precisa de GPU, só do Open WebUI aberto.

---

# Item 9 — Palavras do balão reescritas na troca

## Situação

A regra por componente (`OVERLAY_REWRITTEN = 0.35`, sem commit) baixou os pixels alterados do balão esquerdo de 13,7% para 12,9%; "rather" e "Potter" continuam trocadas. O compositor decide por diferença de pixel: não sabe que aquilo é texto.

## Proposta

1. **Detectar texto na cena original**, uma vez por troca, no ComfyUI (onde o SAM 3.1 já roda): segundo prompt textual no mesmo workflow ("speech bubble", "text"). Alternativa: detector de texto do OpenCV (EAST/DB), que exige baixar um modelo pequeno. [NÃO VERIFICADO se o SAM 3.1 segmenta balões com qualidade; é o primeiro experimento]
2. **Regra:** região de texto ou balão da cena original é **sempre** mantida da original, a não ser que a silhueta da personagem nova a cubra (o caso do chapéu sobre o balão). A ordem de profundidade já existe (`others_order`, Depth Anything 3).
3. Reserva sem modelo novo: componentes conexos claros e de baixa saturação com traços escuros finos dentro (heurística de balão). Mais frágil.

## Critérios de aceite

- Nos casos com balão do laboratório, 0 palavras alteradas fora da área coberta pela personagem nova.
- O caso do chapéu sobre o balão continua correto.
- Nenhuma piora nos 16 casos aprovados (`casos-bons.json`).

## Esforço

Médio; precisa de pod para o experimento com o SAM.

---

# Item 12 — Débito: Qwen-Edit redimensiona para 1 MP

## Situação

O nó de codificação do Qwen redimensiona a referência para cerca de 1 MP, o que desloca o resultado em relação à cena. O G2 compensa na composição. O nosso grafo já usa `TextEncodeQwenImage21` com `resolution: 0` (`workflows/qwen-image-2.1-edit-api.json`), que mantém o canvas da primeira referência; o deslocamento foi medido na troca, que passa por `qwen_image_2_1_edit_2`.

## Referências

- [ComfyUI issue #9481: "Is 1MP fixed resizing necessary for the TextEncodeQwenImageEdit node?"](https://github.com/Comfy-Org/ComfyUI/issues/9481) e [#10198: "Do not rescale image in TextEncodeQwenImageEditPlus"](https://github.com/Comfy-Org/ComfyUI/issues/10198). [Li só os títulos: confirmam que é uma queixa conhecida]
- [Código do nó](https://github.com/Comfy-Org/ComfyUI/blob/master/comfy_extras/nodes_qwen.py). [NÃO LIDO]
- A comunidade tem nós que trocam o redimensionamento ([McTextEncodeQwenImEditPlus](https://comfy.icu/node/McTextEncodeQwenImEditPlus)). O Hub não instala nós de terceiros além do Krea2Edit, então não servem como estão.

## Proposta

Ler `nodes_qwen.py` e verificar se, no caminho de duas imagens, `resolution: 0` também evita o redimensionamento das referências. Se não: pré-escalar a cena para a grade exata que o nó produziria (múltiplos de 32, área alvo) **antes** de enviar, de modo que o redimensionamento interno vire identidade. Continua P3: o ganho é só um encaixe mais limpo.

---

# Item 11 — Primeira resposta do roteador: 56,8 s

## Fato

- 00:59:31, primeiro turno da sessão: `ollama_ps` vazio, roteador em 56,8 s (modelo de 20 GB lido do disco).
- 01:35:14, 18 minutos depois do turno anterior: `ollama_ps` vazio de novo, roteador em 10,0 s. O modelo saiu da GPU por ociosidade (o padrão do Ollama é 5 minutos) e voltou rápido porque os pesos estavam no cache de arquivos [INFERÊNCIA: não vi `OLLAMA_KEEP_ALIVE` definido pelo Hub]. Vale definir `keep_alive` longo junto com a pré-carga.
- Demais 36 turnos, modelo residente: mediana 6,3 s, mínimo 2,5 s, máximo 7,9 s.

## Proposta

- Pré-carregar o modelo de chat na subida dos serviços: requisição vazia a `/api/generate` com o modelo, como a [FAQ do Ollama](https://docs.ollama.com/faq) descreve ("preload a model by sending an empty request").
- Passar `num_ctx` igual ao do perfil (16384) na pré-carga, senão a primeira chamada real recarrega.
- Custo: 20 GB de VRAM ocupados com o Hub ocioso. Não atrapalha imagem: o filtro já descarrega o Ollama antes de cada job.
- Opção no perfil (`runtime.preload_chat_model: true`), ligada na Capella.

## Aceite

Primeiro turno depois de "Iniciar" com roteador abaixo de 12 s.

---

# Item 7 — Texto entre `['...']` ignorado por padrão

## Decisão que falta (sua)

Você escreveu a regra como `['...']`, mas na sessão usou `[ Ignorar: ... ]` e `[Ignorar: ...]`. Proponho aceitar as duas formas:

- `['qualquer coisa']` — colchete com aspas simples (retas ou curvas);
- `[Ignorar: qualquer coisa]` — sem diferenciar maiúsculas, com ou sem espaços.

## Solução

- Função `strip_ignored(text)` aplicada à última mensagem do usuário no começo do `_inlet`, antes do roteador, **e** ao histórico enviado ao roteador e ao modelo (`router_messages`, `neutral_history`).
- Se depois de remover não sobrar texto: resposta fixa curta ("Anotado."), sem chamar roteador nem modelo.
- O trecho ignorado **continua** no trace (campo `ignored_text`): é para isso que você o escreve.

## Casos de teste (reais)

| Entrada | Vai ao roteador |
|---|---|
| `[ Ignorar: para dev, voltamos ao problema de ficar repetindo a mesma imagem ] Sessão encerrada` | `Sessão encerrada` |
| `[Ignorar: para o dev, essa porra voltou ]  Já estava ligado, mas reiniciei, mete bala` | `Já estava ligado, mas reiniciei, mete bala` |
| `['anotação'] deixa o fundo azul` | `deixa o fundo azul` |
| `['só isto']` | nada; resposta fixa |
| `deixa a camiseta [azul]` | inalterado (colchete sem aspas e sem "Ignorar:") |

## Esforço

Pequeno (2 a 3 horas).

---

# Item 8 — Zoom com a roda e fundo do visualizador

Revisão do código sem commit (`pepito_image_ui.js:743-799`). A conta do zoom ancorado está correta para imagem maior que a área. Três pontos:

1. **Imagem menor que a área:** ela fica centralizada (`pepito-viewer-center`) e não há rolagem, então o ponto sob o cursor não fica parado até a imagem passar do tamanho da área. Efeito pequeno; corrige-se somando o deslocamento de centralização à conta.
2. **Firefox e mouses com `deltaMode` em linhas:** `deltaY` vem como ±3 em vez de ±100, e o passo `1.0015^(−deltaY)` vira 0,45% por clique. Normalizar: se `event.deltaMode === 1`, multiplicar por cerca de 33.
3. **Touchpad:** rolar com dois dedos agora dá zoom em vez de deslocar. Prática comum em visualizadores: `ctrlKey` (gesto de pinça) dá zoom, rolagem simples desloca. Decisão de gosto; para mouse, o atual é o esperado.

O texto de ajuda da área (`aria-label`, `:723`) ainda não menciona a roda. Falta você confirmar no navegador.

---

# Item 10 — Q3/Q4 e texto duplicado no "de lado"

Sem correção prevista: é conteúdo gerado pelo modelo. Mitigações possíveis, nenhuma testada: conferência por visão depois da geração (o modelo de chat olha o resultado e responde se há pessoa duplicada ou texto repetido) com uma nova tentativa; custa uma chamada de visão e uma recarga. Só vale se a frequência incomodar.

---

# Itens 13 e 14 — Validar o Hub 1.19 e commitar

Ordem sugerida:

1. Implementar itens 1, 4, 7 e 6 (sem GPU) + Fase 1 do item 2.
2. Suíte completa, commit nos dois ramos, conferir o hash, refazer o TAR.
3. Um pod: validar 1, 4, 6, 7, 8, Fase 1 do item 2; experimentos dos itens 3 (parte de redesenho), 2 (Fase 2) e 9 (SAM em balões).
4. Decisão de lançar.

As mudanças sem commit de ontem: `GLOBAL_EDIT_QUESTION` e o teste dela serão **substituídos** pelo item 4; a regra `OVERLAY_REWRITTEN` é pequena e pode entrar; zoom e fundo entram depois da sua conferência.

---

# Achados novos

## N1 — O modelo de chat é recarregado em todo turno de imagem só para dizer uma frase

**Fato:** em todos os turnos de imagem, `answer_usage` mostra `load_duration` de 6,0 a 6,3 s e `eval_count` de 15 a 21 tokens. Turno típico de edição: 27 s = roteador 6 a 7 s + job 12 a 14 s + recarga e frase 7 s.

**Propostas, da mais barata para a mais ambiciosa:**

1. Responder com texto fixo nos turnos de imagem ("Pronto." mais a nota de variações), sem chamar o modelo. Tira cerca de 7 s do fim do turno. A recarga passa para o próximo turno, dentro do roteador (que hoje já a encontra residente); o ganho real é a imagem aparecer com o turno já encerrado. Medir antes de decidir.
2. **Roteador num modelo pequeno residente** (ex.: 4B, cerca de 3 GB) ao lado do modelo de imagem. Durante os jobs do Krea sobram 14,2 GB livres (`comfyui_stats.vram_free`), então cabe. O 27B só carregaria em turnos de conversa. Turno de imagem cairia para roteador 2 a 3 s + job 12 a 14 s [INFERÊNCIA; precisa medir a qualidade do roteamento com modelo pequeno — o comentário em `router_messages` cita testes com `qwen3.5:9b`].

## N2 — Checklist descreve uma correção automática que não existe mais

`docs/testing/capella-5090-checklist.md:184-186` descreve "Quase nada mudou; refazendo com mais liberdade" e o log `image_change measured=…` com dois valores. No código, `measure_edits` só registra (`pepito_image_chat.py:6274-6275`). O item 3 propõe reativar isso como decisão.

## N3 — Área automática retangular

Ver item 2, Fase 2 (silhueta pelo SAM).

## N4 — `picture_bytes` lê qualquer arquivo pelo id, sem checar o dono

`pepito_image_chat.py:4667` usa `Files.get_file_by_id(id)`; o caminho nativo (`pepito_native.py:181`) usa `get_file_by_id_and_user_id`. Com link de convidado, um usuário que soubesse o id de um upload de outro poderia usá-lo como fonte de edição. Os ids são UUID aleatórios, então a exploração prática é improvável. Severidade baixa; correção simples (passar o `user_id`). Detalhe no relatório 2.

---

# Ordem de ataque recomendada

| Ordem | Item | Por quê | Precisa de pod? |
|---|---|---|---|
| 1 | 1 + 5 + 4 | Mesma região do código, é o que mais irrita, causa confirmada | Só para confirmar |
| 2 | 7, 6 | Pequenos e independentes | Não (6 precisa do Open WebUI aberto) |
| 3 | 2, Fase 1 | Medição mostra ganho de 2,6× para 1,0× no tom | Só para confirmar |
| 4 | 3 | Depende de experimento com a parte de redesenho | Sim |
| 5 | 11, N1 | Latência | Sim |
| 6 | 2 Fase 2, 9 | Usam o SAM; mesma sessão de pod | Sim |
| 7 | 12 | Dívida P3 | Sim |
