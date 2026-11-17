"""Turn the image filter's trace (``turns.jsonl``) into replayable router cases.

    python scripts/router_cases.py <turns.jsonl>... --out tests/fixtures/router_cases.json

Every turn that has a ``router_result`` becomes one case: the user's words, the earlier
messages of the same chat, the router's answer exactly as written, the localizer's answer when
there was one, and what the session did (``observed``, for comparison, never the expectation).
Nothing that identifies a file, a user or a picture is kept: data URIs, file URLs and UUIDs
are replaced by neutral markers. ``tests/router_cases.py`` replays the cases through the filter.

The trace does not record the chat's picture list. It is inferred from what the router named
(``references[].image``, ``plan.base``) and from what each earlier picture job made, so a case
carries ``"images_inferred": true``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable
from pathlib import Path

MODEL_REPLY = "(resposta do modelo)"
CONSENT_MARK = "Antes de editar imagens com conteúdo adulto"
PICTURE_ACTIONS = ("new_image", "edit_image")

_DATA_URI = re.compile(r"data:[\w.+-]+/[\w.+-]+(?:;[\w=.+-]+)*,[A-Za-z0-9+/=%_-]*", re.IGNORECASE)
_FILE_URL = re.compile(r"(?:https?://[^\s\"')]*)?/(?:api/v1/)?files/[^\s\"')]*", re.IGNORECASE)
_UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE
)


def scrub(value):
    """``value`` with picture bytes, file URLs and ids replaced, recursively."""
    if isinstance(value, str):
        value = _DATA_URI.sub("[imagem]", value)
        value = _FILE_URL.sub("[arquivo]", value)
        return _UUID.sub("[id]", value)
    if isinstance(value, list):
        return [scrub(item) for item in value]
    if isinstance(value, dict):
        return {key: scrub(item) for key, item in value.items()}
    return value


# Optional substitutions for words that never change what the router decided, given with
# ``--neutral`` as a JSON list of ``[regex, neutral text]`` pairs. The public tree ships none;
# a case so changed says ``"edited": true``.
_MENTION = re.compile(r"\b(?:imagem|image)\s+(\d+)\b", re.IGNORECASE)


def load_neutral(path: Path | None) -> list[tuple[re.Pattern, str]]:
    if path is None:
        return []
    pairs = json.loads(path.read_text(encoding="utf-8"))
    return [(re.compile(pattern, re.IGNORECASE), neutral) for pattern, neutral in pairs]


def neutralize(value, table):
    if isinstance(value, str):
        for pattern, neutral in table:
            value = pattern.sub(neutral, value)
        return value
    if isinstance(value, list):
        return [neutralize(item, table) for item in value]
    if isinstance(value, dict):
        return {key: neutralize(item, table) for key, item in value.items()}
    return value


def read_records(path: Path) -> list[dict]:
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def group_turns(records: Iterable[dict]) -> list[list[dict]]:
    """The records of each turn, in the order the turns started."""
    turns: dict[str, list[dict]] = {}
    for record in records:
        if record.get("turn") is not None:
            turns.setdefault(record["turn"], []).append(record)
    return [events for events in turns.values() if events[0].get("event") == "turn_start"]


def session_name(path: Path) -> str:
    for part in reversed(path.resolve().parts):
        if part.startswith("sessao-"):
            return part
    return path.resolve().parent.name


def case_id(start: dict) -> str:
    """The turn's start in UTC, to the second: ``2026-11-10T01:58:25``."""
    return str(start["ts"])[:19]


def _job_size(size) -> list[int] | None:
    """A job's size, as ``{"width", "height"}`` or ``"WxH"``."""
    if isinstance(size, dict) and isinstance(size.get("width"), int):
        return [size["width"], size["height"]]
    match = re.fullmatch(r"(\d+)x(\d+)", size) if isinstance(size, str) else None
    return [int(match[1]), int(match[2])] if match else None


def _first(events: list[dict], name: str) -> dict | None:
    return next((event for event in events if event.get("event") == name), None)


def _named_images(raw: str | None) -> int:
    """The highest picture number the router's answer names."""
    try:
        answer = json.loads(raw or "")
    except ValueError:
        return 0
    if not isinstance(answer, dict):
        return 0
    numbers = [
        ref.get("image")
        for ref in answer.get("references") or []
        if isinstance(ref, dict) and isinstance(ref.get("image"), int)
    ]
    plan = answer.get("plan")
    if isinstance(plan, dict) and isinstance(plan.get("base"), int):
        numbers.append(plan["base"])
    mentioned = [int(number) for number in _MENTION.findall(raw or "")]
    return max((number for number in [*numbers, *mentioned] if number > 0), default=0)


def _sources(raw: str | None) -> set[int]:
    """The pictures an edit starts from, as the router named them (references, else base)."""
    try:
        answer = json.loads(raw or "")
    except ValueError:
        return set()
    if not isinstance(answer, dict):
        return set()
    refs = {
        ref["image"]
        for ref in answer.get("references") or []
        if isinstance(ref, dict) and isinstance(ref.get("image"), int)
    }
    base = (answer.get("plan") or {}).get("base")
    return refs or ({base} if isinstance(base, int) and base > 0 else set())


def _grounding(events: list[dict]) -> dict | None:
    refused = _first(events, "region_refused")
    if refused is not None:
        return refused.get("answer")
    region = _first(events, "region")
    if region is not None:
        return {
            "status": "local",
            "confidence": region.get("confidence"),
            "boxes": region.get("boxes") or [],
        }
    return None


