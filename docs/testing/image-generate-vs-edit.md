# Image mode: a new picture or an edit of the one in the chat

The RC bug Pedro found by hand, why Open WebUI 0.11.3 behaves that way, what
decides now, and the evidence from the Pepe root.

## The bug

Image on, in one chat: "Generate an image of the WW2 tiger heavy tank" made a
tank. "Now an image of a necromancer" put a necromancer *into the tank
picture*. "Now I want an image of a blue ceramic mug" put a mug into that.
Reproduced on `19816cc` through the composer's own request envelope:

| turn | ComfyUI job | graph | node 76 input |
| --- | --- | --- | --- |
| tank | `b8dafb46` | text-to-image | — |
| necromancer | `b51ea993` | **edit** | `7146985f…` = the tank PNG |
| mug | `6e8d6ed9` | **edit** | `c0754700…` = the necromancer-in-tank PNG |

## Where the pinned version decides

`utils/middleware.py` `chat_image_generation_handler` (the legacy image
route the Hub's filter forces on Image turns):

1. for a saved chat it reloads the history **from the database**
   (`Chats.get_chat_by_id_and_user_id`, `get_message_list` up to
   `currentId`) — not from the request;
2. `get_images_from_messages` walks that history newest-first and collects
   the `files` of type image (uploads and generated results alike), first two
   sets;
3. `editing = images and Config.get('images.edit.enable')` → `image_edits`,
   otherwise `image_generations`.

The user's words play no part. And because the history comes from the
database, nothing a filter does to the request body or its metadata hides
those images from the handler; the only things that change the outcome are
the global edit switch (shared by every user and turn) and
`features.image_generation`, which decides whether the handler runs at all.

## The fix, in the filter the Hub already owns

`assets/open-webui/pepito_image_chat.py` 0.4.0.

**Intent** (`edit_intent`): a compact ordered rule set over the user's
message, Portuguese and English, biased towards a new picture — an unwanted
fresh image is recoverable, a silently altered old one is not.

1. an explicit reference to the picture ("this image", "the previous one",
   "nessa imagem", "imagem anterior") → edit;
2. a message that opens (after "now", "agora", "please"…) with an editing
   verb ("change", "replace", "mude", "troque"…) → edit;
3. a new-picture phrase ("an image of", "a picture", "uma imagem de",
   "outra foto"…) → new picture;
4. an editing verb anywhere (change, edit, remove, replace, add, put, keep,
   crop… / mude, altere, remova, troque, adicione, coloque, deixe,
   transforme, mantenha…) → edit;
5. a pointing verb with a pointer, per language ("make **it** blue", "paint
   **the** sky", "faça **o** fundo preto") → edit; in English also a
   pointing verb directly followed by **her**/**him** ("make her blonde",
   "turn him into a knight") → edit — only directly after the verb, since
   elsewhere "her" is a possessive in a new picture ("make a girl hugging her
   cat"). Filter 0.5.1; before it, "make her green" and "Make her blonde"
   generated new pictures on the Pepe root (2026-09-23);
6. anything else → new picture ("Generate a castle at night", "A dragon
   flying over mountains", "the same but at night").

**Boundary.** An edit is left to Open WebUI exactly as before. A new picture
in a chat that holds one: the inlet asks the same question the handler would
(`Chats.get_chat_by_id_and_user_id` + `get_images_from_messages`), turns
`features.image_generation` off for that turn so the handler never runs, and
the `request` hook makes the picture through Open WebUI's own
`image_generations` — the same call, the same statuses ("Creating image",
"Image created"), the same `files` event and the same context the handler
uses — with the prompt refined by the task model from **this message only**,
so no earlier picture leaks in through the words either. A failure there is
the handler's failure context and status. Nothing new touches upload,
ComfyUI dispatch, attachment storage or the edit path.

**Edit intent with no picture in the chat** falls through to Open WebUI's
own generation from the instruction, with the "created" context (documented
safe behaviour: nothing is claimed to have been edited).

**Chats Open WebUI does not save** (`temporary:`, the legacy `local:`,
`channel:`): the handler takes their images from the request's messages
instead of the database, and the filter asks the same source
(`images_in_messages`, the handler's `get_images_from_messages` rule) before
applying the same policy. On the pinned 0.11.3 that source is always empty:
`process_messages_with_output` (`utils/middleware.py` 2497) strips `files`
from every request message before the inlet and the handler run, so a
temporary chat can never edit through this route -- every Image turn
generates, an edit request included. Proven on the root: the browser's
temporary chat (tank, necromancer, an uploaded apple + "Now an image of a
banana") and an API-shaped temporary chat that resent `files` on its
messages (tank, necromancer, "Change the necromancer's cloak to red", mug,
dragon) produced text-to-image jobs only, no persisted rows. The filter's
mirror matters for a pipeline in which those files do reach the handler;
the harness covers both.

**The administrator's switch.** `image_generations` itself checks nothing;
the handler and the `/images` routes check `image_generation.enable` (the
`config` row seeded from `ENABLE_IMAGE_GENERATION`; the composer's Image
toggle is a separate, per-request flag) and the user's
`features.image_generation` permission first. The taken-over turn checks
both in that order before anything reaches ComfyUI: a user without the
permission gets nothing, as upstream gives nothing; with the switch off the
turn gets the handler's own "Image generation is disabled" status and
"disabled by the administrator" context -- no job, no file, no success
claimed. On the root, with the switch turned off through the admin Images
API after a generated cat: "Now an image of a dog." → status "Image
generation is disabled", 0 ComfyUI jobs, 0 files, the model says the feature
is disabled; switched back on, the next request generates again.

## Evidence — Pepe root, 2026-09-19

Same envelope as the reproduction, filter 0.4.0 synced by the Hub
(`Open WebUI: filtro de imagem atualizado`; the next run `já atual`,
`did_nothing=True`).

| turn | job | graph | node 76 input | output |
| --- | --- | --- | --- | --- |
| Generate an image of the WW2 tiger heavy tank | `b7844cce` | t2i | — | `504b7b87…` tank |
| Now an image of a necromancer | `b00bdaf2` | t2i | — | `ff3f5544…` necromancer alone |
| Now I want an image of a blue ceramic mug | `a8092408` | t2i | — | `7a397738…` mug alone |
| Change the mug to red | `2809a2e5` | edit | `7a397738…` = the mug | `4696a7c2…` red mug |
| Change the background to black | `3e419a85` | edit | `4696a7c2…` = the red mug | `52c94543…` |
| Generate an image of a medieval castle | `54e5b542` | t2i | — | `73192acb…` castle |

Every persisted attachment equals its ComfyUI output byte for byte. The same
six turns typed in the real browser (`94f64015…`): jobs `69328c49` t2i,
`629bee18` t2i, `451626ab` t2i, `14432fa1` edit of `99c82f4a…` (the mug),
`5c174912` edit of `bfe25a15…` (the red mug), `d5f16e15` t2i; then Image
off, "2 + 2?" → "…2 + 2 is 4…", no job.

Portuguese: "Gere uma imagem de um castelo." t2i, "Agora faça uma imagem de
um carro vermelho." t2i, "Mude a cor para verde." edit of the car, "Coloque
um chapéu nele." edit of the green car, "Troque o fundo para preto." edit of
that. An uploaded apple + "Change the apple to green…" edits the upload
(`327ac534…`), and "Now an image of a banana." afterwards is t2i.

With ComfyUI stopped, "Now an image of a dog." after an apple: status "An
error occurred while generating an image", no job, no file, the model
apologises; no edit attempted, no fabricated picture.

## Tests

`tests/test_image_edit_dispatch.py` — Pedro's sequence pinned
(`test_pedros_sequence_…`, fails on `19816cc`: `['t2i', 'edit', 'edit']`),
the full generate→edit→edit→generate transition with input hashes, the
generation and edit sentences of the brief, the no-picture edit, the failed
new picture, temporary chats on the pinned pipeline and with request files
that survive to the handler (fails on `cda785e`: `['t2i', 'edit', …]`),
the administrator's switch off (fails on `cda785e`: a second
`image_generations` call), a user without the permission, and the previous
filters' behaviour kept as a record.
`tests/test_open_webui_image_filter.py` — the rule set over 45 sentences and
the inlet's contract.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_image_edit_dispatch.py tests/test_open_webui_image_filter.py tests/test_image_dispatch.py tests/test_open_webui_integration.py
```

## Not covered

- The rules are words, not understanding: "Make a cat" generates, "Add a
  hat" edits, "the same but at night" generates. Anything undecided
  generates, on purpose.
- Model-side text: Hermes3 cannot see the pictures, so its acknowledgement
  of an edit describes what it imagines.
