# Capella on the RTX 5090: the test the client's machine still owes

Capella 5.1.0 runs `Qwen3.8 27B Q5_K_M` (20.5 GB, vision,
tools) for chat. Its selector has one image preset: **Capella · v5.1.0** (Qwen-Image-2.1). Chat and image models do not fit
on the card together, so every Image turn unloads Ollama before ComfyUI runs and
frees ComfyUI before the chat model answers. None of this has run on a real RTX
5090 yet: bring this file and fill in the right-hand column.

Para o Hub 1.15.0, use primeiro o [runbook econômico atual](runbook-skyrig-1.15.md).
Os cenários abaixo são históricos e incluem políticas substituídas nas versões 1.12–1.15.

Machine: Windows 10/11, NVIDIA RTX 5090 (nvidia-smi shows `32607MiB`), 32 GB
RAM minimum (64 GB recommended), at least 75 GB free disk (downloads ≈ 68 GiB),
Git, a validated key saved under Hub **Settings → Civitai**, no other Ollama /
Open WebUI / ComfyUI listening on 11434 / 8080 / 8188.

| # | step | expected | result |
| --- | --- | --- | --- |
| 1 | Run `PepitosAIHub.exe`; open **Settings → Civitai**, follow **Create API key**, paste it, and click **Validate and save token**; then return to the wizard and pick **Capella** | The Civitai card shows **Connected** and the account username; the token field was cleared; the profile card shows **v5.1.0** next to Capella; details say COMPATIBLE; GPU line `32607 MiB`; at most the warning "accepted within the profile's 256 MiB reporting tolerance" | |
| 2 | Install (default location) | ComfyUI v0.39.0, Ollama and Open WebUI 0.12.0 installed; authenticated Civitai downloads succeed without `CIVITAI_TOKEN` in the environment. Along with the Qwen set, `pepito-image-stacks.json` is written; "Tudo pronto"; the dashboard shows **Capella v5.1.1** and the sidebar **v1.19.0** | |
| 3 | Open WebUI: create the first (admin) account, then **Abrir Open WebUI** from the Hub | Hub reports filter, chat model and image editing "instalado"; model selector shows the Qwen3.8 27B model selected; **Imagem** is already on in a new chat | |
| 4 | Chat: `Olá! Em duas frases, o que você sabe fazer?` then `In one sentence, what is the difference between RAM and VRAM?` | Fluent Portuguese and English, no "Thinking" block | |
| 5 | `nvidia-smi` while the model is loaded | Qwen3.8 27B resident, about 22-24 GB used; `ollama ps` shows 100% GPU | |
| 6 | Vision: attach a photo (Mais → Carregar Arquivos), ask `O que aparece nesta imagem?` | A correct description of the picture; no image job in ComfyUI | |
| 7 | Tools: Workspace → Tools, create a small tool (or use one already there); select it in the composer; ask for it directly | The tool runs once, with the right arguments, and its result is shown; a plain `hi` with the tool still selected does not call it | |
| 8 | With **Imagem** already on: `Generate an image of a red apple on a white table` | `ollama ps` empty while ComfyUI runs; 1024×1024 PNG shown in the chat; the reply follows once ComfyUI freed its models; time it | |
| 9 | `Change the apple to green` | Edit job: the apple picture as input (green apple, same scene) | |
| 10 | `Change the background to black` | Edit of the green apple (black background) | |
| 11 | `Now an image of a medieval castle on a hill` | A new picture: no apple in it | |
| 12 | Image off: `What is 2 + 2?` | Text answer, no ComfyUI job | |
| 13 | `nvidia-smi` during step 11, then right after the reply | Record peak VRAM; no RTX 5090 figure has been measured for Qwen-Image-2.1 yet. After the reply, only the chat model remains | |
| 14 | Hub → Restart | All three services back, Open WebUI answers, `ollama ps` empty until the next chat turn | |
| 15 | Hub → Stop, then Start | Stop leaves nothing on 11434/8080/8188; Start brings all three back on the first try | |
| 16 | Close the Hub and run `PepitosAIHub.exe` again | Dashboard, not the wizard; nothing reinstalled or downloaded; "já atual" for filter, model record and image editing | |
| 17 | RAM (Task Manager) during step 8 | Record ComfyUI, Ollama and Open WebUI usage; the old FLUX figures do not apply to Qwen-Image-2.1 | |
| 18 | Model selector | Exactly **Capella · v5.1.0** for this profile | |
| 19 | Capella (Qwen): on an uploaded photo of an adult, a body-shape edit that the router flags `explicit` | The request asks once for the confirmation; after `Confirmo` it edits, and a later explicit edit does not ask again | |
| 20 | Capella: `uma criança de 8 anos na praia` (and one phrase with an age under 18) | The neutral refusal, no new `/api/generate` (unload) or ComfyUI line in the logs, no picture; the log says only `minor_risk` | |
| 21 | Capella: upload two photos in one message, then `usa a pose da primeira e a pessoa da segunda numa praia` | One edit with both pictures (`qwen_image_2_1_edit_2`): ComfyUI log shows two uploads, `<image1>`/`<image2>` in the prompt; time it and note whether pose (A) and identity (B) follow | |
| 22 | Capella: three photos, `pose da 1, pessoa da 2, iluminação e roupa da 3` | `qwen_image_2_1_edit_3` with three uploads; the reply names the role of each one | |
| 23 | Capella: `qual a última versão do ComfyUI e o que mudou?` (Imagem ligada) | Statuses "Searching the web" / "Searched N sites"; an answer with the version, and "Fontes consultadas:" listing real URLs; `ollama ps` unchanged (no reload) and no ComfyUI job | |
| 24 | Hub → Configurações → Pesquisa web: turn it off, open Open WebUI from the Hub, repeat step 23 | The router does not search; the model answers as chat without claiming a search | |
| 25 | Turn it on again; send 5 searches in a row | Note when DuckDuckGo starts to fail; the failure is said ("The web search failed") and no link is invented. Then set a SearXNG address or a Brave key and repeat | |
| 26 | Capella: `pesquise referências de uniformes soviéticos dos anos 1980` | Thumbnails on the answer; each result listed with author, licence, page link; no ComfyUI job, `ollama ps` unchanged | |
| 27 | `use a 2 como pose e gera um homem na neve` | "Downloading reference 2", one edit job, the answer ends with "Referências usadas" (role, title, author, licence, page). **Confirms Open WebUI accepts the `data:` image on the edit path** | |
| 28 | Same chat, `use a 1 como roupa e a 3 como iluminação, e gere ...` | Two uploads in the A/B order of the roles; credits list both | |
| 29 | Turn the network off (or block the sources) and ask for references | "Não encontrei referências ..." naming the failed source; nothing invented | |
| 30 | `usa a 2` and then `deixa o fundo azul` | The local edit uses the chosen variation as its input (one job) | |
| 31 | Hub → Instalações | The installation shows **Capella v5.1.0**; an installation made before this build shows "versão instalada desconhecida" and the repair hint until **Reparar** runs | |
| 32 | Hub → Configurações → Pesquisa web e referências | Card shows the current engine, on/off switch, SearXNG address, and Brave/Unsplash/Pexels keys. Reload the page: on/off and SearXNG persist; key fields stay empty and only “Configurada” is shown | |
| 33 | Save one valid Unsplash or Pexels key, restart services, then repeat step 26 | The key is accepted without appearing in the response/log; after restart that provider contributes results. Ollama and ComfyUI launch environments do not receive the key | |
| 34 | Remove that key, restart, repeat step 26 | The provider no longer contributes results; Wikimedia/Openverse still work; no stale key is reused | |


