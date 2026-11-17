"""Bounded local deformation and runtime-resolved masked edit graphs.

Only Pillow and the standard library are used. These helpers never load weights,
contact ComfyUI, or decide whether an edit satisfied the user's instruction.
"""

from __future__ import annotations

import copy
import io
import json
import math
from collections import deque

from PIL import Image, ImageChops, ImageDraw, ImageFilter

WARP_MODES = ("magnify", "width", "height", "extend_down")
MAX_PIXELS = 16 * 1024 * 1024
MAX_BYTES = 32 * 1024 * 1024
COMPONENT_CELLS = 65536
MESH_STEP = 8


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _size(size):
    if (
        not isinstance(size, (tuple, list))
        or len(size) != 2
        or any(not isinstance(v, int) or isinstance(v, bool) or v < 1 for v in size)
        or size[0] * size[1] > MAX_PIXELS
    ):
        raise ValueError("Dimensões inválidas ou acima do limite de pixels.")
    return size


def _box(box, size, *, normalized=False):
    _size(size)
    if not isinstance(box, (tuple, list)) or len(box) != 4 or any(not _number(v) for v in box):
        raise ValueError("Coordenadas inválidas para a deformação.")
    x0, y0, x1, y1 = box
    width, height = (1000, 1000) if normalized else size
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise ValueError("A região de deformação está vazia ou fora da imagem.")
    return box


def _mode(mode):
    if mode not in WARP_MODES:
        raise ValueError("Modo de deformação inválido.")


def _factor(factor, *, expansion=False):
    if not _number(factor) or not (1 <= factor <= 4 if expansion else 0.625 <= factor <= 2):
        raise ValueError("Fator de deformação inválido.")


def box_to_pixels(box, size):
    """Convert a valid normalized 0..1000 box to outward-rounded pixel bounds."""
    x0, y0, x1, y1 = _box(box, size, normalized=True)
    width, height = size
    return (
        math.floor(x0 * width / 1000),
        math.floor(y0 * height / 1000),
        min(width, math.ceil(x1 * width / 1000)),
        min(height, math.ceil(y1 * height / 1000)),
    )


def expand_box(box, factor, size):
    """Expand pixel bounds about their center, clipped to the source canvas."""
    x0, y0, x1, y1 = _box(box, size)
    _factor(factor, expansion=True)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    rx, ry = (x1 - x0) * factor / 2, (y1 - y0) * factor / 2
    return (
        max(0, math.floor(cx - rx)),
        max(0, math.floor(cy - ry)),
        min(size[0], math.ceil(cx + rx)),
        min(size[1], math.ceil(cy + ry)),
    )


def extend_down_box(box, factor, size):
    """Expand sideways and down without including anything above the original top."""
    _x0, y0, _x1, y1 = _box(box, size)
    left, _, right, _ = expand_box(box, factor, size)
    return left, math.floor(y0), right, min(size[1], math.ceil(y0 + (y1 - y0) * (2 * factor - 1)))


def warp_region(box, expand, size, mode):
    """Pixel bounds for the warp; width/height modes leave the other axis unmoved."""
    _mode(mode)
    return (
        extend_down_box(box, expand, size)
        if mode == "extend_down"
        else expand_box(box, expand, size)
    )


def union_mask(size, boxes, expand=1.5, mode="magnify"):
    """Feather inside each allowed rectangle, never outside it (nor above a down warp)."""
    _size(size)
    _mode(mode)
    _factor(expand, expansion=True)
    boxes = list(boxes)
    if not boxes:
        raise ValueError("Selecione ao menos uma região para deformar.")
    hard = Image.new("L", size, 0)
    draw = ImageDraw.Draw(hard)
    for box in boxes:
        x0, y0, x1, y1 = warp_region(box_to_pixels(box, size), expand, size, mode)
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=255)
    return ImageChops.multiply(
        hard, hard.filter(ImageFilter.GaussianBlur(max(1, min(size) * 0.02)))
    )


def _smoothstep(value):
    value = min(1.0, max(0.0, value))
    return value * value * (3 - 2 * value)


