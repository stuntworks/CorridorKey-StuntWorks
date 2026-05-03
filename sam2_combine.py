"""SAM2 + NN alpha combine helper — single source of truth for the v1 INVERT plumbing."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import numpy as np


# ===== Multi-object migration shims (v0.8 multi-object SAM2) =====
# Old single-mask sessions wrote sam_positive / sam_negative / sam_anchor_frame
# in live_params.json and sam2_mask.png / sam2_gate_raw.png on disk. Multi-object
# v0.8 namespaces everything per-object (obj1, obj2, ...). These shims translate
# legacy artefacts into MASK 1 so existing sessions keep working.

def migrate_legacy_sam_keys(lp: dict) -> dict:
    """Translate legacy single-mask live_params keys into MASK 1 (obj1) keys.

    Idempotent: if the obj1 keys already exist, leaves them alone. Returns a
    SHALLOW COPY of lp with the obj1 keys filled in when legacy keys are
    present. Original lp untouched.
    """
    if not isinstance(lp, dict):
        return lp
    out = dict(lp)
    if "sam_positive" in lp and "sam_positive_obj1" not in lp:
        out["sam_positive_obj1"] = lp.get("sam_positive", []) or []
    if "sam_negative" in lp and "sam_negative_obj1" not in lp:
        out["sam_negative_obj1"] = lp.get("sam_negative", []) or []
    if "sam_anchor_frame" in lp and "sam_anchor_frame_obj1" not in lp:
        out["sam_anchor_frame_obj1"] = lp.get("sam_anchor_frame")
    if "sam_clicks" in lp and "sam_clicks_obj1" not in lp:
        out["sam_clicks_obj1"] = lp.get("sam_clicks", []) or []
    return out


def migrate_legacy_sam_pngs(session_dir) -> None:
    """Rename legacy single-mask PNGs to MASK 1 namespace.

    sam2_mask.png        -> sam2_mask_obj1.png        (panel hardmask)
    sam2_gate_raw.png    -> sam2_gate_raw_obj1.png    (viewer soft uint16 gate)

    Idempotent: only migrates when the legacy file exists AND the new file does
    not. Atomic: copies then unlinks the legacy file (so a crash mid-migration
    leaves the legacy file intact for the next attempt). Silent on errors;
    callers may still hit a missing file but won't crash.
    """
    try:
        sd = Path(session_dir)
    except Exception:
        return
    if not sd.exists():
        return
    pairs = (
        ("sam2_mask.png", "sam2_mask_obj1.png"),
        ("sam2_gate_raw.png", "sam2_gate_raw_obj1.png"),
    )
    for old_name, new_name in pairs:
        old_p = sd / old_name
        new_p = sd / new_name
        if old_p.exists() and not new_p.exists():
            try:
                shutil.copy2(str(old_p), str(new_p))
                old_p.unlink()
            except Exception:
                pass


def union_sam2_gates(*gates):
    """OR-combine multiple SAM2 silhouettes via per-pixel max. None-tolerant.

    Drops any None entries; returns None if no usable gate. Used by callers that
    have multiple per-object SAM2 masks (MASK 1, MASK 2, ...) and need a single
    combined gate. Per the multi-object plan, combine usually happens at the
    ALPHA layer (each gate run through apply_sam2_gate with its own bbox first,
    then alphas unioned), but this helper is here for callers that want a raw
    silhouette union — for example the SHOW SAM2 viewer overlay.

    Shapes must match. Caller resizes if needed.
    """
    valid = [g for g in gates if g is not None]
    if not valid:
        return None
    out = valid[0]
    for g in valid[1:]:
        out = np.maximum(out, g)
    return out


def union_alpha(*alphas):
    """OR-combine multiple per-mask alpha results. None-tolerant.

    Drops None entries (a mask with no SAM2 gate). Returns None if every input
    is None. Used by render dispatch: for each MASK, run apply_sam2_gate with
    that mask's bbox-confined halos to get alpha_n, then union all alpha_n.
    """
    valid = [a for a in alphas if a is not None]
    if not valid:
        return None
    out = valid[0]
    for a in valid[1:]:
        out = np.maximum(out, a)
    return out


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

    ARCHITECTURE 2026-05-03 (chroma-aware revision):

    Goal: SAM2 should only act in non-green areas (where CorridorKey couldn't
    key against anything). In green areas, CorridorKey already owns the matte
    and SAM2 must stay out — otherwise SAM2's tighter silhouette cuts hair,
    butt curve, and other body parts NN keyed correctly.

    The "where is green?" signal requires BOTH:
        1. NN drove alpha to ~0 (NN said this is background)
        2. Source RGB has actual green chroma (this pixel really is green)

    Why both: NN-killed alone fails when NN keys non-green floor as background
    (cables on floor get protected as if they were green). Source-chroma alone
    fails when green spill bounces onto studio junk (junk gets protected
    because spill makes it slightly green). Requiring BOTH:
      - Real greenscreen (NN-killed AND green chroma)        → green zone ✓
      - Floor NN happened to kill (NN-killed, NOT green)     → not green zone, killable ✓
      - Spill-tinted junk (NN sees FG, slight green chroma)  → not green zone (fails NN-killed)
                                                                → killable ✓
      - Hair fringe / butt at green edge (NN sees FG, no green chroma)
                                                              → not green zone, but ADJACENT
                                                                to one → protected by EDGE GUARD ✓

    Logic:
        nn_killed     = (alpha < 0.05)                       # NN said background
        chroma_green  = source[:,:,1] - max(R, B) > 0.05     # actually green-coloured
        green_zone    = nn_killed AND chroma_green           # both signals
        dist          = distance to nearest green_zone pixel
        kill_ramp     = clip((dist - buffer) / feather, 0, 1)
        sam2_bg       = 1 - gate_binarized_then_softened
        result        = alpha * (1 - kill_ramp * sam2_bg)

    buffer_px (EDGE GUARD): distance past the green edge before SAM2 may begin
        to kill. Larger = more protection for hair / fringe / butt curve and
        body parts right at the green border. Smaller = more aggressive junk
        kill closer to actor.
    feather_px: width of the soft kill transition.
    source_rgb: REQUIRED. RGB float [0..1]. Used for chroma green detection.
        When None or not provided, falls back to NN-killed-only definition
        (legacy behaviour from pre-2026-05-03).
    screen_type: "green" or "blue". Determines which channel is dominant
        in the chroma score.

    SAM2 gate is binarized at 0.5 BEFORE the spatial Gaussian blur — without
    that, raw sigmoid 0.3-0.7 in body interior would produce the SMART BLEND
    ghost. Spatial feather lives on the binary mask.
    """
    if gate is None:
        return alpha
    import cv2 as _cv2
    gate_bin = (gate > 0.5).astype(np.float32)
    gate_bin = _cv2.GaussianBlur(gate_bin, (11, 11), 2.5)
    # "Green zone" = NN-killed AND ACTUALLY green chroma. Three checks make
    # the green-test strict enough to exclude spill-tinted floor that LED
    # lighting commonly creates:
    #   1. NN killed it (alpha ~ 0)
    #   2. Green channel dominates by ≥ 0.10 (was 0.05 — too lenient, dim
    #      spill-green floor was passing and protecting cables on it)
    #   3. Green channel brightness ≥ 0.25 (real greenscreen is BRIGHT
    #      green; spill-tinted dim floor never hits this)
    # Together, the three checks let real greenscreen protect the actor's
    # NN matte while letting SAM2 kill cables / junk on dim spill-tinted
    # surfaces that NN happened to also kill.
    nn_killed = (alpha < 0.05)
    if source_rgb is not None and source_rgb.ndim == 3 and source_rgb.shape[2] >= 3:
        if screen_type == "blue":
            dom_ch = source_rgb[..., 2]
            chroma_score = dom_ch - np.maximum(source_rgb[..., 0], source_rgb[..., 1])
        else:
            dom_ch = source_rgb[..., 1]
            chroma_score = dom_ch - np.maximum(source_rgb[..., 0], source_rgb[..., 2])
        chroma_green = (chroma_score > 0.10) & (dom_ch > 0.25)
        green_zone = (nn_killed & chroma_green).astype(np.uint8)
    else:
        # Legacy fallback when source_rgb is unavailable.
        green_zone = nn_killed.astype(np.uint8)
    # Empty fallback: no green zone detected at all (e.g. shot has no green
    # AND no NN-killed). Degrade to multiplicative.
    if int(green_zone.sum()) == 0:
        return (alpha * gate_bin).astype(alpha.dtype, copy=False)
    dist = _cv2.distanceTransform(1 - green_zone, _cv2.DIST_L2, 5)
    fp = max(int(feather_px), 1)
    bp = max(int(buffer_px), 0)
    kill_ramp = np.clip((dist - float(bp)) / float(fp), 0.0, 1.0).astype(np.float32)
    sam2_bg = 1.0 - gate_bin
    result = alpha * (1.0 - kill_ramp * sam2_bg)
    return np.clip(result, 0.0, 1.0).astype(alpha.dtype, copy=False)


