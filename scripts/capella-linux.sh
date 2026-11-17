#!/usr/bin/env bash
# Linux-only entry to the existing Hub CLI. No global Ollama, service or PATH install.
# The profile installed and shared is PEPITOS_BENCH_PROFILE (default capella), e.g.
#   PEPITOS_BENCH_PROFILE=pepe bash scripts/capella-linux.sh install /mnt/pepitos-bench/pepe-data
set -euo pipefail

BUNDLE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
ACTION="${1:-help}"
ROOT="${2:-}"
HUB_PYTHON="$BUNDLE/.venv/bin/python"
PROFILE="${PEPITOS_BENCH_PROFILE:-capella}"
[[ "$PROFILE" =~ ^[a-z0-9][a-z0-9-]*$ ]] || { echo "Invalid PEPITOS_BENCH_PROFILE: $PROFILE" >&2; exit 1; }

if [[ "$(uname -s)" != Linux || "$(uname -m)" != x86_64 ]]; then
  echo 'This bench requires Linux x86-64. Do not run it on your Windows PC.' >&2
  exit 1
fi

if [[ "$ACTION" == deps ]]; then
  if [[ "$EUID" != 0 ]] || ! command -v apt-get >/dev/null; then
    echo 'deps requires root on Debian/Ubuntu; otherwise provide Git, Python 3.11/3.12 with venv, libGL, GLib and libgomp yourself.' >&2
    exit 1
  fi
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates git python3 python3-venv libgl1 libglib2.0-0 libgomp1
  exit 0
fi

if [[ "$ACTION" == prepare ]]; then
  command -v git >/dev/null || { echo 'Git missing: run the deps action in the rented pod.' >&2; exit 1; }
  PYTHON=''
  for candidate in python3.11 python3.12 python3; do
    if command -v "$candidate" >/dev/null && "$candidate" -c 'import sys; raise SystemExit(not ((3,11) <= sys.version_info[:2] < (3,13)))'; then
      PYTHON="$candidate"
      break
    fi
  done
  [[ -n "$PYTHON" ]] || { echo 'Provide Python 3.11 or 3.12; Ubuntu 22.04 default Python 3.10 is not enough.' >&2; exit 1; }
  if [[ ! -x "$HUB_PYTHON" ]]; then
    "$PYTHON" -m venv "$BUNDLE/.venv"
  fi
  "$HUB_PYTHON" -m pip install --disable-pip-version-check -e "$BUNDLE"
  "$HUB_PYTHON" -m local_ai_stack hub commands
  echo 'Hub CLI prepared. No models downloaded and no inference started.'
  exit 0
fi

if [[ "$ACTION" == help || -z "$ROOT" ]]; then
  echo 'Profile: PEPITOS_BENCH_PROFILE=capella (default) | pepe | pepe-8gb, with its own dedicated root.'
  echo 'Usage: bash scripts/capella-linux.sh deps | prepare'
  echo '       bash scripts/capella-linux.sh check|install|start|stop|status|collect /absolute/dedicated/root [output.zip]'
  echo '       bash scripts/capella-linux.sh trace /absolute/dedicated/root   (follow the image chat trace live; Ctrl+C stops)'
  echo '       bash scripts/capella-linux.sh share /absolute/dedicated/root   (public link for a guest to sign up; Ctrl+C ends it)'
  echo '       bash scripts/capella-linux.sh swap-lab /absolute/dedicated/root LAB_DIR   (character swap lab, services started)'
  echo '       bash scripts/capella-linux.sh blend-lab /absolute/dedicated/root LAB_DIR   (light repaint on LAB_DIR/results; services started)'
  exit 0
