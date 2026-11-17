# Image chat: Open WebUI 0.11.3 → ComfyUI

How a picture is made when someone turns on **Image** in the Open WebUI
composer, why the first attempt at this never reached ComfyUI, and how the
Hub keeps the fix installed.

## The contract in the pinned Open WebUI

With the Image feature on, `utils/middleware.py:process_chat_payload` has two
routes, chosen by the request's `metadata.params.function_calling`:

- **native** (the UI's default): the builtin `generate_image(prompt)` tool is
  offered to the model together with every other builtin tool it may use. A
  picture is made only if the model answers with a structurally correct tool
  call; the executor then calls `tools/builtin.py:generate_image`, which calls
  `image_generations()` → ComfyUI `POST /prompt`.
- **legacy**: `chat_image_generation_handler` runs *before* the model. Open
  WebUI writes an image prompt from the user's message (its own task-model
  prompt, falling back to the message verbatim), calls `image_generations()`,
  attaches the files to the message, and only then asks the model to
  acknowledge with a system context.

Filter Functions run their `inlet` hook before that choice is made and receive
the same metadata object the choice reads.

## What actually happened on the Pepe root

The persisted function matched the repository byte for byte; nothing was
stale. Instrumenting the installed filter and driving the real browser UI
(Image on, "Generate an image of a red apple on a plain white background.")
showed the native route with Hermes3 8B under the UI's default sampling:

```
request   function_calling=native  features.image_generation=true  tools: 12 builtins
stream    tool_calls=[generate_image({"background_color": "white",
                                      "apple_color": "red", "include_shadow": false})]
tool      generate_image(prompt="")           -> "Image generation failed"
```

The model invented the tool's argument names. Other recorded turns were a
serialized call to a non-existent `create_image`, or silence. In every case
`POST /prompt` was never sent; `/queue` and `/history` stayed empty. The
earlier smoke had passed because it drove `/api/chat/completions` with
`temperature: 0` and hand-picked prompts, for which the model happened to emit
the exact schema -- the composer's real request shape was never modelled.

## The fix

`assets/open-webui/pepito_image_chat.py` (a bundled resource, uploaded
verbatim as the Function `pepito_image_chat`) now routes every UI Image turn through Open WebUI's own
pipeline: at `inlet` it sets `metadata.params.function_calling = "legacy"` for
that one request when `features.image_generation` is on and the request has a
UI session. The picture then does not depend on the model at all. The model's
acknowledgement is kept free of markdown images and links (it had started to
invent `https://www.example.com/red_apple.jpg`), and an empty acknowledgement
becomes `Image generated.` A failure relayed by Open WebUI stays as text.
Turns without the Image feature, and API calls without a session, are untouched.

Consequence to know: without the router (below), while Image is on *every*
message produces an image (that is what the legacy route does). Turn Image off
for ordinary chat.

## The router (Capella, filter 0.7.0)

Profiles with `runtime.image_router: true` get `PEPITO_IMAGE_ROUTER=true` and
`PEPITO_OLLAMA_URL` in Open WebUI's environment. Each Image turn then makes one
`POST /api/chat` to the conversation's own Ollama model (a workspace preset
routes on its `base_model_id`). The call sets `format` to a JSON Schema and
returns exactly `{"action": "chat" | "new" | "edit", "prompt": string}`. The
prompt is in English and literal; for `edit` it is an instruction ("Change X…
Keep … unchanged"). The router sees the last six messages and whether the chat
already holds a picture.
For saved chats, the filter reads image files from persisted history. For
temporary, local and channel chats, it also reads `data:image/...;base64,...`
parts from the current request: Open WebUI removes `files` metadata before the
filter runs, but the uploaded pixels remain in those parts. Without this
fallback, a routed edit can mistake an attached base for no image and generate
an unrelated picture. Regression coverage:
`test_router_edits_inline_uploaded_image_in_a_temporary_chat`.


Call parameters:
- `think: false`.
- `temperature 0.2`, `num_predict 320`, `maxLength 1200` on the prompt.
- No `keep_alive`.
- The chat model's own `num_ctx`, when it sets one. A different value makes
  Ollama reload the resident model.

What the filter does with the answer:
- **chat**: the turn is left as if Image were off: the user's `function_calling`
  mode, image parts kept, and streaming untouched. The feature flag is off, so
  no `generate_image` tool is offered, and the model is told that no image was
  created or edited.
- **new**: the filter generates through `image_generations` with the router's
  prompt, which is not refined again.
- **edit**: the filter edits through `image_edits` with the router's
  instruction and the chat's newest picture only. Open WebUI's handler would
  pass the user's raw words, which it reads from the saved chat, so the filter
  takes the edit over. If editing is disabled by the admin, it generates
  instead, as the handler does.

Checks and fallback:
- `edit` with no picture in the chat becomes `new`, and the model is told the
  picture is new.
- An empty prompt becomes the user's words.
- An invalid answer, a value outside the schema, a timeout (45 s) or an
  unreachable Ollama makes `edit_intent` decide, with the user's words as the
  prompt. The word rules never answer `chat`, so every turn is still answered.

VRAM order with `exclusive_image_vram`: the router runs first, on the model left
resident by the last reply. Ollama is unloaded only for `new`/`edit`, then
ComfyUI runs, then `/free`, then the chat answers. A `chat` turn unloads
nothing. A status `Image prompt: …` shows the prompt that was sent, as
ChatGPT's `revised_prompt` does.

Measured on 2026-10-12, RTX 5070, `qwen3.5:9b`, 30-case PT/EN corpus run twice:

| Router | Score |
| --- | --- |
| Probe, examples as earlier turns | 93% (56/60), no invalid JSON |
| Shipped filter's `route()` + `decide()` | 100% (60/60), no invalid JSON, median latency 0.3 s chat / 0.6 s edit / 0.9 s new |
| Word rules 0.6.2 | 50% (15/30); they never answer chat |

Few-shot examples given as earlier turns leaked into real prompts: a dog from
an example showed up in a castle edit. They now sit in the system prompt,
labelled as unrelated, and the leak is gone. A smaller leak remains: in one of
the two runs a new "black cat" or "red car" still inherited the previous
picture's beach.

The Capella 27B on the 5090 is not measured yet.

Tests:
- `tests/test_open_webui_image_filter.py`: payload, schema, checks, fallback,
  chat turn.
- `tests/test_image_edit_dispatch.py`: turns end to end with a fake Ollama and
  ComfyUI, covering the typo edit, the chat turn, the VRAM order and every
  failure shape.

## Workflow por modelo (filter 0.8.0)

The Hub writes `<open-webui data>/pepito-image-stacks.json` (path passed to
Open WebUI as `PEPITO_IMAGE_STACKS`). It maps each Open WebUI model id -- the
chat model's Ollama tag -- to its
generate and edit workflow, node mapping, model, size and steps. For the
duration of its own call the filter overlays the selected model's entry on
Open WebUI's image config (a ContextVar around
`open_webui.routers.images.get_image_config`), so Capella's chat model renders
with `qwen_image_2_1_t2i` / `qwen_image_2_1_edit`. Open WebUI's stored
text-to-image setting (seeded from env, FLUX on existing installs) no longer
decides routed turns.

