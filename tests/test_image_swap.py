"""Two-source swaps run real stack graphs over fake HTTP; only G2 finals become files."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import sys
import types
import urllib.request
from pathlib import Path

import httpx
import pytest
from comfy_sam_fake import history_item, mask_png
from PIL import Image, ImageDraw
from test_image_edit_dispatch import (
    capella_stacks,  # noqa: F401
    edit_config,  # noqa: F401
    parse_multipart,
    preset,  # noqa: F401
    routed,  # noqa: F401
    settings,  # noqa: F401
    world,  # noqa: F401
)


def png(color, size=(320, 256)):
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "PNG")
    return out.getvalue()


PLAN = {
    "status": "ok",
    "scene_image": "A",
    "confidence": 0.98,
    "box": [200, 100, 700, 950],
    "target": "the woman on the left",
    "pose": "standing with her left arm raised",
    "character": "an adult woman with black hair and a red coat",
    "keep": "the other person, the furniture, speech bubbles and the background",
    "keep_boxes": [[10, 10, 200, 90]],
    "scene_medium": "photo",
    "character_medium": "art",
}
SAID = "troque esta personagem por esta"
DECISION = {
    "action": "edit_image",
    "edit_mode": "swap",
    "swap_medium": "scene",
    "prompt": "Replace the woman on the left with the character. Keep everything else unchanged.",
    "references": [
        {"image": 1, "role": "A", "purpose": "the scene and departing person's pose"},
        {"image": 2, "role": "B", "purpose": "the incoming character's identity and outfit only"},
    ],
    "plan": {
        "request_mode": "refine",
        "scope": "local",
        "region_targets": ["the woman on the left"],
        "base": 1,
        "changes": ["replace the woman"],
        "preserve": ["the background"],
        "correction": "none",
        "clarification": "",
    },
}
# What the vision writer of the whole-picture variation (LAB-04 V2) answers in these tests.
WRITTEN = "Draw the woman from the second picture in place of the woman on the left."


@pytest.fixture
def swapping(request, monkeypatch):
    stacks = request.getfixturevalue("capella_stacks")
    # Private swap tests opt into the pack's scene step through indirect parametrization.
    if not getattr(request, "param", False):
        stacks["qwen3-8-27b-q5"]["swap"].pop("scene", None)
    turn, _comfy, files, ollama = request.getfixturevalue("preset")("qwen3-8-27b-q5")
    ns = turn.filter.inlet.__globals__
    scene, character = png("white"), png("red", (256, 320))
    sources = [files.add(scene), files.add(character)]
    state = {
        "plan": dict(PLAN),
        "vision": [],
        "jobs": [],
        "uploads": {},
        "outputs": {},
        "compose": [],
        "fail": set(),
        "fail_ids": set(),
        "writer": [],
        # SAM detections per prompt in every analyzed picture (ComfyUI v0.39.0's saves).
        "found": {"person": 2, "speech bubble": 2, "text": 2},
        "silent": set(),
        "finals": [],
    }
    ollama.answers[SAID] = DECISION

    async def picture_bytes(url):
        return turn.load_image(url)[0]

    monkeypatch.setitem(ns, "picture_bytes", picture_bytes)

    def compose(scene_png, raw, scene_masks, raw_masks, depth, box, keep_boxes=(), *overlay):
        state["compose"].append(
            (scene_png, raw, scene_masks, raw_masks, depth, box, keep_boxes, *overlay)
        )
        final = png("lime" if len(state["compose"]) == 1 else "blue")
        state["finals"].append(final)
        return final, {"old_found": True, "new_found": True, "aligned_px": [0, 0]}

    module = types.ModuleType("open_webui.pepito_swap_compose")
    module.compose_swap = compose
    monkeypatch.setitem(sys.modules, module.__name__, module)

    def call(request, timeout=None):
        url = request if isinstance(request, str) else request.full_url
        parsed = httpx.URL(url)
        path = parsed.path
        if path == "/api/chat":
            body = json.loads(request.data)
            if body["messages"][-1].get("images"):
                if "format" not in body:  # the whole-picture writer: free text, no schema
                    state["writer"].append(body)
                    ollama.timeline.append("swap_writer")
                    return io.BytesIO(json.dumps({"message": {"content": WRITTEN}}).encode())
                state["vision"].append(body)
                ollama.timeline.append("swap_vision")
                return io.BytesIO(
                    json.dumps({"message": {"content": json.dumps(state["plan"])}}).encode()
                )
        if parsed.port != 8188:
            return ollama(request, timeout)
        ollama.timeline.append(path)
        if path == "/upload/image":
            fields = parse_multipart(
                httpx.Request(
                    "POST", url, content=request.data, headers=dict(request.header_items())
                )
            )
            item = fields["image"]
            state["uploads"][item["filename"]] = item["data"]
            return io.BytesIO(json.dumps({"name": item["filename"], "subfolder": ""}).encode())
        if path == "/prompt":
            graph = json.loads(request.data)["prompt"]
            classes = {node["class_type"] for node in graph.values()}
            kind = (
                "segment"
                if "SAM3_Detect" in classes
                else "depth"
                if "DA3Inference" in classes
                else "scene"
                if "Krea2EditGroundedEncode" in classes
                else "marked"
                if sum(node["class_type"] == "LoadImage" for node in graph.values()) == 2
                else "reference"
            )
            state["jobs"].append((kind, graph))
            prompt_id = str(len(state["jobs"]))
            state["outputs"][prompt_id] = (kind, graph)
            return io.BytesIO(json.dumps({"prompt_id": prompt_id}).encode())
        if path.startswith("/history/"):
            prompt_id = path.rsplit("/", 1)[-1]
            kind, graph = state["outputs"][prompt_id]
            if kind == "segment" and kind not in state["fail"]:
                item = history_item(prompt_id, graph, state["found"])
                for node_id in list(item["outputs"]):  # outputs ComfyUI did not report
                    if graph[node_id]["inputs"]["filename_prefix"] in state["silent"]:
                        del item["outputs"][node_id]
                return io.BytesIO(json.dumps({prompt_id: item}).encode())
            saves = [key for key, node in graph.items() if node["class_type"] == "SaveImage"]
            item = (
                {"status": {"status_str": "error", "completed": False}, "outputs": {}}
                if kind in state["fail"] or prompt_id in state["fail_ids"]
                else {
                    "status": {"status_str": "success", "completed": True},
                    "outputs": {
                        save: {
                            "images": [
                                {
                                    "filename": f"{prompt_id}-{save}-0.png",
                                    "subfolder": "",
                                    "type": "output",
                                }
                            ]
                        }
                        for save in saves
                    },
                }
            )
            return io.BytesIO(json.dumps({prompt_id: item}).encode())
        if path == "/view":
            filename = parsed.params["filename"]
            if state["outputs"][filename.split("-", 1)[0]][0] == "segment":
                return io.BytesIO(mask_png(filename))
            return io.BytesIO(png("gray"))
        return io.BytesIO(b"{}")

    monkeypatch.setattr(urllib.request, "urlopen", call)
    return turn, files, ollama, ns, state, sources, scene, character


def test_swap_router_schema_is_offered_only_by_swap_stacks(swapping):
    _, _, _, ns, _, _, _, _ = swapping
    plain = ns["router_schema"](False, ns["router_capabilities"]({}))
    assert "edit_mode" not in plain["properties"] and "swap_medium" not in plain["properties"]
    caps = ns["router_capabilities"]({"swap": {}})
    schema = ns["router_schema"](False, caps)
    assert schema["properties"]["edit_mode"]["enum"] == ["local", "swap"]
    assert schema["properties"]["swap_medium"]["enum"] == ["scene", "character"]
    assert schema["additionalProperties"] is False
    rules = ns["router_system"](caps)
    assert SAID in rules and "LEAVING A" in rules and "no estilo da personagem" in rules


def test_swap_turn_attaches_the_whole_qwen_picture_then_the_marked_composite(swapping):
    turn, files, ollama, ns, state, sources, _scene, _ = swapping
    reply = turn.turn(SAID, attach=sources)
    assert len(reply["files"]) == 2
    # Variation 1 is Qwen's picture as drawn (LAB-04), variation 2 the composite of the Qwen
    # pass with the marked box: the public Capella has no other swap step.
    assert [files.store[file["id"]][0] for file in reply["files"]] == [
        png("gray"),
        state["finals"][0],
    ]
    assert len(files.store) == 4, "no intermediate becomes an Open WebUI file"
    assert [kind for kind, _ in state["jobs"]] == [
        "reference",
        "marked",
        "marked",
        "segment",
        "segment",
        "depth",
    ]
    assert "scene" not in ns["image_stack"]("hermes3:8b")["swap"]
    parts = ns["image_stack"]("hermes3:8b")["swap"]
    whole, marked = state["jobs"][1][1], state["jobs"][2][1]

    def inputs(graph):
        binding = next(node for node in parts["marked"]["nodes"] if node["type"] == "image")
        return [graph[node]["inputs"][binding["key"]] for node in binding["node_ids"]]

    clean = inputs(marked)[1]
    assert state["uploads"][clean] == png("gray")
    whole_scene, person = (Image.open(io.BytesIO(state["uploads"][name])) for name in inputs(whole))
    canvas = ns["swap_whole_canvas"]((320, 256))
    # Qwen takes its output size from the first image: the scene sets LAB-04's canvas.
    assert whole_scene.size == canvas, "the plain scene at LAB-04's ~1 MP canvas"
    assert whole_scene.getpixel((10, 10))[:3] == (255, 255, 255), "no red outline"
    assert person.getpixel((10, 10))[:3] == (255, 0, 0), "the character itself, not cleaned"
    marked_scene = Image.open(io.BytesIO(state["uploads"][inputs(marked)[0]]))
    assert marked_scene.size == (320, 256), "the marked pass works on the scene as it is"
    whole_text = " ".join(str(node["inputs"].get("prompt", "")) for node in whole.values())
    assert WRITTEN in whole_text and "<image1> is" in whole_text, "V2: writer plus contract"
    assert len(state["compose"]) == 1 and len(state["compose"][0][2]) == 2
    assert state["compose"][0][5:7] == (PLAN["box"], PLAN["keep_boxes"])
    assert (
        ollama.timeline.index("swap_vision")
        < ollama.timeline.index("swap_writer")
        < ollama.timeline.index("/api/generate")
        < ollama.timeline.index("/prompt")
    ), "both vision calls happen before the chat model is unloaded"
    assert ollama.timeline[-1] == "/system_stats"
    recipe = ns["picture_record"]("chat-1")["last_edit"]
    assert recipe["swap_plan"]["box"] == PLAN["box"]
    assert [method["method_label"] for method in recipe["methods"]] == [
        "Qwen inteira",
        "Qwen com marcação",
    ]
    assert "Variação 1: Qwen inteira. Variação 2: Qwen com marcação" in reply["content"]
    writer = state["writer"][0]
    assert len(writer["messages"][-1]["images"]) == 2
    slots = json.loads(writer["messages"][-1]["content"])["reference_slots"]
    assert [slot["roles"] for slot in slots] == [["A"], ["B"]]
    assert slots[0]["purposes"] == [ns["SWAP_WHOLE_PURPOSES"][0]]
    vision = state["vision"][0]
    assert vision["format"]["additionalProperties"] is False
    assert set(vision["format"]["required"]) == set(PLAN)
    assert len(vision["messages"][-1]["images"]) == 2
    assert vision["think"] is False and vision["options"]["temperature"] == 0
    for encoded in vision["messages"][-1]["images"]:
        assert max(Image.open(io.BytesIO(base64.b64decode(encoded))).size) <= 1280


def test_the_whole_swap_canvas_is_lab_04s(swapping):
    ns = swapping[3]
    # The canvas LAB-04 rated (R1/R2 Qwen) for the 2438x2183 scene of the test of 15/11.
    assert ns["swap_whole_canvas"]((2438, 2183)) == (1120, 992)


def test_the_planner_fixes_a_router_that_gave_the_character_as_the_scene(swapping):
    # On the pod (08/11) the router took the character sheet for the scene; the planner,
    # which saw both pictures and "a loira pela de cabelo preto", knew better.
    turn, _, _, ns, state, sources, scene, character = swapping
    state["plan"]["scene_image"] = "B"
    reply = turn.turn(SAID, attach=sources)
    assert len(reply["files"]) == 2
    parts = ns["image_stack"]("hermes3:8b")["swap"]
    jobs = [graph for _, graph in state["jobs"][:3]]
    image = next(node for node in parts["marked"]["nodes"] if node["type"] == "image")
    whole_first = state["uploads"][jobs[1][image["node_ids"][0]]["inputs"][image["key"]]]
    assert Image.open(io.BytesIO(whole_first)).size == ns["swap_whole_canvas"]((256, 320))
    reference = next(node for node in parts["reference"]["nodes"] if node["type"] == "image")
    cleaned = jobs[0][reference["node_ids"][0]]["inputs"][reference["key"]]
    assert state["uploads"][cleaned] == scene, "the clean reference is made of the incoming person"
    assert all(call[0] == character for call in state["compose"])
    recipe = ns["picture_record"]("chat-1")["last_edit"]
    assert recipe["swap_plan"]["scene_image"] == "A"


def test_a_large_scene_is_generated_within_the_lab_bounds_and_composed_at_full_size(swapping):
    # On the pod (08/11) a 2438x2183 scene went to the generators as it was: Krea added the
    # character beside the old person instead of replacing her.
    _, _, _, ns, _, _, _, _ = swapping
    from open_webui.pepito_swap import REFERENCE_PIXELS, SCENE_PIXELS

    full, scene, character, marked, canvas = ns["_swap_inputs"](
        png("white", (2438, 2183)), png("red", (2000, 2000)), [200, 100, 700, 950]
    )
    sizes = [Image.open(io.BytesIO(data)).size for data in (full, scene, character, marked)]
    assert sizes[0] == (2438, 2183), "the composite goes back onto the full-size scene"
    assert sizes[1] == sizes[3] == canvas
    assert canvas[0] * canvas[1] <= SCENE_PIXELS and all(side % 16 == 0 for side in canvas)
    assert abs(canvas[0] / canvas[1] - 2438 / 2183) < 0.02
    assert sizes[2][0] * sizes[2][1] <= REFERENCE_PIXELS
    small = ns["_swap_inputs"](png("white"), png("red", (256, 320)), [200, 100, 700, 950])
    assert small[4] == (320, 256), "a scene within the bounds is never scaled up"


def test_a_retry_after_a_refused_edit_repeats_that_edit_not_the_swap_before_it(swapping):
    # On the pod (08/11): a swap was delivered, "aumenta os braços dela" was refused before
    # anything was made, and "tente de novo" redid the swap instead of the refused edit.
    turn, _, ollama, _, state, sources, _, _ = swapping
    turn.turn(SAID, attach=sources)
    planned = len(state["vision"])
    edit = {
        "action": "edit_image",
        "prompt": "Enlarge her arms. Keep everything else unchanged.",
        "references": [{"image": 99, "role": "A", "purpose": "the picture to change"}],
        "plan": {**DECISION["plan"], "base": 99, "region_targets": ["her chest"]},
    }
    ollama.answers["aumenta os braços dela"] = edit
    refused = turn.turn("aumenta os braços dela")
    assert not refused["files"]
    ollama.answers["tente de novo"] = {
        **edit,
        "plan": {**edit["plan"], "request_mode": "retry", "base": 3},
        "references": [{"image": 3, "role": "A", "purpose": "the picture to change"}],
    }
    retried = turn.turn("tente de novo")
    assert not retried["files"]
    assert retried["content"] == refused["content"], "the same refused request, asked again"
    assert len(state["vision"]) == planned, "the swap was not planned again"


@pytest.mark.parametrize(
    ("changes", "question"),
    [
        ({"status": "ambiguous"}, "Qual personagem devo trocar?"),
        ({"status": "no_person"}, "Pode enviar a cena"),
        ({"confidence": 0.89}, "identificação ficou incerta"),
        ({"box": [700, 100, 200, 900]}, "plano da troca ficou incompleto"),
    ],
)
def test_uncertain_planning_asks_in_portuguese_and_queues_nothing(swapping, changes, question):
    turn, _, ollama, _, state, sources, _, _ = swapping
    state["plan"].update(changes)
    reply = turn.turn(SAID, attach=sources)
    assert reply["files"] == [] and state["jobs"] == [] and state["uploads"] == {}
    assert question in reply["content"]
    assert "/api/generate" not in ollama.timeline


def test_painted_scene_area_supplies_box(swapping, monkeypatch):
    turn, _, _, ns, state, sources, _, _ = swapping
    mask = Image.new("L", (320, 256), 0)
    ImageDraw.Draw(mask).rectangle((32, 64, 159, 191), fill=255)
    data = io.BytesIO()
    mask.save(data, "PNG")

    async def painted(metadata, text):
        metadata[ns["MASK_KEY"]] = {"number": 1, "png": data.getvalue()}

    monkeypatch.setitem(ns, "painted_area", painted)
    turn.turn(SAID, attach=sources)
    expected = [100, 250, 500, 750]
    assert all(call[5] == expected for call in state["compose"])
    assert json.loads(state["vision"][0]["messages"][-1]["content"])["painted_box"] == expected


def test_a_pure_retry_reuses_the_recorded_plan_without_a_new_vision_call(swapping, monkeypatch):
    """A box pinned once by a painted area stays pinned when the swap is simply redone."""
    turn, _, ollama, ns, state, sources, _, _ = swapping
    mask = Image.new("L", (320, 256), 0)
    ImageDraw.Draw(mask).rectangle((32, 64, 159, 191), fill=255)
    data = io.BytesIO()
    mask.save(data, "PNG")
    painted_turns = [data.getvalue()]

    async def painted(metadata, text):
        if painted_turns:  # a one-shot area: only the first turn carries it
            metadata[ns["MASK_KEY"]] = {"number": 1, "png": painted_turns.pop()}

    monkeypatch.setitem(ns, "painted_area", painted)
    turn.turn(SAID, attach=sources)
    expected = [100, 250, 500, 750]
    assert len(state["vision"]) == 1 and len(state["compose"]) == 1
    # Asked again, the planner would now pick somebody else.
    state["plan"]["box"] = [600, 100, 900, 900]
    ollama.answers["de novo"] = {
        **DECISION,
        "plan": {"request_mode": "retry", "correction": "none"},
    }
    reply = turn.turn("de novo")
    assert len(reply["files"]) == 2
    assert len(state["vision"]) == 1, "a pure retry makes no new vision call"
    assert len(state["compose"]) == 2
    assert all(call[5] == expected for call in state["compose"])
    recipe = ns["picture_record"]("chat-1")["last_edit"]
    assert recipe["swap_plan"]["box"] == expected and recipe["swap_medium"] == "scene"


def test_the_whole_picture_failing_still_delivers_the_marked_composite(swapping):
    turn, files, _, ns, state, sources, _, _ = swapping
    state["fail_ids"].add("2")  # the whole picture runs on the marked pass's Qwen graph
    reply = turn.turn(SAID, attach=sources)
    assert len(reply["files"]) == len(state["compose"]) == 1
    assert files.store[reply["files"][0]["id"]][0] == state["finals"][0]
    assert "Não consegui concluir Qwen inteira; entreguei a outra versão" in reply["content"]
    recipe = ns["picture_record"]("chat-1")["last_edit"]
    assert [method["method"] for method in recipe["methods"]] == ["marked"]


def test_the_marked_pass_failing_still_delivers_the_whole_picture_without_analysis(swapping):
    turn, files, _, ns, state, sources, _, _ = swapping
    state["fail_ids"].add("3")  # reference, the whole picture, then the marked pass
    reply = turn.turn(SAID, attach=sources)
    assert len(reply["files"]) == 1 and state["compose"] == []
    assert files.store[reply["files"][0]["id"]][0] == png("gray")
    assert "Não consegui concluir Qwen com marcação; entreguei a outra versão" in reply["content"]
    assert not [kind for kind, _ in state["jobs"] if kind in ("segment", "depth")], (
        "nothing to compose: no people or depth analysis"
    )
    recipe = ns["picture_record"]("chat-1")["last_edit"]
    assert [method["method"] for method in recipe["methods"]] == ["whole"]


def test_both_methods_failing_attaches_nothing(swapping):
    turn, files, _, _, state, sources, _, _ = swapping
    state["fail"].add("marked")  # both variations use Qwen's two-image graph
    reply = turn.turn(SAID, attach=sources)
    assert reply["files"] == [] and len(files.store) == 2
    assert "Não consegui gerar a troca" in reply["content"]


def test_character_medium_choice_reaches_the_marked_prompt(swapping):
    turn, _, ollama, _, state, sources, _, _ = swapping
    ollama.answers[SAID] = {**DECISION, "swap_medium": "character"}
    turn.turn(SAID, attach=sources)
    graph = state["jobs"][2][1]  # the marked pass: the whole picture ran first
    text = " ".join(str(node["inputs"].get("prompt", "")) for node in graph.values())
    assert "exact medium and visual style of the second image" in text


def test_route_normalization_cannot_enable_swap_on_an_unsupported_stack(swapping):
    _, _, _, ns, _, _, _, _ = swapping
    payload = {"messages": [{"role": "user", "content": f"LAST USER MESSAGE: {SAID}"}]}
    decision = ns["_route_sync"]("http://127.0.0.1:11434", payload, ns["router_capabilities"]({}))
    assert decision["edit_mode"] == "local" and "swap_medium" not in decision
    decision = ns["_route_sync"](
        "http://127.0.0.1:11434", payload, ns["router_capabilities"]({"swap": {}})
    )
    assert decision["edit_mode"] == "swap" and decision["swap_medium"] == "scene"


def test_a_compositor_failure_never_exposes_raw_and_the_whole_picture_survives(
    swapping, monkeypatch
):
    turn, files, _, _, _, sources, _, _ = swapping
    module = sys.modules["open_webui.pepito_swap_compose"]

    def compose(*args):
        raise ValueError("Não encontrei a personagem gerada.")

    monkeypatch.setattr(module, "compose_swap", compose)
    reply = turn.turn(SAID, attach=sources)
    assert len(reply["files"]) == 1 and len(files.store) == 3
    assert files.store[reply["files"][0]["id"]][0] == png("gray"), "the whole picture, as drawn"
    assert "Não consegui concluir Qwen com marcação; entreguei a outra versão" in reply["content"]


def test_planner_bounds_both_thumbnails_without_resizing_generator_inputs(swapping):
    from open_webui.pepito_swap import plan_swap

    _, _, _, _, state, _, _, _ = swapping
    plan_swap(
        "http://127.0.0.1:11434",
        "hermes3:8b",
        png("white", (2000, 1500)),
        png("red", (1600, 2000)),
        ["the woman"],
        SAID,
        num_ctx=8192,
    )
    call = state["vision"][0]
    assert call["options"]["num_ctx"] == 8192
    for encoded in call["messages"][-1]["images"]:
        assert max(Image.open(io.BytesIO(base64.b64decode(encoded))).size) == 1280


@pytest.mark.parametrize(
    "form",
    ["{id}", "/api/v1/files/{id}/content", "http://127.0.0.1:8080/api/v1/files/{id}/content"],
)
def test_an_uploaded_picture_is_read_by_its_bare_id_or_its_url(tmp_path, monkeypatch, form):
    # The composer keeps only the file id on an uploaded picture's message; the swap
    # failed with "Não consegui ler as duas imagens" on the pod (08/11).
    from test_image_dispatch import load_filter

    from local_ai_stack.open_webui_filters import filter_source

    file_id = "882cb988-443b-4327-bb19-10e10eecd5d0"
    stored = tmp_path / "scene.png"
    stored.write_bytes(png("white"))

    class Files:
        @staticmethod
        async def get_file_by_id_and_user_id(identifier, user_id):
            if identifier == file_id and user_id == "a":
                return types.SimpleNamespace(path=str(stored), user_id="a")
            return None

    class Storage:
        @staticmethod
        def get_file(path):
            return path

    monkeypatch.setitem(sys.modules, "open_webui.models.files", types.SimpleNamespace(Files=Files))
    monkeypatch.setitem(
        sys.modules, "open_webui.storage.provider", types.SimpleNamespace(Storage=Storage)
    )
    scope = load_filter(filter_source()).inlet.__globals__
    picture_bytes = scope["picture_bytes"]

    async def in_turn(url):
        scope["_TURN_USER"].set("a")
        return await picture_bytes(url)

    assert asyncio.run(in_turn(form.format(id=file_id))) == stored.read_bytes()
    assert asyncio.run(in_turn("not-a-file")) is None


@pytest.fixture
def two_users(tmp_path, monkeypatch):
    from test_image_dispatch import load_filter

    from local_ai_stack.open_webui_filters import filter_source

    mine = "11111111-1111-4111-8111-111111111111"
    theirs = "22222222-2222-4222-8222-222222222222"
    stored = {}
    for owner, file_id, color in (("a", mine, "white"), ("b", theirs, "black")):
        path = tmp_path / f"{owner}.png"
        path.write_bytes(png(color))
        stored[file_id] = types.SimpleNamespace(path=str(path), user_id=owner)

    class Files:
        @staticmethod
        async def get_file_by_id(identifier):
            return stored.get(identifier)

        @staticmethod
        async def get_file_by_id_and_user_id(identifier, user_id):
            file = stored.get(identifier)
            return file if file is not None and file.user_id == user_id else None

    Storage = types.SimpleNamespace(get_file=lambda path: path)
    monkeypatch.setitem(sys.modules, "open_webui.models.files", types.SimpleNamespace(Files=Files))
    monkeypatch.setitem(
        sys.modules, "open_webui.storage.provider", types.SimpleNamespace(Storage=Storage)
    )
    scope = load_filter(filter_source()).inlet.__globals__
    return scope, mine, theirs, stored


def as_user(scope, user, call):
    async def run():
        scope["_TURN_USER"].set(user)
        return await call

    return asyncio.run(run())


def test_a_turn_reads_and_writes_only_its_own_users_files(two_users):
    scope, mine, theirs, stored = two_users
    own = Path(stored[mine].path)
    other = Path(stored[theirs].path)
    before = other.read_bytes()

    assert as_user(scope, "a", scope["picture_bytes"](f"/api/v1/files/{mine}/content")) == (
        own.read_bytes()
    )
    assert as_user(scope, "a", scope["picture_bytes"](theirs)) is None
    assert (
        as_user(scope, "a", scope["replace_picture"](f"/api/v1/files/{theirs}/content", b"x"))
        is False
    )
    assert other.read_bytes() == before
    assert (
        as_user(scope, "a", scope["replace_picture"](f"/api/v1/files/{mine}/content", b"new"))
        is True
    )
    assert own.read_bytes() == b"new"


def test_inline_pictures_need_no_owner(two_users):
    scope, *_ = two_users
    data = png("white")
    url = "data:image/png;base64," + base64.b64encode(data).decode()
    assert as_user(scope, "a", scope["picture_bytes"](url)) == data


def test_outside_a_turn_a_file_is_not_read_nor_written(two_users):
    scope, mine, theirs, stored = two_users
    path = Path(stored[theirs].path)
    before = path.read_bytes()
    assert asyncio.run(scope["picture_bytes"](theirs)) is None
    assert asyncio.run(scope["picture_bytes"](mine)) is None
    assert asyncio.run(scope["replace_picture"](f"/api/v1/files/{theirs}/content", b"x")) is False
    assert path.read_bytes() == before


def test_the_request_hook_runs_a_job_as_its_own_user_in_a_fresh_context(two_users):
    scope, mine, theirs, stored = two_users
    filter_ = scope["Filter"]() if "Filter" in scope else None
    seen = {}

    async def generate(job, metadata, request, user, emit):
        seen["other"] = await scope["picture_bytes"](theirs)
        seen["own"] = await scope["picture_bytes"](mine)
        seen["write"] = await scope["replace_picture"](f"/api/v1/files/{theirs}/content", b"x")

    filter_._generate = generate
    metadata = {scope["STATE_KEY"]: {"generate": {"job": 1}}}
    body = {"messages": [{"role": "user", "content": "x"}]}
    asyncio.run(filter_.request(body, metadata, __user__={"id": "a"}))

    assert seen["other"] is None
    assert seen["write"] is False
    assert seen["own"] == Path(stored[mine].path).read_bytes()


def test_the_scene_analysis_asks_for_bubbles_and_text_by_default_unless_switched_off(swapping):
    turn, _, _, _, state, sources, _, _ = swapping
    turn.turn(SAID, attach=sources, valves={"swap_overlay_detection": False})
    for kind, graph in state["jobs"]:
        if kind == "segment":
            texts = [n["inputs"].get("text") for n in graph.values() if "text" in n["inputs"]]
            assert texts == ["person:1000"], "off: the people only, the job of today"
    assert len(state["compose"]) == 1 and all(len(args) == 7 for args in state["compose"])

    state["jobs"].clear()
    state["compose"].clear()
    turn.turn(SAID, attach=sources)
    segments = [graph for kind, graph in state["jobs"] if kind == "segment"]
    assert len(segments) == 2, "one analysis of the scene and one of the marked Qwen output"
    scene = segments[0]
    assert sorted(n["inputs"]["text"] for n in scene.values() if "text" in n["inputs"]) == [
        "person:1000",
        "speech bubble:1000",
        "text:1000",
    ]
    for graph in segments[1:]:
        assert [n["inputs"]["text"] for n in graph.values() if "text" in n["inputs"]] == [
            "person:1000"
        ], "the generators' outputs are analyzed for the people only"
    assert len(state["compose"]) == 1
    for scene_png, raw, scene_masks, raw_masks, depth, box, keep_boxes, overlay in state["compose"]:
        assert len(scene_masks) == 2 and len(overlay) == 2, "one union mask per kind"


@pytest.mark.parametrize("comfy", ["blank masks", "no overlay outputs"])
def test_a_scene_without_bubbles_or_text_still_swaps_with_the_overlay_by_shape(
    swapping, monkeypatch, comfy
):
    turn, _, _, ns, state, sources, _, _ = swapping
    state["found"].update({"speech bubble": 0, "text": 0})
    if comfy == "no overlay outputs":
        state["silent"] = {"PepitoSwap/overlay_bubble", "PepitoSwap/overlay_text"}
    events = []
    monkeypatch.setitem(
        ns, "trace_event", lambda metadata, event, **fields: events.append((event, fields))
    )
    reply = turn.turn(SAID, attach=sources, valves={"swap_overlay_detection": True})
    assert len(reply["files"]) == 2 and "Variação 1" in reply["content"]
    assert [args[7] for args in state["compose"]] == [[]], "nothing detected, nothing passed"
    assert all(len(args[2]) == 2 for args in state["compose"]), "the people still found"
    detection = [fields for event, fields in events if event == "swap_overlay_detection"]
    assert len(detection) == 1 and detection[0]["masks"] == 0
    assert detection[0]["blank"] == (2 if comfy == "blank masks" else 0)
