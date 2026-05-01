"""SAM2 + NN alpha combine helper — single source of truth for the v1 INVERT plumbing."""
from __future__ import annotations

import numpy as np


def trim_gate_by_chroma(gate: np.ndarray, source_rgb: np.ndarray, screen_type: str = "green", strength: int = 0) -> np.ndarray:
    """Trim screen-colored pixels FROM the SAM2 gate before combine.

    Returns gate unchanged when strength <= 0 (bit-identical to no-trim path).
    For strength > 0, computes a per-pixel chroma score on source_rgb (RGB float
    [0..1]) — green = G - max(R, B), blue = B - max(R, G) — and zeros out gate
    pixels whose chroma exceeds threshold t = 0.05 + (1 - strength/100) * 0.4.
    Higher strength = lower threshold = more aggressive trim. Used to prevent
    SAM2 from claiming green pixels at silhouette edges that NN correctly keyed
    as background.
    """
    if strength is None or int(strength) <= 0:
        return gate
    s = float(int(strength))
    if screen_type == "blue":
        chroma_score = source_rgb[..., 2] - np.maximum(source_rgb[..., 0], source_rgb[..., 1])
    else:  # default to green for any other value
        chroma_score = source_rgb[..., 1] - np.maximum(source_rgb[..., 0], source_rgb[..., 2])
    chroma_score = np.clip(chroma_score, 0.0, 1.0)
    t = 0.05 + (1.0 - s / 100.0) * 0.4
    is_screen = (chroma_score > t).astype(np.float32)
    return (gate * (1.0 - is_screen)).astype(gate.dtype, copy=False)


def fill_holes_color_aware(alpha: np.ndarray, gate: np.ndarray, source_rgb: np.ndarray, screen_type: str = "green", strength: int = 0) -> np.ndarray:
    """Fill alpha=0 holes inside SAM2 region for non-screen-color pixels.

    strength: 0..100 integer. 0 = off (returns input unchanged — bit-identical).
    Higher = more aggressive (more lenient on what counts as "non-screen").

    For each pixel where gate > 0.5 AND alpha < 0.5 AND pixel is not screen-color,
    set alpha = 1.0. Returns a NEW array; input alpha is unchanged.

    Why this differs from trim_gate_by_chroma: trim removes screen-colored pixels
    FROM the gate (kills 0×1 to 0×0 — invisible). Fill flips alpha at low-alpha
    pixels INSIDE the gate that are not screen-colored — rescues NN dropouts on
    yellow shirts / skin / red while leaving correctly-killed green pixels alone.
    """
    if strength is None or int(strength) <= 0:
        return alpha
    s = float(int(strength))
    is_inside = gate > 0.5
    is_low_alpha = alpha < 0.5
    candidates = is_inside & is_low_alpha
    if screen_type == "blue":
        chroma = source_rgb[..., 2] - np.maximum(source_rgb[..., 0], source_rgb[..., 1])
    else:
        chroma = source_rgb[..., 1] - np.maximum(source_rgb[..., 0], source_rgb[..., 2])
    chroma = np.clip(chroma, 0.0, 1.0)
    # Higher strength = higher threshold = more pixels qualify as non-screen.
    # At strength=1, t=0.054 (strict — almost anything green-leaning excluded).
    # At strength=100, t=0.45 (lenient — only pure-green excluded).
    t = 0.05 + (s / 100.0) * 0.4
    is_screen = chroma > t
    fill_mask = candidates & ~is_screen
    out = alpha.copy()
    out[fill_mask] = 1.0
    return out.astype(alpha.dtype, copy=False)


def apply_sam2_gate_additive(alpha, gate, source_rgb, screen_type='green'):
    """Additive combine: alpha = max(NN, gate * non_screen).

    SAM2 can ADD confidence where NN missed but never SUBTRACT NN's correct
    alpha. The non_screen test prevents SAM2 from flooding green-pixel
    regions (e.g. green-screen background that NN correctly killed).

    Use as a drop-in alternative to apply_sam2_gate(alpha, gate) when the
    user has SAM2 ADDITIVE mode toggled ON. source_rgb is float [0..1] RGB.
    """
    if gate is None:
        return alpha
    # Non-screen mask: 1.0 where pixel is NOT screen-color, 0.0 where it is.
    if screen_type == "blue":
        chroma = source_rgb[..., 2] - np.maximum(source_rgb[..., 0], source_rgb[..., 1])
    else:
        chroma = source_rgb[..., 1] - np.maximum(source_rgb[..., 0], source_rgb[..., 2])
    chroma = np.clip(chroma, 0.0, 1.0)
    is_screen = (chroma > 0.1).astype(np.float32)  # fixed threshold; tune later if needed
    non_screen = 1.0 - is_screen
    contribution = gate * non_screen
    return np.maximum(alpha, contribution).astype(alpha.dtype, copy=False)


