# Generic tool calling: Open WebUI 0.11.3 + hermes3:8b

What happens when someone selects a tool in the Open WebUI composer of the
Pepe profile, measured on the real root with a deterministic probe, why the
default route produced nothing, what the Hub now configures, and how far
Hermes3 8B can be trusted with generic tools after that.

## The probe

A temporary Open WebUI tool, `pepito_probe_tool(message: str, count: int)`,
registered through the Tools API for the measurement only and deleted at the
end. It returns `PEPITO_TOOL_OK:<nonce>:<message>:<count>`, where the nonce is
a fresh server-side secret per turn that never appears in any prompt, and it
appends every invocation (arguments, types, nonce) to a log. A nonce in the
transcript therefore proves a real execution with real arguments; a claim
without one is the model talking.

Every trial went through the real product path: the composer's own request
envelope (captured from the browser: `tool_ids`, `session_id`, `features`,
`background_tasks`, no sampling parameters, so the UI's defaults -- Ollama's
temperature 0.8, top_p 0.9, top_k 40), `POST /api/chat/completions`, a live
Socket.IO session, the persisted `chat_message` row as the record. Prompt
sets: 20 direct calls (5 phrasings × 4), 20 natural-language extractions
(numbers as words, quoted and unquoted messages, "once", "twice"), 12 plain
chat turns with the tool still selected, 5 two-turn sequences
(`alpha`,1 then "do it again" `beta`,2).

## What the default route does (native, as shipped)

| set | trials | tool executed | correct args | receipt shown | false positives | empty replies |
| --- | --- | --- | --- | --- | --- | --- |
| direct | 20 | 4 | 4 | 0 | -- | 16 |
| natural | 20 | 0 | 0 | 0 | -- | 13 |
| plain chat | 12 | 0 | -- | -- | 0 | 2 |
| multi-turn | 10 | 0 | 0 | 0 | -- | 10 |

Other failure shapes in the natural set: `create_tasks`, `create_automation`
and `search_calendar_events` called with the probe's arguments (5 turns,
looping up to three calls), and one answer that explained "the result of a
call to the `list_automations` function" to the user.

Root cause, read from the Ollama log (`llama_server.go:317`,
`msg="truncating input prompt" limit=2050 prompt=4943 keep=4 new=2050`): a
UI turn in native mode is offered Open WebUI's 35 builtin tools (memory,
notes, knowledge, chats, tasks, calendar, automations, time; `use_builtin_tools`
at `utils/middleware.py:2769`) next to the selected one, about 4,700 tokens of
schemas. Ollama's default 4,096-token window cannot hold that, so the prompt
is cut to its second half. The first half is the tool-calling instructions
and the selected tool's schema (it is first in the list); what the model sees
is half of the builtin schemas and the user's line. It then emits a call
whose name is cut short (`"pepito_probe"`, `"pepito"`, captured raw), which
Ollama's parser discards silently: 26 generated tokens, no content, no tool
call, an empty bubble. Every plain chat turn of the profile was truncated the
same way (`hi` arrived as 2,050 of 4,834 tokens); the hermes3 template also
drops the system prompt whenever tools are present. 55 truncations in the
root's log before this work, including the earlier image-generation attempts.