fi
[[ "$ROOT" == /* && "$ROOT" != / ]] || { echo 'Use a dedicated absolute installation root, not /.' >&2; exit 1; }
[[ -x "$HUB_PYTHON" ]] || { echo 'Run the prepare action first.' >&2; exit 1; }
export PEPITOS_AI_HUB_ROOT="$ROOT"
export PYTHONUNBUFFERED=1

case "$ACTION" in
  check|install)
    exec "$HUB_PYTHON" -m local_ai_stack.linux_bench "$ACTION" --root "$ROOT" --profile "$PROFILE"
    ;;
  start|stop)
    exec "$HUB_PYTHON" -m local_ai_stack hub run "$ACTION" --wait
    ;;
  status)
    exec "$HUB_PYTHON" -m local_ai_stack hub state
    ;;
  collect)
    OUTPUT="${3:-$ROOT/exports/session-$(date -u +%Y%m%dT%H%M%SZ).zip}"
    exec "$HUB_PYTHON" -m local_ai_stack hub export-session --root "$ROOT" --output "$OUTPUT"
    ;;
  trace)
    # The image chat filter's turn trace, followed across rotation, one readable line per
    # record: local time, chat and turn ids, event, its key fields. TRACE_LINES sets how
    # much history is shown first (default 20).
    TRACE="$ROOT/data/open-webui/pepito-trace/turns.jsonl"
    echo "Following $TRACE (Ctrl+C stops)." >&2
    read -r -d '' FORMATTER <<'PY' || true
import json
import sys
from datetime import datetime

GIB = 1024**3
# The fields shown first for each event; any other field follows, shorter.
KEYS = {
    "turn_start": ("model", "action_requested", "toggles", "user_text"),
    "router_request": ("timeout_s", "resident_state"),
    "router_result": ("seconds", "source", "failure", "decision"),
    "gpu_wait": ("seconds", "reason"),
    "ollama_reload": ("reason",),
    "comfyui_stats": ("vram_free", "torch_vram_total"),
    "image_job": ("workflow", "part", "seconds", "outputs", "params"),
    "preservation": ("metrics",),
    "answer_usage": ("tokens_per_second", "eval_count", "prompt_eval_count", "load_duration"),
    "refusal": ("message",),
    "error": ("message",),
    "turn_end": ("total_seconds", "action"),
}
SKIP = {"ts", "chat_id", "turn", "event", "raw_json"}


def value(key, item, width=160):
    if isinstance(item, bool) or item is None:
        return str(item).lower()
    if isinstance(item, int) and ("vram" in key or "size" in key) and item >= 1 << 24:
        return f"{item / GIB:.1f}G"
    if isinstance(item, float):
        return f"{item:.2f}"
    if not isinstance(item, str):
        item = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
    item = " ".join(item.split())
    return item if len(item) <= width else item[: width - 3] + "..."


def models(listed):
    shown = []
    for model in listed if isinstance(listed, list) else ():
        if not isinstance(model, dict):
            continue
        size, vram = model.get("size") or 0, model.get("size_vram") or 0
        where = "all on GPU" if model.get("full_gpu") else f"{vram / GIB:.1f}G on GPU"
        shown.append(f"{model.get('name')} {size / GIB:.1f}G {where}")
    return "; ".join(shown) or "none"


def stamp(text):
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone().strftime("%H:%M:%S")
    except ValueError:
        return str(text)[:8]


def render(line):
    try:
        record = json.loads(line)
    except ValueError:
        return line.rstrip("\n")
    if not isinstance(record, dict):
        return line.rstrip("\n")
    event = str(record.get("event", "?"))
    head = (
        f"{stamp(record.get('ts'))} {str(record.get('chat_id') or '-')[:8]:8} "
        f"{str(record.get('turn') or '-')[:8]:8} {event:15}"
    )
    if event == "ollama_ps":
        return f"{head} {models(record.get('models'))}"
    first = KEYS.get(event, ())
    fields = [f"{key}={value(key, record[key])}" for key in first if key in record]
    fields += [
        f"{key}={value(key, item, 80)}"
        for key, item in record.items()
        if key not in SKIP and key not in first
    ]
    mark = "!! " if event in {"error", "refusal"} or record.get("failure") else ""
    return f"{head} {mark}{' '.join(fields)}"


try:
    for line in sys.stdin:
        if line.strip():
            print(render(line), flush=True)
except (KeyboardInterrupt, BrokenPipeError):
    pass
PY
    tail -n "${TRACE_LINES:-20}" -F "$TRACE" \
      | PYTHONIOENCODING=utf-8:replace "$HUB_PYTHON" -u -c "$FORMATTER"
    ;;
  share)
    # A temporary https link to Open WebUI only, for a guest to sign up as an ordinary user
    # (docs/testing/runbook-linux-1.17.md). Meant for a pod deleted afterwards.
    exec "$HUB_PYTHON" -m local_ai_stack.linux_share --root "$ROOT" --profile "$PROFILE"
    ;;
  swap-lab)
    # The character swap lab (docs/testing/swap-lab.md): LAB_DIR/cases.json through the
    # running ComfyUI, results and avaliar-troca.html in LAB_DIR/results.
    LAB_DIR="${3:-}"
    [[ -n "$LAB_DIR" && -f "$LAB_DIR/cases.json" ]] || {
      echo 'Usage: bash scripts/capella-linux.sh swap-lab /absolute/dedicated/root LAB_DIR (the folder with cases.json and inputs/).' >&2
      exit 2
    }
    LAB_PYTHON="$ROOT/apps/comfyui/.venv/bin/python"
    [[ -x "$LAB_PYTHON" ]] || { echo "ComfyUI venv missing ($LAB_PYTHON): run the install action first." >&2; exit 1; }
    exec "$LAB_PYTHON" -u "$BUNDLE/scripts/swap_lab.py" run --cases "$LAB_DIR/cases.json" \
      --root "$ROOT" --out "$LAB_DIR/results" "${@:4}"
    ;;
  blend-lab)
    # The blend lab: light masked Qwen repaints on LAB_DIR/results (a finished swap lab),
    # written to LAB_DIR/blend with avaliar-blending.html (docs/testing/swap-lab.md).
    LAB_DIR="${3:-}"
    [[ -n "$LAB_DIR" && -f "$LAB_DIR/cases.json" && -d "$LAB_DIR/results" ]] || {
      echo 'Usage: bash scripts/capella-linux.sh blend-lab /absolute/dedicated/root LAB_DIR (with cases.json and a swap lab results/).' >&2
      exit 2
    }
    LAB_PYTHON="$ROOT/apps/comfyui/.venv/bin/python"
    [[ -x "$LAB_PYTHON" ]] || { echo "ComfyUI venv missing ($LAB_PYTHON): run the install action first." >&2; exit 1; }
    exec "$LAB_PYTHON" -u "$BUNDLE/scripts/blend_lab.py" --cases "$LAB_DIR/cases.json" \
      --swap-results "$LAB_DIR/results" --root "$ROOT" --out "$LAB_DIR/blend" "${@:4}"
    ;;
  *)
    echo "Unknown action: $ACTION" >&2
    exit 2
    ;;
esac