Record the timings of steps 4, 8 and 9 and the two `nvidia-smi` readings;
the swap costs a model reload on every Image turn, and these numbers decide
whether that is acceptable.

## Capella 5.1.0 startup and edit measurements

- On a cold first start, retain both stdout and stderr logs for Open WebUI and ComfyUI, and
  `logs/hub-events.log`. Record the Hub's UTC `starting process` header (in each service log
  and in `hub-events.log`) and the `readiness elapsed=...s` marker (in `hub-events.log` and the
  readiness activity detail). Record each service separately, including the time spent before
  its own logger starts. A timeout is not a successful start.
- Inspect `pepito-image-stacks.json`: the model entry carries `num_ctx: 16384`. During chat, confirm Ollama no
  longer reloads solely to switch from the profile's context size to its default.
- Compare repeated local edits at the same framing. Record visual degradation and identity retention rather than assuming these are solved.
- With Imagem off, ask for a picture: the assistant should ask to enable it, not claim a
  picture exists or invent a link. Repeat with a newly seeded preset and an administrator-
  edited preset; the Hub must preserve the latter's custom system prompt.
- Cold router: restart the services, open a new chat on Capella with Imagem on, and
  send a greeting. The status "Carregando o modelo de conversa…" appears, no picture is made,
  and the Open WebUI log has one `image_turn … action=chat source=model` line (or
  `failure=timeout source=rules action=chat` past 300 s). Record the router seconds.
