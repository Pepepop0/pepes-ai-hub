# Avisos de terceiros

O Pepe's AI Hub (licença MIT, ver [LICENSE](LICENSE)) **não redistribui** nenhum dos
componentes ou modelos abaixo. Eles são baixados da origem durante a instalação, cada um
fixado por versão e SHA-256 em `manifests/`. Cada componente segue a licença do seu autor,
lida na página de origem antes do uso. Onde o repositório não declara a licença, a coluna diz
"ver página de origem".

## Componentes

| Componente | Versão | Origem | Licença |
| --- | --- | --- | --- |
| Ollama | 0.33.2 | https://github.com/ollama/ollama/releases/tag/v0.33.2 | ver página de origem |
| Open WebUI | 0.12.0 (pacote PyPI) | https://pypi.org/project/open-webui/0.12.0/ | ver página de origem |
| ComfyUI | v0.39.0 | https://github.com/comfyanonymous/ComfyUI | ver página de origem |

## Modelos de chat (Ollama)

| Modelo | Perfil | Origem | Licença |
| --- | --- | --- | --- |
| `hermes3:8b` | Pepe | https://ollama.com/library/hermes3 | ver página de origem |
| `hermes3:3b` | Pepinho | https://ollama.com/library/hermes3 | ver página de origem |
| Qwen3.8 27B Q5_K_M (`orcarouter/Qwen3.8-27B-…:q5_K_M`) | Capella | https://ollama.com/orcarouter | ver página de origem |

## Modelos de imagem (Hugging Face, via ComfyUI)

| Modelo | Perfil | Origem | Licença |
| --- | --- | --- | --- |
| FLUX.2 klein 4B (fp8) | Pepinho, Pepe | https://huggingface.co/black-forest-labs/FLUX.2-klein-4b-fp8 | ver página de origem |
| Codificador de texto Qwen 3 4B (ComfyUI) | Pepinho, Pepe | https://huggingface.co/Comfy-Org/flux2-klein-4B | ver página de origem |
| VAE FLUX.2 (ComfyUI) | Pepinho, Pepe | https://huggingface.co/Comfy-Org/flux2-dev | ver página de origem |
| Qwen-Image 2.1 (INT8 ConvRot, ComfyUI) | Capella | https://huggingface.co/Comfy-Org/Qwen-Image-2.1 | Qwen Research (uso não comercial), conforme declarado em `manifests/models.yaml` |
| SAM 3.1 (multiplex fp16, ComfyUI) | Capella | https://huggingface.co/Comfy-Org/sam3.1 | Meta SAM License, conforme declarado em `manifests/models.yaml` |
| Depth Anything 3 Mono Large (ComfyUI) | Capella | https://huggingface.co/Comfy-Org/Depth-Anything-3 | Apache-2.0, conforme declarado em `manifests/models.yaml` |

As URLs exatas de cada arquivo (com a revisão fixada) estão em `manifests/models.yaml`,
`manifests/artifacts.yaml`, `manifests/packages.yaml` e `manifests/repositories.yaml`. Os
nomes e marcas pertencem aos seus donos.