Both Capella records carry
`meta.defaultFeatureIds: ["image_generation"]`. New chats therefore open with
**Imagem** already selected; Pepe and Pepinho records are unchanged.

### Local Qwen smoke, 2026-10-13

ComfyUI v0.39.0 at the pinned commit and comfyui-krea2edit v1.2.5 at its
pinned commit were started from an isolated checkout on an RTX 5070 12 GB with
torch 2.11.0+cu130. The node pack imported as
`[krea2edit] nodes v1.2.5 loaded`.

The shipped Qwen generation workflow produced a 512×512 red cube in 7.6 s at
four steps. Its PNG was uploaded through `/upload/image`; the shipped edit
workflow changed the cube to blue in 12.5 s and preserved its shape, position
and white background. Both `/prompt` submissions had no node errors and ended
with `status_str: success`. Four steps and 512×512 were used only to prove the
graphs and model files; Capella keeps its declared 25 steps at 1024×1024.

Only the Qwen graphs were run here. They still require the RTX 5090 checklist below before release validation.

## Age safeguard and reference consent (filter 0.9.0)

A local check in front of every picture the filter would make or edit, on every
model and preset (no preset opts out since filter 0.10.0). It reads **words only**: no
image, face or estimated age is analysed, and nothing leaves the machine.

- **Word rules** (Portuguese and English, always on): a minor word (child, criança, loli,
  underage, preteen, "menor de idade", an age under 18 next to a person or an explicit request)
  blocks the request by itself; a word that only suggests one (teen, schoolgirl, colegial,
  novinha) blocks next to an explicit request; neutral words (petite, jovem, woman, magra) block
  nothing. A minor word in the user's last two earlier messages counts too ("a 14 year old
  girl" then an explicit request).
- **Router signal**: the router's schema gained `minor_risk` and `explicit`. Either signal
  blocks; with the router down the word rules still do.
- **A blocked turn** unloads nothing, calls nothing (no ComfyUI, no `/free`), is not sent to
  the chat model (so the request is not repeated back), shows a short neutral reply, and logs
  only `minor_risk`, never the text.
- **Reference consent**: an edit that is explicit (`explicit` from the router, or
  the words) needs one confirmation per Open WebUI user: "Confirmo que pessoas reais nas
  referências são adultas e consentiram com esse uso." The filter asks, the user answers
  "Confirmo", the saved request runs, and it is kept in `pepito-reference-consent.json` in
  the Open WebUI data directory. It is never asked again for that user. Delete the entry to
  ask again.
- **Not covered**: chat turns (no picture is made); the model calling Open WebUI's own image
  tool, which the Hub keeps off on Capella; the Image button being off.

Tests: `tests/test_age_safeguard.py` (rules, consent storage) and the last tests of
`tests/test_image_edit_dispatch.py` (the turns, read off a timeline of calls).

## References with roles A, B, C (filter 0.9.0)

An edit may use several pictures of the chat. The router's schema gained `references`
(`[{"image": N, "role": "A"|"B"|"C"}]`, `N` counting the chat's latest pictures from 1, oldest
first, as the `IMAGES:` line of its prompt lists them):

- **A** the pose, composition and scene to follow (the picture the edit starts from);
- **B** the character to keep (face, hair, body, identity);
- **C** the reference for lighting, clothing and style, and nothing else.

The pictures are ordered by role (A first), each goes to its own `LoadImage` node of the
workflow that takes that many, and the instruction is prefixed with what each one is for, in
the words its encoder reads (`<image1>`, `<image2>` for Qwen-Image; "The first image ..." for
Krea2Edit). Without references from the router, the pictures of the latest message are A, B, C
in the order sent; with one picture the edit is the single-image one, unchanged. A new picture
made from references is an edit (it needs the pictures as input).

- Qwen (Capella): `image_edit`, `image_edit_2`, `image_edit_3` (the encoder takes up to ten).
- Krea (where a profile declares it): `image_edit`, `image_edit_2` (Krea2Edit takes two: scene, then person).
- **Nothing is dropped silently.** When the model's workflows take fewer pictures than the turn
  chose, the highest-priority ones (A, then B, then C) are used and the model is told to say which
  role was left out and why.
- The router is told never to name a living artist or ask to copy an artist's style, but to
  describe the visual traits. That is a prompt rule, not a filter.