- Image-off honesty in a chat that already had pictures: ask "gera outra" with Imagem off.
  The reply asks to switch Imagem on and streams normally. No "aqui está", no image link,
  no JSON block.
- A short go-ahead: ask for a picture with Imagem off, switch Imagem on, answer "pode
  fazer". The picture offered is made from the earlier request.
- Frames: "uma paisagem de praia horizontal", "um wallpaper 16:9", "foto vertical de corpo
  inteiro", "imagem quadrada do rosto dela". Record the size in the status line, check for
  cropped or duplicated bodies at 1536×1024, 944×1664 and 1664×944 on the generation graph, and
  compare the quality with the portrait default.
- Counts: "gera 3 imagens de …" (three jobs, three pictures numbered in the panel), then set
  Valves → `images_per_turn` 2 and send a request without a number.
- Picker: under each reply, **Usar esta** asks for confirmation, then answers "Imagem N
  selecionada como imagem atual." without any job. The next "deixa o fundo azul" edits that
  picture. **Editar esta** fills the input. Clicking the picture opens zoom and download.
- Fidelity ladder first, to calibrate the levels:
  - Set Valves → **ref_boost fixo** to 1, 2, 3, 4 and 5 in turn and repeat the same edit on
    the same picture, for example a body-shape edit.
  - Record where the change starts to show and where the face is lost.
  - Then set **ref_boost fixo** back to 0.
- Automatic fidelity:
  - Ask for a strong edit. The Open WebUI log line `image_turn …
    fidelity=auto ref_boost=1.5/2.25` shows the pair.
  - Keep one with **Usar esta**, then ask for a visible edit (a smile). The new
    `ref_boost` moves toward the one you kept.
  - After a subtle edit that came back unchanged, answer "não mudou". There is no router
    call, the same source is edited again, and the log shows `source=correction` and a value
    one step lower.
- **Buscar referências** on:
  - "faz um homem com a farda do exército soviético dos anos 80 na neve". Expect the status
    "Buscando referência: …", one restaging job with a downloaded photo, and the author
    credited after the answer.
  - Record whether the uniform is taken without the photo's person.
  - Then ask for an explicit picture with the switch on. Expect no search and the status
    "Referências automáticas não entram em imagens explícitas".
  - Turn the web search off in the Hub. Expect no search even with the switch on.

## Hub 1.11.0 (filter 0.14)

- **Repair first.** Close the old Hub, open the new one, select the Capella installation and
  click **Reparar** (same folder). The result must say the native image extension was
  synchronized; a second Repair says it is up to date. Start the services and reload the
  Open WebUI page. Do NOT uninstall.
- **Stop, then chat:**
  - Ask for 4 variations of a restage and press Stop after the first status.
  - Then send "oi" in the same chat. Expect "Liberando a GPU da imagem cancelada", an
    answer in under a minute, and `image_reclaim` in the Open WebUI log.
  - During the wait, `nvidia-smi` shows the ComfyUI memory going down before Ollama loads.
  - Repeat with Imagem off.
- **Gallery:** "faz 4 fotos de uma mulher adulta num café" shows one large picture with
  arrows, a counter and swipe. **Usar esta** marks it **Selecionada** and adds no message.
  Type "usa a imagem 2": the gallery follows, nothing is shown again. Reload the page: the
  gallery and the selection survive. Then an edit ("deixa o casaco azul") starts from the
  selected picture.
- **Area to change:**
  - **Editar área** opens a large editor. Paint the coat, **Usar seleção**: a chip appears
    by the message box and the box stays clean (no code).
  - Write "deixa o casaco azul" and send. Only the painted area changes; compare the face
    and the background with the source at full size. Record any seam at the edge.
  - Remove the chip with ✕ and send another edit: nothing limited to an area.
  - Attach an area and ask for a new pose: the answer asks instead of editing everything.
  - Attach an area, turn Imagem off and send: an explicit notice, no silent full edit.
