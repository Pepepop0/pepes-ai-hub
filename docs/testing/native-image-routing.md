# Native / auto image routing: Open WebUI 0.11.3 + Hermes3 8B

Spike question: can a person get a picture by *asking for one* - no manual
**Image** switch - using only Open WebUI 0.11.3's own primitives, while plain
chat stays plain chat? No custom intent router unless the evidence says one is
needed.

Everything below is read from the pinned code (`open_webui` 0.11.3 as
installed by the Hub; `middleware.py`/`builtin.py` byte-identical across the
PEPEH, Capellah_gpt and pepito-e2e roots) or measured on a scratch stack built
from that same install. The docs site for "latest" was not used as evidence.

## 1. What 0.11.3 actually does

### A. Builtin tools in 0.11.3

`open_webui/tools/builtin.py` defines them; `utils/tools.py:get_builtin_tools`
decides which are offered. Grouped by the category key that gates them:

| category key | tools | extra gate |
| --- | --- | --- |
| `time` | `get_current_timestamp`, `calculate_timestamp` | none |
| `user_input` | `ask_user` | none |
| `knowledge` | `list_knowledge_bases`, `search_knowledge_bases`, `query_knowledge_bases`, `grep_knowledge_files`, `search_knowledge_files`, `query_knowledge_files`, `view_knowledge_file` (or the `kb_exec` set) | none |
| `chats` | `search_chats`, `view_chat` | none |
| `memory` | 8 memory tools | `features.memory` + capability `memory` + permission |
| `notes` | `search_notes`, `view_note`, `write_note`, `replace_note_content` | `notes.enable` (default on) |
| `tasks` | `create_tasks`, `update_task` | saved chat |
| `automations` | 5 automation tools | `automations.enable` (default on) |
| `calendar` | 4 calendar tools | `calendar.enable` (default on) |
| `channels` | 4 channel tools | `channels.enable` (default off) |
| `files` | 4 chat-file tools | non-URL chat files + `file_upload` without `file_context` |
| `web_search` | `search_web`, `fetch_url` | global + capability + `features.web_search` |
| `code_interpreter` | `execute_code` | global + capability + `features.code_interpreter` |
| `notifications` | `notify` | user webhooks |
| `subagents` | `delegate_task`, `timer` | `subagents.enable` |
| `image_generation` | `generate_image`; `edit_image` | global + capability `image_generation` + **`features.image_generation`** + permission; `edit_image` also needs `images.edit.enable` |

Measured on the scratch stack with the Hub's configuration (admin user, UI
defaults): **35 tools** offered on a plain turn, **36** with Image on
(`generate_image` added).

### B. How they are enabled per model record

`utils/middleware.py:2769`:

```python
use_builtin_tools = is_note_chat or (
    bool(metadata.get('session_id'))                      # UI turns only
    and metadata.get('params', {}).get('function_calling') != 'legacy'
    and (model...meta.capabilities or {}).get('builtin_tools', True)
)
```

So: capability `builtin_tools` on the model record (default **true**), native
function calling, and a request that came from the UI (has a Socket.IO
`session_id`). `function_calling` resolves request params → model params →
`'native'` (`main.py:1267`), so native is the default.

### C/D. Granular control - yes

`meta.builtinTools` on the model record is a per-category switch map
(`is_builtin_tool_enabled(category)` defaults to true). The model editor exposes
it as **Builtin Tools** with exactly the 16 keys above
(`workspace/Models/BuiltinTools.svelte`), storing only the `false` entries. It
also exists admin-wide as `DEFAULT_MODEL_METADATA.builtinTools`
(`admin/Settings/Models/ModelDefaultsPanel.svelte`).

Setting `capabilities.builtin_tools = true` and every category `false` except
`image_generation` offers **exactly `generate_image`** on an Image turn - measured,
not inferred (section 4). With `images.edit.enable` on, the same record offers
exactly `generate_image` + `edit_image`. There is no separate category for edit:
`image_generation` covers both; edit is switched by the global
`images.edit.enable` (env `ENABLE_IMAGE_EDIT`, default **off** in 0.11.3, and
off in what the Hub ships).

### E/F. Default Features - yes