def apply_sam2_gate_weighted(alpha, gate, source_rgb, screen_type='green', feather_px=10):
    """Weighted blend: NN trusted where green exists, SAM2 trusted where it doesn't.

    Universal fix for "NN keys body well but can't kill non-green floor/props,
    SAM2 can kill them but cuts good NN body parts." Per-pixel weight derived
    from source chroma, smooth feather at the boundary.

    feather_px: Gaussian blur radius for the green/non-green boundary. 0 = hard
    boundary; 10-20 typical for natural transitions.
    """
    if gate is None:
        return alpha
    import cv2 as _cv2
    # Binarize + soften the SAM2 gate first (matches multiplicative path).
    # Without this we'd use SAM2's raw sigmoid (0.3-0.7 in body interior) directly,
    # producing 25-50% opacity in non-green body regions. Binarize at 0.5 then
    # Gaussian blur for soft anti-aliased edge — same as _trimap_fuse does.
    gate_bin = (gate > 0.5).astype(np.float32)
    gate_bin = _cv2.GaussianBlur(gate_bin, (11, 11), 2.5)
    if screen_type == "blue":
        chroma = source_rgb[..., 2] - np.maximum(source_rgb[..., 0], source_rgb[..., 1])
    else:
        chroma = source_rgb[..., 1] - np.maximum(source_rgb[..., 0], source_rgb[..., 2])
    chroma = np.clip(chroma, 0.0, 1.0)
    # Map chroma 0..0.2 to weight 0..1 — soft ramp on green-presence detection
    weight = np.clip(chroma * 5.0, 0.0, 1.0).astype(np.float32)
    # Soft feather at the green/non-green boundary
    if feather_px and feather_px > 0:
        ksize = int(feather_px) * 2 + 1
        weight = _cv2.GaussianBlur(weight, (ksize, ksize), float(feather_px) / 2.0)
    # NN trusted in green region, SAM2 trusted off-green
    gate = gate_bin  # use binarized+softened gate from here
    out = weight * alpha + (1.0 - weight) * gate
    return np.clip(out, 0.0, 1.0).astype(alpha.dtype, copy=False)


