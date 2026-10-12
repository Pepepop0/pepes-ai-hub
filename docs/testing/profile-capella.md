> **Superseded (2026-10).** Capella moved to `orcarouter/Qwen3.8-27B-Uncensored:q5_K_M`
> and FLUX.2 Klein 9B FP8 with alternating VRAM residency; see
> `capella-5090-checklist.md`. The record below describes the earlier
> `qwen3.5:27b` + Klein 4B profile and is kept as history.

# Capella: the 32 GB profile, finalised

What the Capella profile was, what it is now, how its chat model and image
settings were chosen, and what was exercised on a fresh root
(`C:\Users\Pedro\Desktop\CAPELLA_TEST`) on the reference machine. The reference
machine is an RTX 5070 with 12 GB; everything the card's size decides is
**pending on a real RTX 5090** -- `capella-5090-checklist.md` is the list.

## Before and after

Before (`1871437`): `qwen3.5:9b` as chat model (status "tested_chat_tool_calling_unreliable_for_image",
from the tool-based image era), the same FLUX.2 Klein set as Pepe at Open
WebUI's defaults (512×512, 50 steps), `min_vram_gb: 32` with a 128 MiB
tolerance -- which **refuses the very card it is for**: an RTX 5090 reports
`32607MiB` to nvidia-smi ([NVIDIA forums](https://forums.developer.nvidia.com/t/nvidia-rtx-5090-not-detected-by-nvidia-smi-on-ubuntu-server-24-04/327409),
[openmm#4854](https://github.com/openmm/openmm/issues/4854)), 161 short of
32,768; with 128 of tolerance the wizard would have said INCOMPATIBLE and the
first run "Ambiente não está pronto". Reproduced on the test harness before
the change: `GPU reports 32607 MiB VRAM; profile requires 32768 MiB
(reporting tolerance: 128 MiB)`.

After: `qwen3.5:27b` (dense 27B, vision, native tool calling) with
`think: false`, `qwen3.5:9b` kept as the alternative; the same three image
files; text-to-image at **1024×1024, 20 steps**; `vram_tolerance_mib: 256`
(`tests/test_compatibility.py` checks 32,607 accepted, 24,564 and 12,227
refused); RAM 32 GB minimum / 64 GB recommended in the notes, no hard gate.
Same services, same five capabilities, same pet and theme.

## Three generic additions, all declared in the profile

Nothing branches on the profile's name. Three small mechanisms carry what
Capella asks for, and a profile that asks for nothing is configured exactly
as before (Pepe's and Pepinho's tests assert it):

1. `workflows.<id>.size` / `.steps` (`profiles.py`, `schemas/profile.schema.json`):
   validated in Open WebUI's own spelling (`WIDTHxHEIGHT`, steps ≥ 1) and
   emitted as `IMAGE_SIZE` / `IMAGE_STEPS` in the launch environment
   (`service_config.open_webui_image_configuration`). Open WebUI seeds its
   persisted settings from them on first start; the administrator may change
   them afterwards and the Hub does not fight that. Asking a graph for a size
   or a step count it does not map is a configuration problem, not a value
   silently ignored (`tests/test_service_config.py`).
2. `models.chat.params` (`profiles.py`, `open_webui_models.py`): named scalar
   parameters written into the managed model record's `params`, which Open
   WebUI's Ollama route applies to every call for that model id -- chat turns
   and its own background tasks (title, tags, follow-ups, image prompt). The
   record sync now also rewrites its own record when a declared parameter was
   changed, keeping any other parameter an administrator added
   (`tests/test_open_webui_models.py`, `tests/test_open_webui_integration.py`).
3. `vram_tolerance_mib` already existed; Capella just uses it honestly.

## Chat model: the candidates

Digests are the registry's manifest digests, read as for every other pin.

| model | disk | VRAM (4k ctx) | vision | tools | note |
| --- | --- | --- | --- | --- | --- |
| **`qwen3.5:27b`** | 17.4 GB | ~16.4 GB (Ollama `size` 16,342 MiB) | yes | yes | chosen |
| `qwen3.5:35b-a3b` | 23.9 GB | ~24 GB | yes | yes | MoE, fast, but 24 GB of the card and of RAM; too little left for the image models |
| `qwen3.5:9b` (was primary) | 6.6 GB | ~7 GB | yes | yes | kept as alternative |
| `qwen3-vl:32b` | 20.9 GB | ~21 GB | yes | yes | superseded by Qwen3.5 |
| `mistral-small3.2:24b` | 15.2 GB | ~15 GB | yes | yes | no reasoning mode; strong Portuguese; second choice |
| `gemma3:27b` | 17.4 GB | ~17 GB | yes | no | no tool calling in Ollama |
| `gpt-oss:20b` | 13.8 GB | ~14 GB | no | yes | no vision |
| `qwen3:32b`, `qwen2.5vl:32b` | 20-21 GB | ~21 GB | no / yes | yes | older generation |
| `hermes3:70b`, `llama3.3:70b` | 40-43 GB | -- | no | yes | do not fit |

`qwen3.5:27b`: the same family as the model Capella already named, one size
up; vision and tools without a second model; leaves the 5090 about 15 GB for
the image models and the desktop. Measured here with Ollama forced to split it
37/66 layers on the 12 GB card (5.7 tokens/s, so every time below is 8-10×
what a 5090 should show):

- **Thinking** (Ollama's default for Qwen3.5) costs 5-10× the tokens of the
  answer on plain chat (114-711 tokens for one-line answers) and ran to
  **2,684 tokens** when Open WebUI asked it to write one image prompt -- 473 s
  here, a minute on a 5090, before ComfyUI even starts. With `think: false`
  the same turns took 33-91 tokens and the answers were as good or better.
  Hence `models.chat.params: {think: false}` in the profile and on the record
  (`qwen3.5:27b` record: `params {"think": false}`, `builtin_tools` off).
- **Chat** (`flows.py` A1-A5): English and Portuguese fluent ("Uma GPU é uma
  unidade de processamento gráfico…"), memory across turns ("Your favourite
  vehicle is the Tiger tank").
- **Vision** (`vision.py`): a PNG attached to a plain turn -- "A imagem mostra
  uma maçã vermelha e brilhante posicionada sobre uma superfície branca lisa…";
  "A red coffee mug."; no ComfyUI job. With Image on, an uploaded picture plus
  "Make the apple blue." went to the edit graph with the upload as input
  (`35980fed…`), and "What colour is the apple now?" afterwards answered
  "Blue" from the edited picture. Vision and image editing do not collide; no
  capability workaround is needed (the record's `vision: True` is now true).
- **Tools** (the hardening probe, native mode, the composer's envelope,
  builtin tools off as the Hub configures): **33/33** -- 5/5 direct calls,
  10/10 natural-language extractions, 12/12 plain turns with the tool still
  selected and no call made, 3/3 two-turn sequences (`alpha`,1 then "do it
  again" `beta`,2); every call executed once with the right arguments and the
  real receipt (server nonce) shown to the user; no leaks, no duplicates, no
  hallucinated receipts. Hermes 8B on Pepe managed 4/20 direct before the
  hardening and was never reliable on natural phrasing. **Capella tools:
  ready.**

## Image stack: kept, driven harder

FLUX.2 Klein 4B fp8 + Qwen3 4B encoder + FLUX.2 VAE, the two shipped
workflows. Considered and not taken: FLUX.2 Klein 9B (same graph shape, but
about 18 GB of new downloads for both files and the text-encoder name is baked
into the graphs -- a workflow variant on top), FLUX.2 dev (does not fit next
to a 27B). Taken: the card's room. Text-to-image at the model's native
megapixel, **1024×1024**, and **20 steps** instead of the 50 Open WebUI
defaults to (the distilled model is built for 4; 20 is generous). Edits were
already at one megapixel and 4 steps.

On the reference card with the 27B holding 9 GB of it: generations 59-76 s,
edits 24-39 s -- ComfyUI in what was left, streaming weights. The castle at
1024×1024/20 steps (`Flux2-Klein_00009_.png`) is sharp and coherent. Outputs
all 1024×1024; jobs carry `steps 20`, width and height 1024.

## The flows, on the real root

Composer envelope, filter 0.5.0, `qwen3.5:27b` with `think: false`:

| turn | result |
| --- | --- |
| generate red apple | T2I `a73040d7`, 1024×1024, "…is now displayed in the chat above." |
| "Change the apple to green" | EDIT `db7d1b6b`, input = the apple (`6bfedc17…`) |
| tank, necromancer, mug | T2I `752752c8`, `701139c2`, `818618b2` |
| "Change the mug to red" | EDIT `06c50e45`, input = the mug (`f8cb7869…`) |
| "Change the background to black" | EDIT `fb31e854`, input = the red mug (`b26da4d7…`) |
| "Now an image of a medieval castle on a hill" | T2I `25190661`, no mug in it |
| "What is 2 + 2?" (Image off) | "2 plus 2 equals 4.", no job |

Every acknowledgement said the picture is shown; none said "I cannot".

## Install plan

12 steps like the other full profiles: check, three installs, four models,
two workflows, configure. Downloads: 12.45 GB of image models (the same bytes
as Pepe and Pepinho) plus 17.4 GB for `qwen3.5:27b` -- about 30 GB, against
17.1 GB for Pepe and 14.5 GB for Pepinho. The fresh root here took 21 minutes
with the image files already present (copied from the 8 GB root and verified
by SHA-256 by the installer: "Modelo já disponível"), 17.4 GB pulled.

## Lifecycle and second run

`lifecycle.py`: restart replaced every tree (Ollama 6844 → 30160, the
`llama-server` holding 11 GB of RAM gone; Open WebUI 3232 → 9392; ComfyUI
9392 → 3108), stop left no listener and no process, start came back on the
first try (26.5 s). Second `first_run`: every model "já disponível",
configuration unchanged, filter / model record / image editing "já atual",
`did_nothing=True`, 6.6 s.

## What this machine could not show

VRAM and RAM figures for a 5090. Here Ollama kept 9.2 GB of the card and up to
15.5 GB of RAM for the half of the model it could not place, ComfyUI worked in
2.5 GB, and free RAM touched 36 MiB during one generation. On a 5090 the whole
model sits in 16.4 GB of VRAM, ComfyUI has 15 GB, and Ollama's RAM is the
model's page cache. Those numbers, the timings, and the wizard's own
COMPATIBLE line are what the checklist collects.

## Scripts

`stack.py` (first run / start / stop / restart with 5090 facts injected into
the harness's `StackDependencies.detect_facts` -- the product's compatibility
check runs unchanged on them), `flows.py`, `vision.py`, `tools/harness.py`
with `plan_small.py` and `register_tool.py`, `lifecycle.py`, `monitor.py`,
`analyze.py`. Scratch, not product.