def _source_point(x, y, box, region, factor, expand, mode, size):
    x0, y0, x1, y1 = box
    left, top, right, bottom = region
    if mode == "extend_down":
        if x < x0:
            weight = _smoothstep((x - left) / max(1e-6, x0 - left))
        elif x > x1:
            weight = _smoothstep((right - x) / max(1e-6, right - x1))
        else:
            weight = 1.0
        offset, length = y - top, bottom - top
        knee = min(factor * (y1 - y0), 0.85 * length)
        if bottom >= size[1] or offset <= knee:
            source = offset / factor
        else:
            source = knee / factor + (offset - knee) * (length - knee / factor) / (length - knee)
        return x, y - weight * (offset - source)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    rx, ry = (x1 - x0) * expand / 2, (y1 - y0) * expand / 2
    radius = math.hypot((x - cx) / rx, (y - cy) / ry)
    if radius >= 1 or radius == 0:
        return x, y
    # Piecewise increasing inverse radial map, including shrink factors. A smoothstep
    # gain interpolation can fold for shrinking; interpolate the radius instead.
    knee = 0.5 * min(1.0, factor)
    mapped = (
        radius / factor
        if radius <= knee
        else knee / factor + (radius - knee) * (1 - knee / factor) / (1 - knee)
    )
    gain = mapped / radius
    return (
        x if mode == "height" else cx + (x - cx) * gain,
        y if mode == "width" else cy + (y - cy) * gain,
    )


def _warp(image, boxes, factor, expand, mode):
    _size(image.size)
    _factor(factor)
    _factor(expand, expansion=True)
    _mode(mode)
    regions = [(box_to_pixels(box, image.size), box) for box in boxes]
    if not regions:
        raise ValueError("Selecione ao menos uma região para deformar.")
    warped = image.copy()
    if factor == 1:
        return warped
    for pixels, _ in regions:
        region = warp_region(pixels, expand, image.size, mode)
        left, top, right, bottom = region
        mesh = []
        for y in range(top, bottom, MESH_STEP):
            for x in range(left, right, MESH_STEP):
                xe, ye = min(x + MESH_STEP, right), min(y + MESH_STEP, bottom)
                quad = tuple(
                    v
                    for px, py in ((x, y), (x, ye), (xe, ye), (xe, y))
                    for v in _source_point(px, py, pixels, region, factor, expand, mode, image.size)
                )
                mesh.append(((x - left, y - top, xe - left, ye - top), quad))
        patch = warped.transform(
            (right - left, bottom - top), Image.Transform.MESH, mesh, Image.Resampling.BICUBIC
        )
        warped.paste(patch, (left, top))
    return warped


def _composite(original, warped, mask):
    result = Image.composite(warped, original, mask)
    if original.mode == "RGBA":
        result.putalpha(original.getchannel("A"))
    return result


def warp_regions(image, boxes, factor, expand=1.5, mode="magnify"):
    """PIL geometry for normalized boxes, with exact source pixels outside its union mask."""
    boxes = list(boxes)
    return _composite(
        image,
        _warp(image, boxes, factor, expand, mode),
        union_mask(image.size, boxes, expand, mode),
    )


def _load(data, *, mask=False):
    if not isinstance(data, bytes) or not data or len(data) > MAX_BYTES:
        raise ValueError("A imagem não está disponível ou excede 32 MiB.")
    try:
        with Image.open(io.BytesIO(data)) as image:
            _size(image.size)
            if mask and image.format != "PNG":
                raise ValueError("A máscara deve ser PNG.")
            image.load()
            return image.convert("L" if mask else "RGBA")
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError("Não consegui ler a imagem de deformação.") from exc


