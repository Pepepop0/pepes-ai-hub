"""Shared ComfyUI client, image metrics and reviewer for public Qwen laboratories."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

REPO = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO / "workflows"
CHANGED_LEVEL = 20
JOB_TIMEOUT = 1800.0
REVIEW_TEMPLATE = Path(__file__).with_name("lab_review.html")
REVIEW_ROUNDS = "review-rounds.json"

# Production and laboratory geometry have one implementation. The lab runs in the bare
# ComfyUI venv, so it loads the bundled module by path instead of importing the Hub.
ENGINE = REPO / "assets" / "open-webui" / "pepito_edit_methods.py"
if not ENGINE.is_file():
    raise RuntimeError(f"Shared edit engine missing: {ENGINE}")
_engine_spec = importlib.util.spec_from_file_location("_lab_common_pepito_edit_methods", ENGINE)
engine = importlib.util.module_from_spec(_engine_spec)
sys.modules[_engine_spec.name] = engine
_engine_spec.loader.exec_module(engine)


class LabError(RuntimeError):
    """A lab step that cannot continue, with a message meant for the operator."""


def _binary(mask: Image.Image, keep) -> Image.Image:
    return mask.point(lambda value: 255 if keep(value) else 0)


def _masked_mean(image: Image.Image, binary: Image.Image) -> float | None:
    if not binary.histogram()[255]:
        return None
    means = ImageStat.Stat(image, binary).mean
    return round(sum(means) / len(means), 4)


def fit_to(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    image = image.convert("RGB")
    return image if image.size == size else image.resize(size, Image.Resampling.LANCZOS)


def change_metrics(
    source: Image.Image, raw: Image.Image, mask: Image.Image
) -> tuple[Image.Image, dict]:
    """The composite over the source and the RGB mean absolute differences: raw inside
    (mask >= 128) and outside (mask == 0), composite inside, and the share of inside pixels
    where some channel moved by more than CHANGED_LEVEL."""
    source = source.convert("RGB")
    raw = fit_to(raw, source.size)
    final = Image.composite(raw, source, mask)
    inside = _binary(mask, lambda v: v >= 128)
    outside = _binary(mask, lambda v: v == 0)
    diff = ImageChops.difference(source, raw)
    red, green, blue = diff.split()
    peak = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    changed = _masked_mean(_binary(peak, lambda v: v > CHANGED_LEVEL), inside)
    return final, {
        "raw_mad_inside": _masked_mean(diff, inside),
        "raw_mad_outside": _masked_mean(diff, outside),
        "composite_mad_inside": _masked_mean(ImageChops.difference(source, final), inside),
        "changed_fraction_inside": None if changed is None else round(changed / 255, 4),
    }


def _workflow_path(name: str) -> Path:
    path = WORKFLOWS / name
    if path.is_file():
        return path
    raise LabError(f"Workflow graph not installed: {name}. Apply its optional pack first.")


def load_workflow(name: str) -> dict:
    return json.loads(_workflow_path(name).read_text(encoding="utf-8"))


def _inputs(graph: dict, node: str, class_type: str) -> dict:
    found = graph.get(node, {}).get("class_type")
    if found != class_type:
        raise LabError(f"Workflow node {node} is {found}, expected {class_type}.")
    return graph[node]["inputs"]


def _save_node(graph: dict) -> str:
    saves = [node for node, body in graph.items() if body.get("class_type") == "SaveImage"]
    if len(saves) != 1:
        raise LabError("The graph must have exactly one SaveImage node.")
    return saves[0]


def _request(
    url: str, data: bytes | None = None, headers: dict | None = None, timeout: float = 60
) -> bytes:
    request = urllib.request.Request(
        url, data=data, headers=headers or {}, method="GET" if data is None else "POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:3000]
        raise LabError(f"{url}: HTTP {error.code}: {detail}") from None
    except (urllib.error.URLError, OSError) as error:
        raise LabError(f"{url}: {error}") from None


def _json(url: str, body: dict | None = None, timeout: float = 60):
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {} if body is None else {"Content-Type": "application/json"}
    raw = _request(url, data, headers, timeout)
    return json.loads(raw) if raw.strip() else None


class ComfyUI:
    def __init__(self, url: str):
        self.url = url.rstrip("/")
        self.client_id = uuid.uuid4().hex

    def check(self) -> dict:
        return _json(f"{self.url}/system_stats", timeout=10)

    def free(self) -> None:
        _json(f"{self.url}/free", {"unload_models": True, "free_memory": True})

    def upload(self, path: Path, name: str) -> str:
        boundary = uuid.uuid4().hex
        parts = []
        for field, value in (("overwrite", "true"), ("type", "input")):
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"\r\n\r\n'
                f"{value}\r\n".encode()
            )
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="image"; '
            f'filename="{name}"\r\nContent-Type: image/png\r\n\r\n'.encode()
        )
        parts.append(path.read_bytes())
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        answer = json.loads(
            _request(
                f"{self.url}/upload/image",
                b"".join(parts),
                {"Content-Type": f"multipart/form-data; boundary={boundary}"},
                timeout=120,
            )
        )
        subfolder = answer.get("subfolder") or ""
        return f"{subfolder}/{answer['name']}" if subfolder else answer["name"]

    def execute(self, graph: dict, timeout: float = JOB_TIMEOUT) -> bytes:
        """Queue ``graph``, wait for its history entry and return the saved image bytes."""
        queued = _json(f"{self.url}/prompt", {"prompt": graph, "client_id": self.client_id})
        prompt_id = queued.get("prompt_id")
        if not prompt_id:
            raise LabError(f"ComfyUI did not queue the prompt: {queued}")
        deadline = time.monotonic() + timeout
        while True:
            # ComfyUI writes the history entry once the prompt ends, in success or error.
            entry = (_json(f"{self.url}/history/{prompt_id}") or {}).get(prompt_id)
            if entry:
                status = entry.get("status") or {}
                if status.get("status_str") == "error":
                    errors = [
                        message[1]
                        for message in status.get("messages") or ()
                        if message and message[0] == "execution_error"
                    ]
                    detail = "; ".join(
                        f"{e.get('node_type')} {e.get('node_id')}: {e.get('exception_message')}"
                        for e in errors
                    )
                    raise LabError(f"ComfyUI execution error: {detail or status}")
                break
            if time.monotonic() > deadline:
                try:
                    _json(f"{self.url}/interrupt", {})
                except LabError:
                    pass
                raise LabError(f"Timed out after {timeout:.0f}s.")
            time.sleep(1.0)
        images = (entry.get("outputs") or {}).get(_save_node(graph), {}).get("images") or []
        if not images:
            raise LabError("The prompt finished without a saved image.")
        image = images[0]
        query = urllib.parse.urlencode(
            {
                "filename": image["filename"],
                "subfolder": image.get("subfolder", ""),
                "type": image.get("type", "output"),
            }
        )
        return _request(f"{self.url}/view?{query}", timeout=120)


def unload_ollama(url: str) -> list[str]:
    """Unload resident models and require confirmation before any image job is queued."""
    url = url.rstrip("/")
    resident = _json(f"{url}/api/ps", timeout=10) or {}
    names = [m.get("name") or m.get("model") for m in resident.get("models") or ()]
    names = [name for name in names if name]
    for name in names:
        _json(f"{url}/api/generate", {"model": name, "keep_alive": 0}, timeout=120)
    deadline = time.monotonic() + 30
    while names:
        if not (_json(f"{url}/api/ps", timeout=10) or {}).get("models"):
            break
        if time.monotonic() >= deadline:
            raise LabError("Ollama still holds models; no image job was queued.")
        time.sleep(1.0)
    return names


def comfyui_log_tail(root: Path | None, lines: int = 30) -> str:
    """The newest ROOT/logs/comfyui*.log's last lines, attached to a failed variant."""
    if root is None:
        return ""
    try:
        logs = sorted((root / "logs").glob("comfyui*.log"), key=lambda p: p.stat().st_mtime)
        if not logs:
            return ""
        text = logs[-1].read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(text[-lines:])


