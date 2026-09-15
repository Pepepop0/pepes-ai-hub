# Image editing: Open WebUI 0.11.3 → ComfyUI → FLUX.2 Klein

How a user edits a picture in the Pepe profile — attach an image, turn on
**Image**, write the change — what in the pinned Open WebUI already does the
work, what the Hub configures, the three things that were still wrong on the
real root, and the evidence.

## What Open WebUI 0.11.3 already does

The Hub adds no upload, dispatch or attachment code. In the pinned version:

- `utils/middleware.py` `chat_image_generation_handler` (the legacy image
  route, which the Hub's filter already forces on every UI Image turn) looks at
  the conversation's own history, newest message first, and takes up to two
  sets of images (`get_images_from_messages`). With images present and
  `images.edit.enable` set it calls `image_edits`; otherwise it generates from
  text.
- `routers/images.py` `image_edits`, ComfyUI engine: each image is read
  through the Files API by id (a deleted file is a 404), renamed to a fresh
  `uuid.png` (`get_image_file_item`), uploaded with `POST /api/upload/image`
  `type=input`, and the uploaded names are written into the mapped `image`
  node by position; `prompt`, `model` and `seed` are filled the same way
  (`utils/images/comfyui.py` `_apply_workflow_nodes`). The workflow is queued,
  the `SaveImage` outputs are fetched from `/view`, and each PNG is stored as a
  new Open WebUI file linked to the assistant message (`upload_image`), then
  shown through the `files` event, which is persisted on the message.
- The user's original file is only read.

Compared with the current documentation
(docs.openwebui.com, *Image Generation and Editing*): the docs list Image,
Prompt, Model, Width, Height for editing and say seed and steps are not
supported; in 0.11.3 the shared `_apply_workflow_nodes` does fill a `seed`
node (random per edit) and never fills steps. The docs give `prompt` as the
default prompt key; the code defaults to `text`. The docs do not describe how
the image reaches ComfyUI (the upload above) nor which images of a
conversation are used. The environment reference lists no `IMAGE_EDIT_*`
variables, while `config.py` defines them — and, like every persistent
setting, they only seed an instance's database on its first start.

## What the Hub configures

**Workflow.** `workflows/flux2-klein-edit-api.json` is the official ComfyUI
template *Image Edit (Flux.2 Klein 4B Distilled)*, exported to API format by
the pinned ComfyUI's own frontend (see `workflows/README.md`). It loads the
three files the text-to-image workflow already installs — no new download —
and maps, in `manifests/workflows.yaml`:

| parameter | node | key |
| --- | --- | --- |
| image | `76` LoadImage | `image` |
| prompt | `75:74` CLIPTextEncode | `text` |
| model | `75:70` UNETLoader | `unet_name` |
| seed | `75:73` RandomNoise | `noise_seed` |

Width, height and steps are not inputs of this graph: the size follows the
input image (scaled to one megapixel), and the distilled model runs 4 steps at
CFG 1. `declared_capability_problems` now refuses an edit graph named as
`text_to_image` and an `image_edit` graph without an image input.

**Settings.** On an instance that has started once, `images.edit.*` already
exist in Open WebUI's database with their defaults (edit off, engine
`openai`), so launch environment variables would be ignored. The settings go
through the admin Images API (`GET` + `POST /api/v1/images/config/update`,
the form Admin → Images saves): the current form is read, only
`ENABLE_IMAGE_EDIT`, `IMAGE_EDIT_ENGINE=comfyui`, `IMAGE_EDIT_MODEL`,
`IMAGES_EDIT_COMFYUI_BASE_URL`, `IMAGES_EDIT_COMFYUI_WORKFLOW` and
`IMAGES_EDIT_COMFYUI_WORKFLOW_NODES` change, and every other field goes back as
it was (`open_webui_image_edit.sync_image_edit`, next to the filter and chat
model syncs in `StackApplication.integrate_open_webui`).

Ownership is carried by the value: the stored workflow has
`_meta.managed_by = "Pepito's AI Hub: image editing"` on its image node.
Nothing configured (edit off, no ComfyUI edit workflow) → claimed. Marked and
different → repaired. Same → nothing written. Anything else — someone's own
edit engine or workflow — is refused and reported as a warning, untouched.

## What was still wrong on the real root, and the fixes

All three live in the existing filter `assets/open-webui/pepito_image_chat.py`
(0.3.0), the only place between Open WebUI's steps the Hub already owns.

