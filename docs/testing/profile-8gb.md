# Pepe 8 GB: the full stack on an 8 GB NVIDIA card

What the `pepe-8gb` profile is, how its chat model was chosen, and what was
measured on a fresh root (`C:\Users\Pedro\Desktop\PEPITO_8GB_TEST`) with the
GPU held down to 8 GB.

## What the profile is

The same stack as Pepe -- Ollama, Open WebUI, ComfyUI; chat, vision, tools,
image generation, image editing; the same FLUX.2 Klein 4B set and the same two
workflows -- with one change: the chat model is `hermes3:3b` (2.0 GB on disk,
2.4 GiB resident) instead of `hermes3:8b` (4.7 GB, 4.8 GiB resident). Nothing
in the code knows the profile's name; it loads through `profiles/*.yaml` like
the other three, and its VRAM class comes from `hardware.min_vram_gb: 8` with
the same 128 MiB reporting tolerance the Test profile uses (8 GB cards report
8188-8192 MiB; a 6 GB card is refused, `tests/test_compatibility.py`).

Profiles are now listed by id rather than by file name
(`load_profiles`), so the wizard shows Capella, Pepe, Pepe 8 GB, Test.

## How the 8 GB was emulated

The reference machine has an RTX 5070 with 12,227 MiB. A separate CUDA process
(`ballast.py`, the ComfyUI venv's torch) held 4,008 MiB for the whole session,
leaving 8,192 MiB -- exactly what an 8 GB card reports -- for the desktop and
the stack. Ollama sizes its offload from free VRAM and ComfyUI from
`torch.cuda.mem_get_info`, so both saw an 8 GB card. `nvidia-smi` was polled
every 0.5 s (`monitor.py`) together with the RSS of the three services;
per-process VRAM is not available under WDDM, so the figures below are the
card's total minus the ballast, desktop included (about 1 GiB here).

## Chat model: the candidates

Every Ollama model already in the catalog is 8-9B. With one of those resident,
an 8 GB card has 2-3 GB left for a 4 GB diffusion model, an 8 GB text encoder
and the VAE, so a smaller chat model was the first thing to try. Pinned digests
are the registry's manifest digests, read the same way the catalog's existing
pins were checked (`hermes3:8b` → `4f6b83f30b62`, `qwen3:8b` →
`500a1f067a9f`, `qwen3.5:9b` → `6488c96fa5fa` all matched).

| model | disk | resident VRAM (4k ctx) | tools | vision | chat | image-turn acknowledgement (shipped context / firmer context) |
| --- | --- | --- | --- | --- | --- | --- |
| `hermes3:8b` (Pepe) | 4.66 GB | 4,779 MiB | yes | no | good | 5/5 with the firmer context; but VRAM oversubscribed, see below |
| `qwen3:8b` | 5.23 GB | ~5 GB | yes | no | -- | not tried: same class as Hermes 8B |
| `qwen3.5:9b` (Capella) | 6.59 GB | ~6.5 GB | yes | yes | -- | not tried: does not fit next to the image models |
| **`hermes3:3b`** | 2.02 GB | 2,436 MiB | yes | no | fine, verbose | 5/9 → **8/10** (both misses on the "necromancer" turn) |
| `llama3.2:3b` | 2.02 GB | 2,436 MiB | yes | no | fine, terse | 1/5 → **9/10** |
| `qwen3:4b` | 2.50 GB | 3,030 MiB | yes | no | good | 0/5 → 1/5; thinks for 35-45 s per image turn (one turn 230 s) |
| `qwen3.5:4b` | 3.39 GB | ~4 GB | yes | yes | -- | not tried: the same thinking mode, 1 GB more |

"Acknowledgement" is what the model says under a picture the system has just
attached. The shipped context (Open WebUI's own sentence) makes every 3-4B
model answer "I cannot generate or display images" under the very picture it
was asked for; Hermes 8B on Pepe never did. So the filter now, when a picture
really is attached to the reply, says the same thing more firmly
(`SHOWN_CONTEXT`, `pepito_image_chat.py` 0.5.0): the picture is attached and
visible to the user, the model does not see it and need not, say it is shown,
do not say you cannot. Four wordings were tried on the real root; the one
kept is the only one both 3B models follow (a variant with the sentence
appended after Open WebUI's made Hermes 3B echo the negation; a positive-only
variant flipped Llama 3.2 back to denying every turn). Hermes 8B follows it
too (5/5). The edited/created distinction of the original claim is kept.

Chosen: **`hermes3:3b`** -- Pepe's family (same ChatML template, same native
tool calling the hardening was measured on), smallest resident footprint, and
acknowledges the picture 8 times in 10 -- with `llama3.2:3b` pinned as the
alternative (same size, 9/10, terser). Hermes 8B was measured on the emulated
card for the record: it works, but only because Windows pages VRAM. Its 4.8 GiB
plus ComfyUI's needs exceed 8 GB, the driver evicts Ollama's weights to system
RAM during every image turn and pages them back on the next chat turn, and the
machine's free RAM fell to 766 MiB. The 3B stays resident through every image
turn with 0.5-1 GiB of the 8 GB to spare.

## Image stack: kept as Pepe's

FLUX.2 Klein 4B fp8 (4.07 GB), Qwen3 4B text encoder (8.04 GB, fp16 at load),
FLUX.2 VAE; text-to-image at Open WebUI's defaults (512×512, 50 steps), edits at
one megapixel and the distilled model's 4 steps. No lighter alternative exists
in the manifests, and none was needed: ComfyUI loads what fits (the text
encoder partially, the diffusion model fully after unloading it) and the whole
flow ran with the chat model resident. Every generation took 19-27 s and every
edit 12-15 s on the emulated card, the same as with Hermes 8B.

Not reduced, because nothing forced it: resolution, steps, the text encoder's
precision (`--fp8_e4m3fn-text-enc` would halve its 8 GB of RAM, at a cost in
prompt fidelity), the launch flags of ComfyUI. Each would be a new generic
mechanism; the measurements did not ask for one.

## Measurements

`flows.py`, the composer's own request envelope (captured from the UI),
`hermes3:3b`, the shipped filter, the ballast in place. One chat, then one
generation and its edit, then the mixed sequence in one chat, then a plain
question. VRAM is the emulated 8,192 MiB view (desktop included); RAM is the
service's working set.

| turn | seconds | VRAM peak | VRAM after | ComfyUI RSS | Ollama RSS |
| --- | --- | --- | --- | --- | --- |
| A1 `hi` | 8.2 | 2,563 | 2,563 | 9,013 | 821 |
| A2 remember "Pedro / Tiger tank" | 5.2 | 2,563 | 2,563 | 8,973 | 821 |
| A3 recall → "the Tiger tank, Pedro" | 5.2 | 2,565 | 2,565 | 8,973 | 822 |
| B generate red apple → T2I `4d869385` | 19.3 | 7,109 | 3,495 | 10,625 | 897 |
| C "Change the apple to green" → EDIT `f931483f`, input = B's PNG | 12.3 | 7,111 | 2,732 | 10,589 | 916 |
| D1 tiger tank → T2I `4961bf11` | 21.3 | 7,436 | 3,559 | 10,666 | 929 |
| D2 "Now an image of a necromancer" → T2I `ccdb787d` | 19.3 | 7,527 | 3,559 | 10,765 | 929 |
| D3 blue mug → T2I `e215c7de` | 19.3 | 7,527 | 3,495 | 10,429 | 928 |
| D4 "Change the mug to red" → EDIT `5f544d4d`, input = D3's PNG | 15.3 | 7,463 | 3,271 | 10,598 | 947 |
| D5 "Change the background to black" → EDIT `08cf8877`, input = D4's PNG | 15.3 | 7,527 | 3,239 | 10,400 | 947 |
| D6 "Now an image of a medieval castle" → T2I `a33bc2bf` | 19.3 | 7,367 | 3,564 | 10,483 | 962 |
| E "What is 2 + 2?" (Image off) → "4", no job | 5.2 | 3,572 | 3,572 | 8,773 | 1,031 |

Ollama reported `hermes3:3b` at 2,436 MiB of VRAM before, during and after
every image turn. Each edit's LoadImage input hashed equal to the previous
turn's output (`35980fed…`, `ebda015b…`, `b3ee5c56…`). All nine image turns
were acknowledged as pictures ("the modified image is now attached to this
message and can be seen in the chat"), none as "I cannot".

RAM: the stack adds about 12 GB to the machine -- ComfyUI keeps the text
encoder and the diffusion model in system memory between jobs, exactly as on
Pepe. On this 32 GB machine, with its usual desktop, free RAM bottomed at
2.0 GB during generation. A 16 GB machine would page; the profile's notes say
32 GB.

Real browser, same root (`ui-8gb-red-apple.png`, `ui-8gb-mug.png`): Image on
from the composer's Integrações menu; "Generate an image of a red apple on a
white table" → T2I `4bd194e0`, the picture shown in the chat; "Change the apple
to green" → EDIT `1a1910e9`, LoadImage input `ec349ace…` = the apple PNG; "Now
an image of a blue ceramic mug" → T2I `a4d8e7d1`, a mug and no apple.

## Lifecycle, second run, and a fix found on the way

`lifecycle.py`: restart replaced every tree (Ollama root 32520 → 32036, Open
WebUI 27244 → 29416, ComfyUI 7780 → 32860; no old pid of any service alive
afterwards, no old listener on 11434/8080/8188); stop left no listener and no
process of the root; start brought all three back in 25 s.

Start after stop failed on the first try, on this root and -- by the same
code -- on every profile: `start_stack` builds the launch specs before starting
anything, at which point Ollama is down, the chat model reads as "not-installed
(could not read the Ollama model store)", Open WebUI's chat configuration is
not ok, and Open WebUI gets "cannot be started: … is managed by this hub and
can be started". Restart never showed it because its detection snapshot was
taken while Ollama was still up. `_start_services` now forgets the model
detections and rebuilds the remaining launch specs once Ollama is up
(`tests/test_first_run.py::test_start_after_stop_configures_open_webui_from_the_ollama_it_just_started`,
which fails on `ef3b28c`). On the real root: stop, two seconds, start →
Ollama, Open WebUI (filter, model record and edit settings "já atual"),
ComfyUI, 25.6 s.

Second `first_run` on the installed root: every model "já disponível",
configuration unchanged, "Tudo pronto (nada a fazer)", `did_nothing=True`,
5.8 s. Nothing was reinstalled or downloaded again.

## Install plan against Pepe

Identical steps (11), identical image models (12.45 GB of managed files),
one different line: `ollama/hermes3_3b` (2.0 GB) instead of `ollama/hermes3_8b`
(4.7 GB). The first run on the fresh root took 17 minutes at ~29 MB/s,
2.5 of them for ComfyUI's git clone and torch, 4 for Open WebUI, 9 for the
models.

## Frozen build

`scriptsuild_windows.ps1` → `dist\PepitosAIHub\PepitosAIHub.exe`, `--version`
"Pepito's AI Hub 0.1.0", `--smoke` ok with profiles `capella, pepe, pepe-8gb,
test` and both workflows embedded; the bundled `pepe-8gb.yaml`, `models.yaml`
and filter are byte-identical to the checkout. The exe was then run for real
with `PEPITOS_AI_HUB_ROOT` at the test root (selection `pepe-8gb`, default
mode) after the installed filter had been drifted: "Abrir Open WebUI" read the
function, posted the shipped source back, read the model record and the image
config, opened the browser, and `q` ended it with no traceback and no process
left behind.

## Scripts

Everything above ran from the session's scratch directory: `stack.py`
(first run, start, stop, restart through `StackApplication` with
`PEPITOS_AI_HUB_ROOT` pointing at the test root), `ballast.py`, `monitor.py`,
`analyze.py`, `flows.py`, `acks.py`, `compare.py`, `lifecycle.py`,
`stop_start.py`. They are not part of the product.