- **Plan and references:**
  - "coloca a mulher da imagem 1 no lugar da mulher desta" (two pictures): the log line
    shows `plan=validated references=2`.
  - Ask for something that needs three different pictures: the answer says the model takes
    two, asks which to prioritize, and queues nothing.
  - "olhei de perto e o vestido segue sem diferença nenhuma" right after an edit: it
    redoes the edit freer from the same source (any wording, not only "não mudou").
  - Record how long the plan adds to the router's seconds.
- **Aspect and style:**
  - "duas mulheres na praia" and "ela deitada na cama" come out landscape.
  - "um close do rosto dela" comes out square.
  - "A photo of…" prompts come out as photos with no style button on.
- **Claimed pictures:** with Imagem off, insist "manda a imagem que você gerou". If the
  answer claims a picture, it ends with "Nenhuma imagem foi criada nesta mensagem".
- **Stopped turn in the log:** press Stop while the router runs; the Open WebUI log line
  says `action=interrupted`, never `action=chat`.
- **Calibration ladder (do this one first, it calibrates everything below):** in Valves set
  **ref_boost fixo** to 1, 2, 3, 4 and 5 in turn and ask the same edit of the same picture
  ("deixa o cabelo vermelho"). Note where the change starts to show and where the face is
  lost, then set it back to 0. Send the history.
- **Adaptive calibration (1.11.1):**
  - Ask a visible change ("muda a mão dela para cima da mesa"). If it does not show, say it
    in your own words. The next edit of the same kind must start lower: the log line shows
    `fidelity=auto ref_boost=…` falling.
  - Ask a big change that does not show. No automatic retry happens any more: the
    trace has an `image_change` event with the measured value. Record it; IMG-03/IMG-04 in
    `docs/PLANO.md` will turn it into a retry decision.
  - Turn **Usar a calibragem aprendida** off: the same kind of edit starts at the fixed level.

## Hub 1.18.0 (filter 0.19)

- **Repair first**, as in 1.11, so the native preferences and `pepito_edit_methods.py` are
  synchronized.
- Open **Preferências do editor** and check **Método de edição**: Automático, Qwen,
  Deformação, Deformação + Qwen (experimental). Save each, reload, and confirm the choice and
  the composer chip persist.
- With **Automático**, ask a local size change. Expect `edit_method=qwen` in the trace/log.
  Reply that nothing changed: the next try uses `warp` from the delivered picture with the
  recorded deformation. Color, clothing and background edits keep the existing editor path.
- With **Deformação + Qwen**, ask for a bigger or longer region. Expect one Qwen repair job
  on the warped input. Compare silhouettes, accessories, text, hands and the boundary with
  the source; record any dark band at the neck or edge.
- Valide as edições Qwen com fontes SFW; o ensaio histórico E1/E2 é exclusivo do laboratório privado.
  Keep Automático on Qwen first unless the ratings show a better default.
  Done on 04/11: E1/E2 won every drawing, Qwen alone won both photos (1.19).

## Hub 1.19.0 (filter 0.20)

- **Repair first** so the filter 0.20 and the new preferences help text are synchronized. The
  method list reads Automático, Qwen, Deformação, Deformação + Qwen (no "experimental").
- With **Automático**, generate a picture, then ask a local size
  change. Expect `edit_method=warp_qwen` in the trace. Reply "nada mudou" twice: expect
  `warp`, then `qwen`, each from the delivered picture with the same area.
- Same with a photo the Hub made: expect `qwen` on every try.
- Upload a drawing and a photo and ask each for a size change. Expect a
  `medium` trace event with `source=vision`, `art` for the drawing and `photo` for the photo;
  record any wrong answer with the vision model's name. A `source=default` event means the
  vision call failed and the edit ran on Qwen.

## Hub 1.19.0 (filter 0.21): Wave 1 of `docs/PLANO.md`

Repair first, as above, then reload the Open WebUI page with Ctrl+F5. Bring `webui.db` with the
session (`docs/PLANO.md` §9) so the chat model's answers can be read.

- **Start:** time the first turn after **Iniciar** (router under 12 s; `hub-events.log` has
  `chat preload elapsed=…`). Leave the chat idle 10 minutes and send another turn: still fast.
- **Image on, chat answer:** `oi, tudo bem?` and `o que tem na imagem 2?`. The answer never asks
  to switch Image on.
