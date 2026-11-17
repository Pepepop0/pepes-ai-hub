# Múltiplas referências: estado atual, diagnóstico e proposta LAB-04

> Registro técnico SFW. As referências históricas a `edit_lab.py`, seus testes e fontes experimentais pertencem ao pacote privado e não são comandos ou fixtures disponíveis no público. A implementação pública de métricas e avaliação compartilhadas é `scripts/lab_common.py` / `scripts/lab_review.html`.

Pesquisa somente-leitura para Pepito's AI Hub. Escopo: referências de uma edição comum (`edit_refs`), não confundir com o pipeline especial `swap`. Nenhum arquivo do repositório foi alterado; não executei testes, serviços ou geração. Nenhuma imagem real foi aberta, decodificada ou exibida: usei código, JSON, trace textual, documentação e metadados de arquivos.

**Legenda:** **FATO** = observado no código/texto/documentação citados; **INFERÊNCIA** = hipótese causal ou estimativa; **PROPOSTA** = mudança ainda não implementada. Nas citações, `F` abrevia `assets/open-webui/pepito_image_chat.py`, `L` abrevia `scripts/edit_lab.py`. Números de linha são da árvore consultada. As grafias reais dos workflows são `qwen-image-2.1-edit-2-api.json` e `qwen-image-2.1-edit-3-api.json` (ponto em `2.1`), conforme `manifests/workflows.yaml:73-101`.

## 1. Como o código trata hoje 2+ referências

### 1.1 Roteador → plano semântico

1. **FATO:** o roteador recebe histórico textual recente, catálogo numerado de imagens e `SUPPORTED INPUT COUNTS`, não apenas o último anexo. `router_messages` monta `IMAGES`, `RECENT CONVERSATION` e `LAST USER MESSAGE` (`F:1434-1479`); `router_capabilities` obtém as contagens de `supported_counts` (`F:1197-1220`, `3654-3660`).
2. **FATO:** os papéis são A = pose/composição/cena; B = identidade; C = luz/roupa/estilo. O roteador normalmente não deve escrever números de imagens nem a palavra `reference` no prompt: o sistema se responsabiliza por nomeá-las (`F::ROUTER_SYSTEM`, especialmente `1109-1116`; `F:3646-3650`). Portanto, retirar a compilação do prefixo não é neutro: o contrato do roteador supõe sua existência.
3. **FATO:** `validate_intent` exige `plan`, `base` conhecido, listas de alterações/preservação coerentes, inclusão da base nas referências e `purpose` textual não vazio, limitado a 160 caracteres quando escrito pelo roteador (`F:965`, `3672-3759`). Perguntas de esclarecimento interrompem a execução (`F:3731-3735`). `_routed` valida o plano; para o escritor `router`, também acrescenta `Requested changes`, `Keep unchanged` e indicações de lado por `intent_prompt` (`F:9240-9255`, `3906-3927`).
4. **FATO:** referências distintas não são sinônimo de quantidade de imagens a entregar: `variations` deriva do pedido/receita e cada edição usa o conjunto de fontes completo (`F:9394-9399`, `9886-9894`, `8019-8029`).

### 1.2 Plano → slots físicos

**FATO:** `reference_plan(entries, refs, stack, web, base=...)` (`F:3930-3993`):

- resolve números do chat/web para URL/candidato;
- rejeita referência inválida ou inexistente;
- deduplica por **número da imagem**, agregando papéis e `purposes` sem duplicatas;
- na ausência de referências, usa só a imagem atual, papel A;
- ordena por `(number != base, roles[0], number)`: **base primeiro**, depois papel e número, não simplesmente “A sempre primeiro”;
- rejeita quantidade que não esteja em `supported_counts`, sem descartar fontes;
- para `scene_character`, exige exatamente dois slots e papéis **exatos** `[A]`, `[B]`, nessa ordem. Uma base B à frente da cena A é recusada, não trocada silenciosamente.

**FATO:** exemplo “pose da 2 e personagem da 1”: com base 2, slots = `[chat 2/A, chat 1/B]`. No modelo com sintaxe indexada, `<image1>` significa **slot 1**, portanto imagem **2 do chat**, e `<image2>` significa imagem **1 do chat**. O teste existente verifica precisamente os uploads invertidos e o prefixo indexado (`tests/test_image_edit_dispatch.py:1541-1555`).

**FATO:** as restrições físicas variam:

| Stack | Fontes disponibilizadas | Política e ordem |
|---|---|---|
| Qwen 2.1 principal | 1, 2, 3 | `generic`, `syntax=indexed` nos fluxos 2/3; base primeiro |
| Krea 2 (variante privada) | 1, 2 | duas fontes só em Identity Edit V2: cena A primeiro, personagem B depois; `syntax=plain`, `scene_character` |

Fontes: `profiles/capella.yaml:92-99,115-127`; `manifests/workflows.yaml:55-101,202-226`; `F:3970-3992`. **FATO:** Krea não aceita arbitrariamente A+C, B+B ou três imagens por este caminho; há testes de rejeição de base errada e A+C (`tests/test_image_edit_dispatch.py:2677-2709`).

### 1.3 Slots → prompt: diferença exata entre escritores

**FATO importante:** a valve `prompt_writer` só aceita `router`, `vision`, `vision_think`, `raw` (`F:1705,1717-1719,8464-8468`). `revise` e `revise_think` são nomes do lab; `revise` também é o escritor E chamado internamente pelo par. **Não há valve produtiva `revise_think`**, nem execução produtiva dessa revisão com várias fontes. Os escritores não removem slots: o desvio está no **prompt e na informação visual fornecida ao escritor**, não no número físico de referências enviado ao ComfyUI.

