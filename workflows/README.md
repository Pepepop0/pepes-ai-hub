# Workflows

Workflows **ComfyUI API format**, não o JSON visual comum.

- `flux2-klein-t2i-api.json` — exportado do ComfyUI Desktop desta máquina
  (FLUX.2 Klein text-to-image). Versionado byte a byte como saiu do export.
- `flux2-klein-edit-api.json` — edição de imagem com FLUX.2 Klein 4B
  Distilled. Origem: o template oficial
  `image_flux2_klein_image_edit_4b_distilled.json` do pacote
  `comfyui-workflow-templates-json` 0.1.57 (o que acompanha o ComfyUI 0.34.0
  do stack), carregado no frontend do próprio ComfyUI (1.49.6) e exportado
  com `app.graphToPrompt()` — o mesmo código do "Export (API)". Os títulos
  em `_meta` estão no idioma do navegador que exportou. Uma única alteração
  em relação ao export: o nó `81` (um segundo `LoadImage`) foi removido; ele
  só alimentava a variante de duas imagens de referência, que o template
  deixa desativada, e não era usado por nada no grafo exportado. Usa
  exatamente os três arquivos que o text-to-image já instala
  (`flux-2-klein-4b-fp8.safetensors`, `qwen_3_4b.safetensors`,
  `flux2-vae.safetensors`). Largura e altura vêm da imagem de entrada
  (escalada para 1 megapixel, depois `GetImageSize`); o modelo destilado
  roda 4 passos com CFG 1.
- `qwen-image-2.1-t2i-api.json` / `qwen-image-2.1-edit-api.json` — Qwen-Image
  2.1 (perfil Capella). Origem: os templates oficiais
  `templates/image_qwen_image_2_1_t2i.json` e
  `templates/image_qwen_image_2_1_image_edit.json` do repositório
  `Comfy-Org/workflow_templates`, no sha
  `cdccb2ee7a7cd0baee4630cf7186283a98ab408f`. Os templates vêm no formato
  visual com subgraph; a conversão para API foi feita à mão: o subgraph
  (`459`) foi achatado com os ids internos prefixados (`459:451`, …) e os
  valores dos widgets promovidos viraram inputs nomeados. O que mudou:
  - o ramo do *prompt enhancer* (`TextGenerate`, `ComfySwitchNode`,
    `PreviewAny`, o system prompt e o CLIP `qwen3.5_9b_…_pe_*`) saiu; os
    templates já começam com ele desligado, então o prompt vai direto para o
    `TextEncodeQwenImage21`, como no template;
  - Notes, `ImageCompare` e o segundo `LoadImage` de referência saíram;
    `SaveImageAdvanced` virou `SaveImage`;
  - no t2i, o `ResolutionSelector` virou dois `PrimitiveInt` (`485` largura,
    `486` altura, 1024×1024) e o prompt um `PrimitiveStringMultiline` (`484`);
  - no edit, `resolution` do `TextEncodeQwenImage21` é 1024 (o padrão oficial
    citado na nota do template, que começa em 0): a imagem de entrada é
    redimensionada para cerca de 1024×1024 pixels em múltiplos de 32,
    mantendo a proporção, e a saída `latent` do encoder é o canvas
    (`custom_size` desligado, como no template). Só `image`, `prompt`,
    `model` e `seed` são mapeados.

  Valores do template mantidos: `KSampler` euler/simple, 25 passos, CFG 1,
  denoise 1; `QwenImage21Cache` auto/default; `CLIPLoader` tipo
  `qwen_image`; arquivos `qwen_image_2.1_int8_convrot.safetensors`,
  `qwen3vl_8b_int8_convrot.safetensors`, `qwen_image_2.1_vae_bf16.safetensors`.

## Character swap analysis (ComfyUI v0.39.0)

