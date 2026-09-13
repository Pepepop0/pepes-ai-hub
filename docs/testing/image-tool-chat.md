# Native image chat: Open WebUI 0.11.3

The Hub configures Open WebUI's `generate_image(prompt: str)` builtin, which
submits the existing ComfyUI workflow. The builtin attaches images through
`chat:message:files`; its return value is a separate message consumed by the LLM.

The investigation reproduced two distinct faults on the installed Pepe stack:

- Hermes can emit valid native `tool_calls`. The executor generated a valid
  image, but the builtin also returned file IDs/URLs to the LLM. The assistant
  echoed metadata and invented an absolute domain for an otherwise valid file.
- Recorded chats also contained a textual `create_image` call without a native
  function item or attached file. A tool-shaped assistant string is not an
  executed tool. Unknown names must fail explicitly, never be guessed or aliased.

`open_webui_image_filter.py` is a self-contained Open WebUI Filter Function.
It uses the pinned version's `request`, `stream`, and `outlet` hooks. It does not
patch the installed package, change the Hermes model/template, introduce a new
executor, or touch the WebUI database directly.

For native UI turns with image generation enabled, the filter:

1. Uses the streamed native executor loop, including when a model setting had
   requested a non-streamed response.
2. Wraps only the already authorized builtin image callable. The existing
   builtin still generates, stores, and emits the image. Its model-facing result
   is a short confirmation or a sanitized error, without file transport data.
3. Buffers assistant prose for each model round before displaying it. A complete
   `generate_image` envelope with exactly one string `prompt` can be normalized
   into a native delta and handled by the upstream executor. Unknown tools,
   malformed payloads, and ambiguous prose containing a call fail explicitly.
   Nothing executes merely because a user message contains JSON.
4. Emits `Image generated.` only after an actual successful result with images;
   a failed tool cannot become success because the model claims it succeeded.
5. Removes the completed image call's technical display items in the outlet.
   Attached files remain available in the chat and on reload. Other tools and
   earlier messages remain untouched. Per-message state prevents cross-chat
   leakage; concurrent duplicate calls for the same prompt share one acquisition.

The filter leaves ordinary chat and non-image tools enabled. It does not enable
image permissions, change a user's Legacy selection, or offer tools to anonymous
API clients. The native UI contract requires an authenticated session, the Image
feature enabled in the composer, and the model's builtin image capability.

## Deployment and verification

The fix is deployed through the authenticated **Functions API**, to the single
owned ID `pepito_image_chat`. `install_image_chat_filter(client)` in
`local_ai_stack.open_webui_filters` creates/updates that ID, enables the filter,
and reads it back. A repeat is a no-op; another owner's function is refused.
The filter is global in that WebUI instance but its hooks act only on the native
UI image turns described above. No unrelated functions or model settings change.

For another managed root, an administrator can import the self-contained file
in Workspace → Functions and enable it globally, or use the helper with an
authenticated `httpx.Client`. The helper is a source-side deployment utility;
it is not automatically installed by fresh Hub provisioning. This keeps the
existing installation, service lifecycle, and packaging contracts unchanged.
There is no need to reinstall dependencies or restart services. To roll back,
disable only `pepito_image_chat` in Functions.

Focused regression tests:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_open_webui_image_filter.py tests/test_open_webui_filters.py
```

Real smoke must use `/api/chat/completions` with a live authenticated Socket.IO
session, `stream: true`, an assistant message ID, and
`features.image_generation: true`. Read `chat:message:files` and the final
`chat:outlet`; an HTTP 200/task ID alone does not prove execution. Retrieve the
file using the same authorized user and decode it as an image. A direct call to
`/api/v1/images/generations` bypasses the chat tool loop and is insufficient.

References: [Open WebUI server-side tool calling](https://docs.openwebui.com/reference/server-side-tool-calling/),
[Filter Function hooks](https://docs.openwebui.com/features/extensibility/plugin/functions/filter/),
[image-generation usage](https://docs.openwebui.com/features/chat-conversations/image-generation-and-editing/usage/).

## Local evidence — 2026-09-13

Starting HEAD: `6ee7dd79a7ba30b951c58df86d6c4d6517566903`. Work branch:
`fix/image-tool-chat`. Managed root: `Desktop/PEPEH`. Open WebUI 0.11.3,
Ollama 0.33.2, Hermes3 8B, ComfyUI 0.34.0, existing FLUX2 Klein workflow.
No weights, dependency installation, model template, or service configuration
were changed. The filter is installed and enabled in this root through the API;
reapplying the same source reported `changed=False`.

The before-A native chat produced an attached PNG but assistant prose invented
an `https://app.yourorg.com/...` image link. The recorded earlier conversation
also contained an unexecuted `create_image` JSON message. The latter is an
unregistered name, not the builtin's schema. Direct Ollama testing returned
`generate_image` in `message.tool_calls` with empty assistant `content`.

| Smoke | Result | Elapsed |
| --- | --- | --- |
| A: red apple / plain white background | One native image call, PNG decoded, clean final message | 19.09 s |
| B: Image tool exactly once / exact supplied prompt | One native image call; prompt preserved byte-for-byte | 21.64 s |
| C: repeat A immediately after B | New image generated; no previous-turn cache reuse | 22.27 s |
| D: `hi`, ordinary text chat | `Hello! How can I assist you today?`; zero tools/images | 0.64 s |
| A, model defaults (no sampling overrides) | One image; clean final message | 27.39 s |
| B, model defaults | One image; exact prompt; clean final message | 20.49 s |
| `hi`, model defaults with Image still enabled | Ordinary greeting; zero tools/images | 4.44 s |
| Saved chat / reload from persisted history | One image attachment; only a message output item | 21.73 s |
| Final source, B with model defaults | One valid image; clean final message | 22.17 s |
| Explicit textual call to unregistered `create_image` | Zero images; explicit sanitized failure; no JSON in final chat | 7.11 s |

All generated files were fetched through the authenticated file endpoint and
decoded as 512×512 PNGs. The final B image was 232,698 bytes with SHA-256
`00699b8ae221a47410f5c03eb88c6aded9d5b01601130a233ad6093a239daf2a`.
The decoded image was visually inspected as a red apple on white. Final assistant
prose was `Image generated.`; final `chat:outlet` and saved history contained no
image function-call/argument/result display items. The native execution trace
was retained separately in local smoke evidence.

The real Hub `open_webui_target()` returned its loopback target; browser opening
returned true and the page served HTML with HTTP 200. All three service health
endpoints remained HTTP 200. Browser DOM/screenshot automation was unavailable;
UI delivery was verified through the actual Socket.IO events, persisted chat,
and decoded image, rather than claiming a visual browser automation run.

Local raw evidence, smoke scripts, and generated PNGs are under
`.git/image-tool-investigation/`; they contain no persisted bearer tokens and
are deliberately not committed as binaries. Failure fixtures cover malformed
serialized calls, missing/invalid native arguments, missing tools, backend
errors, false success text, bounded buffering, concurrent duplicate calls,
normal text chat, and deployment ownership/authentication/idempotence.

Final gates: 2,556 tests passed, one existing environment-dependent smoke skipped
(281.47 seconds); zero pytest warnings. The 23 focused filter/deployment cases
also passed independently. `ruff check .`, `ruff format --check .` (226 files),
and staged `git diff --check` passed. No installed third-party code, model
artifact, TUI, provisioning, or packaging source was modified.
