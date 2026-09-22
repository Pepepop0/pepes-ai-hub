# Capella on the RTX 5090: the test the client's machine still owes

Capella was finalised and exercised on the reference machine (RTX 5070,
12 GB) with the facts a 5090 reports injected into the test harness -- see
`profile-capella.md`. Everything the card's size decides (a 27B chat model
resident next to the image models, 1024×1024 generation, the timings) is
therefore **not validated** until this list is run on a real RTX 5090. Bring
this file and fill in the right-hand column.

Machine: Windows 10/11, NVIDIA RTX 5090 (nvidia-smi shows `32607MiB`), 32 GB
RAM minimum (64 GB recommended), ~50 GB free disk, Git, no other Ollama /
Open WebUI / ComfyUI listening on 11434 / 8080 / 8188.

| # | step | expected | result |
| --- | --- | --- | --- |
| 1 | Run `PepitosAIHub.exe`; in the wizard pick **Capella** | Details say COMPATIBLE; GPU line `32607 MiB`; at most the warning "accepted within the profile's 256 MiB reporting tolerance" | |
| 2 | Install (default location) | ComfyUI, Ollama, Open WebUI installed; models acquired: `flux2_klein_fp8` 4.07 GB, `qwen_3_4b_encoder` 8.04 GB, `flux2_vae` 0.34 GB, `qwen3.5:27b` 17.4 GB; "Tudo pronto" | |
| 3 | Open WebUI: create the first (admin) account, then **Abrir Open WebUI** from the Hub | Hub reports filter, chat model and image editing "instalado"; model selector shows `qwen3.5:27b` selected | |
| 4 | Chat: `Olá! Em duas frases, o que você sabe fazer?` then `In one sentence, what is the difference between RAM and VRAM?` | Fluent Portuguese and English; a collapsed "Thinking" block may precede each reply | |
| 5 | `nvidia-smi` while the model is loaded | `qwen3.5:27b` resident, about 18-20 GB used; `ollama ps` shows 100% GPU | |
| 6 | Vision: attach a photo (Mais → Carregar Arquivos), ask `O que aparece nesta imagem?` | A correct description of the picture; no image job in ComfyUI | |
| 7 | Tools: Workspace → Tools, create a small tool (or use one already there); select it in the composer; ask for it directly | The tool runs once, with the right arguments, and its result is shown; a plain `hi` with the tool still selected does not call it | |
| 8 | Image on (Integrações → Imagem): `Generate an image of a red apple on a white table` | ComfyUI job, 1024×1024 PNG shown in the chat, reply says it is shown; time it | |
| 9 | `Change the apple to green` | Edit job: the apple picture as input (green apple, same scene) | |
| 10 | `Change the background to black` | Edit of the green apple (black background) | |
| 11 | `Now an image of a medieval castle on a hill` | A new picture: no apple in it | |
| 12 | Image off: `What is 2 + 2?` | Text answer, no ComfyUI job | |
| 13 | `nvidia-smi` right after step 11 | Under 32 GB with the chat model still resident (about 25-30 GB) | |
| 14 | Hub → Restart | All three services back, Open WebUI answers, `ollama ps` empty until the next chat turn | |
| 15 | Hub → Stop, then Start | Stop leaves nothing on 11434/8080/8188; Start brings all three back on the first try | |
| 16 | Close the Hub and run `PepitosAIHub.exe` again | Dashboard, not the wizard; nothing reinstalled or downloaded; "já atual" for filter, model record and image editing | |
| 17 | RAM (Task Manager) during step 8 | ComfyUI about 11 GB, Ollama about 1 GB (model on the GPU), Open WebUI under 1 GB | |

Record the timings of steps 4, 8 and 9 and the two `nvidia-smi` readings;
they are what the profile's settings (1024×1024, 20 steps) were chosen for
and the only numbers the reference machine could not produce.