def _open_rgb(path: Path) -> Image.Image | None:
    if not path.is_file():
        return None
    with Image.open(path) as picture:
        return picture.convert("RGB")


def review_round(out: Path, lab: str) -> str:
    """The round of ``lab`` in the folder ``out``: made when the folder's first page of that
    lab is written and kept in ``review-rounds.json``, so regenerating the page keeps its
    ratings and a new results folder starts empty."""
    path = out / REVIEW_ROUNDS
    rounds = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    if not isinstance(rounds.get(lab), str):
        rounds[lab] = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        out.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rounds, indent=2) + "\n", encoding="utf-8")
    return rounds[lab]


def review_page(out: Path, page: str, data: list[dict], *, lab: str, title: str) -> Path:
    """Write ``out/page``: the shared reviewer (``lab_review.html``) with ``data``.

    ``data`` lists cases ``{id, ask, instruction, source, references?, prompt?, variants}``;
    a variant is ``{id, label, note, status, ok, error, paths: {final, raw?}, base?, prompt?,
    meta?, seconds?, changed?, outside?, last_attempt?}``, paths relative to ``out``. A case
    with an ``ok`` variant flagged ``base`` (today's behaviour) shows it on the left and rates
    the others against it, ``--`` to ``++``; a case without one is rated S/P/N. ``prompt`` is
    shown under the picture; ``meta`` replaces the area-change line. The ratings are stored
    under one key per ``lab`` and round, and the export carries only this page's cases."""
    round_id = review_round(out, lab)
    config = {
        "lab": lab,
        "round": round_id,
        "title": title,
        "file": f"avaliacao-{lab}-{round_id}.md",
    }
    fills = {
        "/*__EDIT_LAB_DATA__*/[]": data,
        "/*__REVIEW_KEY__*/''": f"pepito-review:{lab}:{round_id}",
        "/*__REVIEW_PAGE__*/{}": config,
    }
    template = REVIEW_TEMPLATE.read_text(encoding="utf-8")
    for marker in fills:
        if template.count(marker) != 1:
            raise LabError(f"scripts/lab_review.html has no single {marker} marker.")
    page_text = re.sub(
        "|".join(re.escape(marker) for marker in fills),
        lambda found: json.dumps(fills[found.group(0)], ensure_ascii=False).replace("<", "\\u003c"),
        template,
    )
    out.mkdir(parents=True, exist_ok=True)
    path = out / page
    path.write_text(page_text, encoding="utf-8")
    return path


def retain_attempt(records: dict, variant_id: str, attempt: dict) -> None:
    """A dry run or failed rerun cannot erase an earlier successful result."""
    previous = records.get(variant_id)
    if previous and previous.get("status") == "ok" and attempt["status"] != "ok":
        previous["last_attempt"] = attempt
    else:
        records[variant_id] = attempt