`sam3-people-api.json` and `da3-depth-api.json` are API-format reductions of the
official [SAM3 image segmentation blueprint](https://github.com/comfyanonymous/ComfyUI/blob/v0.39.0/blueprints/Image%20Segmentation%20%28SAM3%29.json)
and [DA3 image depth blueprint](https://github.com/comfyanonymous/ComfyUI/blob/v0.39.0/blueprints/Image%20Depth%20Estimation%20%28Depth%20Anything%203%29.json).
The model loaders, output slots and widget inputs were checked against that tag's
[`nodes_sam3.py`](https://github.com/comfyanonymous/ComfyUI/blob/v0.39.0/comfy_extras/nodes_sam3.py)
and [`nodes_depth_anything_3.py`](https://github.com/comfyanonymous/ComfyUI/blob/v0.39.0/comfy_extras/nodes_depth_anything_3.py).

- SAM loads MODEL and CLIP from `CheckpointLoaderSimple`; `CLIPTextEncode` feeds
  `SAM3_Detect`. Its prompt is `person:1000`, not plain `person`: the native
  [`sam3_clip.py`](https://github.com/comfyanonymous/ComfyUI/blob/v0.39.0/comfy/text_encoders/sam3_clip.py)
  tokenizer strips `:N` from the category text and emits `sam3_multi_cond` with
  `max_detections=N`. Plain conditioning is capped at one detection even with
  `individual_masks=true`. The generous cap preserves all detector candidates;
  the probability threshold remains `> 0.5`, with two refinement iterations.
  `MaskToImage` and `SaveImage` (node `6`) save one PNG per person; no detection
  produces an empty mask batch, not a fabricated foreground.
- DA3 uses Mono Large, default dtype, resolution 504 and `upper_bound_resize`.
  DynamicCombo API inputs are **flat dotted keys**:
  `mode: "mono"`; `output: "depth"`; `output.normalization: "v2_style"`;
  `output.apply_sky_clip: false`. These names match the blueprint's input/widget
  names and [`_io.py`](https://github.com/comfyanonymous/ComfyUI/blob/v0.39.0/comfy_api/latest/_io.py):
  `finalize_prefix` joins with dots, `DynamicCombo._expand_schema_for_dynamic`
  keeps the root selection and expands children under that prefix, and
  `build_nested_inputs` reconstructs the dictionaries passed to `execute`.
  Nested JSON dictionaries or unprefixed normalization/sky keys are incorrect.
  `DA3Render` emits grayscale depth with near = white; `SaveImage` is node `5`.
- `models.yaml` records the immutable Hugging Face revisions, LFS SHA-256 and
  sizes, re-read from the raw pointers at those exact revisions. Both models
  have kind `auxiliary`; **placement**, not kind, chooses the managed directory:
  `checkpoints` for SAM and `geometry_estimation` for DA3. ComfyUI v0.39.0
  `folder_paths.py` registers both search categories, and its extra-path loader
  accepts those same names.
- Capella's profile-level `character_swap` declares four passes (`reference`, `marked`,
  `segment`, `depth`); a pack may add a fifth, `scene`. Its `models.swap` roles enter
  ordinary model acquisition and size estimation; Qwen roles use their existing groups.
  Missing weights/graphs disable the entire optional `stack["swap"]` section.
  Parts retain the existing `workflow` JSON string, `nodes` list
  (`type`, `key`, `node_ids`), `model` runtime filename, capabilities and optional
  filter-owned bindings; the depth part intentionally has no prompt mapping.

Offline tests check manifest bindings, dependency links, model filenames, DynamicCombo
keys, and shared stack assembly. These checks do not execute inference or establish
mask/depth quality; GPU execution with the real weights remains unverified.



O mapping dos node ids de todos está em `manifests/workflows.yaml` e é
validado contra estes arquivos pelos testes.

Os valores gravados no export (prompt, seed, nome do modelo, imagem de
exemplo) são apenas o estado em que o grafo foi salvo: quem submete o grafo
(o `render_workflow` do Hub, ou o Open WebUI pelo mapping que o Hub
configura) sobrescreve todos eles.

## Pacotes opcionais

`scripts/apply_pack.py <nome>` instala arquivos novos de `packs/<nome>/`, sem
sobrescrever arquivos existentes diferentes. Uma colisão impede a instalação
antes da primeira cópia; arquivos idênticos permitem reaplicar o pacote.
Links e junções não são aceitos. A pasta `archive/` do pacote guarda originais
privados para consulta e nunca é instalada.

O Hub combina `manifests/models.d/*.yaml` e `manifests/workflows.d/*.yaml`
com os manifestos principais, recusando IDs duplicados. Perfis
`*.overlay.yaml` usam `extends: <id>`; mapas são combinados recursivamente e
variantes são combinadas pelo seu `id`. `behavior.router_rules` e
`behavior.poses` são enviados à pilha do modelo, sem regras extras embutidas
no código. A retenção reconhece apenas prefixos `SaveImage` dos workflows
carregados, inclusive os adicionados pelo pacote.

O bundle Linux padrão não inclui `packs/`; `scripts/build_linux.py --pack <nome>`
inclui os recursos instaláveis do pacote. Laboratórios que dependem de grafos
opcionais exigem o pacote aplicado; não leem a cópia ainda não instalada.
