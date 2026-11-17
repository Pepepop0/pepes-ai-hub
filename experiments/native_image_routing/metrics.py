"""Per-trial classification and the aggregate routing metrics.

Every field is derived from wire evidence, never from the model's own claims:

- ``image_calls``: tool calls the model emitted, as Ollama parsed them (probe log)
- ``engine_runs``: ComfyUI ``/prompt`` submissions, each with a server nonce (fake ComfyUI log)
- ``truncated``: Ollama's own ``truncating input prompt`` warning inside the turn window
- ``prompt_tokens``: Ollama's ``prompt_eval_count`` for the first model call of the turn
"""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter, defaultdict

IMAGE_TOOLS = {"generate_image", "edit_image"}
# Tool-call syntax that should never reach the user as text.
RAW_JSON = re.compile(
    r'(<tool_call>|</tool_call>|"name"\s*:\s*"(generate_image|edit_image)"|'
    r'"arguments"\s*:\s*\{|\bgenerate_image\s*\(|\bedit_image\s*\(|\{\s*"prompt"\s*:)',
    re.IGNORECASE,
)


def classify(summary: dict, *, suite: str) -> dict:
    offered = set(summary.get("tools_offered") or [])
    calls = summary.get("model_tool_calls") or []
    image_calls = [c for c in calls if c[0] in IMAGE_TOOLS]
    generate_calls = [c for c in calls if c[0] == "generate_image"]
    edit_calls = [c for c in calls if c[0] == "edit_image"]
    malformed = []
    for name, arguments in calls:
        if name not in offered:
            malformed.append({"reason": "unknown tool", "name": name, "arguments": arguments})
            continue
        args = arguments if isinstance(arguments, dict) else {}
        if name in IMAGE_TOOLS:
            if not isinstance(args.get("prompt"), str) or not args.get("prompt", "").strip():
                malformed.append({"reason": "missing prompt", "name": name, "arguments": arguments})
            extra = set(args) - (
                {"prompt"} if name == "generate_image" else {"prompt", "image_urls"}
            )
            if extra:
                malformed.append(
                    {"reason": "unexpected arguments", "name": name, "arguments": arguments}
                )
            if name == "edit_image" and not args.get("image_urls"):
                malformed.append(
                    {"reason": "missing image_urls", "name": name, "arguments": arguments}
                )
    content = summary.get("content_full") or summary.get("content") or ""
    return {
        "suite": suite,
        "image_calls": len(image_calls),
        "generate_calls": len(generate_calls),
        "edit_calls": len(edit_calls),
        "other_tool_calls": [c[0] for c in calls if c[0] not in IMAGE_TOOLS],
        "engine_runs": len(summary.get("engine_prompts") or []),
        "malformed": malformed,
        "raw_json_leak": bool(RAW_JSON.search(content)),
        "empty": not content.strip(),
        "error": summary.get("error") is not None or not summary.get("done"),
        "truncated": bool(summary.get("truncations")),
        "prompt_tokens": (summary.get("prompt_eval_counts") or [None])[0],
    }


def aggregate(rows: list[dict]) -> dict:
    by_suite: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_suite[row["classification"]["suite"]].append(row["classification"])

    def rate(numerator: int, denominator: int) -> str:
        return (
            f"{numerator}/{denominator} ({100 * numerator / denominator:.1f}%)"
            if denominator
            else "n/a"
        )

    generate = by_suite.get("generate", [])
    chat = by_suite.get("chat", [])
    ambiguous = by_suite.get("ambiguous", [])
    everything = [r["classification"] for r in rows]

    true_positive = sum(1 for c in generate if c["generate_calls"] >= 1)
    exactly_once = sum(1 for c in generate if c["generate_calls"] == 1 and c["engine_runs"] == 1)
    false_positive = sum(1 for c in chat if c["image_calls"] >= 1)
    with_image_call = [c for c in everything if c["image_calls"] >= 1]
    tokens = [c["prompt_tokens"] for c in everything if isinstance(c["prompt_tokens"], int)]

    return {
        "trials": len(everything),
        "GENERATE RECALL": rate(true_positive, len(generate)),
        "GENERATE EXACTLY ONCE (call + engine run)": rate(exactly_once, len(generate)),
        "GENERATE PRECISION": rate(true_positive, true_positive + false_positive),
        "FALSE POSITIVE RATE (chat)": rate(false_positive, len(chat)),
        "FALSE NEGATIVE RATE (generate)": rate(len(generate) - true_positive, len(generate)),
        "DUPLICATE CALL RATE": rate(
            sum(1 for c in with_image_call if c["image_calls"] > 1 or c["engine_runs"] > 1),
            len(with_image_call),
        ),
        "MALFORMED CALL RATE": rate(sum(1 for c in everything if c["malformed"]), len(everything)),
        "RAW JSON LEAK RATE": rate(
            sum(1 for c in everything if c["raw_json_leak"]), len(everything)
        ),
        "EMPTY RESPONSE RATE": rate(sum(1 for c in everything if c["empty"]), len(everything)),
        "ERROR RATE": rate(sum(1 for c in everything if c["error"]), len(everything)),
        "PROMPT TOKENS (first call)": {
            "min": min(tokens) if tokens else None,
            "median": statistics.median(tokens) if tokens else None,
            "max": max(tokens) if tokens else None,
        },
        "TRUNCATION EVENTS": sum(1 for c in everything if c["truncated"]),
        "AMBIGUOUS": {
            "image_called": rate(
                sum(1 for c in ambiguous if c["image_calls"] >= 1), len(ambiguous)
            ),
        },
        "OTHER TOOLS CALLED": dict(
            Counter(name for c in everything for name in c["other_tool_calls"])
        ),
        "PER SUITE": {
            suite: {
                "trials": len(items),
                "image_called": rate(sum(1 for c in items if c["image_calls"] >= 1), len(items)),
                "generate_called": sum(c["generate_calls"] for c in items),
                "edit_called": sum(c["edit_calls"] for c in items),
                "engine_runs": sum(c["engine_runs"] for c in items),
                "empty": sum(1 for c in items if c["empty"]),
                "errors": sum(1 for c in items if c["error"]),
            }
            for suite, items in by_suite.items()
        },
    }


def load(path) -> list[dict]:
    return [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
