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

Consequence to know: while Image is on, *every* message produces an image
(that is what the legacy route does). Turn Image off for ordinary chat.

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
