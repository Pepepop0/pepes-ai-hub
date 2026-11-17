"""Real turns of the image filter, replayed without a GPU, Ollama or ComfyUI.

``tests/fixtures/router_cases.json`` holds the turns the filter received in the pod sessions
(``scripts/router_cases.py`` makes it from their traces). ``load_cases`` picks some by id and
``run_case`` runs the filter's ``inlet`` on one, with the router, the localizer and the
pictures answered from the recording. Which result each turn deserves belongs to the tests
that use it; ``case["observed"]`` is only what happened in the session.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from test_open_webui_image_filter import load_module, run, ui_metadata

CASES_PATH = Path(__file__).resolve().parent / "fixtures" / "router_cases.json"
OLLAMA_URL = "http://127.0.0.1:11434"
USER = {"id": "router-cases-user"}
MODEL_REPLY = "(resposta do modelo)"
# The stack of the pod's chat model: edits with one picture or a scene and a character, a
# swap pass. No pose adapters, no styled generation.
STACK = {
    "family": "qwen_edit",
    "generate": {},
    "edit": {},
    "edit_refs": {"2": {"reference_policy": "scene_character"}},
    "swap": {},
}
PRESERVATION = Path(__file__).resolve().parents[1] / "assets" / "open-webui"
DEFAULT_SIZE = (1024, 1536)
# The picture record of the chat the last ``run_case`` ran (what the turn left in it).
LAST_RECORD: dict = {}


def load_cases(*ids: str) -> list[dict]:
    """The cases named by ``ids``, in that order; every case with no ids. An unknown id fails."""
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    if not ids:
        return cases
    by_id = {case["id"]: case for case in cases}
    missing = [one for one in ids if one not in by_id]
    if missing:
        raise KeyError(f"no router case {missing}; known: {sorted(by_id)}")
    return [by_id[one] for one in ids]


def picture_png(size: list[int] | None = None) -> bytes:
    """A gray picture of the size the trace recorded (a portrait one when it did not)."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", tuple(size or DEFAULT_SIZE), (120, 120, 120)).save(buffer, "PNG")
    return buffer.getvalue()


def picture_url(case: dict, number: int) -> str:
    return f"memory://router-case/{case['id']}/{number}"


def chat_messages(case: dict) -> list[dict]:
    """The chat as Open WebUI sends it: the history, then the user's words, each picture
    attached to the message that holds it."""

    def attach(message: dict, numbers: list[int]) -> dict:
        files = [{"type": "image", "url": picture_url(case, number)} for number in numbers]
        return {
            "role": message["role"],
            "content": message["content"],
            **({"files": files} if files else {}),
        }

    messages = [attach(message, message.get("images", [])) for message in case["history"]]
    messages.append(
        attach(
            {"role": "user", "content": case["user_text"]},
            case.get("user_images", []),
        )
    )
    return messages


class Recording:
    """Ollama as the recording answers it. Anything else on the network is a failure."""

    def __init__(self, case: dict) -> None:
        self.case = case
        self.router_calls = 0
        self.unanswered: list[str] = []
        self.forbidden: list[str] = []

    def __call__(self, request, timeout=None):
        url = request if isinstance(request, str) else request.full_url
        if not url.startswith(OLLAMA_URL):
            self.forbidden.append(url)
            raise AssertionError(f"the case tried to reach {url}")
        if url.endswith("/api/ps"):
            return io.BytesIO(b'{"models": []}')
        payload = json.loads(request.data)
        schema = payload.get("format") or {}
        if "action" in (schema.get("properties") or {}):
            self.router_calls += 1
            return io.BytesIO(
                json.dumps({"message": {"content": self.case["router_raw"]}}).encode()
            )
        # A vision call (the swap's planner, the medium of a picture): nothing was recorded
        # for it, so it fails the way an unreachable model does.
        self.unanswered.append(url)
        raise OSError("no recorded answer")