`meta.defaultFeatureIds` (model record) and the admin default of the same name.
It is **frontend-only**: `chat/Chat.svelte:1060` sets
`imageGenerationEnabled = defaultFeatureIds.includes('image_generation')` when
a chat opens with that model, if the model has capability `image_generation`,
the instance has image generation enabled and the user is admin or has the
permission. The composer then sends `features.image_generation: true` on every
turn of that chat (`getFeatures()`), exactly as if the person had pressed
**Image**. The backend never reads `defaultFeatureIds` for chat (only
`utils/automations.py` does, for headless runs). Editor:
`workspace/Models/DefaultFeatures.svelte`, available for `web_search`,
`code_interpreter`, `image_generation`.

### G. Can native mode decline to make an image while Image is on?

By construction, yes. With `features.image_generation` on and native function
calling, the middleware does **not** run the image pipeline
(`middleware.py:2689`: `chat_image_generation_handler` only when
`function_calling == 'legacy'`). It only puts `generate_image` in the `tools`
list. No system prompt is added - the captured wire payload for an Image turn is
the user message and the tool schemas, nothing else. Whether a picture is made
is entirely the model's choice to emit a structurally valid tool call. That is
the property this spike measures.

Legacy is the opposite: `chat_image_generation_handler` generates (or edits)
**before** the model runs, for every message, which is why the current filter
(c46bf00) says "every message produces an image while Image is on".

### H. With an image attached

Captured on the wire (scripted model, real middleware):

- The composer uploads the image with `POST /api/v1/files/?process=false` and
  keeps it on the user message as `{type: "file", content_type: "image/png",
  url: "<file id>"}` (`chat/MessageInput.svelte:1015`: the url is the bare id).
- `add_file_context` (`middleware.py:1711`, native + saved chat only) prepends
  `<attached_files><file type="file" id="<id>" url="<id>" content_type="image/png" name="apple.png"/></attached_files>`
  to that user message's text.
- The image bytes are **also** sent to Ollama in the message `images` field.
- Nothing tells the model which tool to use. Generate vs edit vs describe is
  again the model's decision, with the file id available to copy into
  `edit_image.image_urls`; `routers/images.py:image_edits` accepts a bare file id.
- Legacy with an attachment: `chat_image_generation_handler` edits instead of
  generating when the last two message sets contain images **and**
  `images.edit.enable`; otherwise it generates. It never describes.

## 2. Contracts

`generate_image` (exact schema sent to Ollama):

```json
{"name": "generate_image", "description": "Generate an image based on a text prompt.",
 "parameters": {"type": "object", "required": ["prompt"],
  "properties": {"prompt": {"type": "string", "description": "A detailed description of the image to generate"}}}}
```

`edit_image` (offered only with `images.edit.enable`):

```json
{"name": "edit_image",
 "description": "Transform one or more existing images according to a text prompt.\nSupports targeted edits such as adding, removing, replacing, inpainting, extending, or compositing image content.",
 "parameters": {"type": "object", "required": ["prompt", "image_urls"],
  "properties": {"prompt": {"type": "string", "description": "A description of the transformation to apply to the provided images"},
                 "image_urls": {"type": "array", "items": {"type": "string"}, "description": "Source image URLs to modify or use as composition inputs"}}}}
```

Execution: `builtin.generate_image` → `routers/images.py:image_generations` →
engine (ComfyUI: `WS /ws`, `POST /prompt`, wait for `executing node=null`,
`GET /history/{id}`, `GET /view`) → files persisted on the assistant message →
tool result `{"status": "success", "message": "...already visible to the user...", "images": [...]}`
reinjected → model continuation. `edit_image` is the same through `image_edits`
(inputs uploaded with `POST /api/upload/image`).

## 3. The harness

`experiments/native_image_routing/` - a server-side replica of one composer
turn, built so the 727482a failure (helper test passed, composer failed) cannot
repeat:

- **Envelope**: `harness.OpenWebUI.send_turn` copies `Chat.svelte`
  `sendMessageSocket` field by field for a new persisted chat: no `messages`
  (the backend loads them), `user_message`, `id`, `parent_id`, `model_item` from
  `/api/models`, `features` from `getFeatures()` with Image either as the person
  set it or as `defaultFeatureIds` would open it, `variables`,
  `background_tasks`, **no sampling parameters** (so Ollama applies the UI's
  defaults), and a **live Socket.IO `session_id`**, which is what switches on
  builtin tools and the server-side agent loop.
- **Middleware and loop**: real Open WebUI 0.11.3, unpatched. The turn is
  complete when the persisted assistant message is `done`.
- **Model**: a real Ollama through `ollama_probe.py`, a transparent proxy that
  records per model call the tools offered, the messages sent, the model's raw
  content and tool calls as Ollama parsed them, `done_reason`,
  `prompt_eval_count`. Scripted mode replaces the model for self-tests.
- **Executor**: real `generate_image`/`edit_image`; only the engine below them is
  `fake_comfyui.py`, which speaks the exact ComfyUI protocol above, gives every
  `/prompt` a server-side nonce that appears in no prompt, and returns a PNG
  carrying that nonce. A nonce in the ComfyUI log = the executor ran; the same
  nonce inside the file Open WebUI attached to the message = the result went
  back into the chat. Verified: nonce present in the stored upload, 0
  occurrences in anything sent to the model.
- **Isolation**: its own ports (Open WebUI 38080, probe 31435, fake ComfyUI
  38188, Ollama 31434), `DATA_DIR`/`STATIC_DIR`/cwd in scratch, Ollama store of
  hard links to the user-level store (same `hermes3:8b` digest as PEPEH), Hub's
  generated `open-webui.env` values re-pointed at scratch. Title/tag/follow-up
  generation are off (they run after the routing decision and only cost time).
- **Cooperation**: `bench --yield-root <root>` unloads the harness model and
  waits whenever that root's logs changed in the last 3 minutes or free VRAM is
  short, so it never competes with someone testing a real root.

Reproduce:

```powershell
$env:NATIVE_IMAGE_SCRATCH = "C:\some\scratch"
# Ollama on 31434 with OLLAMA_MODELS=<store>, OLLAMA_CONTEXT_LENGTH=4096 (production default on a 12 GB GPU)
$py = "<an Open WebUI 0.11.3 venv>\Scripts\python.exe"
& $py experiments\native_image_routing\run.py up --mode scripted
& $py experiments\native_image_routing\run.py selftest
& $py experiments\native_image_routing\run.py down
& $py experiments\native_image_routing\run.py up --mode proxy
& $py experiments\native_image_routing\run.py bench --variant image-only --suites generate,chat,ambiguous --repeats 2 --out results.jsonl --ollama-log <ollama stderr log>
& $py experiments\native_image_routing\run.py report results.jsonl
```

## 4. Measured - the 35-builtin bug, and what removing it changes

Scratch stack, real `hermes3:8b` (same blob digest as the Pepe root), Ollama
0.33.2 on the RTX 5070 with the production default `num_ctx` 4096, no sampling
parameters (UI defaults), Image on.