Tests: the references tests at the end of `tests/test_image_edit_dispatch.py` (order, node,
role text, a one-image model, an older picture as reference, the consent) and the reference graph
tests in `tests/test_workflows.py`.

## Capella (public profile, filter 0.10.0)

Capella generates and edits with Qwen-Image 2.1 only (`qwen_image_2_1_t2i` / `qwen_image_2_1_edit`). The public profile has no style presets, no extra variants and no second model family.

- **Character swap** (`character_swap`) uses Qwen only: two variations, "Qwen inteira" (`reference`) and "Qwen com marcação" (`marked`, composed from `segment` and `depth`). The filter accepts a swap without the `scene` step.
- **Safeguards** (age, reference consent, `explicit`) apply as on every model; see the sections above.
- Material of the private package is not described in this public document.

## How the Hub keeps it installed

`open_webui_filters.sync_image_chat_filter(data_dir, base_url)` runs whenever
Open WebUI is up and the profile enables image generation: after the first
run starts it, on every `start`, and on the "Abrir Open WebUI" action. It
signs a two-minute token for the instance's first administrator with the
instance's own secret key (the same signature Open WebUI uses for sessions),
then through the Functions API: creates the function if absent, updates it if
the shipped source differs, enables it if disabled, and reads it back. Same
source → nothing written. A function under our id whose description was not
written by the Hub is refused and left alone; every other function is never
touched. Before the first administrator exists the sync reports `no-admin`;
while the Hub is open it then looks for that account every few seconds and
syncs as soon as it exists, and otherwise the next start does. (A new
installation's first start always comes before its first account: without
the catch-up, the whole first session ran with no filter, no chat model
record and image editing off.) An unreachable or refusing instance is a warning
on the operation, not a failure. A change counts as work for that run; an
unchanged filter keeps a second run a no-op.

## Evidence — Pepe root, 2026-09-13

Real browser, real Open WebUI, real ComfyUI (pid 17052, port 8188), no model
re-downloads, no reinstall:

| Turn (Image on) | ComfyUI job | Prompt node 76 | Output |
| --- | --- | --- | --- |
| red apple / white background | `38ed6854` success | "…single, ripe red apple resting on a clean, pure white surface…" | `Flux2-Klein_00018_.png`, red apple, attached and shown |
| green apple / black background | `a65a265d` success | "…solitary green apple… stark black background…" | `Flux2-Klein_00019_.png`, green apple, attached and shown |
| `hi` (Image still on) | `7eb4f94b` success | from the conversation | one more image, as the route defines |
| `hi` (Image off) | none | — | "Hello! How can I assist you today?" |

Workflow `flux2_klein_t2i`, `flux-2-klein-4b-fp8.safetensors`, 512×512,
50 steps. The Hub's sync reported `updated` once and `unchanged` afterwards;
the real Pepe second run ended `did_nothing=True` with
"Open WebUI: filtro de imagem já atual".

Observed but out of this fix's scope: one generation returned noise when the
LLM was evicted from VRAM in the middle of sampling (ComfyUI's dynamic VRAM
loading moved weights mid-run; the sampling rate jumped from ~1 s/it to
~3 it/s at step 29). That is the VRAM-scheduling work tracked separately.