Two controlled variations, for the record: an 8,192-token window with the
35 builtins still offered gave 9/21 (execution 17/21, but four turns looped
into duplicate calls, up to ten in a row, plus calls to `get_current_timestamp`
and `calculate_timestamp` nobody asked for). Legacy function calling
(`params.function_calling = "legacy"`, the route the image filter uses)
executed 50/50 with exact arguments but called the tool on 9 of 12 plain chat
turns (a haiku, 17 × 23, a joke) and framed the result as a retrieved document
("Based on the provided context…", `<source>` tags in the answer, "to use the
tool, follow these steps"): 27/50 answers carried the real receipt.

## What the Hub configures

Open WebUI's own switch for the builtin tools is the model capability
`builtin_tools`. `open_webui_models.sync_chat_model` keeps a model record for
the profile's primary chat model (`hermes3:8b`, resolved through the catalog)
with that capability off -- the UI's full default capability set with exactly
one switch flipped, a public-read grant, and `meta.managed_by` marking it as
the Hub's. It runs next to the image filter sync whenever Open WebUI is up and
the profile declares tools: created once, updated only if the capability was
turned back on under our record, never touching a record somebody else
created under that id (reported as refused, a warning). The running instance
reloads its model list right after a write, so the record counts without a
page refresh. Same record → nothing written → a second run stays a no-op.

Nothing else changes: the image route stays legacy through the filter, plain
chat gains its full context back, and no parser, regex or retry was added.

## The same trials with the record in place (native, no builtin tools)

| set | trials | tool executed | correct args | receipt shown | false positives | empty | leaks | duplicates |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| direct | 20 | 20 | 20 | 11 | -- | 0 | 0 | 0 |
| natural | 20 | 20 | 20 | 11 | -- | 0 | 0 | 0 |
| plain chat | 12 | 1 | -- | -- | 1 | 0 | 0 | 0 |
| multi-turn | 10 | 10 | 10 | 2 | -- | 0 | 0 | 0 |

Prompts of ~350 tokens, no truncation, 2.5 s per turn. What still fails is
the model, not the plumbing: asked to show the receipt, Hermes reports
"successfully repeated 'moon' four times" without it in 26 of 50 turns, and
three times invents a receipt of its own ("[2022-09-14 09:30:00] Pepito says:
hello"); "Tell me a short joke about programmers" made it call the probe once
with the joke as the message (the reply itself was a normal joke). An argument
the executor cannot type (`count: "many"`) comes back as
`{"error": "invalid literal for int() with base 10: 'many'"}`, no traceback,
nothing executed, and the model asks for a valid count.

Verdict for the profile: generic tools are **experimental** with Hermes3 8B.
Execution and argument extraction are solid (100/100 with the record); the
zero-false-positive and faithful-result bars are not met, and those are model
behaviour under the UI's default sampling.

## Evidence — Pepe root, 2026-09-14

Real browser, real Open WebUI, real ComfyUI, no model re-downloads, no
reinstall. The Hub run reported `Open WebUI: modelo de chat instalado` once
and `já atual` afterwards; the second run ended `did_nothing=True`.

| turn | route | execution proof | shown |
| --- | --- | --- | --- |
| composer, tool selected: "Use the Pepito probe tool with message banana and count 3, then show me the receipt." | native | log `banana, 3, nonce 2e5643480bc9` | "Ver resultado de pepito_probe_tool"; `2e5643480bc9:banana:3` |
| same chat: "Now do it again with message beta and count 2…" | native | log `beta, 2, nonce 01caf5403842` | `01caf5403842:beta:2` |
| "Could you pass the phrase 'green tea' through the probe four times…" | native | log `green tea, 4` | "Here is the receipt:" and nothing after it |
| "Use the Pepito probe tool with message 'oops' and count 'many'." | native | not executed; `{"error": "invalid literal for int()…"}` | asks for a whole number from 1 to 10 |
| `hi` (tool selected) | native | none | "Hello! How can I assist you today?" |
| Image on, red apple / white background | legacy (filter) | ComfyUI job `76f0b9ae` success | `Flux2-Klein_00025_.png` shown |
| `hi` (Image off) | -- | no job | "Hello! How can I assist you today?" |

No `truncating input prompt` line was written to the Ollama log after the
record was installed. The probe tool and every trial chat were deleted from
the instance afterwards.

Regression tests:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_tool_dispatch.py tests/test_open_webui_models.py tests/test_open_webui_integration.py tests/test_image_dispatch.py
```

`tests/test_tool_dispatch.py` models the pinned native loop line by line
(argument parsing, unknown tool, type coercion, result reinjection) and what
the record changes in the prompt the model is offered; A–K cover a structured
call, typed arguments, reinjection, a textual pseudo-call, an unknown tool, malformed
JSON, a wrong type, plain chat, two sequential calls, the raw payload staying
out of the text, and an image turn still becoming a ComfyUI job.

## Not done here, deliberately

- The model was not changed. The catalog's alternatives (`qwen3.5:9b`,
  `qwen3:8b`, both pinned with `tools: true`) were not downloaded or measured;
  a model decision is its own step with the same probe.
- Ollama's context window (`OLLAMA_CONTEXT_LENGTH`) was not raised: it costs
  VRAM the profile has budgeted elsewhere, and the 8k experiment shows it
  would not have been enough on its own.
- Runtime observations left as notes: the LLM was evicted from VRAM once
  while ComfyUI sampled (earlier phase), and three `llama-server.exe`
  processes were alive under the one managed Ollama at the end of this
  session (1.4 GB, 0.4 GB and 1.6 GB working sets). Not investigated here.