class Chat:
    """What the chat held before a turn: messages, pictures, the recorded failed request."""

    def __init__(self) -> None:
        self.history: list[dict] = []
        self.owners: list[str] = []  # who made picture number i + 1: "user" or "assistant"
        self.sizes: list[list[int] | None] = []  # its size, when the trace tells
        self.generated: set[int] = set()  # the pictures the filter itself made from scratch
        self.failed: dict | None = None

    def snapshot(self, new_user_images: int, canvas: list[int] | None = None) -> dict:
        owners = self.owners + ["user"] * new_user_images
        sizes = [
            canvas if by == "user" and size is None else size
            for by, size in zip(owners, self.sizes + [None] * new_user_images, strict=True)
        ]
        return {
            "history": json.loads(json.dumps(self.history)),
            "user_images": list(range(len(self.owners) + 1, len(owners) + 1)),
            "images": [
                {
                    "number": number,
                    "by": by,
                    "current": number == len(owners),
                    "generated": number in self.generated,
                    "size": sizes[number - 1],
                }
                for number, by in enumerate(owners, 1)
            ],
        }

    def finish(self, events: list[dict], new_user_images: int, text: str) -> None:
        mine = list(range(len(self.owners) + 1, len(self.owners) + new_user_images + 1))
        self.owners += ["user"] * new_user_images
        self.sizes += [None] * new_user_images
        self.history.append({"role": "user", "content": text, **({"images": mine} if mine else {})})
        refusal = _first(events, "refusal")
        canvas = (_first(events, "swap_canvas") or {}).get("scene")
        if canvas:
            # The scene the swap worked on is one of the user's pictures.
            self.sizes = [
                canvas if by == "user" and size is None else size
                for by, size in zip(self.owners, self.sizes, strict=True)
            ]
        made: list[list[int] | None] = []
        for job in (event for event in events if event.get("event") == "image_job"):
            found = _job_size((job.get("params") or {}).get("size")) or canvas
            made += [found] * int(job.get("outputs") or 0)
        reply = {"role": "assistant", "content": refusal["message"] if refusal else MODEL_REPLY}
        result = _first(events, "router_result") or {}
        if made:
            current = len(self.owners)
            numbers = list(range(current + 1, current + len(made) + 1))
            reply["images"] = numbers
            self.owners += ["assistant"] * len(made)
            self.sizes += made
            # A new picture is the filter's own; an edit is, only if every source was.
            sources = _sources(result.get("raw_json")) or {current}
            if (result.get("decision") or {}).get("action") == "new_image" or (
                sources <= self.generated
            ):
                self.generated.update(numbers)
        self.history.append(reply)
        decision = result.get("decision")
        if made:
            self.failed = None
        elif (
            refusal
            and isinstance(decision, dict)
            and decision.get("action") in PICTURE_ACTIONS
            and result.get("source") != "correction"
        ):
            self.failed = decision


def build_cases(records: Iterable[dict], session: str, table=()) -> list[dict]:
    chats: dict[str, Chat] = {}
    consented = False
    cases = []
    for events in group_turns(records):
        start = events[0]
        chat = chats.setdefault(str(start.get("chat_id")), Chat())
        text = start.get("user_text") or ""
        result = _first(events, "router_result")
        refusal = _first(events, "refusal")
        if result is not None:
            named = _named_images(result.get("raw_json"))
            decision = result.get("decision") or {}
            wanted = max(named, 1 if decision.get("action") == "edit_image" else 0)
            new_images = max(0, wanted - len(chat.owners))
            end = _first(events, "turn_end") or {}
            cases.append(
                {
                    "id": case_id(start),
                    "session": session,
                    "user_text": text,
                    **chat.snapshot(new_images, (_first(events, "swap_canvas") or {}).get("scene")),
                    "images_inferred": True,
                    "model": start.get("model"),
                    "toggles": start.get("toggles") or [],
                    "consent": consented,
                    "router_raw": result.get("raw_json"),
                    "grounding_answer": _grounding(events),
                    "pending": chat.failed,
                    "observed": {
                        "action": end.get("action"),
                        "refusal": refusal["message"] if refusal else None,
                    },
                }
            )
        else:
            new_images = 0
        chat.finish(events, new_images, text)
        if (
            str(text).strip().lower() == "confirmo"
            and len(chat.history) >= 3
            and CONSENT_MARK in chat.history[-3]["content"]
        ):
            consented = True
    cleaned = []
    for case in scrub(cases):
        neutral = neutralize(case, table)
        cleaned.append({**neutral, "edited": True} if neutral != case else neutral)
    return cleaned


def merge(cases: Iterable[dict]) -> list[dict]:
    """One case per id (the first seen), oldest first."""
    seen: dict[str, dict] = {}
    for case in cases:
        seen.setdefault(case["id"], case)
    return [seen[key] for key in sorted(seen)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("turns", nargs="+", type=Path, help="turns.jsonl of a session")
    parser.add_argument("--out", type=Path, required=True, help="the cases file to write")
    parser.add_argument("--session", help="session name (default: the sessao-* folder)")
    parser.add_argument(
        "--neutral", type=Path, help="JSON list of [regex, text] substitutions (default: none)"
    )
    arguments = parser.parse_args(argv)
    table = load_neutral(arguments.neutral)
    cases = []
    for path in arguments.turns:
        cases += build_cases(read_records(path), arguments.session or session_name(path), table)
    merged = merge(cases)
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"{len(merged)} cases -> {arguments.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
