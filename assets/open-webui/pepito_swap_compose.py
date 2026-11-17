"""G2 swap compositor: put a swap's raw output back into the scene without cut marks.

The swap lab's final (rounds 1-2) pasted the raw output through one feathered mask: the
target's box joined with what changed. Measured on rounds 1-2 (05/11/2026), the marks the
user reported come from three causes, each with a fix here:

1. **Color drift.** Krea darkens the whole frame by 5-26 levels per channel and Qwen by
   1-7; at the feather ring the step between raw and scene is almost all low-frequency.
   The rectangles around T1/T8's speech bubbles and Krea's "pasted on the screen" look in
   T7/T9 are that step. Fix: ``drift_field`` measures scene - raw on the background around
   the region and spreads it, smoothly, over the region before compositing.
2. **Other people repainted.** Krea erased the right man's arm in T3 and the box-joined
   mask carried that. Fix: ``protection`` keeps the scene's pixels of every other person
   who is in front of the replaced one where they meet (nearer in the scene's depth).
3. **Ghosts of the old person.** Where a hard box or a grown mask cut through a limb, the
   feather mixed both bodies (T7 shoulder, T4 hand). Fix: the region always contains the
   old person's whole silhouette and the new one's, plus changes attached to them (hats,
   held things), so the feather lies on background.

The chat's character swap calls ``compose_swap`` in a worker thread with ComfyUI's SAM 3.1
person masks and Depth Anything 3 depth; ``scripts/swap_composite.py`` (the lab) imports
the same functions, so there is one implementation. numpy, OpenCV and Pillow only.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field

import cv2
import numpy as np
from PIL import Image

FRONT_MARGIN = 1.03  # an other person is "in front" when this much nearer at the contact
OVERLAY_LEVEL = 28  # a scene pixel is overlay content (bubble, text) this far from its blur
DRIFT_OUTLIER = 30  # band pixels this far from the band's median drift are content, not drift
KEPT_LEVEL = 15  # overlay the model left this close stays the scene's own
OVERLAY_REWRITTEN = 0.35  # an overlay piece with fewer pixels changed than this is kept whole
MASK_THRESHOLD = 127  # a mask PNG pixel above this is the person
ANALYSIS_PIXELS = 2_000_000  # larger scenes are analyzed at this size (SPEC-313)
FINAL = "G2"


# --- pure image math -----------------------------------------------------------------


def dilate(hard: np.ndarray, grow: int) -> np.ndarray:
    if grow <= 0:
        return hard.astype(bool)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * grow + 1, 2 * grow + 1))
    return cv2.dilate(hard.astype(np.uint8), kernel) > 0


def feather(hard: np.ndarray, grow: int, blur: float) -> np.ndarray:
    grown = dilate(hard, grow).astype(np.float32)
    return np.clip(cv2.GaussianBlur(grown, (0, 0), max(0.5, blur)), 0, 1)


def drift_field(scene: np.ndarray, raw: np.ndarray, band: np.ndarray) -> np.ndarray:
    """Smooth scene - raw, measured on ``band`` (background where both should agree) and
    spread by normalized convolution; far from any measurement, the drift of the nearest
    measurements at coarser scales.

    Band pixels far from the band's median drift are content the model changed (a thigh
    of the old person that the detector left outside her silhouette), not drift: counted,
    they spread a brown "burned spot" over T8 (07/11 marks). Far from the band the band's
    mean used to fill in, and in T8 it lit a glow around the new head where the band
    nearby said darker ("ajuste de cor intenso", E2 marks)."""
    weight = band.astype(np.float32)
    if not weight.any():
        return np.zeros_like(scene)
    median = np.median((scene - raw)[band], axis=0)
    weight *= np.abs(scene - raw - median).max(axis=2) <= DRIFT_OUTLIER
    if not weight.any():
        return np.broadcast_to(median, scene.shape).astype(np.float32)
    h, w = scene.shape[:2]
    sigma = max(6.0, min(h, w) / 25)
    diff = (scene - raw) * weight[..., None]
    # The field is smooth at ``sigma``: it is measured on an image 8 times smaller.
    size = (max(1, round(w / 8)), max(1, round(h / 8)))
    small_diff = cv2.resize(diff, size, interpolation=cv2.INTER_AREA)
    small_weight = cv2.resize(weight, size, interpolation=cv2.INTER_AREA)
    field = np.broadcast_to(diff.sum(axis=(0, 1)) / float(weight.sum()), small_diff.shape)
    for scale in (8, 4, 2, 1):  # coarse to fine, each trusted where it has measurements
        num = cv2.GaussianBlur(small_diff, (0, 0), sigma * scale / 8)
        den = cv2.GaussianBlur(small_weight, (0, 0), sigma * scale / 8)[..., None]
        trust = np.clip(den / 0.05, 0, 1)
        field = num / np.maximum(den, 1e-6) * trust + field * (1 - trust)
    return cv2.resize(field.astype(np.float32), (w, h), interpolation=cv2.INTER_LINEAR)


def align_to_scene(
    scene: np.ndarray, raw: np.ndarray, new: np.ndarray | None, background: np.ndarray
) -> tuple[np.ndarray, np.ndarray | None, float]:
    aligned, new, moved, _ = _align(scene, raw, new, background)
    return aligned, new, moved


def _align(
    scene: np.ndarray, raw: np.ndarray, new: np.ndarray | None, background: np.ndarray
) -> tuple[np.ndarray, np.ndarray | None, float, np.ndarray | None]:
    """``raw`` (and the new person's mask) moved onto the scene by the affine map that best
    matches their backgrounds, and how far the map moved the farthest corner, in pixels.

    The models return the scene slightly zoomed and shifted: Qwen-Edit rescales its
    reference to about 1 MP internally (Q4 was 0.3% narrower and 0.45% taller, 4 px at the
    floor; Q8 up to 15 px), Krea shifts a few pixels (K6 4 px). Every seam then joined two
    backgrounds a few pixels apart: the baseboard and windowsill breaks in T4, the chain and
    the shoe in T6 ("erros de continuidade", F2 marks). A fit that fails, or that would
    move the image by more than 3% (a model that redrew the scene), leaves ``raw`` as is."""
    h, w = scene.shape[:2]
    if background.mean() < 0.1:
        return raw, new, 0.0, None
    gray = [
        cv2.GaussianBlur(
            cv2.cvtColor(np.clip(i, 0, 255).astype(np.uint8), cv2.COLOR_RGB2GRAY), (0, 0), 1.5
        ).astype(np.float32)
        for i in (scene, raw)
    ]
    warp = np.eye(2, 3, dtype=np.float32)
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 100, 1e-5)
    try:
        _, warp = cv2.findTransformECC(
            gray[0], gray[1], warp, cv2.MOTION_AFFINE, criteria, background.astype(np.uint8), 5
        )
    except cv2.error:
        return raw, new, 0.0, None
    corners = np.array([[0, 0], [w, 0], [0, h], [w, h]], np.float32)
    moved = float(np.hypot(*(corners @ warp[:, :2].T + warp[:, 2] - corners).T).max())
    if moved > 0.03 * min(h, w):
        return raw, new, 0.0, None
    flags = cv2.WARP_INVERSE_MAP
    aligned = cv2.warpAffine(
        raw, warp, (w, h), flags=flags | cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE
    )
    if new is not None:
        new = (
            cv2.warpAffine(new.astype(np.uint8), warp, (w, h), flags=flags | cv2.INTER_NEAREST) > 0
        )
    return np.clip(aligned, 0, 255).astype(np.float32), new, round(moved, 1), warp


def protection(
    old: np.ndarray, others: list[np.ndarray], near: np.ndarray, grow: int
) -> tuple[np.ndarray, list[str]]:
    """The other people's pixels a swap must keep: those of anyone in front of the old
    person where they meet. People behind or apart are left to the change mask (protecting
    them cut the new person's glove in T6; a false detection of a curtain cut a leg in T4)."""
    h, w = near.shape
    protected = np.zeros((h, w), bool)
    decisions = []
    reach = max(grow * 3, round(min(h, w) * 0.04))
    near_old = dilate(old, reach)
    for full in others:
        hard = full & ~old
        contact = hard & near_old
        if contact.sum() < 50:
            decisions.append("apart")
            continue
        # The old person's own pixels next to the contact: where two detections overlap,
        # the overlap is the occluder's, not the old person's.
        partner = old & ~full & dilate(hard, reach)
        if partner.sum() < 50:
            decisions.append("apart")
            continue
        front = bool(np.median(near[contact]) > np.median(near[partner]) * FRONT_MARGIN)
        if front:
            protected |= full  # its whole body, also where it covered the old person
        decisions.append("front" if front else "behind")
    return protected, decisions


def overlay_mask(scene: np.ndarray, boxes) -> np.ndarray:
    """The overlay content (speech bubbles, text) inside each keep box, by shape: pixels far
    from a blur of the scene, closed and filled. Rectangular keep areas left a visible box
    around the bubbles in T1/T8 (07/11 ratings); the background between them now follows
    the composite like the rest."""
    h, w = scene.shape[:2]
    found = np.zeros((h, w), bool)
    if not boxes:
        return found
    background = cv2.GaussianBlur(scene, (0, 0), max(4.0, min(h, w) / 40))
    # Grain is not content: on T8's grainy art, grain specks closed into patches that kept
    # the scene's grain over the model's smooth background (E2 marks).
    denoised = cv2.medianBlur(np.clip(scene, 0, 255).astype(np.uint8), 3).astype(np.float32)
    deviation = np.abs(denoised - background).max(axis=2) > OVERLAY_LEVEL
    close = max(2, round(min(h, w) * 0.008))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * close + 1, 2 * close + 1))
    for box in boxes:
        inside = box_mask(box, (w, h))
        content = cv2.morphologyEx((deviation & inside).astype(np.uint8), cv2.MORPH_CLOSE, kernel)
        # Fill enclosed holes (the bubble's interior around its letters) from the box edge.
        flood = content.copy()
        rows, cols = np.nonzero(inside)
        if rows.size == 0:
            continue
        y0, y1, x0, x1 = rows.min(), rows.max() + 1, cols.min(), cols.max() + 1
        window = np.pad(flood[y0:y1, x0:x1], 1)
        mask = np.zeros((window.shape[0] + 2, window.shape[1] + 2), np.uint8)
        cv2.floodFill(window, mask, (0, 0), 2)
        holes = window[1:-1, 1:-1] == 0
        filled = content[y0:y1, x0:x1].astype(bool) | holes
        found[y0:y1, x0:x1] |= filled
    return dilate(found, 2)