# DANGER ZONE FRAGILE: this is the SINGLE SOURCE OF TRUTH for SAM2 + NN combine. All 6 callsites must use this. Do NOT inline alpha*gate elsewhere.
def apply_sam2_gate(alpha: np.ndarray, gate: np.ndarray | None, invert: bool = False,
                    halo_px: int = 0, halo_body_px: int = 0) -> np.ndarray:
    """Combine NN alpha with SAM2 gate. invert=True = garbage matte mode (subtract clicked region).

    halo_px (HALO FEET):
        > 0: extend silhouette DOWNWARD by halo_px rows (anisotropic-down kernel).
        < 0: SHRINK silhouette upward from the bottom edge by |halo_px|.
             Removes connected floor patches that the largest-component filter
             couldn't drop.
        = 0: silhouette unchanged.
    halo_body_px (HALO BODY):
        > 0: extend silhouette UPWARD by halo_body_px rows (anisotropic-up
             kernel). Recovers hair above the head, butt-above-gap, fingertip
             wisps. Cannot extend below the silhouette by construction.
        = 0: silhouette unchanged.

    Kernels include the silhouette CENTER row so lateral extension at the
    silhouette row works (preserves hair flowing sideways at silhouette top
    edge).

    When both halos are 0: returns alpha * gate (bit-identical no-halo path).
    """
    if gate is None:
        return alpha
    gate_use = (1.0 - gate) if invert else gate
    has_halo = bool(halo_px) or bool(halo_body_px)
    if not has_halo:
        return alpha * gate_use
    import cv2 as _cv2
    binary = (gate_use > 0.5).astype(np.uint8)

    # Drop spurious SAM2 blobs (small floor patches near feet, crew behind
    # actor, etc.) before any halo. Geometric, not heuristic: always keep the
    # largest component, then keep additional components >= 5% of the largest
    # (or 500 px, whichever is greater). Bit-identical to no-filter when only
    # one component exists.
    n_lab, labels, stats, _ = _cv2.connectedComponentsWithStats(binary, connectivity=8)
    if n_lab > 1:
        sizes = stats[1:, _cv2.CC_STAT_AREA]
        largest_idx = int(sizes.argmax()) + 1
        largest = int(sizes.max())
        threshold = max(500, largest // 20)
        keep = np.zeros(n_lab, dtype=np.uint8)
        keep[largest_idx] = 1
        for i in range(1, n_lab):
            if i != largest_idx and int(stats[i, _cv2.CC_STAT_AREA]) >= threshold:
                keep[i] = 1
        binary = keep[labels].astype(np.uint8)

    gate_combined = binary.copy()

    # HALO BODY: anisotropic UP, restricted to the silhouette TOP STRIP.
    # Why "top strip" (top h_b rows of bbox) instead of the whole silhouette:
    # the previous version dilated EVERY silhouette pixel upward, which
    # creates a lateral pillow at every silhouette row — including at feet
    # level on a whole-actor mask, where the pillow extends into the floor.
    # By dilating only the top h_b rows, the halo lives where the head /
    # hair are. Body middle and feet contribute zero halo, so no pillows
    # form below the head no matter how big the silhouette is. Hair-fringe
    # recovery (the whole point of HALO BODY) still works because the head
    # top is always in the top strip.
    if halo_body_px and halo_body_px > 0:
        h_b = int(halo_body_px)
        ys = np.where(binary > 0)[0]
        if len(ys) > 0:
            bbox_top = int(ys.min())
            top_strip = np.zeros_like(binary)
            strip_bot = min(binary.shape[0], bbox_top + h_b)
            top_strip[bbox_top:strip_bot, :] = binary[bbox_top:strip_bot, :]
            kb = h_b * 2 + 1
            body_kernel = _cv2.getStructuringElement(_cv2.MORPH_ELLIPSE, (kb, kb))
            body_kernel[:h_b, :] = 0  # zero TOP half → spreads UP
            body_extension = _cv2.dilate(top_strip, body_kernel)
            gate_combined = np.maximum(gate_combined, body_extension)

    # HALO FEET positive: anisotropic DOWN. Kernel BOTTOM half zeroed →
    # top-half + center active → dilation extends silhouette DOWNWARD.
    if halo_px and halo_px > 0:
        h_f = int(halo_px)
        kf = h_f * 2 + 1
        feet_kernel = _cv2.getStructuringElement(_cv2.MORPH_ELLIPSE, (kf, kf))
        feet_kernel[h_f + 1:, :] = 0  # zero BOTTOM half → spreads DOWN
        feet_extension = _cv2.dilate(binary, feet_kernel)
        gate_combined = np.maximum(gate_combined, feet_extension)

    # HALO FEET negative: shrink the silhouette only at the bottom of its
    # bounding box. Two-step:
    #   (a) find the silhouette's bbox bottom row.
    #   (b) erode only within rows [bbox_bottom - |halo_px| + 1, bbox_bottom].
    # Above that range, the silhouette is preserved as-is — protects the
    # arms / upper body from global erosion. Erosion uses a pure vertical
    # 1-col kernel so lateral pixels never reach outside the silhouette.
    elif halo_px and halo_px < 0:
        h_neg = abs(int(halo_px))
        ys = np.where(gate_combined > 0)[0]
        if len(ys) > 0:
            bbox_bottom = int(ys.max())
            feet_zone_top = max(0, bbox_bottom - h_neg + 1)
            kn = h_neg * 2 + 1
            erode_kernel = np.zeros((kn, 1), dtype=np.uint8)
            erode_kernel[h_neg:, 0] = 1  # active rows h_neg..2*h_neg → dr [0, h_neg]
            eroded = _cv2.erode(gate_combined, erode_kernel)
            # Apply erosion only in the bottom h_neg rows of the bbox.
            gate_combined = gate_combined.copy()
            gate_combined[feet_zone_top:bbox_bottom + 1, :] = (
                eroded[feet_zone_top:bbox_bottom + 1, :]
            )

    return (alpha * gate_combined.astype(np.float32)).astype(alpha.dtype, copy=False)
