# Estado atual — setup manual de referência (Pepe)

## Hardware

- Windows 10
- RTX 5070
- 12 GB VRAM

## Ollama

Instalação nativa Windows.

Modelos usados/testados:
- `qwen3:8b` — confirmado em GPU;
- `qwen3.5:9b` — usado como chat principal em testes;
- `hermes3:8b` — candidato para Native tool calling.

## Open WebUI

Instalação nativa via `uvx`/Python 3.11.

```text
C:\open-webui\data
```

```powershell
$env:DATA_DIR="C:\open-webui\data"
uvx --python 3.11 open-webui@latest serve
```

UI: `http://localhost:8080`

## ComfyUI

ComfyUI Desktop funcionando. Base path observado no setup manual:

```text
C:\Users\Pedro\AppData\Local\Comfy-Desktop\ComfyUI-Shared\models
```

Esse path não deve ser hardcodado no produto.

API local: `http://127.0.0.1:8188/`

## FLUX.2 Klein

Arquivos testados:
- `diffusion_models\flux-2-klein-4b-fp8.safetensors`
- `text_encoders\qwen_3_4b.safetensors`
- `vae\flux2-vae.safetensors`

Text-to-image confirmado em 1024x1024 / 20 steps e Open WebUI -> ComfyUI -> FLUX funcional.

Edição single-image confirmada diretamente no ComfyUI. Integração Edit Image do Open WebUI ainda não foi consolidada.

## Mapping conhecido — text-to-image

```yaml
prompt: {node_id: "76", key: value}
model: {node_id: "75:70", key: unet_name}
width: {node_id: "75:68", key: value}
height: {node_id: "75:69", key: value}
steps: {node_id: "75:62", key: steps}
seed: {node_id: "75:73", key: noise_seed}
```

## Legacy vs Native

Legacy funcionou para geração. Native foi pouco confiável com o Qwen, que às vezes afirmou não possuir capacidade de imagem. Não assumir backend quebrado: o pipeline de geração já foi validado.

## VRAM

FLUX.2 Klein em 1024x1024 chegou a aproximadamente 11.4 GB de VRAM na 5070. Após geração, ComfyUI pode manter vários GB residentes. Orchestration/unload é requisito futuro importante.

## Decisões derivadas

- automação não deve depender do ComfyUI Desktop;
- Open WebUI deve ser configurado por mecanismos suportados;
- workflows API devem ser versionados;
- Pepe exige política agressiva de VRAM.