def detected_overlay_kept(
    overlay: np.ndarray, covered: np.ndarray, near: np.ndarray, old: np.ndarray, grow: int
) -> tuple[np.ndarray, list[str]]:
    """The detected overlay (bubbles, text) the scene keeps, and each piece's verdict:
    "clear" (the new person does not touch it), "behind" or "front". A piece is the scene's
    whole, except where ``covered`` (the new person's silhouette) lies on it and the new
    person is in front: the old person's depth near the piece, whom the new one replaces
    in the same pose, is nearer than the piece's own (the same ``FRONT_MARGIN`` order
    ``protection`` uses for other people)."""
    h, w = overlay.shape
    kept = overlay.copy()
    order: list[str] = []
    count, labels, stats, _ = cv2.connectedComponentsWithStats(overlay.astype(np.uint8))
    reach = max(grow * 3, round(min(h, w) * 0.04))
    for index in range(1, count):
        x, y, bw, bh = (int(v) for v in stats[index, :4])
        y0, y1, x0, x1 = (
            max(0, y - reach),
            min(h, y + bh + reach),
            max(0, x - reach),
            x + bw + reach,
        )
        piece = labels[y0:y1, x0:x1] == index
        inside = piece & covered[y0:y1, x0:x1]
        if not inside.any():
            order.append("clear")
            continue
        depth = near[y0:y1, x0:x1]
        beside = old[y0:y1, x0:x1] & dilate(piece, reach)
        if not beside.any():
            beside = old[y0:y1, x0:x1]
        if not beside.any():
            order.append("behind")
            continue
        front = bool(np.median(depth[beside]) > np.median(depth[piece]) * FRONT_MARGIN)
        order.append("front" if front else "behind")
        if front:
            kept[y0:y1, x0:x1] &= ~inside
    return kept, order


