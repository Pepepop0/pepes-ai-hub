# Capella on the RTX 5090: the test the client's machine still owes

Capella now runs `orcarouter/Qwen3.8-27B-Uncensored:q5_K_M` (20.5 GB, vision,
tools) for chat and FLUX.2 Klein 9B FP8 for generation and editing. The two
do not fit on the card together, so every Image turn unloads Ollama before
ComfyUI runs and frees ComfyUI before the chat model answers. None of this has
run on a real RTX 5090 yet: bring this file and fill in the right-hand column.

Machine: Windows 10/11, NVIDIA RTX 5090 (nvidia-smi shows `32607MiB`), 32 GB
RAM minimum (64 GB recommended), ~50 GB free disk, Git, no other Ollama /
Open WebUI / ComfyUI listening on 11434 / 8080 / 8188.

| # | step | expected | result |
| --- | --- | --- | --- |
| 0 | Accept the license at huggingface.co/black-forest-labs/FLUX.2-klein-9b-fp8, then `PepitosAIHub.exe --hf-login` with a read token, then `--hf-status` | `authenticated: true`, `capella_model_access: true`; the token is never echoed | |
| 1 | Run `PepitosAIHub.exe`; in the wizard pick **Capella** | Details say COMPATIBLE; GPU line `32607 MiB`; at most the warning "accepted within the profile's 256 MiB reporting tolerance" | |
| 2 | Install (default location) | ComfyUI, Ollama, Open WebUI 0.12.0 installed; models acquired: `flux2_klein_9b_fp8` 9.43 GB, `qwen_3_8b_fp8mixed_encoder` 8.66 GB, `flux2_small_decoder` 0.25 GB, `orcarouter/Qwen3.8-27B-Uncensored:q5_K_M` 20.5 GB; "Tudo pronto" | |
| 3 | Open WebUI: create the first (admin) account, then **Abrir Open WebUI** from the Hub | Hub reports filter, chat model and image editing "instalado"; model selector shows the Qwen3.8 27B model selected | |
| 4 | Chat: `Olá! Em duas frases, o que você sabe fazer?` then `In one sentence, what is the difference between RAM and VRAM?` | Fluent Portuguese and English, no "Thinking" block | |
| 5 | `nvidia-smi` while the model is loaded | Qwen3.8 27B resident, about 22-24 GB used; `ollama ps` shows 100% GPU | |
| 6 | Vision: attach a photo (Mais → Carregar Arquivos), ask `O que aparece nesta imagem?` | A correct description of the picture; no image job in ComfyUI | |
| 7 | Tools: Workspace → Tools, create a small tool (or use one already there); select it in the composer; ask for it directly | The tool runs once, with the right arguments, and its result is shown; a plain `hi` with the tool still selected does not call it | |
| 8 | Image on (Integrações → Imagem): `Generate an image of a red apple on a white table` | `ollama ps` empty while ComfyUI runs; 1024×1024 PNG shown in the chat; the reply follows once ComfyUI freed its models; time it | |
| 9 | `Change the apple to green` | Edit job: the apple picture as input (green apple, same scene) | |
| 10 | `Change the background to black` | Edit of the green apple (black background) | |
| 11 | `Now an image of a medieval castle on a hill` | A new picture: no apple in it | |
| 12 | Image off: `What is 2 + 2?` | Text answer, no ComfyUI job | |
| 13 | `nvidia-smi` during step 11, then right after the reply | During: only ComfyUI holds memory (about 18-20 GB); after: only the chat model | |
| 14 | Hub → Restart | All three services back, Open WebUI answers, `ollama ps` empty until the next chat turn | |
| 15 | Hub → Stop, then Start | Stop leaves nothing on 11434/8080/8188; Start brings all three back on the first try | |
| 16 | Close the Hub and run `PepitosAIHub.exe` again | Dashboard, not the wizard; nothing reinstalled or downloaded; "já atual" for filter, model record and image editing | |
| 17 | RAM (Task Manager) during step 8 | ComfyUI about 11 GB, Ollama about 1 GB (model on the GPU), Open WebUI under 1 GB | |

Record the timings of steps 4, 8 and 9 and the two `nvidia-smi` readings;
the swap costs a model reload on every Image turn, and these numbers decide
whether that is acceptable.