| model record | tools offered | prompt (Ollama's own count) | truncation |
| --- | --- | --- | --- |
| `all-builtins` (UI default before 7972472) | 36 | 4886-5020, cut to **2050** | **every turn** |
| `image-only` (this spike) | 1 (`generate_image`) | 237-257 | none |
| `hub-7972472` (master today) | **0** | ~150 | none |

The truncation is Ollama's own warning: `truncating input prompt limit=2050
prompt=4897 keep=4 new=2050`. Six turns on `all-builtins` reproduced the whole
failure family from 7972472: an empty answer, a duplicated call
(`generate_image` twice, one engine run), and the tool call leaked as text
("To generate an image using the generate_image() function, you...").

Note what `hub-7972472` means for this question: with builtin tools off, an
Image turn in native mode is offered **no tools at all**, so no picture can be
made. That is exactly why c46bf00's filter has to force legacy.

## 5. Measured - routing quality, `image-only`

160 trials per model (30 generate + 30 chat + 20 ambiguous, each twice; EN and
PT-BR). "engine run" = a `/prompt` reached the fake ComfyUI with its nonce.

| metric | hermes3:8b | qwen3:8b | bar |
| --- | --- | --- | --- |
| generate recall | 60/60 (100%) | 60/60 (100%) | >= 95% |
| exactly one call + one engine run | 60/60 (100%) | 60/60 (100%) | - |
| chat false positives | **4/60 (6.7%)** | 0/60 (0%) | 0% |
| generate precision | 93.8% | 100% | - |
| duplicate calls | 0 | 0 | 0% |
| malformed calls | 0 | 0 | - |
| raw JSON leaked to the user | **3/160 (1.9%)** | 0/160 (0%) | 0% |
| empty answers | 0 | 0 | 0% |
| errors | 0 | 0 | - |
| truncation | 0 | 0 | 0% |
| prompt tokens (median) | 246 | 154 | - |
| seconds per turn (median) | 2.0 | 5.5 | - |
| ambiguous that generated | 20/40 (50%) | 11/40 (27.5%) | classified only |

**Hermes3 8B fails the bar**, on two counts that are model behaviour, not
plumbing:

- *false positives*: "Write a haiku about the ocean." and "Escreva um haicai
  sobre a chuva." generated a picture (twice each, both languages); "Tell me a
  joke." called `generate_image(prompt="Tell me a joke.")`. The written answer
  was still a haiku or a joke - the image was extra.
- *raw JSON*: three turns printed the call instead of making it, e.g.
  `{"name": "generate_image", "arguments": {"prompt": "A good portrait"}}`.

**Qwen3 8B passes every bar** over the same 160 trials, at ~2.7x the latency
(it thinks first) and a third less prompt. Its ambiguous behaviour is the more
conservative one: it generated for "imagine a castle" / "How would you draw a
cat?" / "imagine um castelo" and answered in words for "Describe a red apple.",
"What makes a good portrait?", "What colors go well with a sunset painting?".
Its image prompts are also richer (it expands "a red apple on a white table"
into a lit, textured scene).

## 6. Measured - an image attached

12 turns per model (6 "describe this", 6 "change the background"), with
`images.edit.enable` on so both `generate_image` and `edit_image` were offered.

**Every one failed, for both models**: Ollama answers HTTP 400 `Multimodal data
provided, but model does not support multimodal requests`, Open WebUI ends the
turn with an error and no assistant text. Neither `hermes3:8b` nor `qwen3:8b`
has vision, and Open WebUI sends the attached image to the model regardless
(the capability `vision` on the record is a UI gate, not an Ollama one).

So the edit question cannot be answered by the model choice alone: **attaching
an image to the Pepe profile's chat model breaks the turn today**, before any
routing decision. A vision-capable chat model, or a route that does not send
the image to a text-only model, is a prerequisite for native `edit_image`.

## 7. Verdict

- Auto image **without a custom router** is achievable with 0.11.3 primitives
  alone: `capabilities.builtin_tools = true`, `meta.builtinTools` with every
  category false except `image_generation`, and
  `meta.defaultFeatureIds = ["image_generation"]`. No code, no regex, no
  parser: one model record.
- With **hermes3:8b** it is not acceptable yet: 6.7% of ordinary chat turns
  produce an unwanted picture and 1.9% leak the tool call as text.
- With **qwen3:8b** the same configuration meets every bar on 160 trials.
- Legacy stays what it is: with Image on, every message becomes an image. It
  remains the compatibility fallback, unchanged (c46bf00).
- Native `edit_image` is blocked by the chat model having no vision, not by the
  configuration.

## 8. Limitations of this evidence

- The image engine is a stand-in. Everything above the engine is real Open
  WebUI; ComfyUI itself was not exercised (deliberately: the question is
  routing, and 50 real FLUX runs cost minutes of GPU each).
- Sampling is the UI's default and therefore stochastic. 160 trials per model
  bound the rates but do not prove a 0% floor; a rare false positive under
  qwen3:8b is not excluded by this sample.
- Background title/tag/follow-up generation was disabled. It runs after the
  routing decision.
- Only one turn per chat was measured. Multi-turn behaviour (an image request
  following a chat, or "another one, but blue") was not.
- qwen3:8b was measured here but is **not** the Pepe profile's pinned model;
  changing that pin is a product decision with its own VRAM and quality
  consequences, outside this spike.