EXTEND_LEVEL = 30  # a change that may belong to the new person (after drift correction)
STRONG = 45  # over other people, a change this large once blurred is the new person
CHROMA = 20  # ... or a color change this large (a hat over a dark shirt, T6)
FRONT_KEPT = 25  # a person in front changed this much (blurred) by the model is put back
FRONT_LIKE = 40  # ... where the scene is this close to that person's color nearby
SEAM_CONFLICT = 25  # detail (denoised, high-pass) this different across the seam is a ghost


def fill_holes(mask: np.ndarray) -> np.ndarray:
    """``mask`` with every region it encloses filled (flood from the border)."""
    padded = np.pad(mask.astype(np.uint8), 1)
    flood = np.zeros((padded.shape[0] + 2, padded.shape[1] + 2), np.uint8)
    cv2.floodFill(padded, flood, (0, 0), 2)
    return mask | (padded[1:-1, 1:-1] == 0)


def extend_person(
    new: np.ndarray,
    old: np.ndarray,
    corrected: np.ndarray,
    scene: np.ndarray,
    others: np.ndarray,
) -> np.ndarray:
    """The new person plus what is attached to them and changed: a hat's lobes and pom-poms,
    a glove, a raised leg. The silhouette detector missed the jester hat in T6 and the
    composite cut it ("chapéu cortado", 07/11). A dark hat on a dark background changes
    only in patches, so the changes are closed and their holes filled before keeping the
    pieces that touch the new person.

    Over other people the change is measured after a blur: a hat lobe or a pom-pom over a
    neighbor (T6) is a large smooth change, a model's redraw of the neighbor's face is
    fine texture that mostly averages out. A colored hat tail over a dark shirt changes
    less in brightness than that, but changes color, which a redraw does not (T6, E2
    marks); a raised leg reaches far from both silhouettes (K4's toe, 26% of the image)."""
    h, w = scene.shape[:2]
    difference = np.abs(corrected - scene).max(axis=2)
    sigma = max(1.5, min(h, w) / 300)
    blurred = [cv2.GaussianBlur(image, (0, 0), sigma) for image in (corrected, scene)]
    smooth = np.abs(blurred[0] - blurred[1]).max(axis=2)
    lab = [cv2.cvtColor(np.clip(image / 255, 0, 1), cv2.COLOR_RGB2Lab) for image in blurred]
    chroma = np.hypot(*(lab[0][..., 1:] - lab[1][..., 1:]).transpose(2, 0, 1))
    changed = (difference > EXTEND_LEVEL) & (~others | (smooth > STRONG) | (chroma > CHROMA))
    small = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    size = 2 * max(3, round(min(h, w) * 0.012)) + 1
    large = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
    opened = cv2.morphologyEx(changed.astype(np.uint8), cv2.MORPH_OPEN, small)
    closed = cv2.morphologyEx(opened, cv2.MORPH_CLOSE, large).astype(bool)
    reach = max(8, round(min(h, w) * 0.3))
    candidates = fill_holes(new | (closed & dilate(new | old, reach)))
    _, labels = cv2.connectedComponents(candidates.astype(np.uint8))
    seeds = np.unique(labels[new])
    return np.isin(labels, seeds[seeds > 0])


