"""SPEC-109: a picture turn's trace explains the result: the seed, where the time went, the
change measured, the chat model's answer and the swap's own ComfyUI jobs."""

from __future__ import annotations

from test_image_gpu_serialization_and_trace import trace_records
from test_image_swap import (
    SAID,
    capella_stacks,  # noqa: F401
    edit_config,  # noqa: F401
    preset,  # noqa: F401
    routed,  # noqa: F401
    settings,  # noqa: F401
    swapping,  # noqa: F401
    world,  # noqa: F401
)


def numeric(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def test_a_long_answer_is_cut_like_every_other_traced_text(monkeypatch, tmp_path):
    from test_open_webui_image_filter import load_module

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    module = load_module()
    metadata = {"chat_id": "chat-3", "message_id": "m"}
    module["start_trace"](metadata)
    module["trace_event"](
        metadata, "answer_text", text="palavra " * (module["TRACE_TEXT_CHARS"] // 4)
    )
    text = trace_records(tmp_path)[-1]["text"]
    assert len(text) == module["TRACE_TEXT_CHARS"] + 1 and text.endswith("…")


def test_a_swap_traces_each_methods_stages_and_its_comfyui_jobs(swapping, tmp_path):  # noqa: F811
    turn, _, _, _, state, sources, _, _ = swapping
    reply = turn.turn(SAID, attach=sources)
    assert len(reply["files"]) == 2
    records = trace_records(tmp_path)
    methods = [r for r in records if r["event"] == "swap_method"]
    # The composite of the marked pass is traced when composed; the whole Qwen picture needs no analysis.
    assert [m["method"] for m in methods] == ["marked", "whole"]
    for method in methods:
        assert method["status"] == "ok" and numeric(method["gen_s"])
    stages = ("segment_s", "depth_s", "compose_s")
    assert all(numeric(methods[0][key]) for key in stages)
    assert all(methods[1][key] is None for key in stages)
    job = next(r for r in records if r["event"] == "image_job" and r["part"] == "swap")
    assert isinstance(job["params"]["seed"], int)
    assert all(numeric(job[key]) for key in ("unload_s", "job_s", "post_s"))
    comfy = [r for r in records if r["event"] == "swap_comfy_job"]
    assert len(comfy) == len(state["jobs"])
    assert all(r["status"] == "ok" and r["prompt_id"] and r["output_files"] for r in comfy)
    assert {r["step"] for r in comfy} == {"reference", "whole", "marked", "scene", "depth"}
    assert len({r["seed"] for r in comfy}) == len(comfy)