1. **Raw JSON after a successful edit.** Open WebUI copies every attached image
   into the model request as `image_url` parts; Hermes3 has no vision and
   Ollama answered `400 "Multimodal data provided, but model does not support
   multimodal requests."`, shown verbatim under the edited picture. On Image
   turns the filter's `inlet` now removes those parts from what the model is
   sent; the message `files` the edit path reads are untouched. (Setting the
   model's `vision` capability off is not an option: the composer then refuses
   to attach images at all.)
2. **A made-up `<img src="…">`** in the acknowledgement survived the cleaner,
   which only knew markdown images and links. HTML image tags are removed too.
3. **False success.** When ComfyUI's execution fails (tested with a file that
   is not an image), Open WebUI gets no picture, attaches nothing, and still
   tells the model the image "is now being shown to the user"; the model
   announced it. The filter's `request` hook (after the image step, before the
   model) sees that context, checks the persisted message files, and when there
   are none replaces the context with a failure, emits the status
   "The image could not be created" and makes that the turn's text.

## Evidence — Pepe root, 2026-09-14

Real browser, real Open WebUI 0.11.3, real ComfyUI 0.34.0, no model
downloads, no reinstall. Original `Flux2-Klein_00025_.png` (red apple, white
background) sha256 `327ac534…4943b` before and after; only copies were
uploaded.

| step | ComfyUI job | input reaching node 76 | output | shown / persisted |
| --- | --- | --- | --- | --- |
| direct `POST /prompt`, "Change the apple from red to green while keeping the plain white background." | `c617af01` success, 18 s | uploaded copy | `Flux2-Klein_00026_.png`, green apple on white | — |
| UI: attach apple, Image on, "Change the apple to green but keep the background white." | `de2418bf` success | uuid upload, sha256 = original | `Flux2-Klein_00029_.png` | shown; file `86f081de…` = output byte for byte |
| UI, same chat: "Change the background to black while keeping the apple green." | `2a81c3a0` success | uuid upload, sha256 = `_00029_` (the first edit) | `Flux2-Klein_00030_.png`, green apple on black | shown; file `65660a20…` = output byte for byte |
| UI, new chat, Image on: "Generate an image of a red apple on a plain white background." | `b2ceffa8` success (t2i graph) | — | `Flux2-Klein_00032_.png` | shown |
| UI, new chat, Image off: `hi` | none | — | — | "Hello! It's nice to meet you…" |

Before fix 1 the first UI edit (`b1c09639`, `Flux2-Klein_00027_.png`) was made
and shown with the raw 400 JSON as the reply; before fix 2 a turn showed
`<img src=" alt="…">`.

Failure paths (composer envelope captured from the browser, live session):

| case | outcome |
| --- | --- |
| Image on, no image in the chat | generation from text (job `9eb03cd7`), as the route defines |
| attachment deleted before the turn | status "An error occurred while generating an image", no job, apology text |
| file that is not an image | job `4f6e0459` `execution_error`; no file; status "The image could not be created"; text "The image could not be created: the image engine finished without a picture." (before fix 3: "Here is the edited image…") |
| edit mapping pointed at a missing node | error status, no job; the next Hub run reported `edição de imagem atualizado` and restored node `76` |
| ComfyUI stopped | error status ("Failed to upload images to ComfyUI."), no job; ComfyUI started again by the next run |

Hub runs: the run introducing the configuration reported
`Open WebUI: edição de imagem instalado` (`integrated=['open-webui']`); the
next reported `já atual` with `did_nothing=True`; the
`image_edit: … not mapped yet; skipped` warning is gone.

## Known behaviour, not fixed here

- Every edit leaves its uploads in `apps/comfyui/input` (uuid names, one per
  image; a second edit in a chat uploads both the previous result and the
  original). Upstream behaviour; no cleanup exists.
- Hermes3 cannot see the picture, so its acknowledgement describes what it
  imagines ("a slice of green apple on a plate").
- In a chat that holds an uploaded image, a turn with Image **off** still sends
  the pixels to Hermes3 and shows Ollama's 400 JSON. That is chat with a
  non-vision model, outside this change.
- With Image on, every turn in a chat that holds an image is an edit of the
  newest image.

Focused tests:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_image_edit_dispatch.py tests/test_open_webui_image_edit.py tests/test_open_webui_integration.py tests/test_open_webui_image_filter.py tests/test_image_dispatch.py tests/test_workflows.py tests/test_frozen_composition.py
```