def spread(image: np.ndarray, known: np.ndarray, sigma: float) -> np.ndarray:
    """``image`` where ``known``, extended outward by normalized convolution."""
    weight = known.astype(np.float32)
    num = cv2.GaussianBlur(image * weight[..., None], (0, 0), sigma)
    den = cv2.GaussianBlur(weight, (0, 0), sigma)[..., None]
    return np.where(den > 1e-4, num / np.maximum(den, 1e-4), image)


def occluder_matte(
    scene: np.ndarray, front: np.ndarray, grow: int
) -> tuple[np.ndarray, np.ndarray]:
    """The matte (alpha, color) that puts the scene's people in front of the old person back
    on top of a composite.

    The detector's arm in T3 is a little thinner than the real one. Kept as is, the strip
    between them showed the new person's dark jacket as a shadow along the arm; kept with
    a margin, it showed the old person's white shirt as a pale halo (07/11 marks). In the
    edge band, each scene pixel is a mix of the arm's color F (spread out from its inside)
    and the old background B (spread in from outside); the mix's share of F is the alpha
    that puts the pure arm color over the new composite instead. Inside, the color is the
    scene's own."""
    margin = max(2, grow // 2)
    core = cv2.erode(front.astype(np.uint8), np.ones((3, 3), np.uint8), iterations=margin) > 0
    outer = dilate(front, margin * 2)
    edge = outer & ~core
    sigma = max(2.0, float(margin))
    fore = spread(scene, core, sigma)
    back = spread(scene, ~outer, sigma * 2)
    span = fore - back
    alpha = ((scene - back) * span).sum(axis=2) / np.maximum((span * span).sum(axis=2), 25.0)
    alpha = np.where(core, 1.0, np.where(edge, np.clip(alpha, 0, 1), 0.0)).astype(np.float32)
    # A light background close to the arm's color (T3's white shirt) reads as arm: the share
    # is capped by the distance from the arm's sure inside, and isolated specks smoothed.
    distance = cv2.distanceTransform((~core).astype(np.uint8), cv2.DIST_L2, 3)
    alpha = np.minimum(alpha, np.clip(1.5 - distance / (margin * 1.5), 0, 1))
    alpha = cv2.medianBlur((alpha * 255).astype(np.uint8), 5).astype(np.float32) / 255
    alpha = np.where(core, 1.0, alpha).astype(np.float32)
    return alpha, np.where(core[..., None], scene, fore).astype(np.float32)


def occluder_over(
    composite: np.ndarray, scene: np.ndarray, front: np.ndarray, grow: int
) -> np.ndarray:
    """``composite`` with the scene's people in front of the old person matted back on top
    (``occluder_matte``)."""
    if not front.any():
        return composite
    alpha, color = occluder_matte(scene, front, grow)
    a3 = alpha[..., None]
    return color * a3 + composite * (1 - a3)


def settle_seam(soft: np.ndarray, top: np.ndarray, base: np.ndarray) -> np.ndarray:
    """``soft`` (the region's feathered edge) with each seam patch where ``top`` and
    ``base`` hold different detail settled on one side.

    A partial alpha over two versions of the same thing a few pixels apart shows both: T8's
    "Segs" with two tails, a doubled shoe edge in T6, a doubled windowsill in T4 (E2
    marks). Each patch takes the side most of it already leans to; where only color or
    shading differs, the seam stays soft."""
    ring = (soft > 0.02) & (soft < 0.98)
    if not ring.any():
        return soft

    def detail(image: np.ndarray) -> np.ndarray:
        clean = cv2.medianBlur(np.clip(image, 0, 255).astype(np.uint8), 3).astype(np.float32)
        return clean - cv2.GaussianBlur(clean, (0, 0), 3.0)

    conflict = (np.abs(detail(top) - detail(base)).max(axis=2) > SEAM_CONFLICT) & ring
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    patches = cv2.morphologyEx(conflict.astype(np.uint8), cv2.MORPH_CLOSE, kernel)
    patches = cv2.dilate(patches, kernel) & ring.astype(np.uint8)
    count, labels = cv2.connectedComponents(patches)
    if count <= 1:
        return soft
    sums = np.bincount(labels.ravel(), weights=soft.ravel(), minlength=count)
    sizes = np.bincount(labels.ravel(), minlength=count)
    side = (sums / np.maximum(sizes, 1) > 0.5).astype(np.float32)
    settled = np.where(labels > 0, side[labels], soft).astype(np.float32)
    near = dilate(labels > 0, 2)
    return np.where(near, cv2.GaussianBlur(settled, (0, 0), 1.0), soft).astype(np.float32)


def box_mask(box: list[float], size: tuple[int, int]) -> np.ndarray:
    w, h = size
    left, top, right, bottom = box
    mask = np.zeros((h, w), bool)
    mask[
        int(top * h / 1000) : int(bottom * h / 1000), int(left * w / 1000) : int(right * w / 1000)
    ] = True
    return mask


@dataclass
class Composite:
    finals: dict[str, np.ndarray]
    alpha: np.ndarray
    info: dict = field(default_factory=dict)
    # What ``full_size`` needs to redo the final on larger pixels: the alignment (None when
    # the raw output was left as is), the drift field, the region's alpha before the people
    # in front go back on top, and their matte (alpha and color minus the scene's).
    warp: np.ndarray | None = None
    drift: np.ndarray | None = None
    blend: np.ndarray | None = None
    front_alpha: np.ndarray | None = None
    front_shift: np.ndarray | None = None


def compose(
    scene: np.ndarray,
    raw: np.ndarray,
    old: np.ndarray | None,
    new: np.ndarray | None,
    others: list[np.ndarray],
    near: np.ndarray,
    keep_boxes: list[list[float]] = (),
    detected: np.ndarray | None = None,
) -> Composite:
    """The composite of one raw output.

    The raw output is first aligned to the scene (``align_to_scene``). The region is the
    old person's and the extended new person's silhouettes, nothing rectangular. Scene
    pixels win on overlay content the new person does not cover and the model kept, on the
    parts of people in front of the old person that the model changed (matted back on top),
    and on other people elsewhere except near the old person and where the new person
    covers them. The raw output's color drift is removed before compositing. From the
    07/11 and 08/11 marks:
    - the old person is taken with a margin: her hair strands outside the detector's
      silhouette showed beside the new person in T6 ("vazou");
    - the new person wins over a bubble she covers: the hat was cut by T8's bubbles;
    - overlay the model painted over is not kept: the old person's strand beside T1's dots;
    - a person in front is the model's own where the model kept them: a pasted arm's edge
      showed as cuts in T3;
    - seam patches where both images hold misaligned detail take one side: a doubled
      "Segs" in T8, a doubled shoe edge in T6.
    A multi-band (Laplacian pyramid) seam was rated below this sharp one in every round
    but one case (D3, E3, F3) and was dropped."""
    h, w = scene.shape[:2]
    empty = np.zeros((h, w), bool)
    old_hard = old if old is not None else empty
    grow = max(4, round(min(h, w) * 0.012))
    others_all = empty.copy()
    for hard in others:
        others_all |= hard
    people = old_hard | others_all | (new if new is not None else empty)
    raw, new, moved, warp = _align(scene, raw, new, ~dilate(people, round(min(h, w) * 0.04)))
    new_hard = new if new is not None else empty
    front, order = protection(old_hard, others, near, grow)
    # With a detection (SAM "speech bubble" / "text" on the scene) the overlay is what was
    # found; without one, what the shape says inside the keep boxes (SPEC-205).
    detection = detected is not None and bool(detected.any())
    overlay = dilate(detected, 2) if detection else overlay_mask(scene, keep_boxes)
    old_wide = dilate(old_hard, max(grow, round(min(h, w) * 0.03))) & ~front

    def corrected_for(region: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        band = dilate(region, grow * 6) & ~dilate(region, grow) & ~others_all & ~overlay
        drift = drift_field(scene, raw, band)
        return raw + drift, band, drift

    first, _, _ = corrected_for(dilate(old_hard | new_hard, grow * 2))
    new_ext = (
        extend_person(new_hard, old_hard, first, scene, others_all & ~old_wide)
        if new is not None
        else empty
    )
    region = dilate(old_hard | new_ext, grow) | old_wide
    corrected, band, drift = corrected_for(region)
    # Overlay content detected inside a keep box can be the old person's hair strand next
    # to a bubble (the white streak beside T1's dots, 07/11 marks), and on grainy art the
    # detector takes most of the box. A strand the model painted over is mostly changed and
    # goes, edges included (it is 1-2 px wide, so the change is not blurred). A bubble is the
    # scene's own even when the model rewrote some of its words: on the pod (08/11) "rather"
    # and "Potter" came out in the model's handwriting, 3-14% of a bubble's pixels. So an
    # overlay component with few changed pixels is kept whole, the rest only where unchanged.
    if detection:
        # Detected overlay is the scene's, whatever the model drew over it, except where
        # the new person covers it and stands in front.
        overlay_kept, overlay_order = detected_overlay_kept(overlay, new_ext, near, old_hard, grow)
    else:
        overlay_order = []
        changed = np.abs(corrected - scene).max(axis=2) >= KEPT_LEVEL
        count, parts = cv2.connectedComponents(overlay.astype(np.uint8))
        changed_share = np.bincount(
            parts.ravel(), weights=changed.ravel(), minlength=count
        ) / np.maximum(np.bincount(parts.ravel(), minlength=count), 1)
        whole = np.isin(parts, np.flatnonzero(changed_share[1:] < OVERLAY_REWRITTEN) + 1)
        overlay_kept = overlay & ~new_ext & (whole | ~dilate(changed, 1))
    # People in front are put back only where the model changed them. Where the model kept
    # T3's arm, its own arm meets its own background; the pasted arm's edge showed as cuts
    # (E2 marks). Where the new person took the place, a large patch unlike the arm beside
    # it is someone else's that the detector gave to the arm (K3: the old person's strap),
    # and the new person's coat stays; a watch or a shadow on the hand is the arm's own (it
    # left holes in K3's hand, F2 marks).
    sigma = max(1.5, min(h, w) / 300)
    smooth = np.abs(
        cv2.GaussianBlur(corrected, (0, 0), sigma) - cv2.GaussianBlur(scene, (0, 0), sigma)
    ).max(axis=2)
    small = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    altered = cv2.morphologyEx((smooth > FRONT_KEPT).astype(np.uint8), cv2.MORPH_OPEN, small)
    sure = cv2.erode(front.astype(np.uint8), np.ones((3, 3), np.uint8), iterations=grow) > 0
    like = np.abs(scene - spread(scene, sure, grow * 3)).max(axis=2) < FRONT_LIKE
    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        (front & new_ext & ~like).astype(np.uint8)
    )
    large = np.flatnonzero(stats[1:, cv2.CC_STAT_AREA] >= 12 * grow * grow) + 1
    allowed = front & ~np.isin(labels, large) if count > 1 else front
    restore = dilate(allowed & (altered > 0), grow) & allowed
    protect = overlay_kept | (others_all & ~front & ~old_wide & ~new_ext)
    keep = np.clip(cv2.GaussianBlur(protect.astype(np.float32), (0, 0), 1.5), 0, 1)
    soft = feather(region, grow, grow * 0.8)
    blend = settle_seam(soft, corrected, scene) * (1 - keep)
    a3 = blend[..., None]
    sharp = corrected * a3 + scene * (1 - a3)
    if restore.any():
        front_alpha, front_color = occluder_matte(scene, restore, grow)
        f3 = front_alpha[..., None]
        final = front_color * f3 + sharp * (1 - f3)
        front_shift = (front_color - scene) * f3
    else:
        front_alpha, front_shift, final = None, None, sharp
    alpha = blend * (1 - restore)

    def step(image: np.ndarray) -> float:
        return round(float(np.abs(image - scene)[band].mean()), 2) if band.any() else 0.0

    info = {
        "others_order": order,
        "aligned_px": moved,
        "region_share": round(float(region.mean()), 3),
        "new_extended_share": round(float((new_ext & ~new_hard).mean()), 4),
        "protected_share": round(float((protect | front).mean()), 3),
        "overlay_share": round(float(overlay.mean()), 4),
        "overlay_source": "detected" if detection else "shape",
        "overlay_detected_share": round(float(overlay.mean()), 4) if detection else 0.0,
        "overlay_kept_share": round(float(overlay_kept.mean()), 4),
        "overlay_order": overlay_order,
        "drift_rgb": [round(float(v), 1) for v in (scene - raw)[band].mean(axis=0)]
        if band.any()
        else [],
        "band_step_raw": step(raw),
        "band_step_corrected": step(corrected),
    }
    return Composite({FINAL: final}, alpha, info, warp, drift, blend, front_alpha, front_shift)


def full_size(result: Composite, scene: np.ndarray, raw: np.ndarray) -> np.ndarray:
    """The final of ``result`` (composed on a reduced copy of the scene) redone on the
    full-size ``scene`` and ``raw`` (``raw`` already at the scene's size): the alignment is
    scaled up and applied to the full raw output, and the drift field, the region's alpha
    and the front matte are enlarged; every final pixel mixes full-size pixels (SPEC-313)."""
    h, w = scene.shape[:2]
    sh, sw = result.blend.shape
    sx, sy = w / sw, h / sh

    def up(image: np.ndarray) -> np.ndarray:
        return cv2.resize(image.astype(np.float32), (w, h), interpolation=cv2.INTER_LINEAR)

    if result.warp is not None:
        # Pixel centers: x_full + 0.5 = s * (x_small + 0.5).
        to_full = np.array([[sx, 0, (sx - 1) / 2], [0, sy, (sy - 1) / 2], [0, 0, 1]])
        warp = to_full @ np.vstack([result.warp, [0, 0, 1]]) @ np.linalg.inv(to_full)
        raw = cv2.warpAffine(
            raw,
            warp[:2].astype(np.float32),
            (w, h),
            flags=cv2.WARP_INVERSE_MAP | cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_REPLICATE,
        )
        raw = np.clip(raw, 0, 255)
    a3 = up(result.blend)[..., None]
    final = (raw + up(result.drift)) * a3 + scene * (1 - a3)
    if result.front_alpha is not None:
        f3 = up(result.front_alpha)[..., None]
        final = (scene * f3 + up(result.front_shift)) + final * (1 - f3)
    return final


# --- chat entry ------------------------------------------------------------------------


def _decode_rgb(data: bytes, size: tuple[int, int] | None = None) -> np.ndarray:
    image = Image.open(io.BytesIO(data)).convert("RGB")
    if size and image.size != size:
        image = image.resize(size, Image.Resampling.LANCZOS)
    return np.asarray(image, dtype=np.float32)


def _decode_mask(data: bytes, size: tuple[int, int]) -> np.ndarray:
    image = Image.open(io.BytesIO(data)).convert("L")
    if image.size != size:
        image = image.resize(size, Image.Resampling.NEAREST)
    return np.asarray(image) > MASK_THRESHOLD


def pick_person(
    masks: list[np.ndarray], box: list[float], size: tuple[int, int]
) -> tuple[np.ndarray | None, list[np.ndarray]]:
    """(the mask overlapping ``box`` most or None, every other mask), ranked as the lab's
    ``Detectors.person``: a mask counts only when more than half of it lies in the box,
    then by ``inside * 0.5 + overlap``."""
    target = box_mask(box, size)
    ranked = []
    for hard in masks:
        if not hard.any():
            continue
        inside = (hard & target).sum() / hard.sum()
        overlap = (hard & target).sum() / max(1, target.sum())
        ranked.append((inside * 0.5 + overlap if inside > 0.5 else 0.0, hard))
    ranked.sort(key=lambda item: item[0], reverse=True)
    if not ranked or ranked[0][0] == 0.0:
        return None, [hard for _, hard in ranked]
    return ranked[0][1], [hard for _, hard in ranked[1:]]


def compose_swap(
    scene_png: bytes,
    raw_png: bytes,
    scene_masks: list[bytes],
    raw_masks: list[bytes],
    depth_png: bytes,
    box: list[float],
    keep_boxes: list[list[float]] = (),
    overlay_masks: list[bytes] = (),
) -> tuple[bytes, dict]:
    """The final PNG (``FINAL``) of one generator output composed onto the scene, and its
    facts. ``box`` and ``keep_boxes`` are [left, top, right, bottom] in 0-1000; masks are
    white = person, depth is near = white. Raises ValueError when no scene mask is the
    person in ``box``."""
    full = _decode_rgb(scene_png)
    full_h, full_w = full.shape[:2]
    factor = min(1.0, (ANALYSIS_PIXELS / (full_w * full_h)) ** 0.5)
    w, h = max(1, round(full_w * factor)), max(1, round(full_h * factor))
    scene = full if factor == 1.0 else cv2.resize(full, (w, h), interpolation=cv2.INTER_AREA)
    raw_full = _decode_rgb(raw_png, (full_w, full_h))
    raw = raw_full if factor == 1.0 else cv2.resize(raw_full, (w, h), interpolation=cv2.INTER_AREA)
    old, others = pick_person([_decode_mask(m, (w, h)) for m in scene_masks], box, (w, h))
    if old is None:
        raise ValueError("Não encontrei a pessoa a trocar na cena.")
    new, _ = pick_person([_decode_mask(m, (w, h)) for m in raw_masks], box, (w, h))
    depth = Image.open(io.BytesIO(depth_png)).convert("L")
    if depth.size != (w, h):
        depth = depth.resize((w, h), Image.Resampling.BILINEAR)
    near = np.asarray(depth, dtype=np.float32)
    detected = None
    if overlay_masks:
        detected = np.zeros((h, w), bool)
        for data in overlay_masks:
            detected |= _decode_mask(data, (w, h))
    result = compose(scene, raw, old, new, others, near, list(keep_boxes or ()), detected)
    final = result.finals[FINAL] if factor == 1.0 else full_size(result, full, raw_full)
    buffer = io.BytesIO()
    Image.fromarray(np.clip(final, 0, 255).astype(np.uint8)).save(buffer, "PNG")
    facts = {
        "old_found": True,
        "new_found": new is not None,
        "others": len(others),
        "size": [full_w, full_h],
        "analysis_size": [w, h],
        **result.info,
    }
    return buffer.getvalue(), facts
