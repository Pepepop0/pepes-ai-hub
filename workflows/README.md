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
- `flux2-klein-9b-t2i-api.json` / `flux2-klein-9b-edit-api.json` — os mesmos
  dois grafos, com os mesmos node ids e o mesmo mapping, apontando para o
  conjunto do FLUX.2 Klein 9B usado pela Capella:
  `flux-2-klein-9b-fp8.safetensors`, `qwen_3_8b_fp8mixed.safetensors`
  (encoder Qwen3 8B) e `full_encoder_small_decoder.safetensors` (VAE com
  decoder pequeno). O 9B é destilado: 4 passos, CFG 1. Ainda não rodou numa
  RTX 5090 real (ver `docs/testing/capella-5090-checklist.md`).

O mapping dos node ids de ambos está em `manifests/workflows.yaml` e é
validado contra estes arquivos pelos testes.

Os valores gravados no export (prompt, seed, nome do modelo, imagem de
exemplo) são apenas o estado em que o grafo foi salvo: quem submete o grafo
(o `render_workflow` do Hub, ou o Open WebUI pelo mapping que o Hub
configura) sobrescreve todos eles.