def apply_sam2_gate_subtract(alpha, gate, source_rgb=None, screen_type='green',
                             buffer_px=8, feather_px=4):
    """Subtract-only combine: SAM2 may only kill OUTSIDE CorridorKey's green zones.

    ARCHITECTURE 2026-05-01 (revised twice in one session):

    Goal: SAM2 should only act in non-green areas (where CorridorKey couldn't key
    against anything). In green areas, CorridorKey already owns the matte and
    SAM2 must stay out — otherwise SAM2's tighter silhouette cuts hair / fringe.

    The "where is green?" signal is CorridorKey's OWN MATTE — specifically the
    pixels where NN drove alpha to ~0. Reading green from the original RGB chroma
    (the first version) misfired because green spill bouncing off the screen
    onto nearby studio junk (couch, equipment) registered as "green-ish" and
    protected the junk from SAM2's kill. NN's matte does not get fooled by
    spill: NN keys spill-tinted-but-not-actually-green pixels as foreground
    (alpha > 0), so they're correctly outside the protection zone.

    Logic:
        nn_killed    = (alpha < 0.05)                 # where NN confidently killed the green
        dist         = distance to nearest nn_killed pixel  (= distance from green zone)
        kill_ramp    = clip((dist - buffer) / feather, 0, 1)
        sam2_bg      = 1 - gate_binarized_then_softened
        result       = alpha * (1 - kill_ramp * sam2_bg)

    Pixel walk:
      - Green pixel:        alpha=0, in nn_killed zone, dist=0 → no kill (alpha is 0 anyway)
      - Hair fringe:        alpha=0.4, distance to green ~ 1-2px → within buffer → no kill
      - Body interior:      alpha=1, distance to green ~ small → within buffer → no kill
      - Couch off-screen:   alpha=1, distance to green = 100+px → past buffer → SAM2 kills
      - Foot on non-green:  alpha=1, distance to green ~ small (foot at green edge)
                            → if SAM2 says actor, no kill regardless. If SAM2 says bg, only
                            killed past EDGE GUARD distance from the green edge.

    buffer_px (EDGE GUARD): distance past the green edge before SAM2 may begin
        to kill. Larger = more protection for hair / fringe and for body parts
        right at the green border, but also more protection for any junk that
        happens to be within that distance of the green.
    feather_px: width of the soft kill transition.
    source_rgb / screen_type: signature kept for callsite compatibility; ignored.

    SAM2 gate is binarized at 0.5 BEFORE the spatial Gaussian blur — without
    that, raw sigmoid 0.3-0.7 in body interior would produce the SMART BLEND
    ghost. Spatial feather lives on the binary mask.
    """
    if gate is None:
        return alpha
    import cv2 as _cv2
    gate_bin = (gate > 0.5).astype(np.float32)
    gate_bin = _cv2.GaussianBlur(gate_bin, (11, 11), 2.5)
    # Green zones: where NN confidently drove alpha to 0. Threshold low so
    # hair fringe (alpha 0.2-0.6) isn't included.
    nn_killed = (alpha < 0.05).astype(np.uint8)
    # Empty-killed fallback: no NN-killed pixels (no green / SAM2 invoked
    # before refiner). Degrade to multiplicative.
    if int(nn_killed.sum()) == 0:
        return (alpha * gate_bin).astype(alpha.dtype, copy=False)
    # PROTECTION ZONE = NN's green pixels  +  SAM2's filled actor silhouette.
    # Reasoning:
    #   Closing the green mask alone (prior approach) filled small non-green
    #   holes inside the green region — including the couch and rack, which
    #   are NOT body. It also failed when the body extended past the green
    #   (open-bottom silhouette) because closing can't fill open holes.
    #
    #   SAM2's actor silhouette is the only signal that genuinely identifies
    #   actor vs studio junk. Closing SAM2's mask fills internal gaps (e.g.
    #   a strap crossing the butt) without expanding into junk. Unioning
    #   SAM2's filled zone with green pixels gives a clean protection mask:
    #     - Pure green pixels: protected (NN already keyed)
    #     - Body interior where SAM2 says actor: protected (sam2_filled)
    #     - Body across strap (small SAM2 gap): protected by closing
    #     - Junk in non-green areas: not in either set → SAM2 can kill
    #     - Studio prop in green-bounded hole: not green, no SAM2 actor →
    #       not protected → SAM2 can kill
    #
    # Tradeoff: body parts that extend past the green screen MUST have
    # positive SAM2 dots placed on them. Without dots, SAM2 won't cover them
    # and SUBTRACT will treat them as junk.
    sam2_actor = (gate > 0.5).astype(np.uint8)
    if int(sam2_actor.sum()) > 0:
        _SAM2_CLOSE_R = 30
        _ck = _cv2.getStructuringElement(_cv2.MORPH_ELLIPSE,
                                         (_SAM2_CLOSE_R * 2 + 1, _SAM2_CLOSE_R * 2 + 1))
        sam2_filled = _cv2.morphologyEx(sam2_actor, _cv2.MORPH_CLOSE, _ck)
    else:
        sam2_filled = sam2_actor
    nn_protected = (nn_killed | sam2_filled).astype(np.uint8)
    dist = _cv2.distanceTransform(1 - nn_protected, _cv2.DIST_L2, 5)
    fp = max(int(feather_px), 1)
    bp = max(int(buffer_px), 0)
    kill_ramp = np.clip((dist - float(bp)) / float(fp), 0.0, 1.0).astype(np.float32)
    sam2_bg = 1.0 - gate_bin
    result = alpha * (1.0 - kill_ramp * sam2_bg)
    return np.clip(result, 0.0, 1.0).astype(alpha.dtype, copy=False)


# DANGER ZONE FRAGILE: this is the SINGLE SOURCE OF TRUTH for SAM2 + NN combine. All 6 callsites must use this. Do NOT inline alpha*gate elsewhere.
def apply_sam2_gate(alpha: np.ndarray, gate: np.ndarray | None, invert: bool = False, halo_px: int = 0) -> np.ndarray:
    """Combine NN alpha with SAM2 gate. invert=True = garbage matte mode (subtract clicked region).

    halo_px: trimap-guided halo band width in pixels. When 0 (default), behavior is
    bit-identical to the previous version: alpha * gate (or alpha * (1-gate) when
    invert). When >0, the SAM2 gate is binarized at 0.5 and dilated by halo_px so a
    band of NN-driven alpha values around the SAM2 silhouette survives the gate
    multiply. Recovers hair / motion-blur detail at the SAM2 edge.
    """
    if gate is None:
        return alpha
    gate_use = (1.0 - gate) if invert else gate
    if halo_px and halo_px > 0:
        # Lazy import — keep the no-halo fast path numpy-only and avoid forcing
        # cv2 onto callers that never use halo. Viewers already import cv2.
        import cv2 as _cv2
        binary = (gate_use > 0.5).astype(np.uint8)
        k = int(halo_px) * 2 + 1
        kernel = _cv2.getStructuringElement(_cv2.MORPH_ELLIPSE, (k, k))
        extended = _cv2.dilate(binary, kernel).astype(np.float32)
        return alpha * extended
    return alpha * gate_use
