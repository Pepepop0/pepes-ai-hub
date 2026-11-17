"""What ComfyUI v0.39.0 saves for a SAM graph, for the fake ComfyUI of the swap tests.

``SAM3_Detect`` (comfy_extras/nodes_sam3.py) returns one mask per detection with
``individual_masks`` (none: an empty batch) or one union mask, all zeros when nothing was
found; ``MaskToImage`` keeps the batch; ``SaveImage`` (nodes.py) reads ``images[0]`` for the
file name, so an empty batch fails the whole prompt with an ``IndexError``.
"""

from __future__ import annotations

import io

from PIL import Image

EMPTY_BATCH = "index 0 is out of bounds for dimension 0 with size 0"


def saved_masks(graph: dict, found: dict[str, int]) -> dict[str, list[bool]]:
    """Per ``SaveImage`` fed by ``MaskToImage`` <- ``SAM3_Detect``, one flag per PNG it saves
    (True: something detected, False: a blank mask). ``found``: detections per prompt text
    (``"speech bubble"`` for ``speech bubble:1000``). Raises ComfyUI's ``IndexError``."""
    saved = {}
    for node_id, node in graph.items():
        if node["class_type"] != "SaveImage":
            continue
        to_image = graph[node["inputs"]["images"][0]]
        if to_image["class_type"] != "MaskToImage":
            continue
        detect = graph[to_image["inputs"]["mask"][0]]
        prompt = graph[detect["inputs"]["conditioning"][0]]["inputs"]["text"]
        count = found.get(prompt.rsplit(":", 1)[0], 0)
        masks = [True] * count if detect["inputs"]["individual_masks"] else [count > 0]
        if not masks:
            raise IndexError(EMPTY_BATCH)
        saved[node_id] = masks
    return saved


def history_item(prompt_id: str, graph: dict, found: dict[str, int]) -> dict:
    """The ``/history/<prompt_id>`` entry: the saved files (``-blank`` in a blank mask's
    name) or ComfyUI's execution error."""
    try:
        saved = saved_masks(graph, found)
    except IndexError as error:
        return {
            "status": {
                "status_str": "error",
                "completed": False,
                "messages": [
                    [
                        "execution_error",
                        {"exception_type": "IndexError", "exception_message": str(error)},
                    ]
                ],
            },
            "outputs": {},
        }
    return {
        "status": {"status_str": "success", "completed": True},
        "outputs": {
            node_id: {
                "images": [
                    {
                        "filename": f"{prompt_id}-{node_id}-{index}{'' if drawn else '-blank'}.png",
                        "subfolder": "",
                        "type": "output",
                    }
                    for index, drawn in enumerate(masks)
                ]
            }
            for node_id, masks in saved.items()
        },
    }


def mask_png(filename: str, size: tuple[int, int] = (320, 256)) -> bytes:
    """The mask PNG ``/view`` serves for ``filename``: black when blank, else a white blob."""
    image = Image.new("RGB", size, "black")
    if "-blank" not in filename:
        image.paste("white", (size[0] // 4, size[1] // 4, size[0] // 2, size[1] // 2))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()