Focused regression tests:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_image_dispatch.py tests/test_open_webui_image_filter.py tests/test_open_webui_filters.py tests/test_open_webui_integration.py
```

`tests/test_image_dispatch.py` models the pinned request path line by line and
asserts `POST /prompt` with the Flux2 workflow and the user's words for each
answer shape captured above; the same turns post nothing with the 727482a
filter, and that is asserted too.

## Router reliability and honesty (filter 0.11)

The router checks Ollama `/api/ps` with a three-second timeout before asking for
a decision. A resident model's `context_length` takes precedence over the stack's
`num_ctx` and the chat model's configured parameters. If the resident model omits
its context length, the router does not substitute a conflicting configured value.
Sampling options do not change the model's load settings or keep-alive.

Warm routing has a 60-second timeout. A nonresident model has 300 seconds and a
Portuguese loading status. An unavailable residency endpoint uses the cold-load
allowance. Timeout, connection failure and invalid answers are distinguished.
Failure does not imply a picture request: greetings and ordinary conversation
stay chat, while clear picture and edit requests use the word rules. A short
confirmation following an assistant's picture offer can recover the preceding
request. Styled-stack fallbacks skip Open WebUI's second prompt-refinement call.

Each Image-on UI turn emits one INFO decision line containing action, decision
source, failure category, elapsed router time, the stack part, pose presence,
reference count, style, aspect, picture count, the minor-risk boolean and whether
consent is required. Image-off turns on stack models also emit one line. These
records contain no conversation text, prompts, image addresses or user/chat
identifiers.

For a UI turn on a stack model with Image off, the filter explicitly tells the
chat model that no image tool can run and that it cannot make pictures itself.
It tells the model to mention the composer switch only when a picture is
requested. Image-off replies and routed chat replies lose invented markdown or
HTML images while they stream; ordinary chat links remain. The reply still
streams live: only text that may become image markup (`![`, `<img`) is held back
until it closes (dropped) or proves to be plain text (released). The outlet cannot
do this afterwards, because Open WebUI calls it with fresh metadata. The context
forbids fabricated picture links, code, JSON and promises; this is a model
instruction, not a guarantee that every prose claim is detected.

Generated-picture addresses are recorded per chat beside the derived-image
record in `DATA_DIR/pepito-characters`. An edit's outputs join that record only
when every one of its inputs was already there, so an upload's edits stay the
user's picture. An explicit edit skips reference consent only when every final
input is in that record. Uploads, their edits, web references, mixed sources and
missing provenance still require the existing consent. The age safeguard remains
independent and runs before edits.

Regression coverage: `tests/test_image_router_reliability.py`,
`tests/test_open_webui_image_filter.py` and the router/consent cases in
`tests/test_image_edit_dispatch.py`. These changes still need a real GPU check:
confirm no context-switch reload in Ollama's logs, measure a cold model load,
and exercise output honesty with the deployed chat model. Unit tests do not
measure disk latency, GPU residency or model compliance with the context.

## Style modes, sizes, counts and picking (filter 0.11)

**Style, three modes.** A profile that declares two composer switches offers:
**Fotorrealista** (`pepito_image_photo`) and **Arte digital** (`pepito_image_art`).

- One switch on: that medium.
- Neither or both: *sem preferência*. The turn tells the router `STYLE: auto` and the
  router answers `style`.

Inside *sem preferência*, the medium is chosen in this order:

1. The user's words. New pictures also count the generic words "foto" and "arte".
   Edits count only words that name a medium ("realista", "anime", "desenho"), because
   "muda o cabelo na foto" names the picture.
2. For an edit, the recorded style of its source picture.
3. The router's `style`.
4. Photo.

When the router fails on a styled stack, a new picture's prompt is prefixed with the
medium ("A photo of" / "A digital illustration of") instead of going through Open
WebUI's prompt refinement, which never saw the style.

**Size.** The router answers `aspect`: portrait 2:3, landscape 3:2, square, tall 9:16 or
wide 16:9.

- An explicit word wins over the router's `aspect`: "horizontal", "vertical",
  "quadrada", "16:9", "wallpaper", "stories".
- The filter keeps the stack part's pixel area, rounds both sides to multiples of 16, and
  passes the result as `CreateImageForm(size=…)`. Open WebUI's ComfyUI path then sets the
  workflow's width and height. Example: 1024×1536 becomes 1536×1024 for landscape and
  944×1664 for tall.
- Without an answer from the router or a frame word, the stack's own size stays.
- Edits keep the source's frame. A named position keeps its adapter's trained 1024²
  canvas.
- The status line shows the size.

**Count.** The number of pictures is decided by the first of these that applies:

1. A count in the words: a digit, or two to four ("gera 3", "duas versões", "two
   variations"). "uma imagem" is the article, not a count.
2. The router's `count`.
3. The user valve `images_per_turn` (0 = automatic, 1–4), in the filter's Valves in
   Open WebUI's user settings.
4. The old defaults: new picture 1, restaging and positions 2, local edit 1.

Edits and new pictures run one job per picture, each with its own seed. A quality
switch, when a profile declares it, doubles only the default. Its graph makes pairs, so an odd count is met by
dropping the extra picture.

**What the chat knows about its pictures.** Every picture the filter attaches is
recorded per chat in `DATA_DIR/pepito-characters/<hash>.pictures.json`. A record holds
addresses and text only:

- a chat-wide number;
- the English instruction, truncated;
- style, kind (new / edit / restage / remove / pose) and aspect;
- which reply it belongs to, and its variation.

The router's IMAGES list uses those numbers. It labels each picture's owner and its
variation ("variation 2 of 2 of your last reply"), marks `[CURRENT]` and adds the
description. The candidate window is still the first picture plus the latest ones, but
the numbers no longer depend on it. Chat turns and picture turns give the chat model the
same numbered list, so it can talk about "a imagem 3" without claiming to see pixels.

**Picking.** Under each reply with pictures, the filter emits an Open WebUI `embeds`
panel. The panel is self-contained: escaped text, no external resource. It has one row
per picture of that reply:

- **Usar esta** sends "usa a imagem N". Open WebUI asks for confirmation, because the
  prompt comes from an embed.
- **Editar esta** fills the input with "usa a imagem N e ".

Clicking the picture itself opens Open WebUI's full-screen preview with zoom and
download.

A message that only picks a picture ("usa a imagem 7", "usa a 2", "escolho a 3", "use
number 4") gets a fixed reply. No router, no unload, no job.

- A bare number after a reply with several variations means that reply's variation.
  "imagem N" is always the chat-wide number.
- The picked picture becomes CURRENT: the default source of the next edit and the anchor
  of a restaging.
- The pick expires when a newer picture appears.
- "usa a 2 e deixa o fundo azul" is an edit of that picture.

**Edits.**

- A one-picture local edit's `ref_boost` (General Edit V2's hold on its source) is set per
  turn through the stack's `strength` binding. Since filter 0.12 it follows the rules in
  "Edit fidelity" below. Every other part keeps its workflow's value.
- Krea edit sources are now scaled to 1.5 MP with `lanczos`, instead of 1 MP with
  `nearest-exact`.

**Not validated on an RTX 5090.** The following are unmeasured:

- the `ref_boost` values used by the fidelity levels;
- whether 1.5 MP lanczos sources reduce the accumulated "dirt" without losing edit
  fidelity;
- non-portrait sizes on the generation graphs.

The behaviour tests (`tests/test_image_chat_features.py`) prove the plumbing, not the
pictures.

## Edit fidelity and automatic references (filter 0.12)

### Edit fidelity

For a one-picture local edit on a stack whose `edit` part has the `strength` binding, the filter picks a `ref_boost`. The first rule that applies decides:

1. **A fixed number.** The user valve `ref_boost_manual` is set to a value from 1 to 5.
2. **A fixed level.** The user valve `fidelidade_edicao` is set to `fiel` (4.0),
   `equilibrado` (2.5) or `livre` (1.5).
3. **A correction.** The planner reads the message as `no_change` (the edit did not show) or
   `too_much` (it changed too much), in any wording, and the chat's latest picture is still
   that edit's. With no such edit to redo, the filter asks.
   - The same instruction runs again on the same source.
   - The value is one step (1.0) below the last try's for `no_change`, one above for
     `too_much`, within 1.0–5.0.
   - The offset of that kind of change (below) learns the step.
4. **Automatic.**
   - The router's `change` sets the level: `subtle` → 4.0, `moderate` → 2.5,
     `strong` → 1.5.
   - The user's learned adjustment for that level is added (what picks taught, plus what
     corrections of that kind taught), and the result is held within 1.5–4.5.

With two or more variations, the automatic and corrected values alternate 0.75 below and
0.75 above the target. A fixed choice from the valves is never spread.

**Learning.** It is persistent, per user, in `DATA_DIR/pepito-fidelity.json` (a hash of the
user id), and has three sources:

- **A pick.** Keeping one of several variations made at different values moves the general
  bias by half the distance between it and the reply's mean. Fixed choices teach nothing.
- **A correction, per kind of change.** A correction moves the offset of that level
  (`subtle`, `moderate`, `strong`) by 75% of the step, so the next edit of the same kind
  starts near what worked. Offsets of other levels are not touched.
- **A measured nothing.** After a `moderate` or `strong` automatic edit, the filter compares
  the source and each result (mean absolute difference on a 256 px grayscale, 0–255). If every
  result is under 4.0 it makes one more edit one step freer, delivers both, and teaches the
  level as a correction. Never with a fixed value from the valves, never with an area, never
  twice. The measure is logged (`image_change measured=…`); 4.0 is a start, not calibrated on
  an RTX 5090 (on 21/10, local edits measured 3.9–111.8 and big changes that did not show
  5.7–8.8).

Limits and forgetting: each offset and the bias stay within ±1.5, and every offset fades by
5% each time something is learned. The valve `fidelidade_aprendida` (on by default) can
switch the use of everything learned off without erasing it; deleting the file erases it.

**Where to set it.** The valves are the filter's user valves (Chat › Controls › Valves ›
Pepito Image Chat). `fidelidade_edicao` is a plain `str` with an `enum` in its schema, so
Open WebUI shows a list. A `Literal` would not resolve when the function module is
executed, because the file uses postponed annotations.

### Redesenho: "refaça", "mais realista", "harmonizar"

The router mode `redraw` (SPEC-202) is a whole-picture redo, drawn from the **current**
picture: "refaça do zero", another style or medium ("fotorrealista", "anime"), "harmonizar".
A new pose or place stays `restage`; one changed thing stays `local`. The user valve
`redraw_part` picks what runs it, and the router offers `redraw` only when the stack has that
part:

| `redraw_part` | Job |
|---|---|
| `restage_current` | The Identity Edit (part `restage`) with the current picture as the source. |
| `edit_free` | The General Edit at `ref_boost` 1.0, change level `strong`. Needs the `ref_boost` binding. |
| `qwen` (default) | The Qwen edit, on a canvas of at most 2 MP. Needs the Qwen family installed and a model whose own stack edits pictures: Capella. |
| `t2i_described` | The chat model describes the current picture once (before the card is released); a new picture is generated from the request and that description. |

`qwen` is the default since the redraw lab of 12/11 (SPEC-306; user ratings, mean of "changed
what I asked" and "same character": `qwen` 4.4, `edit_free` 3.95, `restage_current` 3.4,
`t2i_described` 3.05). No option reproduces the old behaviour (a local General Edit that keeps
almost everything). The prompt of a redraw has no
"Keep unchanged" list: only the character's identity and what the user asked to keep ("mantendo
esse fundo"). The trace's `image_job.params` carries `edit_mode: "redraw"` and `redraw_part`;
`image_change` is measured for every redraw that edits a picture.

### "Nada mudou": the ladder and the automatic retry (SPEC-203, off by default)

Two user valves, both **off** (today's behaviour). **Escada do "nada mudou"** (`retry_ladder`):
each "nada mudou" climbs one rung from the part the last try used: the same part one
`ref_boost` freer down to 1.0 (`boost`), then the redraw part on the current picture
(`redraw`), then a new picture from a description of the current one (`new`, said with "Fiz
uma imagem nova a partir desta, porque a edição não mudava"). Each rung is a `retry_step`
event in the trace. **Refazer sozinho quando quase nada mudou** (`auto_retry`): a one-picture
edit of level moderate or strong, without an area, whose `image_change` is below 6 (constant
`NO_CHANGE_THRESHOLD`, to be measured on the pod) is redone once, one `ref_boost` freer, with
the status "Quase nada mudou; refazendo com mais liberdade"; both pictures are delivered, the
weak one first. It costs one more job and only covers the `boost` rung.

### "Buscar referências"

The switch is the `pepito_image_refs` toggle. It is offered on every preset with an image
edit workflow when the profile has the image router and the web search (Capella).
The user's web search setting must also be on.

With it on, the router schema gains `ref_query` (at most 80 characters) and `ref_role`
(`C` for clothing, objects or style; `A` for a place or scene to follow). It never gains
B: a web photo is never the person of a picture. For a new picture that names a specific
real thing, the filter:

1. searches the reference providers (Wikimedia Commons, Openverse, and Unsplash and
   Pexels with the user's keys);
2. keeps the results as the chat's last search, so the next turn can say "usa a 3 como
   roupa";
3. downloads the first result through the same safe path as a chosen reference;
4. makes the picture from it.

Role C uses the restaging graph (Identity Edit V2, photo beside a fresh canvas) when the
stack has one; role A edits the photo itself. The prompt starts with that role's sentence
("The image is the reference for lighting, clothing and style: take only those. Take
nothing else from it."). The photo's author and licence are listed after the answer.

Guards:

- **Explicit pictures never use it.** The router is told so. The filter also checks its
  `explicit`, the pose and the explicit words, and skips the search with a status line.
  The photo would be the base of the result and may show a real person.
- **The query never names a person.** The router is told so, and the filter empties any
  query with person, body or adult-content words (`ref_decision`).
- **Nothing usable means no reference.** With no result, or a refused download, the
  picture is made without a reference.
- **The decision log records the outcome.** It gains `fidelity`, `ref_boost` and
  `auto_ref` (off, not_needed, explicit, none, found, download_failed).

**Not validated on an RTX 5090:**

- whether the levels 4.0 / 2.5 / 1.5 and the 0.75 spread match "subtle / visible /
  strong" for General Edit V2;
- whether Identity Edit V2 takes only the clothing from a reference photo, or also the
  person in it;
- the time a reference adds (search plus download).

Behaviour tests: `tests/test_image_fidelity_and_refs.py`.

## Freeing the GPU, honesty, style and the router (filter 0.13)

### A stopped picture never keeps the card

SkyRig, 21/10 (UTC−7): the user stopped a four-variation restage at 20:25:57. Open WebUI
cancels the request, never the ComfyUI job it queued, so nothing freed the card. At
20:26:10 Ollama started loading the 27B beside the running sampler. ComfyUI steps 7 and 8
took 143 s and 159 s, and Ollama gave up at 20:31:08 ("client connection closed before
llama-server finished loading"). Every later turn waited on "Carregando o modelo de
conversa".

With exclusive VRAM, the filter now:

- **when a picture turn is stopped** (`asyncio.CancelledError` in `Filter.request`):
  clears ComfyUI's queue, interrupts the running prompt and calls `/free` at once;
- **before every turn that may load the chat model** (the routed path, and Image off):
  asks `/queue`, then interrupts, clears and frees when a job is running; with an empty
  queue it asks `/system_stats` and frees only when ComfyUI's torch holds more than
  1 GiB.

The status line is "Liberando a GPU da imagem cancelada", and the log gets
`image_reclaim`. While this process is making a picture (`_ACTIVE_JOBS`), nothing is
interrupted, so another chat's picture is never stopped.

### Honesty, style and the router

- **Claims of a picture.** A reply on a turn that made no picture, which says "aqui está a
  imagem", "já gerei", "here is the image" and similar, ends with a notice that no
  picture was made.
- **Style.** A new picture's style follows the medium its prompt opens with ("A photo
  of…" is a photo; anime, illustration and painting are art). It only falls back to the
  router's `style` when the prompt names no medium. On 21/10 the router wrote "A photo
  of…" with `style: art`.
- **References.** The image model sees only the pictures listed in `references`. The
  router now lists every picture its prompt relies on, never writes image numbers, and
  treats "esta personagem" on an upload as B.
- **Aspect.** Portrait only for one standing figure. Landscape for two or more people,
  someone lying down and wide settings. Square for close-ups.

**Not validated on an RTX 5090:** the interrupt and free against a real ComfyUI under
load.

Behaviour tests: `tests/test_image_gpu_and_native_flow.py`.

## Native gallery, attached area and validated plan (filter 0.14, Hub 1.11)

The 1.10 carousel (an embed iframe with data-URI thumbnails) and brush (a 32 × 32 grid
written into the user's text) are gone. A sandboxed embed can only send words to the
chat, which is why the area ended up as `[área N:…]` in the message and the thumbnails
were the size of a stamp. Both now live in the page itself.

### What the Hub installs

On a fresh install **and on every Repair** (idempotent: unchanged files report "ignorado"),
the Hub adds to the *managed* Open WebUI 0.12.0 only:

- `open_webui/pepito_native.py`: the API under `/api/v1/pepito/images`;
- `open_webui/frontend/pepito-native/v1/pepito_image_ui.js` and `.css`;
- an owned block in `main.py` (the router, before the images router) and one in
  `frontend/index.html` (the stylesheet and the script).

The blocks are marked `PEPITO NATIVE IMAGE EXTENSION` and stripped and rewritten as a
whole. An external Open WebUI is never touched. Another pin, a missing anchor or a package
outside the managed environment fails the repair; it is never reported as done.

### Gallery

After each batch the filter publishes the pictures (`publish_gallery`: every file must be
the user's, and it is linked to the chat) and emits one marker embed. The page script
replaces the marker with a gallery:

- one large picture at a time, arrows, swipe, ← →, a counter and dots;
- **Usar esta** saves the selection through the API. It sends no message, so the chat does
  not repeat the gallery (23/10: each "usa a imagem N" added a 58 KB embed);
- pictures load as authenticated originals, only near the viewport, with at most six kept;
- the stock previews of that batch are hidden only after the first picture has loaded. If
  the script is missing, the files stay on the message and the status says to use Repair.

A typed "usa a imagem N" selects in the same gallery (`select_image`) and shows nothing.
A selection made in the gallery is read back (`read_selection`) as the next edit's source.

### Area to change

**Editar área** opens a large editor over the original (zoom, brush size, eraser, undo,
clear; keyboard and narrow layouts). **Usar seleção** attaches a chip next to the message
box; it is pinned to that picture, editable and removable, and holds the mask only in the
browser.

On send, the script arms a one-shot draft for the chat with the mask PNG, its source
number and the exact last user text, then forwards the original request untouched:

1. the draft lives 300 s and is matched on the SHA-256 of the exact text; a different text,
   an expired draft or a source that is no longer the selected one returns nothing or 409;
2. the mask is one opaque grayscale PNG, exactly the source's size (at most 8192 px a
   side, 16,777,216 pixels, 8 MiB);
3. the filter consumes it in `inlet` (`consume_draft`), so the router and the chat model
   never see it, and the text of the chat stays clean;
4. each edit is composited over the source at the mask's own pixels: the source stays
   exactly outside the area, at the source's size, and the editor paints the soft edge.

An area only limits a local edit (`edit` or `remove`) of the very picture it was painted
on. For a pose, a restage, another source or a multi-picture edit the filter **asks**
instead of editing the whole picture. If the composite fails, nothing is delivered and the
user is told that the original is intact.

### The plan

The router no longer returns only an action. Before `prompt` it fills a structured `plan`:

- `base`: the picture to change (0 for a new canvas);
- `changes` and `preserve`: what to alter and what to keep, in short English;
- `correction`: `none`, `no_change`, `too_much` or `wrong_source`, judged from the user's
  meaning in any language (no keyword list);
- `clarification`: a question, only when the source or the change cannot be resolved;
- each reference carries its `purpose`: what to take from that picture, and what not to.

The router is told which input counts the stack supports and that this is not a licence
to attach everything. **Code validates the plan, never the words** (`validate_intent`,
`reference_plan`):

- every number exists, the base is among the references, nothing is both changed and
  preserved, every reference has a purpose;
- the sources are exactly those named, a source needed twice is one slot with both
  roles, and the prompt is compiled from the same plan (it cannot cite a picture that is
  not sent);
- a request that needs more distinct pictures than the workflow takes (Krea takes two)
  **asks which to prioritize** and queues nothing. No source is dropped silently;
- without a decision from the router (it timed out) the word rules still edit the
  current picture.

**A correction** (`no_change`/`too_much`) redoes the last edit's instruction on its
original source, one step freer (or more faithful), reusing its mask if it had one. With no
eligible edit to redo, the filter asks. "Usa a 2 e deixa o fundo azul" still names the
source deterministically and teaches the fidelity bias.

The router does not use the model's hidden thinking mode: the plan is the reasoning, in
the structured output, so its cost is bounded and visible.

**Not validated on an RTX 5090:** the quality of the plan on the real chat model, the
interrupt and free under load, and the editor on real pictures.

Behaviour tests: `tests/test_image_gpu_and_native_flow.py` (filter),
`tests/test_open_webui_native.py` (backend, provisioning).

## Faithful execution (Hub 1.12 / filter 0.15)

The earlier correction/retry and count policies above are historical. The executable plan
now distinguishes `new`, `refine` and pure `retry`; a retry carrying new changes asks rather
than discarding them. Pure retries restore the complete executed recipe; refinements use
the current selected result and keep their fresh requirements. Base governs actual slot
order and each workflow declares its supported reference layout.

Krea edit graphs expose width, height and grounding bindings. Explicit format requests
reach the output latent; local edits otherwise keep source ratio within a 2 MP canvas.
Manual-mask/geometry conflicts ask before loading the image model. Requested counts
remain stable across HQ and retries; no extra sampling is triggered by global pixel MAD.
The final acknowledgement derives from delivered files and failure state. The image-off
path cannot claim a new image was delivered.


## Accepted calibration ledger (Hub 1.13 / filter 0.16)

`pepito_calibration.py` is installed beside the native router. Its transactional SQLite
ledger records only delivered attempts, binds each output number to its account/chat and
attempt, and trains only after explicit approval of an automatic/corrected result. A
source pick never approves, default publication never picks explicitly, and a newly
published batch consumes the previous explicit source choice so cumulative edits advance.

`Usar esta` and `usa a imagem N` select. `Aprovar resultado` and `aprovo a imagem N` use
the same owner-checked approval helper. Repeated approval cannot train twice. Manual,
failed, cancelled or undelivered work never trains. Rejection records evidence without
assuming a subsequent retry worked. Scopes include model family, graph identity, change
level and operation; accounts do not share calibration. Bias remains bounded and decays
with time and subsequent samples. Legacy pick bias is migrated once; legacy offsets from
unconfirmed failures remain diagnostic evidence only. Reset is account-scoped.

The native API also persists explicit editor preferences; it validates editor IDs against
the installed edit-capable stacks and never silently substitutes a missing editor.


## Pixel preservation and explicit editors (Hub 1.14 / filter 0.17)

`pepito_image_preservation.py` performs source-only grounding with the configured resident VL
model before Ollama unload. A bounded schema, confidence threshold and region-size checks
require manual painting on uncertain results. This is bounding-region grounding, not
pixel-perfect semantic segmentation and not verification that an output obeyed the edit.
Manual masks win. Whole-scene/geometry requests do not receive an automatic local mask.

The final operation composites source pixels outside the full-resolution feathered mask,
with no further VAE pass. RGB inside/outside metrics are telemetry, never semantic success.
HQ originals retain their original dimensions outside local edits; unmasked global edits
report the 2 MP canvas limit. Qwen uses resolution=0 with bounded prepared references;
reframing adds a transparent border rather than stretching source content.

Editor choice is explicit and validated against installed stacks. A comparison seed patches
actual sampler seeds and removes Open WebUI's random-seed override; multiple requested
variations use consecutive seeds. Use the same explicitly selected source, mask, instruction
and seed for each editor. Cross-architecture outcomes are not expected to be identical.


## Compact image UX (Hub 1.15)

The native gallery is image-only with rounded corners. Hover/focus (always usable on touch)
exposes Edit and the function menu; only multi-image batches display a counter. No prompt,
base description or permanent hint is rendered. Clicking opens a modal fullscreen viewer
with zoom/fit/pan, navigation, download and the same function menu; Escape and focus return
are supported. The approved canvas editor and removable/editable area chip remain.

The menu separately selects the next source and approves an outcome, exposes installed
editor preferences and a comparison seed, and offers account-scoped calibration reset.
No simulated share/background-removal actions are displayed.

Image-off mask submissions are guarded in capture phase when the upstream toggle state is
unambiguous; the request wrapper remains the authoritative backstop and restores rejected
text without overwriting newly typed content. The mask stays attached on rejection.
Validation used real Open WebUI 0.12.0 with synthetic images and a text-only stand-in,
never Capella inference. Runtime quality and speed on the 5090 remain separate gates.

## Edit corrections and the edit lab (Hub 1.17.2 / filter 0.18.2)

Session 02 (RTX 5090, 02/11) showed timid, one-sided body edits. Raw ComfyUI outputs
already had little change before compositing; the mask was not the primary cause.
The Krea 2 General Edit V2 adapter's suitability for these edits is unproven.
Diagnosis: material do pacote privado; market review: `docs/research/edit-industry-review.md`.
Seven pipeline bugs found on the way are fixed:

- **Ledger range.** Attempts at `ref_boost` 0.5 now register (`ATTEMPT_STRENGTH_RANGE`
  0.5–5.0); a learned target still stays within 1.0–5.0.
- **Sided changes are new.** A change with side, scope or negation words (right/left,
  only, both, not…) is a repeat only if the old recipe has the same words with the same
  qualifiers; "only the right one" after "both" is a new request.
- **"Nada mudou" builds on the current picture** for a single-source edit or removal;
  "repita exatamente", restagings and multi-source edits still use their own sources.
- **Area on chained edits.** The previous area is reused when the base is the previous
  edit's source or output, rescaled for a different size; a different shape is reported.
- **No compounding dilation.** Growth starts from the area as found, per area, capped at
  2.5× and 0.65 of the picture; a merged area for the same part never shrinks.
- **Painted-area drafts** are checked before use: other words keep the draft armed, a
  different selected picture, expiry or a mismatched picture is explained and nothing
  is edited. While a draft armed for other words is pending (up to 5 min), picture
  requests in that chat get that explanation until the area is re-marked or removed.
- **Consistent prompts.** Keep items that name what the change touches are dropped; a
  side that already grew is never asked to shrink or match the other one; the user's own
  side words are quoted. Every correction's floor is `ref_boost` 1.0.

O laboratório histórico de edição e seus testes estão no pacote privado; o bundle público não fornece esse comando. A cobertura pública de métricas e avaliação usa imagens sintéticas nos testes de troca Qwen.

Validação física na RTX 5090 continua necessária para qualidade e tempos. Regressões gerais: `tests/test_image_gpu_serialization_and_trace.py`, `tests/test_image_calibration_store.py`, `tests/test_open_webui_native.py` e `tests/test_swap_lab.py`.

## Edit methods (Hub 1.18 / filter 0.19)

Native Preferences persist `edit_method`: `auto`, `qwen`, `warp`, `warp_qwen`. Auto applies
only to one-source local shape changes: Qwen first, then a bounded warp after a no-change
correction of a Qwen shape edit. Ordinary color, clothing, background and removal edits keep
the configured editor. Explicit choices never fall back silently; missing families, multiple
sources, geometry/pose requests or an unplanned warp produce an actionable reply.

The router may add `plan.deformation = {mode, factor}` with `magnify`, `width`, `height` or
`extend_down` and a factor from 0.625 to 2.0. A warp needs that plan and a mask. The filter
warps only a bounded model input, keeps the original URL and mask as provenance and paste-back
authority, and does not expose the intermediate image as a chat picture. Deformation + Qwen
uses one Qwen repair edit (E1); masked-latent E2 is lab-only.

Pixel preservation keeps source alpha and every zero-mask pixel exact. Its boundary correction
only removes a small, near-achromatic illumination shift when the outside ring proves it. A
recolor or textured boundary is left alone. The dark neck band has not been attributed to a
proven cause.

Not validated on an RTX 5090: Qwen-vs-warp selection quality, E1/E2 output, timing, VRAM and
the boundary correction on real model output. Behaviour tests:
`tests/test_image_edit_methods.py`, `tests/test_image_gpu_serialization_and_trace.py`,
`tests/test_open_webui_native.py`, `tests/test_swap_lab.py`.

## Automatic shape edits by medium (Hub 1.19 / filter 0.20)

The user's ratings of the 1.18 edit lab (04/11, six cases, twelve variants) set Auto. On the
four drawings, E1/E2 (warp + Qwen repair) were the favourites in every case: growth kept the
style with almost no side effects; the warps alone grew well but altered accessories and text;
Qwen alone was faithful but grew less. On the two photos, Qwen alone was the only clean result
and every warp, with or without Qwen, stretched the person or the coat.

So Auto now tries, for a one-source local shape change:

| Source medium | Order | After "nada mudou" |
|---|---|---|
| art | Deformação + Qwen, Deformação, Qwen | the next method in the order; Qwen stays Qwen |
| photo | Qwen | Qwen, with the wider no-change area |

A repeat keeps the previous method. A method that cannot run (its model family not installed,
or no planned deformation for a warp) is skipped for the next one in the order; an explicit
choice in Preferências do editor still never falls back.

The medium is, in order: the Foto/Arte switch, the user's words ("em anime"), the medium the
Hub recorded for a picture it made, and for an upload with none, one call to the installed
vision model on a 768 px copy (`classify_medium`, JSON `{"medium": "photo"|"art"}`). A failed or
invalid answer means photo, i.e. Qwen alone, which the ratings show is safe for both. A medium
seen by the vision model also sets that edit's style strength. Trace: `medium` event
(`source=vision|default`), `medium` in the turn trace.

Not validated on an RTX 5090: the vision model's photo/art accuracy on real uploads, and the
chain on real model output beyond the lab's six cases.

## Ignored text (SPEC-106)

Text the user marks as a note to the developer never reaches the router or the chat model:
`['qualquer coisa']` (straight or curly single quotes) or `[Ignorar: qualquer coisa]` (any
case or accent, spaces optional, no nested brackets; an unclosed bracket is not ignored).
Every user message of the chat is cleaned, with Image on or off. The passages removed from
the last message are kept in the `turn_start` trace as `ignored_text`. A message with only
ignored text (and no picture) is answered "Anotado." with no router or model call.

Behaviour tests: `tests/test_ignored_text.py`.