def localizer(case: dict):
    """``ground_regions`` answering from the recorded ``grounding_answer``: the module's own
    ``region_mask`` turns it into an area or into the refusal the session got."""
    spec = importlib.util.spec_from_file_location(
        "router_cases_preservation", PRESERVATION / "pepito_image_preservation.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def ground_regions(base_url, model, source, targets, instruction, num_ctx=None, timeout=40):
        del base_url, model, targets, instruction, num_ctx, timeout
        answer = case.get("grounding_answer")
        if answer is None:
            # The session did not record what its localizer said: it fails like an
            # unreachable vision model, which the filter must survive.
            raise module.RegionSelectionError("Não consegui localizar a região com segurança.")
        try:
            return module.region_mask(module._image(source).size, answer)
        except module.RegionSelectionError as exc:
            exc.answer = {
                "status": answer.get("status"),
                "confidence": answer.get("confidence"),
                "boxes": answer.get("boxes"),
                **({"people_count": answer["people_count"]} if "people_count" in answer else {}),
            }
            raise

    module.ground_regions = ground_regions
    return module


def run_case(case: dict, monkeypatch) -> tuple[dict, dict, list[dict]]:
    """The filter's ``inlet`` on ``case``: ``(body, metadata, trace_events)``.

    The router answers with the recorded ``router_raw``; the localizer with
    ``grounding_answer``; pictures are a gray rectangle; the swap's vision planning has no
    recording and fails like an unreachable model; reaching ComfyUI (or any other address)
    raises. ``consent`` (the user already confirmed) and ``pending`` (the refused request a
    "tente de novo" repeats) are put where the filter keeps them, and so are
    ``pending_question`` (asked just now unless it says when), ``last_edit`` (the recipe the
    chat delivered last; its ``sources`` and ``outputs`` are picture numbers) and
    ``refusals`` (the streak of the same plan refusal the turn starts with)."""
    module = load_module()
    recording = Recording(case)
    preservation = localizer(case)
    sizes = {image["number"]: image.get("size") for image in case.get("images", [])}

    async def picture_bytes(url):
        return picture_png(sizes.get(int(url.rsplit("/", 1)[-1])))

    async def preferences(_metadata, _user_id):
        return {
            "auto_region": True,
            "editor_model_id": None,
            "comparison_seed": None,
            "edit_method": "auto",
        }

    async def prepare_swap_job(slots, *_args, **_kwargs):
        # What the real job says about the decision: that it is a swap, and of which pictures.
        return {"part": "swap", "style": "photo", "images": [slot["url"] for slot in slots]}

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as data_dir:
        stacks = Path(data_dir) / "stacks.json"
        stacks.write_text(json.dumps({case["model"]: STACK}), encoding="utf-8")
        monkeypatch.setenv("DATA_DIR", data_dir)
        monkeypatch.setenv(module["STACKS_ENV"], str(stacks))
        monkeypatch.setenv("PEPITO_IMAGE_ROUTER", "true")
        monkeypatch.setenv("PEPITO_OLLAMA_URL", OLLAMA_URL)
        monkeypatch.setenv("PEPITO_COMFYUI_URL", "http://127.0.0.1:8188")
        monkeypatch.delenv("PEPITO_EXCLUSIVE_VRAM", raising=False)
        monkeypatch.setattr(urllib.request, "urlopen", recording)
        monkeypatch.setitem(sys.modules, "open_webui.pepito_image_preservation", preservation)
        monkeypatch.setitem(module, "picture_bytes", picture_bytes)
        monkeypatch.setitem(module, "image_preferences", preferences)
        monkeypatch.setitem(module, "prepare_swap_job", prepare_swap_job)

        metadata = ui_metadata()
        metadata["chat_id"] = f"temporary:{case['id']}"
        metadata["filter_ids"] = list(case.get("toggles") or [])
        chat_id = metadata["chat_id"]
        if case.get("consent"):
            module["record_consent"](USER["id"])
        made_here = [
            picture_url(case, image["number"])
            for image in case.get("images", [])
            if image.get("generated")
        ]
        if made_here:
            module["record_generated"](chat_id, made_here)
        record = module["picture_record"](chat_id)
        if case.get("pending"):
            record["failed_edit"] = json.loads(json.dumps(case["pending"]))
        if case.get("last_edit"):
            recipe = json.loads(json.dumps(case["last_edit"]))
            for key in ("sources", "outputs"):
                recipe[key] = [picture_url(case, number) for number in recipe[key]]
            record["last_edit"] = recipe
        if case.get("refusals"):
            record[module["REFUSALS_KEY"]] = dict(case["refusals"])
        if case.get("pending_question"):
            asked = json.loads(json.dumps(case["pending_question"]))
            asked.setdefault("asked_at", time.time())
            record["pending_question"] = asked
        LAST_RECORD.clear()
        LAST_RECORD["record"] = record
        numbers = [image["number"] for image in case.get("images", [])]
        current = next(
            (image["number"] for image in case.get("images", []) if image.get("current")), None
        )
        if current is not None and numbers and current != max(numbers):
            record["selected"] = module["picture_key"](picture_url(case, current))
            record["selected_after"] = module["picture_key"](picture_url(case, max(numbers)))

        body = {"model": case["model"], "messages": chat_messages(case)}
        # Recorded turns predate the Qwen redraw default and the E/C pair (SPEC-319). Replay
        # their historical Valves; current defaults are covered in test_image_redraw and
        # test_prompt_pair.
        valves = {"redraw_part": "restage_current", "prompt_pair": False}
        user = {**USER, "valves": {**valves, **case.get("valves", {})}}
        run(module["Filter"]().inlet(body, metadata, user))
        trace_file = Path(data_dir) / module["TRACE_DIR"] / module["TRACE_FILE"]
        events = (
            [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines()]
            if trace_file.exists()
            else []
        )
    assert not recording.forbidden, f"reached {recording.forbidden}"
    assert recording.router_calls <= 1, "the router is asked once per turn"
    return body, metadata, events