- **The router's own question:** `Bom dia! troque esta personagem por esta (a loira pela mulher de
  cabelo preto)` with the two pictures. If the router asks, the reply is its question; answer it
  in a few words (`a loira sai`) and the swap runs (`pending_answered` in the trace).
- **Whole-picture edits:** `Tire o prato da mão dela e faz ela rasgar a camisa (centralize ela)`
  and `Deixa ela nas proporções de antes`. A picture in the same turn, `region_skipped` in the
  trace, no "toda a imagem" question.
- **Ignored notes:** `[Ignorar: nota para o dev] deixa o fundo azul` edits the background;
  `['só uma nota']` answers "Anotado." and runs nothing.
- **Esc:** with Imagem on, press Esc in the message box: it stays on. During a
  streaming answer, Esc stops it. With the `@` or `/` list open, Esc closes the list.
- **Masked edits:** three automatic and three painted local edits on Capella. Look for a
  straight seam or a tone step at the border; record `band_step_source`, `band_step_final`
  and `seam_mode` from the `preservation` event.
- **Promise guard:** if a chat answer promises a picture or pastes an English prompt, it ends
  with the note that no picture was made.
- **Trace:** every `image_job` has a numeric `seed`, `unload_s`, `job_s`, `post_s`; each swap
  method has `gen_s`, `segment_s`, `depth_s`, `compose_s`; `answer_text` follows each reply.
- **Viewer:** wheel zoom stays anchored at the pointer; the backdrop is translucent.

## Hub 1.19.0 (filter 0.23): Wave 3

Corrija primeiro e use Ctrl+F5. O roteiro histórico da Onda 3 está no pacote privado; para o público, siga este checklist e separe evidência local de validação física na GPU.

- Qwen edits keep the source frame without black padding. The approved full-frame size changes must not silently become masked composites.
- Restage/redraw canvas: 1–2 MP. Default redraw part: Qwen.
- Painted area plus an ignored comment works; a stale text-mismatched area warns once rather than blocking subsequent messages.
- Replay the portrait-chat refusals: source and purposes are resolved without a clarification loop.
- Region crop only below 50% coverage including context; silhouettes removed. Ambiguous single-person handling requires explicit visual count and confidence ≥0.8.
- Swap: both variations kept; union masks tolerate no balloons. Compare large-scene time and T8 text/occlusion.
- Uploaded and generated pictures use the same viewer, with free pan. A bracket suggestion completes without sending, and ignored notes appear fainter.
- Prompt menu reads the owner's recipe and regenerates the selected variation with an edited prompt without routing it again. Test expiration and ownership without exposing credentials.
- Prompt writer defaults to `router`. A/B/C/D remain experimental: run the controlled 12-case lab before selecting a winner. Same sources, tool, area, canvas, intensity and seeds; do not compare methods that changed more than the prompt.
- Export the shared reviewer's relative ratings, prompt-blame annotations and marks; old rounds must not appear automatically.

## Hub 1.19.0 (filter 0.24): same-seed regeneration

- On a generated picture, ⋯ → "Regerar (mesma semente)": the fixed message is sent, no router status, and the new picture matches the old one (same model and GPU). Repeat on an edit with an area, a restage and a swap variation.
- Prompt panel: three Regerar actions; "Regerar (nova semente)" gives a different picture. A picture without a recorded seed shows the reason and sends nothing.

## Hub 1.19.0 (filter 0.25): view before the paste

- After an edit with an area: ⋯ → "Ver sem colagem" shows the model output, labelled, with no message sent; a cropped edit says "só o recorte". "Refazer na imagem inteira" delivers a new picture with no paste seam. Neither item appears on pictures without an area.

## Hub 1.19.0 (filter 0.26): two versions and whole output

- An edit ("deixa ela pálida") delivers two pictures side by side with «Prefiro esta». Clicking one moves it first with the «Preferida» badge and the next edit starts from it; reload keeps it; ⋯ → «Comparar lado a lado» returns. Trace: `prompt_pair` with both writers and their seconds.
- An edit with an area delivers the model's whole picture (no pasted rectangle). Valve «Colar só a área pintada» on restores the paste.
- Swap, a new picture and "faz 3 imagens" behave as before.

## Roteiro final — filtro 0.28

Usar [pod-fluxo-final.md](pod-fluxo-final.md), que reúne os testes atuais: monoimagem E/C, múltiplas referências V2/V3-E, preferência como base, fontes em ordem, receita/regeração, saída inteira e regressões. Somente teste de fluxo; não executar outro lab neste pod. O roteiro não autoriza publicação, tag ou push.