def _mask_boxes(mask):
    """Connected regions on at most 65536 cells; no all-target enclosing bbox.

    BOX averaging of a binary mask retains isolated pixels at this bounded source
    pixel budget. Grid cells conservatively widen component bounds, not authority:
    the original full-resolution mask is still applied at the final operation.
    """
    width, height = mask.size
    gw = min(width, max(1, int(math.sqrt(COMPONENT_CELLS * width / height))))
    gh = min(height, max(1, COMPONENT_CELLS // gw))
    gw = min(width, max(1, COMPONENT_CELLS // gh))
    small = mask.point(lambda v: 255 if v else 0).resize((gw, gh), Image.Resampling.BOX)
    cells = bytearray(small.tobytes())
    boxes = []
    for index in range(len(cells)):
        if not cells[index]:
            continue
        cells[index] = 0
        queue = deque([index])
        minx = maxx = index % gw
        miny = maxy = index // gw
        while queue:
            point = queue.popleft()
            x, y = point % gw, point // gw
            minx, maxx, miny, maxy = min(minx, x), max(maxx, x), min(miny, y), max(maxy, y)
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if 0 <= nx < gw and 0 <= ny < gh and cells[ny * gw + nx]:
                    cells[ny * gw + nx] = 0
                    queue.append(ny * gw + nx)
        boxes.append(
            [minx * 1000 / gw, miny * 1000 / gh, (maxx + 1) * 1000 / gw, (maxy + 1) * 1000 / gh]
        )
        if len(boxes) > 64:
            raise ValueError("A máscara tem regiões demais; simplifique a seleção.")
    if not boxes:
        raise ValueError("A área selecionada está vazia.")
    return boxes


def warp_image(source: bytes, mask_png: bytes, deformation: dict, *, boxes=None) -> bytes:
    """Apply an explicit shape plan; the approved mask alone authorizes changed pixels."""
    if (
        not isinstance(deformation, dict)
        or "mode" not in deformation
        or "factor" not in deformation
    ):
        raise ValueError("Informe modo e fator de deformação.")
    mode, factor = deformation["mode"], deformation["factor"]
    _mode(mode)
    _factor(factor)
    original, mask = _load(source), _load(mask_png, mask=True)
    if mask.size != original.size:
        raise ValueError("A máscara não corresponde às dimensões da origem.")
    if mask.getbbox() is None:
        raise ValueError("A área selecionada está vazia.")
    selected = _mask_boxes(mask) if boxes is None else list(boxes)
    warped = _warp(original, selected, factor, 1.5, mode)
    out = io.BytesIO()
    _composite(original, warped, mask).save(out, "PNG")
    return out.getvalue()


def harmonize_prompt(instruction: str) -> str:
    """Do not replay the original growth request on an already deformed input."""
    if not isinstance(instruction, str):
        raise TypeError("A instrução deve ser texto.")
    return (
        "Repair only local deformation artifacts in the supplied, already deformed image: "
        "restore coherent texture, edges, lighting and fine detail in the selected region. "
        "Keep its CURRENT size, proportions, silhouette and placement exactly; do not enlarge, "
        "shrink, lengthen or widen anything further. Preserve the existing style, colors, "
        "identity, pose, framing and everything outside the selected region."
    )


def masked_part(
    part: dict, *, family: str, denoise: float = 0.45, style: float | None = None
) -> dict:
    """Convert a resolved Krea generator or one-reference Qwen editor into masked img2img.

    The returned image node binding consumes [warped PNG, grayscale mask PNG], in
    that order. Model names and runtime seed/prompt bindings are never replaced.
    """
    if family not in {"krea2", "qwen21"} or not isinstance(part, dict):
        raise ValueError("A família não suporta edição mascarada.")
    if part.get("family") not in (None, family):
        raise ValueError("A família não corresponde ao workflow resolvido.")
    if not _number(denoise) or not 0 < denoise <= 1:
        raise ValueError("Denoise inválido.")
    if style is not None and not _number(style):
        raise ValueError("Estilo inválido.")
    result = copy.deepcopy(part)
    try:
        graph = json.loads(result["workflow"])
        nodes = result["nodes"]
        if not isinstance(graph, dict) or not isinstance(nodes, list) or not result.get("model"):
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("O workflow precisa estar resolvido com modelo e mapeamento.") from exc

    def unique(class_type):
        found = [key for key, body in graph.items() if body.get("class_type") == class_type]
        if len(found) != 1:
            raise ValueError(f"Workflow incompatível: esperado um {class_type}.")
        return found[0]

    def add(name, class_type, inputs):
        node = "pepito_" + name
        while node in graph:
            node += "_"
        graph[node] = {"class_type": class_type, "inputs": inputs}
        return node

    sampler_id = unique("KSampler")
    sampler = graph[sampler_id]["inputs"]
    mask_id = add("mask", "LoadImageMask", {"image": "", "channel": "red"})
    if family == "krea2":
        empty = unique("EmptyLatentImage")
        if sampler.get("latent_image") != [empty, 0]:
            raise ValueError("O gerador não usa o latente vazio esperado.")
        vae = unique("VAELoader")
        image_id = add("image", "LoadImage", {"image": ""})
        encode = add("encode", "VAEEncode", {"pixels": [image_id, 0], "vae": [vae, 0]})
        latent = [encode, 0]
        del graph[empty]
        # Width/height primitives become unreachable after removing the empty latent.
        for binding in nodes:
            if binding.get("type") in {"width", "height"}:
                for node_id in binding.get("node_ids", []):
                    node_id = str(node_id)
                    used = any(
                        isinstance(value, list) and value and value[0] == node_id
                        for body in graph.values()
                        for value in body.get("inputs", {}).values()
                    )
                    if not used:
                        graph.pop(node_id, None)
        sampler.update(steps=8, cfg=1, sampler_name="euler", scheduler="simple")
        if style is not None:
            binding = result.get("style")
            if not isinstance(binding, dict) or not binding.get("node_ids"):
                raise ValueError("O gerador não declara o controle de estilo.")
            for node_id in binding["node_ids"]:
                graph[str(node_id)]["inputs"][binding["key"]] = style
        result["steps"] = 8
    else:
        image_bindings = [binding for binding in nodes if binding.get("type") == "image"]
        if len(image_bindings) != 1 or len(image_bindings[0].get("node_ids", [])) != 1:
            raise ValueError("Qwen mascarado requer exatamente uma referência de origem.")
        image_id = str(image_bindings[0]["node_ids"][0])
        if graph.get(image_id, {}).get("class_type") != "LoadImage":
            raise ValueError("A referência Qwen precisa de um LoadImage.")
        encoder = unique("TextEncodeQwenImage21")
        if sampler.get("latent_image") != [encoder, 2] or graph[encoder]["inputs"].get(
            "images.image_1"
        ) != [image_id, 0]:
            raise ValueError("O canvas Qwen não vem da referência esperada.")
        graph[encoder]["inputs"]["resolution"] = 0
        # TextEncodeQwenImage21 output 2 is an EMPTY canvas, not its reference latents.
        # Low-denoise img2img must start from the actual warped pixels, encoded by the
        # same VAE used for reference conditioning and decoding.
        vae = graph[encoder]["inputs"].get("vae")
        if not isinstance(vae, list) or len(vae) != 2:
            raise ValueError("Qwen mascarado requer o VAE da referência.")
        encode = add("encode", "VAEEncode", {"pixels": [image_id, 0], "vae": vae})
        latent = [encode, 0]
    masked = add("masked_latent", "SetLatentNoiseMask", {"samples": latent, "mask": [mask_id, 0]})
    sampler.update(latent_image=[masked, 0], denoise=denoise)
    # These mappings must not overwrite source geometry or the fixed Krea sampler.
    omit = {"image", "width", "height"} | ({"steps"} if family == "krea2" else set())
    result["nodes"] = [binding for binding in nodes if binding.get("type") not in omit]
    result["nodes"].append({"type": "image", "key": "image", "node_ids": [image_id, mask_id]})
    if not any(binding.get("type") == "prompt" for binding in result["nodes"]):
        raise ValueError("O workflow não declara o prompt.")
    if not any(binding.get("type") == "seed" for binding in result["nodes"]) and not isinstance(
        result.get("seed"), dict
    ):
        # Exact replay may deliberately remove the runtime seed mapping after patching the
        # graph. Keep its explicit filter binding, if present, without inventing one.
        raise ValueError("O workflow não declara a semente.")
    # reference_policy stays the resolved part's own value; parent routing owns policy.
    result.update(workflow=json.dumps(graph, sort_keys=True), family=family, reference_count=2)
    return result