| Escritor/caminho | O que vê e escreve | Compilação final em 2+ referências | Slots/workflow |
|---|---|---|---|
| `router` | Roteador normal sem pixels, `think=False`, JSON. Prompt enriquecido pelo plano | `reference_prompt(slots, syntax)` + prompt; cada slot tem papel + seus `purposes`; Qwen usa `<imageN>`, Krea usa ordinais | Slots validados/ordenados; `edit_refs[N]` |
| `vision` | Segunda chamada com **todas as fontes dos slots, na ordem física**, pedido, decisão e histórico; `think=False`, texto livre, temperatura 0,5 | **Sem** prefixo de papéis e sem `intent_prompt`; fica o texto retornado. O sistema diz que ordem visual é a do workflow, mas o JSON da decisão ainda usa números do chat: não há tabela explícita chat→slot | Mesmos slots; pode gerar prosa ambígua sobre qual fonte usar |
| `vision_think` da valve | O próprio roteador recebe imagens do catálogo, na ordem `IMAGES` exceto web, e `think=True`; decide fontes/rota e prompt conjuntamente | **Sem** prefixo; mantém prompt desse roteador. Sua ordem visual pode não coincidir com a ordem final dos slots, que só é resolvida depois | Slots seguem a decisão resultante; a decisão pode diferir daquela do `router` normal |
| `vision_think` compatibilidade | Se Ollama rejeitar `think` + `format` com HTTP 400 correspondente: roteia sem imagens/think e faz segunda chamada de texto livre com todas as fontes escolhidas e `think=True` | Ainda sem prefixo; não equivale ao C congelado do lab | Slots da rota normal |
| `raw` | Texto do usuário literalmente | Sem prefixo/compilação; números e português permanecem como escritos | Os mesmos slots do plano; “imagem 2” pode ser reinterpretada indevidamente como slot 2 |
| `revise_think`/C2 | `prompt_revision_payload`, JSON com prompt/`fits_area`/`outside`; uma fonte + eventual cópia anotada; `think=True`, temperatura 0,2 | Hoje **não executa 2+ fontes** no filtro/lab; simplesmente acrescentar outra imagem conflitaria com a regra “segunda imagem é a fonte com área” | Não existe caminho produtivo multirreferência |
| `revise`/E | Mesmo payload, `think=False`; uma fonte + eventual cópia anotada | Hoje par só com uma fonte; substitui o prompt do job pelo texto revisado, sem recompilar prefixo/Source purpose | Em 2+ fontes o par nem é elegível |
| C do par/lab (`vision_think`, mas não a valve) | `frozen_route_payload`: chamada separada ao roteador com decisão congelada, uma imagem, `think=True`; usa só `prompt` e salvaguarda | Não suporta N fontes em sua assinatura atual | Par não é executado em 2+ fontes |

Fontes da tabela: `F:1614-1639,1649-1671,1705-1751,1774-1801,1844-1888,1908-1945,1957-2042,2045-2101,9416-9435,9677-9687,9766-9789,10003-10043`; `L:2330-2357`.

**FATO, correção de escopo da lacuna 2:** `Source purpose:` é acrescentado **somente em uma fonte** (`len(slots)==1`, `F:9774-9789`). Em 2+ fontes, os mesmos fins já aparecem **dentro do prefixo `reference_prompt`**, um por slot (`F:3996-4007`). Não se deve acrescentar um `Source purpose` único e sem dono às edições de várias fontes.

**FATO:** em uma fonte, a normalização de `roles`/`purposes` de restage/pose para identidade B e a limpeza de preservações contraditórias acontecem para qualquer escritor; só a incorporação dessas informações no prompt depende de `writer == "router"` (`F:9774-9789`). Em 2+ fontes, nenhum desses ajustes de uma fonte se aplica. `PROMPT_WRITER_SYSTEM` manda explicitamente não copiar listas `Requested changes`, `Keep unchanged`, `Source purpose` (`F:1706-1713`).

**FATO:** o par E/C exige `ACTION_EDIT`, exatamente uma fonte, partes `edit/restage/remove`, sem referência automática, sem warp e sem múltiplas variações já pedidas (`F:9677-9687`). Ao final, troca `prompt/prompts/writers` e grava `requested_count=1` (`F:10003-10045`). Não basta remover `len(...) == 1`: os dois payloads continuam monoimagem.

### 1.4 Workflow e entrega

**FATO:** `_stack_edit` converte `reference_syntax` para `syntax` e inclui os vínculos dos nós; o construtor de stacks registra cada número de fontes em `edit_refs[str(count)]` (`src/local_ai_stack/service_config.py:674-701,790-800`). `stack_settings(stack,"edit",N,patch)` escolhe esse fluxo para N>1 e aplica seed/canvas (`F:279-302`).

**FATO:** `_routed` grava `images=[slot.url...]`, prompt já final, parte e receita com `slots.roles/purposes` (`F:9886-9928`). `edit_chat_image` passa a lista ordenada para `image_edits(EditImageForm(prompt=..., image=model_inputs))` sob o overlay do workflow (`F:7991-8017`).

- **Qwen:** os nós LoadImage 470/471/472 alimentam `images.image_1/2/3` de `TextEncodeQwenImage21`; `resolution=0`, 25 passos e CFG 1 (`workflows/qwen-image-2.1-edit-3-api.json:68-106`; mapa de dois/três: `manifests/workflows.yaml:82-101`). Antes disso, `model_edit_sources` redimensiona preservando a ordem: primeira imagem carrega o canvas de saída, demais são limitadas a 1.048.576 pixels; originais continuam sendo a proveniência (`F:5636-5666`).
- **Krea:** cena vai para imagem principal e personagem para `_b`; o patch usa `ref_boost_a=1`, `ref_boost=2.5`, cena `aligned`, personagem `beside`, grounding 768 no positivo e no negativo (`workflows/krea2-identity-edit-v2-2-api.json:134-218`; LoadImages 72/74 no mapa em `manifests/workflows.yaml:219-226`).
- **FATO:** calibração de força de edição local só se aplica a uma fonte; multirreferência fica com força do workflow (`F:9700-9714,7971-7974`). A primeira fonte também é a referência de proporção/métricas da entrega (`F:8127-8143`).

### 1.5 Receita → retry/regeração

**FATO:** cada saída mantém prompt efetivamente usado, `source_urls` **em ordem**, seed/patch, força, estilo e receita interna com os slots; pares guardam também writer por saída (`F:8172-8204`). `remember_edit` mantém a receita inteira e fontes; fontes `data:` de web não são persistidas nesse registro de último edit, impedindo esse retry (`F:4297-4314`). `previous_edit` reconstrói as referências a partir do zip fontes/slots, restaurando papéis e `recipe_purpose` (`F:4270-4294`).

**FATO:** regerar pela receita seleciona novamente `edit_refs[len(source_urls)]`, preserva a lista de fontes e o prompt salvo; com seed/edição de prompt mantém a seed, com regeração comum sorteia outra; executa uma variação e não escreve um novo par (`F:8718-8753,8763-8766,8808-8837`). **INFERÊNCIA:** a correção deve acontecer antes da persistência; reintroduzir prefixos cegamente na regeração duplicaria ou alteraria a receita que o usuário pediu para repetir.

## 2. Como os modelos usam múltiplas imagens

### 2.1 Fontes externas e convenções verificadas

| Família/componente | FATO documentado | Consequência para o Hub |
|---|---|---|
| Qwen-Image-Edit-2509 | Suporte a pessoa+pessoa, pessoa+produto, pessoa+cena; melhor faixa declarada 1–3 entradas; exemplo oficial recebe lista de duas imagens e prompt descritivo sem tags | Não generalizar regras de 2.1 para toda a família antiga. [Model card oficial](https://huggingface.co/Qwen/Qwen-Image-Edit-2509/blob/main/README.md) |
| Qwen-Image 2.1 | Até dez referências; modelo unificado geração/edição; README tem exemplo de três personagens sem tags | “Sem tag nunca funciona” **não está provado**; a exigência formal vem do reescritor. [README oficial](https://github.com/QwenLM/Qwen-Image-2.1#image-editing-multiple-reference-images) |
| Reescritor oficial 2.1 | Para N≥2, exige `<image1>`, `<image2>` etc., proíbe “the first image”/“image A”, exige papel e o que tirar de **cada** imagem, distinguindo canvas de material a transferir; para N=1 pede referência natural, sem tags | Convenção recomendada e verificável a seguir no prompt final do Qwen; nosso `router` já a usa. [Sistema oficial, Image Reference Rules](https://github.com/QwenLM/Qwen-Image-2.1/blob/main/prompt_rewrite/prompts/system_prompt_edit.txt) |
| `TextEncodeQwenImageEditPlus` | Três portas opcionais `image1/2/3`; lista em ordem; template injeta `Picture 1: ...`, `Picture 2: ...`; gera visão (~384²) e latentes (~1024²) por imagem | Não é o nó do workflow 2.1 atual; não trocar o prompt/workflow só por ele ter “Plus”. [Código fixado](https://github.com/Comfy-Org/ComfyUI/blob/b0b743566f65daafc423b4fea8a2fbda94b3384a/comfy_extras/nodes_qwen.py#L54-L113) e [documentação do nó](https://docs.comfy.org/built-in-nodes/TextEncodeQwenImageEditPlus) |
| `TextEncodeQwenImage21` | Ordena numericamente `image_1...`; imagem e VAE na mesma geometria; primeiro input determina tamanho do latent; `resolution=0` mantém tamanho arredondado em 32; autogrow do nó expõe até 16 nomes, mas isso não altera o limite de dez anunciado pelo modelo | Ordem física tem significado; tags devem ser numeradas **após** ordenar slots. Suporte do nó não é garantia de qualidade nem capacidade configurada no Hub. [Código fixado](https://github.com/Comfy-Org/ComfyUI/blob/b0b743566f65daafc423b4fea8a2fbda94b3384a/comfy_extras/nodes_qwen.py#L115-L194) |
| Krea 2 + Krea2Edit V2 comunitário | Dual conditioning: VAE para aparência + Qwen3-VL para semântica; em duas fontes, cena no principal, sujeito em `_b`; cena aligned, pessoa beside recomendados; ≤2MP; grounding 384–768 faixa de treino indicada | Respeitar o par ordenado A/B e suas posições é mais fundamental que uma tag textual nova. [README do autor](https://github.com/lbouaraba/comfyui-krea2edit#usage-notes-read-these--they-matter) |
| Encoder Krea2Edit fixado v2.0.0 | `Krea2EditGroundedEncode` aceita apenas `image`/`image_b`; concatena dois blocos visuais em ordem cena→sujeito e depois o prompt, **sem** introduzir `<imageN>` nem nomes de papéis automaticamente | Não há evidência de tags especiais obrigatórias; ordinais são prosa, não tokens de contrato. O prefixo Krea atual é compatível com essa ordem, mas sua eficácia não foi medida. [Código v2.0.0, símbolo `Krea2EditGroundedEncode`](https://github.com/lbouaraba/comfyui-krea2edit/blob/v2.0.0/__init__.py#L419-L502) |
| FLUX.1 Kontext oficial | Guia mostra uma imagem-base `input_image` + instrução; recomenda FLUX.2 para multirreferência | Não importar para o Krea uma suposta regra oficial multirreferência de Kontext. [Guia Kontext](https://docs.bfl.ml/kontext/kontext_image_editing.md) |
| FLUX.2 oficial, comparação útil | Guia multirreferência recomenda “image 1”, “image 2” e dizer explicitamente o que cada fonte fornece; exemplos usam até três fontes distintas | Princípio de vincular fonte→atributo é transferível como desenho, **não** prova de que `<imageN>` é token certo em Krea/Kontext. [Guia BFL](https://docs.bfl.ml/guides/usecases_editing_multi_image_compositing.md), [API FLUX.2](https://docs.bfl.ml/flux_2/flux2_image_editing.md) |

**FATO:** o ComfyUI do Hub está fixado em `b0b743...`/v0.39.0 e Krea2Edit em `13ef60...`/v2.0.0 (`manifests/repositories.yaml:15-18,61-68`). Consultei também os arquivos externos nessas revisões, não só o `master`. A página documental do Plus informa que foi gerada por IA; por isso o código fixado é a evidência mais forte para ordem/portas/template ([página](https://docs.comfy.org/built-in-nodes/TextEncodeQwenImageEditPlus)).

### 2.2 Comparação com a escrita atual

- **FATO:** Qwen + `router` já gera `<imageN>` em ordem física, papéis e fins individuais (`F:3996-4007,9768-9770`; `manifests/workflows.yaml:76,91`). A solução não é “inventar tags que ainda não existem”; é manter esse contrato nos demais escritores.
- **FATO:** Qwen + `vision`/valve `vision_think`/`raw` não tem imposição final equivalente (`F:2059-2088,9768-9789`). Além disso, os prompts do roteador desencorajam números (`F::ROUTER_SYSTEM`), enquanto o oficial 2.1 pede tags para N≥2 ([sistema oficial](https://github.com/QwenLM/Qwen-Image-2.1/blob/main/prompt_rewrite/prompts/system_prompt_edit.txt)).
- **FATO:** Krea usa ordinais e não tags; o encoder comunitário não cria rótulos automáticos ([código v2.0.0](https://github.com/lbouaraba/comfyui-krea2edit/blob/v2.0.0/__init__.py#L419-L502)). **INFERÊNCIA:** enviar `<imageN>` indiscriminadamente às duas famílias pode piorar o Krea sem corrigir seu principal limite, que é a geometria/ordem cena→pessoa.
- **FATO:** o oficial Qwen distingue o canvas de fontes de atributos; nossa legenda A agrega pose, composição **e cena** (`F:3646-3650`), mas `purpose` pode restringir essa abrangência (`F:3996-4005`). **INFERÊNCIA:** “use apenas a pose” pode entrar em tensão com a frase fixa que também manda seguir a cena; o lab precisa medir vazamento de fundo/luz/estilo.
- **FATO:** o reescritor oficial coloca formato em `wh_ratio`/`ratio_follow`, fora do prompt; o Hub seleciona canvas por código e primeira fonte (`F:9799-9854,5636-5666`). **PROPOSTA:** adaptar as regras de identificação de fontes sem incorporar o schema externo inteiro nem trocar canvas no mesmo experimento.

## 3. Diagnóstico, incluindo o turno de 10/11 às 01:44

### 3.1 O que está comprovado e o que é hipótese

| Achado | Classificação | Efeito/limite |
|---|---|---|
| Prefixo/purposes só compilados para `router` | FATO — `F:9768-9789` | Diferença real de informação textual final, não perda de imagens físicas |
| Nenhum caso de duas fontes no LAB-03 | FATO — LAB-03 (material do pacote privado), linhas 3-5, 51; `L:2235-2307,2330-2357,2410-2424` | A vitória de E/C em fonte única não mede composição |
| E/C monoimagem e regra que interpreta segunda imagem como área | FATO — `F:1775-1779,1844-1861,1908-1929,9682` | Adicionar referência B sem alterar schema/regra tornaria B uma “máscara” para o revisor |
| Números do chat versus ordem física | FATO — `F:3970,1745-1749`; sem tabela explícita em `prompt_writer_payload` | INFERÊNCIA: um revisor associa a fonte errada quando base/ordem do catálogo difere dos slots |
| Perda do binding fonte→atributo aumenta mistura de identidade, pose e roupa | INFERÊNCIA baseada nos contratos acima | Precisa de avaliações pareadas, não de MAD global |
| A genérico contradiz “mudar a pose” ou “só copiar pose, não cena” | INFERÊNCIA — legenda fixa em `F:3647`, fins individuais em `3996-4005` | Pode produzir preservação excessiva ou vazamento de cena |
| Krea scene aligned / pessoa beside favorece preservar a cena-base | FATO da configuração — workflow `:144-168`; hipótese de rigidez relativa = INFERÊNCIA | Trocar prefixo não remove um vínculo geométrico forte |
| Lab escolhe Qwen por `edit_method == "qwen"` e não por família/manifesto | FATO — `L:2412-2424` | Turnos Qwen nativos de várias fontes podem escolher grafo Krea errado; contar fontes apenas não basta |

### 3.2 Evidência do turno real

Arquivo de trace `T` = `$HOME/Downloads/swap-lab/sessao-final-20261109-2312/data/open-webui/pepito-trace/turns.jsonl`. Identificador do turno `b27d4bc52adf4eabb3b3af1e2284679d`.

**FATO:** na varredura textual desse arquivo, o único `image_job` com `params.inputs > 1` foi a linha 141 (`inputs=2`). Isso comprova a unicidade **nesse arquivo**, não em toda sessão histórica existente.

- `T:137`: pedido: “Faz ela de lado, mas com a cabeça virada/jogada pra camera, deixa a pele dela mais pálida, os olhos pretos e o cabelo mais curto”. Modelo da variante privada.
- `T:140`: roteador = `edit_image`, `edit_mode=restage`, `scope=global`, `base=7`, referências `7/A` (“the current image to be edited; use as the base for the change”) e `6/B` (“the character identity reference; apply only the requested changes”). Preservações: roupa, fundo, luz, estilo de ilustração; mudanças: pose lateral/cabeça para câmera, pele mais pálida, olhos pretos, cabelo mais curto; `aspect=square`, `style=art`.
- `T:141`: execução efetiva `part=edit`, duas entradas, `edit_method=native`, 1408×1408, uma variação, sem máscara, `ref_boost=[null]`, seed **não registrada** (`null`), 35,94 s e uma saída. `null` na força significa que o filtro não a calibrou por fonte única, não que o nó foi executado “sem ref_boost” (`F:7971-7974,8010-8017`; força fixa do workflow em `workflows/krea2-identity-edit-v2-2-api.json:144-145`).

**Prompt gravado completo (`T:141`):**

> The first image is the pose, composition and scene to follow. the current image to be edited; use as the base for the change The second image is the character identity reference; borrow only the requested attributes. the character identity reference; apply only the requested changes Take nothing else from the reference images. The same woman stands in a side profile pose, facing left, but her head is turned directly toward the camera with a neutral expression. Her skin is noticeably paler, her eyes are solid black, and her hair is cut shorter. Keep her clothing, the background, the lighting, and the illustration style unchanged. Requested changes: change pose to side profile with head turned toward camera; make skin paler; make eyes black; make hair shorter. Keep unchanged: clothing; background; lighting; illustration style.

**FATO:** o turno **já tem prefixo de papéis e fins das duas imagens**. Logo, ele **não é exemplo da omissão causada por um escritor não-router**. É um controle positivo do caminho antigo `router`. Seu prompt não tem `<imageN>`, mas isso corresponde ao Krea/ordinais, não comprova descumprimento de Qwen. A seleção de `edit_refs[2]` é compatível com modelo da variante privada + parte edit + duas fontes no código atual (`F:279-302`; `profiles/capella.yaml:121-122`); **INFERÊNCIA:** esse é o manifesto executado na ocasião, pois o trace não registra hash/ID do grafo histórico.

**FATO:** `T:142` registra `rgb_mad=35.1612`; `T:144` registra 49,313 s para o turno. **Não há avaliação visual nesse trace**; MAD indica diferença, não êxito de pose/identidade/olhos. Não afirmo que a imagem falhou.

**INFERÊNCIAS diagnósticas específicas:**

1. “Primeira imagem é a pose [...] a seguir” e “mudar a pose para perfil” deixam prioridades implícitas. Melhor dizer que a primeira fornece **canvas/fundo/composição**, enquanto a pose é substituída pelo pedido; B fornece identidade, não todos os atributos da pessoa original.
2. “borrow only the requested attributes” para B é genérico: o usuário não pediu explicitamente uma segunda referência. O roteador a introduziu para identidade. Não é errado por definição, mas precisa ser testado contra fonte única como controle separado, não silenciosamente removido.
3. “facing left” e “neutral expression” foram acrescentados pelo roteador em `T:140`; não aparecem no pedido `T:137`. Um revisor com todas as fontes poderia evitar a expressão inventada e reconciliar luz/estilo/preservação, mas não há prova visual de que esse foi o problema.
4. A força fixa/posição aligned da primeira referência pode competir com uma alteração grande de pose. Isso é outra variável; não variar força junto com o texto na primeira ablação.

## 4. Soluções candidatas e recomendação

Custos abaixo são **estimativas de engenharia**, não medições de implementação. Tempos E/C de fonte única são dados históricos do LAB-03, não orçamento garantido para múltiplas imagens (material do pacote privado, linhas 23-29).

| Candidata | Mudança concreta | Custo | Risco / o que não resolve |
|---|---|---|---|
| A. Compilação de contrato independente do escritor | Separar texto autoral de envelope determinístico; após ordenar slots, compilar papéis/fins via `reference_prompt` para todos os escritores produtivos. Uma fonte usa Source purpose; N fontes usam fins por slot. Não recompilar receita em retry/regeração | Baixo; sem chamada extra de modelo | Duplicação/contradição se o autor já emitiu tags ou atribuições; raw deixa de ser literalmente cru se incluído sem modalidade explícita |
| B. Todas as fontes + mapa de slots para E/C | Generalizar `prompt_revision_payload`, `frozen_route_payload`, `write_prompt_pair` para lista ordenada de fontes e metadados `slot_index/chat_number/roles/purposes/is_canvas`; separar cópia de área como contexto, não referência. Mudar sistemas de escrita | Médio; aumento de tokens visuais/latência, sem novo modelo | Contexto/VRAM do 27B; revisão pode nomear o que não foi pedido; não altera limites físicos Krea |
| C. Convenção por família | Qwen 2.1: `<imageN>` no envelope e nas instruções referenciadas; Krea: ordinais em cena→pessoa. Um mapa explícito alimenta revisor/compilador; tags nunca usam número do chat | Baixo/médio; precisa testes de remapeamento | Aplicar tags universalmente seria inadequado; reescrever regex sobre texto arbitrário é frágil. O `router` Qwen já faz a parte básica |
| D. Par E/C para N fontes | Depois de B/C, ampliar elegibilidade de `pair`, reutilizando envio de `prompts` com seed compartilhada, receitas por saída e preferência já existente | Médio; aproximadamente dois samplings em vez de um, mais duas chamadas de escrita | Dobra custo de imagem; não justificado pelo LAB-03 monoimagem; simplesmente tirar a guarda monoimagem seria incorreto |
| E. Ajuste semântico do papel A/preservação | Manter A/B/C, mas o envelope dá prioridade aos `purposes` e às mudanças, distinguindo canvas de pose/material. Não prometer preservar pose/cena quando elas mudam | Médio; só texto | Pode alterar casos bons atuais; precisa ablação contra prefixo atual, não alteração simultânea escondida |

### Recomendação

**PROPOSTA:** implementar **A+B+C como um contrato único de referências**, medir com LAB-04 e só então habilitar D em 2+ fontes. Manter o roteador atual como baseline de produção até a medição; conservar as restrições `scene_character`/capacidade. E é uma ablação posterior/estrato específico, não pré-requisito para corrigir a perda de binding.

Contrato mínimo, sem nova abstração distribuída: uma lista de slots resolvida uma vez em `_routed`, contendo número do chat e índice físico; os escritores veem essa lista e as mesmas imagens ordenadas; o compilador final acrescenta o envelope apropriado à família. O texto do escritor não deve renumerar nem redefinir papéis. O prompt final completo é o que chega ao workflow e o que se persiste. Isto aproveita `reference_plan`/`reference_prompt`, em vez de criar um segundo planejador (`F:3930-4007`).

**PROPOSTA sobre raw:** preservar `raw` como controle diagnóstico literalmente cru. Comparar `raw+contrato` com nome distinto no lab, se necessário; não mudar o significado da valve silenciosamente. “Independente do escritor” vale para escritores assistidos e E/C; raw pode continuar sendo a exceção explícita/documentada. Essa decisão deve aparecer no aceite da futura especificação.

**PROPOSTA sobre receita:** novos jobs/revisões passam pela compilação; regeração exata usa o prompt final gravado, sem envelope extra e sem rerotear; retry textual puro não concatena de novo fins antigos. Deve haver teste de idempotência no ponto de entrada, não heurística que tenta apagar trechos parecidos do prompt (`F:8718-8753,8808-8837,9768`).

## 5. Desenho do LAB-04

### 5.1 Inventário permitido, somente nomes e tamanhos

**FATO observado por listagem de diretório**, sem abrir imagens. Tamanhos em bytes.

`$HOME/Downloads/pod-onda-4/lab-prompt-2/sources`:

| Nome | Bytes | Nome | Bytes |
|---|---:|---|---:|
| A1.png | 2140941 | A2.png | 2296059 |
| A3.png | 1344468 | A4.png | 1294267 |
| A5.png | 1293335 | B1.png | 1278428 |
| B2.png | 792154 | P1.png | 2328559 |
| P1_mask.png | 48029 | P2.png | 293909 |
| P3.png | 1277245 | P4.png | 293909 |
| P5.png | 993464 | | |

`$HOME/Downloads/swap-lab/inputs`:

| Nome | Bytes | Nome | Bytes |
|---|---:|---|---:|
| T1_character.png | 293909 | T1_scene.jpg | 1608518 |
| T2_character.jpg | 3854793 | T2_scene.jpg | 121819 |
| T3_character.jpg | 121819 | T3_scene.png | 1418233 |
| T4_character.png | 460984 | T4_scene.png | 1099609 |
| T5_character.png | 460984 | T5_scene.png | 615846 |
| T6_character.png | 1603795 | T6_scene.png | 1270112 |
| T7_character.jpg | 29170 | T7_scene.png | 605514 |

**Limite:** nomes e tamanhos não provam conteúdo, estilo ou que duas fontes tenham identidades diferentes. A associação temática abaixo é uma proposta baseada nos nomes/casos textuais já existentes, não uma inspeção visual. Seleção/adequação visual final cabe ao dono do acervo; o assistente não precisa abrir imagens. `P1_mask.png` é máscara, não terceira referência.

### 5.2 Oito casos propostos

Pedido textual sempre não explícito, sem inventar detalhes da fonte. Para R1–R4, rodar **ambas** as famílias; R5–R8 só Qwen, porque as combinações não cabem na política Krea atual (`F:3982-3992`; `profiles/capella.yaml:97-99,121-122`).

| Caso | Fontes e papéis físicos | Tipo de pedido/objetivo |
|---|---|---|
| R1 — 2 fontes, ordem invertida | Upload lógico 1=`T1_character.png`/B, 2=`T1_scene.jpg`/A; base=2; execução `[scene,character]` | “Use a personagem da 1 na cena da 2, mantendo composição e luz da 2.” Detecta confusão chat→slot |
| R2 — 2 fontes, controle de ordem | Mesmas fontes de R1, mas upload `[scene,character]`; base=1 | Mesmo pedido semântico. Deve produzir o mesmo contrato físico e, com prompt final idêntico/seed, grafo equivalente salvo nomes de upload |
| R3 — pose sem cenário | `T4_scene.png`/A + `T4_character.png`/B | “Use apenas a pose da cena e a identidade da personagem; coloque-a em fundo cinza neutro.” Mede vazamento da cena que A genericamente manda seguir |
| R4 — mudança forte sobre canvas | `T6_scene.png`/A + `T6_character.png`/B | Reproduzir o tipo do turno 01:44: vista lateral/cabeça para câmera, cabelo mais curto, preservar fundo/luz/roupa, sem conteúdo explícito. Testa conflito preservação×alteração |
| R5 — identidade e roupa | `P3.png`/B como base + `A3.png`/C | “Mantenha a pessoa e pose da 1; use somente a roupa da 2.” Mede transferência de roupa sem copiar identidade, corpo ou fundo de C |
| R6 — iluminação e não identidade | `P4.png`/A+B como base + `A1.png`/C | “Mantenha personagem, enquadramento e roupa da 1; use apenas a iluminação da 2.” C restrito a luz; três papéis, só duas imagens deduplicadas |
| R7 — 3 fontes A/B/C | `T5_scene.png`/A, `T5_character.png`/B, `P3.png`/C | “Composição da 1, personagem da 2, apenas o estilo da 3.” O dono confirma previamente que C fornece estilo distinguível; mede separação de três atributos |
| R8 — 3 fontes, duas identidades | `T7_scene.png`/A + `T1_character.png`/B + `T6_character.png`/B | “As duas personagens lado a lado na cena da 1; identidade de cada fonte preservada.” Testa dois B sem fusão/duplicação. Distinguir esquerda/direita pelos slots, não pela aparência inventada |

**PROPOSTA:** guardar R1/R2 como metamorfismo obrigatório de ordem e R6 como deduplicação/multipapel. Separar um controle adicional não gerativo para Krea A+C/três imagens: rejeitar antes de upload/sampling, nenhuma referência descartada. Não contabilizar rejeição correta como “falha de qualidade”.

### 5.3 Variantes e isolamento causal

Congelar decisão, slots, canvas, family/manifesto, modelo, passos, força e seeds por combinação caso×família. Escrever prompts uma vez por variante e reutilizar nas duas seeds; não rerotear por variante. Assim mede escrita/contrato, não decisão diferente. O C do lab deve ser o **congelado**, não a valve `vision_think` de uma passada (`F:1908-1945` versus `1623-1639`).

| Variante | O que compara |
|---|---|
| V0 | `router` atual com seu envelope completo — baseline funcional, não apenas `decision.prompt` |
| V1 | `vision` com todas as fontes e sem envelope — controle da lacuna atual; decisão original e ordem visual mantidas |
| V2 | **Mesmo texto V1**, acrescido de contrato determinístico por família — isola efeito do prefixo sem chamar outro escritor |
| V3 | E com todas as fontes + mapa chat→slot, sem confundir área com fonte; envelope por família |
| V4 | C congelado com todas as fontes + mesmo mapa; envelope por família |
| V5, só Qwen | **Mesmo texto V3**, mesmo contrato, mas ordinais em lugar de `<imageN>` — ablação de tags, não mudança de modelo/temperatura |

**PROPOSTA:** V1/V2 isolam prefixo, V3/V4 comparam sistemas de escrita E/C (não “raciocínio puro”, pois os sistemas diferem), V3/V5 isolam tags. Se o alvo posterior for raciocínio isolado, acrescentar E-think com sistema/temperatura/schema idênticos ao E em uma subrodada, não atribuir automaticamente a diferença de V3/V4 ao think.

**PROPOSTA:** primeira rodada sem máscara e saída inteira, para não misturar problema de composição por área com referência. Não variar `ref_boost`, `grounding_px`, steps ou largura das referências junto com prefixo. R4 será o estrato em que uma futura ablação de papel A/força faz mais sentido.

### 5.4 O que precisa mudar no lab

| Ponto atual | FATO do limite | PROPOSTA LAB-04 |
|---|---|---|
| `prompt_prepare` | Resolve só `base` ou primeira referência e copia um upload; schema usa `source` singular (`L:2258-2291`) | Manifesto de casos com `sources[]` ordenadas, `chat_number`, `roles[]`, `purposes[]`, `is_canvas`, tamanho/hash; conservar fonte original do pedido e slots efetivos. Extrair **todas** as referências com `reference_plan`, não “primeira referência”. Casos sintéticos do acervo devem declarar decisão explicitamente |
| `prompt_request` | Recebe `image: bytes`; prepara uma imagem; E/C2 ganham só fonte+área; C chama frozen monoimagem (`L:2330-2357`) | Receber lista/slots; construir mapa explícito; revisores recebem todas as fontes em ordem. Contexto da área deve ser campo separado com índice próprio, nunca slot N+1 de geração |
| `prompt_graph` | Detecta Qwen pelo `edit_method`, escolhe uma variante monoimagem (`L:2410-2424`) | Escolher pelo manifesto/família **e N**, usando o mesmo `edit_refs[N]` resolvido que o filtro usa. Recusar arquivo/mapeamento incompleto, sem fallback monoimagem |
| `redraw_graph` | Só um `image_name`, LoadImage 72 ou 470 e IDs fixos de um fluxo (`L:1682-1718`) | Não transformar redraw monoimagem em planejador universal. Introduzir construtor de edit multirref baseado no mapa do manifesto/stack, recebendo lista de filenames; `prompt_graph` chama-o. Manter redraw como wrapper de N=1 ou delegar edição ao mesmo helper |
| `prompt_run` | Lê/copia/upload de `source.png` único (`L:2588-2602,2642-2669`) | Ler cada fonte, preparar Qwen com a mesma geometria/bounding de produção (`F:5636-5666`), fazer upload de todas, preservar ordem e reaproveitar uploads por caso/família; gerar grafo com todos os inputs. Agrupar execuções por modelo real, não `part` |
| Revisão/métricas | Review exporta uma `source`, variante por escritor×seed e compara contra A (`L:2427-2474,2528-2558`) | Exportar `references[]` com legenda papel/índice e fontes; mostrar outputs inteiros; rótulos de variantes cegos para avaliação; métricas por família/N/caso. Proteger chave da rodada para não misturar notas antigas |

**PROPOSTA concreta de grafo:** carregar o workflow escolhido do manifesto; preencher cada `mapping.image.node_ids[i]` com o upload do slot i; preencher `mapping.prompt`, `mapping.seed` e bindings de canvas/força que existem; no Qwen os LoadImages 470/471/472 já estão conectados ao encoder; no Krea usar 72/74 e manter `114`/`115` e suas conexões internas, sem ligar a segunda fonte só na visão e esquecer o VAE. Manter `target_latent`, positivo e negativo conectados conforme o workflow existente (`manifests/workflows.yaml:82-101,219-226`; `workflows/krea2-identity-edit-v2-2-api.json:140-218`).

**FATO:** o lab roda sem dependência do Hub/Open WebUI e já importa helpers do filtro por caminho (`L:2091-2097`). **PROPOSTA:** resolver os manifestos no preparo Windows e embutir workflow+mapa+bindings no documento portátil, ou usar o bundle já disponível; não exigir pywebview/Pydantic no pod. Reutilizar o compilador do filtro no lab, não copiar sua convenção.

**PROPOSTA de artefatos obrigatórios:** decisão congelada, mapa chat→slot, hash e tamanho de cada fonte, prompt autoral, envelope, prompt final, request/answer completo (incluindo thinking onde retornado), grafo final, seed, família/manifesto/hash, software fixado, timings por fase, output bruto e erros reais. O lab já salva requests/answers/grafos sem inventar respostas no dry-run (`L:2602-2622,2666-2675`); preservar esse comportamento.

### 5.5 Métricas e regra de decisão

**PROPOSTA — integridade antes de qualidade:**

- 100% dos slots pedidos presentes uma vez por imagem distinta, papéis agregados corretamente;
- `<imageN>` Qwen dentro de 1..N e correspondendo ao upload/latente; nenhum número do chat usado como índice físico;
- R1/R2 produzem o mesmo contrato e conexões semânticas; seed/canvas/força idênticos em pares;
- nenhuma alteração de `action`, `mode`, fonte ou base pelo revisor; recusa de capacidade explícita;
- receita/regeração preserva ordem e prompt final, sem duplicar envelope;
- zero chamadas ComfyUI na salvaguarda, fonte faltante ou capacidade inválida.

**PROPOSTA — avaliação cega pelo usuário, saída inteira, escala 0–4 por eixo:** cumprimento da operação; pose/composição A; identidade de cada B; transferência **somente** do atributo pedido de C; preservação do restante; qualidade/coerência geral. Marcar separadamente troca de identidade, fusão/duplicação de pessoa, fonte errada, vazamento de roupa/fundo/estilo, subedição. Preferência pareada −2…+2 contra V0 e comparação V1×V2/V3×V4. Notas por seed e por caso/família; não tratar as duas seeds como dois usuários independentes.

**PROPOSTA — números auxiliares:** latência escrita/visão/sampling/upload/carga/troca, pico de VRAM/RAM, tokens, erros/OOM e tempo total. Pose-keypoints/semelhança de identidade ou embeddings só como auxiliares locais com consentimento; sem usar serviços externos nem afirmar que um score de face comprova fidelidade. MAD/SSIM global não é métrica de sucesso numa troca de pose/cena; o próprio lab já avisa que diferença mede mudança, não obediência (`L:1078-1081`; definição de MAD em `F:4240-4251`).

**PROPOSTA de gate:** corrigir integridade antes de gerar. Promover o contrato V2 sobre V1 se reduzir erros de binding e obtiver ganho médio ≥0,25 ponto na preferência pareada, sem regressão sistemática por família e sem piora grave de identidade. Para E versus C, E é preferido pelo custo se ficar a até 0,25 ponto de C e sem falha grave adicional; se C tiver vantagem consistente por família/caso, manter E/C nessa composição. Habilitar par multirref apenas após os dois serem melhores ou não inferiores ao V0 e o custo ser aceito. Amostra inconclusiva → mais seeds nos casos discordantes, não declarar empate comprovado.

### 5.6 Custo de GPU

**PROPOSTA de contagem:** R1–R4 em duas famílias = 8 combinações; R5–R8 em Qwen = 4; total 12 combinações × 2 seeds × V0..V4 = **120 samplings**. V5 roda nas 8 combinações Qwen × 2 seeds = **16**, total **136**. V1/V2 compartilham escrita; V3/V5 também. E/C são escritos uma vez por combinação e reutilizados nas seeds. O par no chat não precisa de sampling adicional no lab: são os outputs E e C já comparados.

**FATOS de referência, não extrapolação garantida:** LAB-03 monoimagem levou 1.187,42 s para 96 jobs (material do pacote privado, linhas 3-5); o único turno real de duas fontes levou 35,94 s no job (`T:141`).

**INFERÊNCIA/orçamento inicial:** 20–90 s por sampling multirref = 45–204 minutos para 136 imagens, mais escrita/cargas/transferências. Reservar aproximadamente **1–4 horas de GPU**, como faixa conservadora, não promessa. O custo monetário é `horas faturadas × tarifa real do pod`; não consultei cotação nem há preço verificado. N fontes aumentam latentes e tokens visuais ([nós ComfyUI fixados](https://github.com/Comfy-Org/ComfyUI/blob/b0b743566f65daafc423b4fea8a2fbda94b3384a/comfy_extras/nodes_qwen.py)); três fontes não podem ser orçadas só com média monoimagem.

**PROPOSTA de execução econômica:** piloto incluído na contagem, com N=2/N=3 Qwen e N=2 Krea; medir p50/p95 e memória antes do restante. Recalcular orçamento = escrita + cargas + jobs restantes×tempo observado; não paralelizar Ollama e ComfyUI na mesma GPU; agrupar por família, salvar prompts antes de descarregar Ollama, como o lab já faz (`L:2584-2585,2639-2651`; exclusividade do perfil em `profiles/capella.yaml:129-146`).

## 6. Plano de implementação em passos pequenos

**PROPOSTA. Nada desta seção foi implementado nesta pesquisa.** Manter o filtro autocontido, pois é enviado como arquivo único e não importa o pacote Hub (`F:9-10`).

| Passo | Arquivos/símbolos | Entrega e testes propostos |
|---|---|---|
| 1. Fixar contrato antes de escrita | `assets/open-webui/pepito_image_chat.py::reference_plan/reference_prompt/_routed` | Tornar explícito mapa chat→slot/base sem mudar a ordenação/capacidade. Em `tests/test_image_edit_dispatch.py`, expandir o teste `test_two_references_reach_their_own_nodes_in_role_order_with_what_each_is_for` (`:1541`) com base fora de ordem, A+B+C, A+B no mesmo slot e referências duplicadas |
| 2. Generalizar payloads de N fontes | Mesmo filtro: `prompt_writer_payload`, `prompt_revision_payload`, `frozen_route_payload`, `write_prompt_pair` | Lista ordenada de imagens e descritores; anotação de área em campo/contexto separado. Atualizar todos os callers no filtro **e** `scripts/edit_lab.py::prompt_request`, sem shim monoimagem. Testes em `tests/test_image_prompt_writer.py`, `tests/test_prompt_pair.py`, `tests/test_edit_lab_prompt.py`: quantidade/ordem de pixels sintéticos, mapa de tags, fonte≠área, decisão imutável, erro de fonte faltante |
| 3. Unificar compilação final | Filtro `_routed`, reaproveitando `reference_prompt` | Compilar envelope após prompt autoral para router/vision/vision_think/E/C, antes de salvaguarda/persistência. Não duplicar prefixo no router; monoimagem mantém sem tags; raw continua diagnóstico explícito. Testar prefixo e cada purpose em todos os escritores, Qwen tags versus Krea ordinais e risco de idade no texto final. Testar Source purpose monoimagem separadamente |
| 4. Lab monta grafo real de N fontes | `scripts/edit_lab.py::prompt_prepare/prompt_request/prompt_graph/redraw_graph/prompt_run/write_prompt_review`; mapas de `manifests/workflows.yaml` | Schema portátil com fontes/workflow/mapa; uploads N, preparo Qwen equivalente e seleção por família/manifesto. Ampliar `tests/test_edit_lab_prompt.py` e `tests/test_edit_lab_redraw.py`: grafos 2/3 Qwen e 2 Krea, ordem de LoadImages/encoder/VAE, seed/canvas, falha sem grafo, raw e requests reais no dry-run. Não gerar imagens reais nos unitários |
| 5. Rodar LAB-04 com contrato ainda controlado | Casos/artefatos fora do repo; roteiro do lab | Rodar teste focado e dry-run uma vez após integrar mudanças; depois piloto/GPU e avaliação do usuário. Gravar também V1 sem envelope como controle experimental, não como default produtivo. Não confundir correção de binding com melhoria estética não medida |
| 6. Ampliar par condicionado à evidência | Filtro `_routed::pair` (`F:9677-9687`) e `write_prompt_pair` | Só então permitir N compatível com o workflow. Manter exclusão swap/warp/geração/regeneração/N explicitamente pedido. Testes `tests/test_prompt_pair.py`: E/C N fontes, mesma seed/ordem/canvas, quedas E/C existentes, duas receitas completas, preferência e próxima edição partindo da escolhida |
| 7. Fechar persistência e documentação | Filtro `previous_edit/remember_edit/_regenerate`, testes de receita/regeneração existentes; `docs/PLANO.md` CHAT-15; futura spec com aceite; changelog existente do projeto | Testar regeração igual/mesma seed/prompt editado com N fontes, sem perder ordem nem duplicar contrato; fonte indisponível recusa. Registrar resultado e limites por família, atualizar CHAT-15 e a SPEC-319 para o novo escopo apenas se aprovado pelo lab |

**Conjunto focado sugerido, a executar pelo implementador após integrar:** `tests/test_image_edit_dispatch.py`, `tests/test_image_prompt_writer.py`, `tests/test_prompt_pair.py`, `tests/test_edit_lab_prompt.py`, `tests/test_edit_lab_redraw.py`, `tests/test_workflows.py` e testes existentes de receitas/seleção de prompt afetados. São sugestões de verificação, **não checks executados neste estudo**. Casos existentes já cobrem rejeição sem descarte e ordem cena/personagem (`tests/test_image_edit_dispatch.py:1568,2677-2709`); a ampliação deve preservar esses invariantes, não afrouxá-los para fazer o lab passar.

## 7. Limites e dúvidas abertas

1. **Sem evidência visual de falha multirreferência:** o único turno do trace teve uma saída e diferença mensurável; não há nota de qualidade naquele evento (`T:141-144`). O diagnóstico causal continua hipótese.
2. **LAB-03 não transfere automaticamente:** 17 comparações por escritor após exclusões, um avaliador, nenhum caso de duas fontes (material do pacote privado, linhas 21-29, 51). Não inferir que E/C vencerão em composição.
3. **`Source purpose` foi descrito amplamente demais na pendência:** em múltiplas fontes o que falta é o envelope com fins por slot; a string literal só é monoimagem (`F:9768-9789`; pendência `docs/PLANO.md:373`).
4. **Tags são contrato do reescritor 2.1, não pré-condição universal comprovada:** o próprio README Qwen dá exemplo sem tags ([README](https://github.com/QwenLM/Qwen-Image-2.1#image-editing-multiple-reference-images)). Medir V3/V5, sem prometer causalidade antes do lab.
5. **Krea2Edit não é FLUX Kontext:** o nó/LoRA comunitário tem sua própria ordem e geometria ([autor](https://github.com/lbouaraba/comfyui-krea2edit)); os guias oficiais atuais de Kontext não documentam o mesmo contrato multirreferência ([BFL](https://docs.bfl.ml/kontext/kontext_image_editing.md)). Não encontrei uma especificação oficial Krea exigindo `<imageN>`: **[NÃO VERIFICADO]** qualquer afirmação de que essas tags são necessárias no Krea.
6. **Capacidade configurada versus modelo:** Qwen anuncia dez fontes ([oficial](https://github.com/QwenLM/Qwen-Image-2.1)); Hub só tem workflows 1/2/3 (`profiles/capella.yaml:95-99`); Krea configurado só cena+personagem (`:121-122`). Não expandir isso no mesmo ticket.
7. **Várias identidades sem canvas:** o sistema oficial Qwen contempla composição sem canvas ([reescritor](https://github.com/QwenLM/Qwen-Image-2.1/blob/main/prompt_rewrite/prompts/system_prompt_edit.txt)); nosso fluxo sempre usa a primeira fonte como referência geométrica (`F:5636-5666`). R8 mede uma cena explícita; retrato coletivo sem cena-base exige desenho próprio de canvas, fora da correção de prefixo.
8. **Área com N fontes:** como apontar a área do canvas sem que seja confundida com referência? O schema revisado deve distinguir fontes de geração e imagem auxiliar anotada. O sistema atual promete colagem fora da área (`F:1777-1779`), mas o padrão `area_paste=False` entrega a saída inteira (`F:7978-7983`). Essa instrução precisa corresponder ao modo real; não basta acrescentar N à assinatura.
9. **Raw:** decidir formalmente se continua literalmente cru ou se passa a ser “pedido cru + envelope”. Recomendo conservar cru e não contaminar a ablação.
10. **Tempo e memória do revisor:** o custo de ver duas/três fontes no 27B, seu contexto 16k e eventual offload não foram medidos. Fato de configuração: `profiles/capella.yaml:45-48`; medir antes de ligar par multirreferência por padrão.
11. **Fontes do lab não inspecionadas visualmente:** inventário comprova nomes/tamanhos, não conteúdo ou contraste de estilo. R5/R7/R8 precisam de confirmação visual pelo usuário para que roupa/estilo/duas identidades sejam julgáveis; não é necessário fornecer novas imagens ao assistente.
12. **Seed e grafo históricos incompletos:** o turno real registra seed nula e não grava hash do grafo (`T:141`). A repetição exata não é demonstrável só por esse trace. A receita/configuração atual permite reconstrução estrutural, não prova de equivalência bit a bit com 10/11.

**Conclusão:** o caminho físico de N referências já existe e preserva slots; a falha comprovada é a inconsistência do contrato textual e da informação fornecida aos escritores. A intervenção mais segura é compartilhar mapa de slots + envelope por família com E/C e com o lab, medir em grafos `edit_refs` reais e só depois ampliar o par produtivo.
